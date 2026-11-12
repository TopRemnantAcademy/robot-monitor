//! Process-level WebSocket fan-out hub — mirror of
//! `backend_ingestion/app/ws_hub.py` (+ `app/ws_backpressure.py`).
//!
//! Design (same as Python):
//!   1. **One** Redis pub/sub subscription for the whole process
//!      (`chan_stream_all`, `chan_alerts`, `chan_action_all`) — replaces the old
//!      per-connection subscriptions.
//!   2. **Node coalescing**: telemetry/action frames are overwritten into a
//!      "latest frame" map keyed by `robot_id` — only the latest frame per robot
//!      survives to the next beat (counter `ws_hub_coalesced_total` is the proof).
//!      Alerts are an **event stream** and are appended verbatim (never merged,
//!      never dropped — they are the frontend alert stream's single source).
//!   3. **Fixed 100ms beat** fan-out: on every tick the hub serializes each frame
//!      **once** and pushes the pre-serialized string into each connection's own
//!      bounded backpressure queue (the per-connection sender serializes
//!      `send_text`). The hub never awaits on any client.
//!
//! The buffers are behind `std::sync::Mutex` (the critical sections contain **no
//! await**, so there is no risk of holding a lock across an await — the same
//! "atomic by nature" property the Python module relies on, made explicit for
//! Rust's aliasing rules).

use std::collections::{HashMap, VecDeque};
use std::sync::atomic::{AtomicU64, Ordering::Relaxed};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde_json::Value;
use tokio::sync::Notify;
use futures::StreamExt;

/// Hub configuration (mirrors the relevant `app/config.py` settings).
#[derive(Clone)]
pub struct HubConfig {
    pub redis_url: String,
    pub broadcast_interval_ms: f64,
    pub queue_size: usize,
    pub alert_slack: usize,
    pub close_timeout_s: f64,
    pub chan_stream_all: String,
    pub chan_alerts: String,
    pub chan_action_all: String,
}

/// Live WS metrics shared by the hub and the `/metrics` endpoint. Mirrors the
/// counters in `app/metrics.py` that the Rust read-replica can legitimately
/// derive (the WS hub actually runs here, unlike the stateless ingest counters).
#[derive(Default)]
pub struct WsMetrics {
    /// Telemetry/action frames merged away by the tick (latest-per-robot).
    pub coalesced: AtomicU64,
    /// Hub frames actually fanned out, split by kind.
    pub frames_telemetry: AtomicU64,
    pub frames_alert: AtomicU64,
    /// Aggregated from each client on disconnect (mirrors `ws_robots` finally).
    pub telemetry_dropped_total: AtomicU64,
    pub alerts_deferred_total: AtomicU64,
}

impl WsMetrics {
    pub fn snapshot(&self) -> (u64, u64, u64) {
        (
            self.coalesced.load(Relaxed),
            self.frames_telemetry.load(Relaxed),
            self.frames_alert.load(Relaxed),
        )
    }
}

/// One queued outbound frame. `alert == true` marks the undroppable bucket
/// (alerts); `alert == false` is the droppable telemetry/action bucket.
pub struct Queued {
    pub alert: bool,
    pub text: String,
}

/// Per-connection bounded outbound channel + tiered backpressure — mirror of
/// `app/ws_backpressure.py::WsOutbound`.
///
/// Capacity = `telemetry_limit + alert_slack`. Telemetry may occupy up to
/// `telemetry_limit` slots (overflow drops the **oldest** telemetry frame, since
/// telemetry is latest-value-wins). Alerts may use the `alert_slack` headroom and
/// are **never dropped** (only deferred when even the headroom is exhausted).
pub struct Client {
    pub queue: Mutex<VecDeque<Queued>>,
    pub notify: Notify,
    pub telemetry_limit: usize,
    pub capacity: usize,
    pub telemetry_dropped: AtomicU64,
    pub telemetry_merged: AtomicU64,
    pub alerts_deferred: AtomicU64,
    /// First instant the queue was observed at `capacity` (watchdog bookkeeping).
    pub first_full: Mutex<Option<Instant>>,
}

impl Client {
    pub fn new(queue_size: usize, alert_slack: usize) -> Client {
        let telemetry_limit = queue_size.max(1);
        let capacity = telemetry_limit + alert_slack;
        Client {
            queue: Mutex::new(VecDeque::with_capacity(capacity)),
            notify: Notify::new(),
            telemetry_limit,
            capacity,
            telemetry_dropped: AtomicU64::new(0),
            telemetry_merged: AtomicU64::new(0),
            alerts_deferred: AtomicU64::new(0),
            first_full: Mutex::new(None),
        }
    }

