//! WebSocket endpoint `/ws/robots` — mirror of `backend_ingestion/app/main.py`
//! `ws_robots` (auth + snapshot + incremental stream via the process hub).
//!
//! Contract surface (verbatim from the Python handler):
//!   * Auth via `?token=<JWT>` (HS256, audience `fastapi-users:auth`). Missing or
//!     invalid token -> close with code **1008** (policy violation), sent **after**
//!     the handshake is already accepted. (Under ASGI, closing before accept
//!     becomes HTTP 403; axum's `on_upgrade` already accepted, so we mirror the
//!     accept-then-close ordering.)
//!   * First frame: a full snapshot of the 100 demo robots
//!     `{"type":"snapshot","data":{"robots":[...]}}`.
//!   * Subsequent frames: produced by the hub's 100ms coalescing beat —
//!     `{"type":"telemetry","data":{...}}`, `{"type":"alert","data":{...}}`,
//!     `{"type":"action","data":{...}}` — byte-for-byte matching the Python
//!     `ws_hub._envelope`.
//!   * Alerts are never coalesced or dropped.

use std::borrow::Cow;
use std::sync::atomic::Ordering::Relaxed;
use std::sync::Arc;
use std::time::{Instant, SystemTime, UNIX_EPOCH};

use axum::extract::ws::{CloseFrame, Message, WebSocket, WebSocketUpgrade};
use axum::extract::{Query, State};
use axum::response::Response;
use futures::{SinkExt, StreamExt};
use jsonwebtoken::{decode, DecodingKey, Algorithm, Validation};
use serde::Deserialize;
use serde::Serialize;
use tokio::time::Duration;

use crate::hub::Client;
use crate::{AppState, base_ids, build_ws_snapshot, RobotTelemetryOut};

#[derive(Deserialize)]
pub struct WsQuery {
    pub token: Option<String>,
}

pub async fn ws_handler(
    ws: WebSocketUpgrade,
    State(state): State<AppState>,
    Query(params): Query<WsQuery>,
) -> Response {
    ws.on_upgrade(move |socket| handle_socket(socket, state, params.token))
}

#[derive(Deserialize)]
struct Claims {
    sub: String,
    // Decoded here so we can enforce PyJWT's exact `exp` semantics ourselves:
    // PyJWT only rejects when `exp` is present AND in the past (absent `exp` is
    // accepted). `validate_exp` in some jsonwebtoken builds only fires when the
    // reserved claim is present in the claims struct, so we do it explicitly.
    #[serde(default)]
    exp: Option<i64>,
}

/// Mirror of `app/auth/deps.py::authenticate_ws_token`: decode HS256 JWT, require
/// audience `fastapi-users:auth`, return the `sub` (or `None` on any failure).
/// Deliberately **no DB lookup** — a signed, unexpired token suffices to decide
/// "one of us" for a long-lived connection.
///
/// Uses `jsonwebtoken` v10 with the pure-Rust `rust_crypto` backend (no `ring` /
/// `boring` / C toolchain). The token is a standard RFC 7519 HS256 JWT,
/// byte-for-byte interchangeable with the Python side's PyJWT.
pub fn authenticate_ws_token(token: &str, secret: &str) -> Option<String> {
    if token.is_empty() {
        return None;
    }
    let key = DecodingKey::from_secret(secret.as_bytes());
    let mut validation = Validation::new(Algorithm::HS256);
    validation.set_audience(&["fastapi-users:auth"]);
    validation.validate_aud = true;
    validation.validate_exp = false; // enforced manually below
    let claims = match decode::<Claims>(token, &key, &validation) {
        Ok(data) => data.claims,
        Err(_) => return None,
    };
    // PyJWT semantics: reject only when `exp` is present and in the past.
    if let Some(exp) = claims.exp {
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_secs() as i64)
            .unwrap_or(0);
        if exp < now {
            return None;
        }
    }
    if claims.sub.is_empty() {
        None
    } else {
        Some(claims.sub)
    }
}

#[derive(Serialize)]
struct SnapshotEnvelope<'a> {
    #[serde(rename = "type")]
    kind: &'a str,
    data: SnapshotData<'a>,
}