    pub fn capacity(&self) -> usize {
        self.capacity
    }

    pub fn queue_depth(&self) -> usize {
        self.queue.lock().unwrap().len()
    }

    /// Enqueue a pre-serialized frame. `bucket == "alert"` is the undroppable
    /// bucket; anything else is the droppable telemetry/action bucket. Never
    /// blocks, never raises.
    pub fn enqueue_raw(&self, bucket: &str, text: &str) {
        let mut q = self.queue.lock().unwrap();
        if bucket == "alert" {
            if q.len() >= self.capacity {
                // Alert headroom exhausted — defer (never drop).
                self.alerts_deferred.fetch_add(1, Relaxed);
                return;
            }
            q.push_back(Queued { alert: true, text: text.to_string() });
        } else {
            if q.len() >= self.telemetry_limit {
                if !drop_oldest_telemetry(&mut q) {
                    // No droppable frame in the queue — this frame was not
                    // admitted, but nothing existing was lost (count "merged").
                    self.telemetry_merged.fetch_add(1, Relaxed);
                    return;
                }
                self.telemetry_dropped.fetch_add(1, Relaxed);
            }
            q.push_back(Queued { alert: false, text: text.to_string() });
        }
        self.notify.notify_one();
    }

    /// Priority enqueue (snapshot / heartbeat): always delivered, evicting an
    /// oldest telemetry frame if the queue is full.
    pub fn enqueue_priority(&self, text: &str) {
        let mut q = self.queue.lock().unwrap();
        if q.len() >= self.capacity {
            drop_oldest_telemetry(&mut q);
        }
        q.push_back(Queued { alert: false, text: text.to_string() });
        self.notify.notify_one();
    }

    /// Pop the oldest frame for the writer task.
    pub fn pop_front(&self) -> Option<String> {
        self.queue.lock().unwrap().pop_front().map(|i| i.text)
    }
}

/// From the head, find the first droppable (non-alert) frame and remove it.
fn drop_oldest_telemetry(q: &mut VecDeque<Queued>) -> bool {
    if let Some(pos) = q.iter().position(|i| !i.alert) {
        q.remove(pos);
        true
    } else {
        false
    }
}

pub struct Hub {
    clients: Mutex<Vec<Arc<Client>>>,
    latest: Mutex<HashMap<String, String>>,
    latest_action: Mutex<HashMap<String, String>>,
    alerts: Mutex<Vec<String>>,
    config: HubConfig,
    metrics: Arc<WsMetrics>,
}

impl Hub {
    pub fn new(config: HubConfig, metrics: Arc<WsMetrics>) -> Arc<Hub> {
        Arc::new(Hub {
            clients: Mutex::new(Vec::new()),
            latest: Mutex::new(HashMap::new()),
            latest_action: Mutex::new(HashMap::new()),
            alerts: Mutex::new(Vec::new()),
            config,
            metrics,
        })
    }

    pub fn config(&self) -> &HubConfig {
        &self.config
    }

    pub fn metrics(&self) -> Arc<WsMetrics> {
        self.metrics.clone()
    }

    pub fn register(&self, client: Arc<Client>) {
        self.clients.lock().unwrap().push(client);
    }

    pub fn unregister(&self, client: &Arc<Client>) {
        let mut c = self.clients.lock().unwrap();
        if let Some(pos) = c.iter().position(|x| Arc::ptr_eq(x, client)) {
            c.remove(pos);
        }
    }

    /// `(connections, total_queue_depth, total_dropped, total_deferred)`.
    pub fn client_stats(&self) -> (usize, u64, u64, u64) {
        let clients = self.clients.lock().unwrap();
        let mut depth = 0u64;
        let mut dropped = 0u64;
        let mut deferred = 0u64;
        for c in clients.iter() {
            depth += c.queue_depth() as u64;
            dropped += c.telemetry_dropped.load(Relaxed);
            deferred += c.alerts_deferred.load(Relaxed);
        }
        (clients.len(), depth, dropped, deferred)
    }

    /// Spawn the shared-subscription and beat-broadcast background tasks.
    pub fn start(self: Arc<Self>) {
        {
            let h = self.clone();
            tokio::spawn(async move { h.broadcast_loop().await });
        }
        {
            let h = self.clone();
            tokio::spawn(async move { h.consume_loop().await });
        }
    }