#[derive(Serialize)]
struct SnapshotData<'a> {
    robots: &'a [RobotTelemetryOut],
}

async fn handle_socket(mut socket: WebSocket, state: AppState, token: Option<String>) {
    // Auth first. axum's `on_upgrade` already completed the handshake, so this
    // matches the Python accept-then-close(1008) ordering (no HTTP 403).
    let sub = token.as_deref().and_then(|t| authenticate_ws_token(t, &state.jwt_secret));
    if sub.is_none() {
        let _ = socket
            .send(Message::Close(Some(CloseFrame {
                code: 1008,
                reason: Cow::Borrowed("policy violation"),
            })))
            .await;
        return;
    }
    // `sub` (the audit `ack_by` source) is intentionally available here for a
    // future audit writer; the read-replica spike does not yet persist it.

    // ① First frame: full snapshot of the 100 demo robots (mirrors
    //    `state_cache.snapshot(settings.robot_ids)` — demo set only, no merge).
    let robot_ids = base_ids();
    let mut redis = state.redis.clone();
    let snap = build_ws_snapshot(&mut redis, &robot_ids).await;
    let envelope = SnapshotEnvelope {
        kind: "snapshot",
        data: SnapshotData { robots: &snap },
    };
    let snapshot_text = serde_json::to_string(&envelope).expect("snapshot serialize");

    // ② Split the socket: a sender half (owned by the writer task) and a receiver
    //    half (owned by the reader task).
    let (mut sender, mut receiver) = socket.split();

    let client = Arc::new(Client::new(
        state.hub.config().queue_size,
        state.hub.config().alert_slack,
    ));

    // Writer: drain the client queue and send frames **in order** (WebSocket
    // frames must be ordered; the queue is FIFO). The queue lock is held only for
    // `pop_front` (no await inside); actual `send_text` happens outside the lock.
    let writer_client = client.clone();
    let close_timeout = Duration::from_secs_f64(state.hub.config().close_timeout_s);
    let writer = tokio::spawn(async move {
        loop {
            tokio::select! {
                _ = writer_client.notify.notified() => {}
                _ = tokio::time::sleep(Duration::from_secs(15)) => {}
            }
            loop {
                match writer_client.pop_front() {
                    Some(text) => {
                        if sender.send(Message::Text(text)).await.is_err() {
                            return; // client gone
                        }
                    }
                    None => break,
                }
            }
            // Watchdog (ISSUE #73 acceptance #3): a connection whose queue stays
            // saturated for the whole close-timeout window is actively evicted,
            // so its counter returns to baseline without waiting for TCP.
            // Compute the decision while the lock is held, then drop the guard
            // before any `.await` (a `std::sync::MutexGuard` is not `Send`).
            let qlen = writer_client.queue_depth();
            let evict = if qlen >= writer_client.capacity() {
                let mut ff = writer_client.first_full.lock().unwrap();
                match *ff {
                    None => {
                        *ff = Some(Instant::now());
                        false
                    }
                    Some(t) if t.elapsed() >= close_timeout => true,
                    _ => false,
                }
            } else {
                *writer_client.first_full.lock().unwrap() = None;
                false
            };
            if evict {
                let _ = sender
                    .send(Message::Close(Some(CloseFrame {
                        code: 1000,
                        reason: Cow::Borrowed("slow client"),
                    })))
                    .await;
                return;
            }
        }
    });

    // Reader: any close / error ends the connection (covers normal close, crash,
    // and network unplug — `receive` raises on FIN/RST).
    let reader = tokio::spawn(async move {
        while let Some(msg) = receiver.next().await {
            match msg {
                Ok(Message::Close(_)) => return,
                Ok(_) => continue, // ignore client pings/text
                Err(_) => return,
            }
        }
    });

    // Snapshot is enqueued BEFORE register, so it is always the first frame.
    client.enqueue_priority(&snapshot_text);
    state.hub.register(client.clone());

    // Wait for the client to disconnect (reader task ends).
    let _ = reader.await;

    // Teardown: aggregate backpressure counters into the hub (mirrors
    // `ws_robots` finally), then unregister and stop the tasks.
    let dropped = client.telemetry_dropped.load(Relaxed);
    let deferred = client.alerts_deferred.load(Relaxed);
    state
        .hub
        .metrics()
        .telemetry_dropped_total
        .fetch_add(dropped, Relaxed);
    state
        .hub
        .metrics()
        .alerts_deferred_total
        .fetch_add(deferred, Relaxed);

    state.hub.unregister(&client);
    // `reader` has already finished (it is the disconnect signal we awaited), so
    // only the writer loop needs an explicit abort here.
    writer.abort();
}

#[cfg(test)]
mod tests {
    use super::*;
    use jsonwebtoken::{encode, EncodingKey, Header};
    use serde::Serialize;
    use std::time::{SystemTime, UNIX_EPOCH};

    #[derive(Serialize)]
    struct TestClaims {
        sub: String,
        aud: String,
        exp: usize,
    }

    fn make_token(secret: &str, sub: &str) -> String {
        let exp = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_secs() as usize
            + 3600;
        let claims = TestClaims {
            sub: sub.to_string(),
            aud: "fastapi-users:auth".to_string(),
            exp,
        };
        encode(
            &Header::new(Algorithm::HS256),
            &claims,
            &EncodingKey::from_secret(secret.as_bytes()),
        )
        .unwrap()
    }

    #[test]
    fn jwt_roundtrip_valid() {
        let secret = "spike-secret";
        let token = make_token(secret, "42");
        assert_eq!(authenticate_ws_token(&token, secret), Some("42".to_string()));
    }

    #[test]
    fn jwt_rejects_wrong_secret() {
        let token = make_token("right-secret", "1");
        assert_eq!(authenticate_ws_token(&token, "wrong-secret"), None);
    }

    #[test]
    fn jwt_rejects_empty_and_garbage() {
        assert_eq!(authenticate_ws_token("", "x"), None);
        assert_eq!(authenticate_ws_token("not.a.jwt", "x"), None);
    }

    #[test]
    fn jwt_rejects_expired() {
        let secret = "spike-secret";
        let exp = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_secs() as usize
            - 10;
        let claims = TestClaims {
            sub: "7".to_string(),
            aud: "fastapi-users:auth".to_string(),
            exp,
        };
        let token = encode(
            &Header::new(Algorithm::HS256),
            &claims,
            &EncodingKey::from_secret(secret.as_bytes()),
        )
        .unwrap();
        assert_eq!(authenticate_ws_token(&token, secret), None);
    }

    #[test]
    fn client_backpressure_tiering() {
        // telemetry_limit=2, slack=1 -> capacity=3.
        let c = Client::new(2, 1);
        // Fill telemetry up to the soft limit (2). A 3rd telemetry drops oldest.
        c.enqueue_raw("telemetry", "t1");
        c.enqueue_raw("telemetry", "t2");
        c.enqueue_raw("telemetry", "t3"); // drops t1, pushes t3
        assert_eq!(c.telemetry_dropped.load(Relaxed), 1);
        assert_eq!(c.queue_depth(), 2);
        // Alert fits in headroom.
        c.enqueue_raw("alert", "a1");
        assert_eq!(c.queue_depth(), 3); // at capacity
        // Another alert: headroom exhausted -> deferred, not dropped.
        c.enqueue_raw("alert", "a2");
        assert_eq!(c.alerts_deferred.load(Relaxed), 1);
        assert_eq!(c.queue_depth(), 3);
        // Pop order is FIFO: t2, t3, a1 (t1 was dropped).
        assert_eq!(c.pop_front(), Some("t2".to_string()));
        assert_eq!(c.pop_front(), Some("t3".to_string()));
        assert_eq!(c.pop_front(), Some("a1".to_string()));
        assert_eq!(c.pop_front(), None);
    }

    #[test]
    fn envelope_matches_python_format() {
        assert_eq!(
            crate::hub::envelope("telemetry", "{\"a\":1}"),
            "{\"type\":\"telemetry\",\"data\":{\"a\":1}}"
        );
    }
}