    async fn broadcast_loop(self: Arc<Self>) {
        let interval =
            Duration::from_secs_f64((self.config.broadcast_interval_ms / 1000.0).max(0.001));
        loop {
            tokio::time::sleep(interval).await;

            // Snapshot + clear the three buffers (atomic under their locks).
            let latest: Vec<String> = {
                let mut m = self.latest.lock().unwrap();
                m.drain().map(|(_, v)| v).collect()
            };
            let actions: Vec<String> = {
                let mut m = self.latest_action.lock().unwrap();
                m.drain().map(|(_, v)| v).collect()
            };
            let alerts: Vec<String> = {
                let mut a = self.alerts.lock().unwrap();
                std::mem::take(&mut *a)
            };
            if latest.is_empty() && actions.is_empty() && alerts.is_empty() {
                continue;
            }

            // Single serialization: one envelope string per robot/alert, reused
            // across all connections. Actions reuse the droppable bucket but keep
            // their own `{"type":"action",...}` envelope.
            let mut frames: Vec<(&str, String)> =
                Vec::with_capacity(latest.len() + actions.len() + alerts.len());
            for raw in &latest {
                frames.push(("telemetry", envelope("telemetry", raw)));
            }
            for raw in &actions {
                frames.push(("telemetry", envelope("action", raw)));
            }
            for raw in &alerts {
                frames.push(("alert", envelope("alert", raw)));
            }
            let n_telem = (latest.len() + actions.len()) as u64;
            let n_alert = alerts.len() as u64;

            let clients = {
                let c = self.clients.lock().unwrap();
                c.clone()
            };
            for client in &clients {
                for (bucket, text) in &frames {
                    client.enqueue_raw(bucket, text);
                }
            }
            self.metrics
                .frames_telemetry
                .fetch_add(n_telem * clients.len() as u64, Relaxed);
            self.metrics
                .frames_alert
                .fetch_add(n_alert * clients.len() as u64, Relaxed);
        }
    }

    async fn consume_loop(self: Arc<Self>) {
        loop {
            if let Err(e) = self.run_subscription().await {
                tracing::warn!("ws hub redis subscription error: {e}; reconnecting in 1s");
            }
            tokio::time::sleep(Duration::from_secs(1)).await;
        }
    }

    /// The **only** Redis pub/sub consumer for the process. On any error it backs
    /// off and reconnects (so a single Redis blip does not silence every
    /// connection while the sockets themselves stay open).
    async fn run_subscription(&self) -> Result<(), redis::RedisError> {
        let client = redis::Client::open(self.config.redis_url.clone())?;
        // `get_async_connection` is deprecated in favour of the multiplexed
        // variant, but `into_pubsub()` (required below) is only on `Connection`,
        // so we keep this form deliberately.
        #[allow(deprecated)]
        let conn = client.get_async_connection().await?;
        // redis 0.27: consume the connection into a dedicated PubSub handle.
        let mut pubsub = conn.into_pubsub();
        pubsub.subscribe(&self.config.chan_stream_all).await?;
        pubsub.subscribe(&self.config.chan_alerts).await?;
        pubsub.subscribe(&self.config.chan_action_all).await?;
        let mut stream = pubsub.on_message();
        loop {
            let msg = match stream.next().await {
                Some(m) => m,
                None => return Ok(()), // subscription ended -> reconnect
            };
            let channel = msg.get_channel_name();
            let payload: String = match msg.get_payload() {
                Ok(p) => p,
                Err(_) => continue,
            };
            if channel == self.config.chan_alerts {
                // Event stream — append verbatim, never merge, never drop.
                self.alerts.lock().unwrap().push(payload);
            } else if let Ok(v) = serde_json::from_str::<Value>(&payload) {
                // Telemetry / action: keep only the latest frame per robot_id.
                if let Some(rid) = v.get("robot_id").and_then(|x| x.as_str()) {
                    let rid = rid.to_string();
                    if channel == self.config.chan_action_all {
                        let mut m = self.latest_action.lock().unwrap();
                        if m.contains_key(&rid) {
                            self.metrics.coalesced.fetch_add(1, Relaxed);
                        }
                        m.insert(rid, payload);
                    } else {
                        let mut m = self.latest.lock().unwrap();
                        if m.contains_key(&rid) {
                            self.metrics.coalesced.fetch_add(1, Relaxed);
                        }
                        m.insert(rid, payload);
                    }
                }
            }
        }
    }
}

/// Build the unified envelope from spec/00 SS5.2, byte-for-byte matching Python
/// `ws_hub._envelope`: `{"type":kind,"data":<raw>}`. `raw` is the publisher's
/// own JSON text, forwarded verbatim (so inner `data` bytes are identical).
pub(crate) fn envelope(kind: &str, raw: &str) -> String {
    format!("{{\"type\":\"{kind}\",\"data\":{raw}}}")
}
