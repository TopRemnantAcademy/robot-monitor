//! RobotMonitor Rust spike backend — T3: `GET /robots` parity with the Python
//! `backend_ingestion` service.
//!
//! Logic mirror (see backend_ingestion/app/main.py:334 `list_robots`):
//!   1. Always include the 100 demo `humanoid-00N` ids (base set).
//!   2. Merge every `robot_metadata` id NOT in the base set (the real SEGWAY fleet).
//!   3. Per robot: read Redis Hash `robot:state:{id}` (restored to typed values)
//!      or an offline placeholder when absent.
//!   4. Batch-enrich `is_online` / `last_seen` from the liveness ZSET
//!      (`liveness:last_seen`), replicating liveness.enrich().
//!   5. For real (extra) robots whose snapshot `ts == 0` (no live feed), backfill
//!      `battery_pct` from the latest `robot_telemetry` row and `is_online` from
//!      `robot_metadata` — exactly the Python extra_ids branch.
//!
//! Auth: the read endpoints (`/robots`, `/supported-robots`, `/history`,
//! `/alerts`, `/metrics`) intentionally defer JWT — they validate data/contract
//! parity. The real-time `/ws/robots` endpoint now authenticates with the same
//! HS256 JWT (audience `fastapi-users:auth`) as the Python service, per the
//! user-approved "full port" of the WebSocket layer.

mod hub;
mod ws;

use std::collections::HashMap;
use std::sync::Arc;
use std::sync::atomic::Ordering::Relaxed;
use std::time::{SystemTime, UNIX_EPOCH};

use axum::extract::{Path, Query, State};
use axum::{routing::get, Json, Router};
use chrono::{DateTime, Utc};
use hub::{Hub, HubConfig, WsMetrics};
use redis::aio::ConnectionManager;
use redis::AsyncCommands;
use serde::Deserialize;
use serde::Serialize;
use serde_json::json;
use sqlx::postgres::PgPoolOptions;
use sqlx::PgPool;
use sqlx::Row;

#[derive(Clone)]
struct AppState {
    pool: PgPool,
    redis: ConnectionManager,
    hub: Arc<Hub>,
    jwt_secret: String,
}

#[derive(Clone, Serialize)]
pub struct RobotTelemetryOut {
    robot_id: String,
    status_code: i32,
    error_code: i32,
    battery_pct: f64,
    max_joint_temp: f64,
    joint_temperatures: HashMap<String, f64>,
    joint_positions: HashMap<String, f64>,
    joint_torques: HashMap<String, f64>,
    ts: i64,
    is_online: bool,
    last_seen: Option<i64>,
}

#[derive(Serialize)]
struct RobotListResponse {
    robots: Vec<RobotTelemetryOut>,
}

// --------------------------------------------------------------- /supported-robots
// Mirror of backend_ingestion/app/catalog.py: a static display-metadata supplement
// keyed by `model_type`. The DB (distinct robot_metadata.model_type) is the source
// of truth for *which* models exist; this dict only adds readable display info.
// Unregistered models fall back to a `generic` placeholder — never fabricated.

#[derive(Clone, Serialize)]
struct CatalogEntry {
    model_type: String,
    display_name: String,
    form_factor: String,
    dof: i32,
    vendor: String,
    description: String,
    generic: bool,
}

fn lookup(model_type: &str) -> CatalogEntry {
    if model_type == "HUMANOID-24DOF" {
        return CatalogEntry {
            model_type: "HUMANOID-24DOF".into(),
            display_name: "Humanoid 24-DOF".into(),
            form_factor: "bipedal humanoid".into(),
            dof: 24,
            vendor: "RobotMonitor Platform".into(),
            description: "The platform's main model: a 24-DOF highly anthropomorphic bipedal \
                robot covering shoulder/elbow/wrist/hip/knee/ankle joint telemetry, supporting \
                real-time temperature, torque, and attitude-angle monitoring and alerting."
                .into(),
            generic: false,
        };
    }
    // Generic placeholder: display_name mirrors the raw model_type; no fabrication.
    CatalogEntry {
        model_type: model_type.to_string(),
        display_name: if model_type.is_empty() {
            "Unknown model".into()
        } else {
            model_type.to_string()
        },
        form_factor: "—".into(),
        dof: 0,
        vendor: "—".into(),
        description: "Model present in the DB but not yet registered in the catalog metadata; \
            contact an admin to add it."
            .into(),
        generic: true,
    }
}

#[derive(Serialize)]
struct SupportedRobotOut {
    model_type: String,
    display_name: String,
    form_factor: String,
    dof: i32,
    vendor: String,
    description: String,
    generic: bool,
    total: i32,
    online: i32,
    firmware_versions: Vec<String>,
    stations: i32,
}

#[derive(Serialize)]
struct SupportedRobotsResponse {
    models: Vec<SupportedRobotOut>,
    total_robots: i32,
}

pub fn base_ids() -> Vec<String> {
    (1..=100).map(|i| format!("humanoid-{i:03}")).collect()
}

fn offline_placeholder(robot_id: String) -> RobotTelemetryOut {
    RobotTelemetryOut {
        robot_id,
        status_code: 0,
        error_code: 0,
        battery_pct: 0.0,
        max_joint_temp: 0.0,
        joint_temperatures: HashMap::new(),
        joint_positions: HashMap::new(),
        joint_torques: HashMap::new(),
        ts: 0,
        is_online: false,
        last_seen: None,
    }
}

fn parse_joint_map(value: Option<&String>) -> HashMap<String, f64> {
    match value {
        Some(s) => serde_json::from_str::<HashMap<String, f64>>(s).unwrap_or_default(),
        None => HashMap::new(),
    }
}

/// Replicates `state_cache._decode()` + offline placeholder resolution.
async fn snapshot_one(conn: &mut ConnectionManager, robot_id: &str) -> RobotTelemetryOut {
    let key = format!("robot:state:{robot_id}");
    let h: HashMap<String, String> = conn.hgetall(&key).await.unwrap_or_default();
    if h.is_empty() {
        return offline_placeholder(robot_id.to_string());
    }
    let numf = |v: Option<&String>| v.and_then(|s| s.parse::<f64>().ok()).unwrap_or(0.0);
    let numi = |v: Option<&String>| v.and_then(|s| s.parse::<i64>().ok()).unwrap_or(0);
    RobotTelemetryOut {
        robot_id: robot_id.to_string(),
        status_code: numi(h.get("status_code")) as i32,
        error_code: numi(h.get("error_code")) as i32,
        battery_pct: numf(h.get("battery_pct")),
        max_joint_temp: numf(h.get("max_joint_temp")),
        joint_temperatures: parse_joint_map(h.get("joint_temperatures")),
        joint_positions: parse_joint_map(h.get("joint_positions")),
        joint_torques: parse_joint_map(h.get("joint_torques")),
        ts: numi(h.get("ts")),
        is_online: false,
        last_seen: None,
    }
}

/// Replicates `liveness.enrich()`: batch online/last_seen from the ZSET ledger.
async fn enrich(redis: &mut ConnectionManager, robots: &mut [RobotTelemetryOut], threshold_s: f64) {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0);
    for r in robots.iter_mut() {
        let score: Option<f64> = redis
            .zscore::<&str, &str, Option<f64>>("liveness:last_seen", r.robot_id.as_str())
            .await
            .unwrap_or(None);
        match score {
            None => {
                r.is_online = false;
                r.last_seen = None;
            }
            Some(s) => {
                r.is_online = (now - s) <= threshold_s;
                r.last_seen = Some(s as i64);
            }
        }
    }
}

/// WS first-frame snapshot — mirror of `state_cache.snapshot(robot_ids)` for the
/// demo set (NO fleet merge: `ws_robots` calls `snapshot(settings.robot_ids)`,
/// which is the 100 `humanoid-00N` ids only). Enumerates every id (offline
/// robots get the placeholder, not omission), then batch-enriches `is_online` /
/// `last_seen` from the liveness ledger — identical to the Python path.
pub async fn build_ws_snapshot(
    redis: &mut ConnectionManager,
    robot_ids: &[String],
) -> Vec<RobotTelemetryOut> {
    let mut robots: Vec<RobotTelemetryOut> = Vec::with_capacity(robot_ids.len());
    for id in robot_ids {
        robots.push(snapshot_one(redis, id).await);
    }
    enrich(redis, &mut robots, 30.0).await;
    robots
}

async fn list_robots(State(state): State<AppState>) -> Json<RobotListResponse> {
    let base = base_ids();

    // Merged fleet: demo set + every real robot in robot_metadata not in the demo set.
    let extra_rows = sqlx::query("SELECT robot_id FROM robot_metadata WHERE robot_id != ALL($1::text[])")
        .bind(&base)
        .fetch_all(&state.pool)
        .await
        .expect("robot_metadata query");
    let extra_ids: Vec<String> = extra_rows
        .iter()
        .map(|r| r.get::<String, _>("robot_id"))
        .collect();
    let merged: Vec<String> = base.iter().cloned().chain(extra_ids.iter().cloned()).collect();

    // Snapshot (Redis state hash -> typed, or offline placeholder).
    let mut redis = state.redis.clone();
    let mut robots: Vec<RobotTelemetryOut> = Vec::with_capacity(merged.len());
    for id in &merged {
        robots.push(snapshot_one(&mut redis, id).await);
    }

    // Batch-enrich online/last_seen (liveness ledger).
    enrich(&mut redis, &mut robots, 30.0).await;

    // Backfill real (extra) robots with no live feed: real battery + DB is_online.
    if !extra_ids.is_empty() {
        let meta_rows = sqlx::query(
            "SELECT m.robot_id, m.is_online, \
             (SELECT battery_pct FROM robot_telemetry t WHERE t.robot_id = m.robot_id \
              ORDER BY time DESC LIMIT 1) AS last_battery \
             FROM robot_metadata m WHERE m.robot_id = ANY($1::text[])",
        )
        .bind(&extra_ids)
        .fetch_all(&state.pool)
        .await
        .expect("robot_metadata backfill query");

        let mut meta_map: HashMap<String, (bool, f64)> = HashMap::new();
        for r in &meta_rows {
            let rid: String = r.get("robot_id");
            let online: bool = r.get("is_online");
            let last_battery: Option<f64> = r.get("last_battery");
            meta_map.insert(rid, (online, last_battery.unwrap_or(0.0)));
        }
        for robot in robots.iter_mut() {
            if let Some((online, bat)) = meta_map.get(&robot.robot_id) {
                if robot.ts == 0 {
                    robot.battery_pct = *bat;
                    robot.is_online = *online;
                }
            }
        }
    }

    Json(RobotListResponse { robots })
}

/// Mirror of `main.py:394 supported_robots`: catalog of platform-supported models
/// + composition of the registered fleet. Source of truth for *which* models exist
/// is `robot_metadata.model_type` (DB-driven); `lookup()` only supplements display
/// metadata. Auth (JWT) is deferred — spike validates data/contract parity.
async fn supported_robots(State(state): State<AppState>) -> Json<SupportedRobotsResponse> {
    let rows = sqlx::query(
        "SELECT model_type,
                COUNT(*)                                              AS total,
                COUNT(*) FILTER (WHERE is_online)                     AS online,
                array_remove(
                    array_agg(DISTINCT firmware_version)
                    FILTER (WHERE firmware_version IS NOT NULL), NULL
                )                                                     AS firmwares,
                COUNT(DISTINCT assigned_station)
                    FILTER (WHERE assigned_station IS NOT NULL)        AS stations
         FROM robot_metadata
         GROUP BY model_type
         ORDER BY total DESC, model_type",
    )
    .fetch_all(&state.pool)
    .await
    .expect("supported_robots query");

    let mut models: Vec<SupportedRobotOut> = Vec::with_capacity(rows.len());
    let mut total_robots: i64 = 0;
    for r in &rows {
        let model_type: String = r.get("model_type");
        let total: i64 = r.get("total");
        let online: i64 = r.get("online");
        let firmwares: Option<Vec<String>> = r.get("firmwares");
        let stations: i64 = r.get("stations");
        let meta = lookup(&model_type);
        total_robots += total;
        models.push(SupportedRobotOut {
            model_type,
            display_name: meta.display_name,
            form_factor: meta.form_factor,
            dof: meta.dof,
            vendor: meta.vendor,
            description: meta.description,
            generic: meta.generic,
            total: total as i32,
            online: online as i32,
            firmware_versions: firmwares.unwrap_or_default(),
            stations: stations as i32,
        });
    }
    Json(SupportedRobotsResponse {
        models,
        total_robots: total_robots as i32,
    })
}

async fn healthz() -> Json<serde_json::Value> {
    Json(serde_json::json!({
        "status": "ok",
        "service": "robot-monitor-backend-rs",
        "lang": "rust",
    }))
}

// --------------------------------------------------------------- /robots/{id}/history
// Mirror of backend_ingestion/app/main.py:452 `history()`: timeline for the Drawer
// (lines + joint heatmap). Source of truth for *which* aggregate: prefer the
// continuous aggregate `robot_history_10s` (ISSUE #76, R-03 §4 contract red line),
// fall back to the raw `robot_telemetry` table ONLY when the aggregate returns an
// empty set (cold start / policy not yet refreshed / window uncovered) — never
// double-query the non-empty path. Span routing (R-12): `minutes > 180` switches to
// the hourly aggregate `robot_history_1h` and echoes `resolution = "3600s"`.
//
// `last(joint_positions, <order>)` is kept in BOTH branches — it is the ONLY data
// source for the Drawer's "angle (rad)" line (RobotDetailDrawer.tsx:88).
// Auth (JWT) is deferred — spike validates data/contract parity.

const MAX_HISTORY_MINUTES: i64 = 7 * 24 * 60; // 10080
const HOURILY_AFTER_MINUTES: i64 = 180;

#[derive(Deserialize)]
struct HistoryQuery {
    minutes: Option<i64>,
    resolution: Option<String>,
}

#[derive(Serialize)]
struct HistoryPointOut {
    time: String,
    status_code: i32,
    battery_pct: f64,
    max_joint_temp: f64,
    joint_temperatures: serde_json::Value,
    joint_torques: serde_json::Value,
    joint_positions: serde_json::Value,
}

#[derive(Serialize)]
struct RobotHistoryResponse {
    robot_id: String,
    resolution: String,
    points: Vec<HistoryPointOut>,
}

/// Mirror of `main.py:639 _parse_resolution`: `'10s'/'30s'/'1m' -> integer seconds`.
/// Non-integer multiples and sub-1s are REJECTED (HTTP 400), never silently truncated.
fn parse_resolution(res: &str) -> Result<i64, String> {
    let s = res.trim().to_lowercase();
    if s.ends_with("ms") {
        return Err("resolution below 1s not supported".into());
    }
    let unit = if s.ends_with('s') || s.ends_with('m') {
        s.chars().last().unwrap().to_string()
    } else {
        String::new()
    };
    if unit.is_empty() {
        return Err(res.to_string());
    }
    let raw = &s[..s.len() - 1];
    let value: f64 = match raw.parse() {
        Ok(v) => v,
        Err(_) => return Err(res.to_string()),
    };
    if value != value || value.is_infinite() {
        // NaN / ±inf: parse succeeded but is not a meaningful resolution.
        return Err(res.to_string());
    }
    if (value - value.trunc()).abs() > f64::EPSILON {
        return Err(format!("resolution must be a whole number of seconds: {res}"));
    }
    let seconds = value as i64 * if unit == "m" { 60 } else { 1 };
    if seconds < 1 {
        return Err("resolution below 1s not supported".into());
    }
    Ok(seconds)
}

/// Python `datetime.isoformat()` for a UTC timestamp: `YYYY-MM-DDTHH:MM:SS[.ffffff]+00:00`.
fn iso_utc(ts: DateTime<Utc>) -> String {
    let micros = ts.timestamp_subsec_micros();
    let base = ts.format("%Y-%m-%dT%H:%M:%S").to_string();
    if micros == 0 {
        format!("{base}+00:00")
    } else {
        format!("{base}.{micros:06}+00:00")
    }
}

/// Python pydantic v2 datetime serialization (AlertItem.time/ack_at) for a UTC
/// timestamp: `YYYY-MM-DDTHH:MM:SS[.ffffff]Z`. pydantic v2 emits a bare `Z` for a
/// zero UTC offset (not `+00:00`), which is what `GET /alerts` actually returns on
/// the wire — so the Rust output must match it byte-for-byte.
fn iso_utc_z(ts: DateTime<Utc>) -> String {
    let micros = ts.timestamp_subsec_micros();
    let base = ts.format("%Y-%m-%dT%H:%M:%S").to_string();
    if micros == 0 {
        format!("{base}Z")
    } else {
        format!("{base}.{micros:06}Z")
    }
}

async fn history(
    State(state): State<AppState>,
    Path(robot_id): Path<String>,
    Query(params): Query<HistoryQuery>,
) -> Result<Json<RobotHistoryResponse>, (axum::http::StatusCode, Json<serde_json::Value>)> {
    let raw_minutes = params.minutes.unwrap_or(5);
    let resolution = params.resolution.clone().unwrap_or_else(|| "10s".into());

    let res_s = match parse_resolution(&resolution) {
        Ok(v) => v,
        Err(_) => {
            return Err((
                axum::http::StatusCode::BAD_REQUEST,
                Json(json!({"detail": format!("invalid resolution: {resolution}")})),
            ))
        }
    };
    if raw_minutes > MAX_HISTORY_MINUTES {
        return Err((
            axum::http::StatusCode::BAD_REQUEST,
            Json(json!({"detail": "history span too large"})),
        ));
    }
    let minutes = raw_minutes.max(1);
    let span_s = minutes * 60;
    let hourly = minutes > HOURILY_AFTER_MINUTES;
    let agg_source = if hourly { "robot_history_1h" } else { "robot_history_10s" };
    let out_res_s = if hourly { 3600 } else { res_s };

    // Column set is byte-for-byte identical between branches (name == response field);
    // only FROM / WHERE differ. `time_order` is the ordering column for `last()`:
    // `bucket` for the cagg (no raw `time` column), `time` for the raw table.
    let sql_cagg = format!(
        "SELECT bucket                                          AS bucket,
                robot_id,
                last(status_code, bucket)                        AS status_code,
                COALESCE(last(battery_pct, bucket), 0.0)         AS battery_pct,
                COALESCE(max(max_joint_temp), 0.0)               AS max_joint_temp,
                last(joint_temperatures, bucket)                 AS joint_temperatures,
                last(joint_torques, bucket)                      AS joint_torques,
                last(joint_positions, bucket)                    AS joint_positions
         FROM {agg_source}
         WHERE robot_id = $1
           AND bucket >= now() - interval '{span_s} seconds'
         GROUP BY bucket, robot_id
         ORDER BY bucket"
    );
    let sql_raw = format!(
        "SELECT time_bucket('{res_s} seconds'::interval, time)    AS bucket,
                robot_id,
                last(status_code, time)                           AS status_code,
                COALESCE(last(battery_pct, time), 0.0)            AS battery_pct,
                COALESCE(max(max_joint_temp), 0.0)                AS max_joint_temp,
                last(joint_temperatures, time)                    AS joint_temperatures,
                last(joint_torques, time)                         AS joint_torques,
                last(joint_positions, time)                       AS joint_positions
         FROM robot_telemetry
         WHERE robot_id = $1
           AND time >= now() - interval '{span_s} seconds'
         GROUP BY bucket, robot_id
         ORDER BY bucket"
    );

    let rows = match sqlx::query(&sql_cagg).bind(&robot_id).fetch_all(&state.pool).await {
        Ok(r) => {
            if r.is_empty() {
                // Aggregate not ready for this window -> fall back to raw (single extra query).
                sqlx::query(&sql_raw)
                    .bind(&robot_id)
                    .fetch_all(&state.pool)
                    .await
                    .map_err(|e| server_err(&e))?
            } else {
                r
            }
        }
        Err(e) => return Err(server_err(&e)),
    };

    let mut points = Vec::with_capacity(rows.len());
    for r in &rows {
        let bucket: DateTime<Utc> = r.get("bucket");
        let status_code: i32 = r.try_get("status_code").unwrap_or(0);
        let battery_pct: f64 = r.get("battery_pct");
        let max_joint_temp: f64 = r.get("max_joint_temp");
        let jt: Option<serde_json::Value> = r.try_get("joint_temperatures").ok().flatten();
        let jq: Option<serde_json::Value> = r.try_get("joint_torques").ok().flatten();
        let jp: Option<serde_json::Value> = r.try_get("joint_positions").ok().flatten();
        points.push(HistoryPointOut {
            time: iso_utc(bucket),
            status_code,
            battery_pct,
            max_joint_temp,
            joint_temperatures: jt.unwrap_or(serde_json::Value::Object(serde_json::Map::new())),
            joint_torques: jq.unwrap_or(serde_json::Value::Object(serde_json::Map::new())),
            joint_positions: jp.unwrap_or(serde_json::Value::Object(serde_json::Map::new())),
        });
    }

    Ok(Json(RobotHistoryResponse {
        robot_id,
        resolution: format!("{out_res_s}s"),
        points,
    }))
}

fn server_err(e: &dyn std::fmt::Display) -> (axum::http::StatusCode, Json<serde_json::Value>) {
    (
        axum::http::StatusCode::SERVICE_UNAVAILABLE,
        Json(json!({"detail": format!("query failed: {e}")})),
    )
}

// --------------------------------------------------------------- /metrics
// Mirror of backend_ingestion/app/metrics.py + main.py:299 `prometheus_metrics`.
//
// PARITY SCOPE (deliberately narrow): this is the Prometheus *exposition*
// endpoint, not a dashboard data API. In Python, counter/gauge/histogram values
// are produced by the live INGESTION pipeline (batch_writer / MQTT / WS hub).
// This Rust spike is a STATELESS READ REPLICA — it does NOT ingest — so every
// ingest/WS metric is emitted at 0 (honest: "this process runs no ingest loop").
// The one metric Rust can legitimately derive live is `robot_backend_robots_online`,
// computed by mirroring `liveness.online_count()` (ZCOUNT of `liveness:last_seen`
// within the offline threshold). The content-type is passed through verbatim from
// prometheus_client (`text/plain; version=0.0.4; charset=utf-8`) — Prometheus
// negotiates the format by this version string, so it must not be altered.
//
// Therefore parity for this endpoint is SCHEMA parity, NOT value parity:
//   (1) content-type identical to Python;
//   (2) every metric name Rust emits is also declared by Python (no invented names).
// Both are verified live in verify_spike.py.

const OFFLINE_THRESHOLD_S: f64 = 30.0; // mirror config.py: robot_offline_threshold_s
// Mirrors prometheus_client.CONTENT_TYPE_LATEST (the installed client is 1.0.0).
const PROMETHEUS_CONTENT_TYPE: &str = "text/plain; version=1.0.0; charset=utf-8";

/// Mirror of `liveness.online_count()`: count members of the `liveness:last_seen`
/// ZSET whose score (epoch seconds) is within `OFFLINE_THRESHOLD_S` of now.
async fn online_count(redis: &mut ConnectionManager) -> Result<i64, redis::RedisError> {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0);
    let min = format!("{:.6}", now - OFFLINE_THRESHOLD_S);
    let cnt: i64 = redis::cmd("ZCOUNT")
        .arg("liveness:last_seen")
        .arg(&min)
        .arg("+inf")
        .query_async(redis)
        .await?;
    Ok(cnt)
}

#[derive(Clone, Copy)]
enum MType {
    Counter,
    Gauge,
    Histogram,
}

struct Metric {
    name: &'static str,
    mtype: MType,
    help: &'static str,
    /// For labeled counters, one series is emitted per (label, value) pair.
    labels: &'static [(&'static str, &'static str)],
}

/// Metric registry mirrored from backend_ingestion/app/metrics.py (verbatim names,
/// types, and HELP strings). Adding/renaming here must track that file.
const METRICS: &[Metric] = &[
    Metric { name: "robot_backend_telemetry_written_total", mtype: MType::Counter, help: "Telemetry rows successfully written to TimescaleDB (accumulated per batch, unit: rows)", labels: &[] },
    Metric { name: "robot_backend_batch_flushes_total", mtype: MType::Counter, help: "BatchWriter flush count, split by trigger reason (count=full 500 rows / timeout=full 1s)", labels: &[("reason", "count"), ("reason", "timeout")] },
    Metric { name: "robot_backend_queue_depth", mtype: MType::Gauge, help: "Current backlog of the asyncio.Queue (steadily rising = flush cannot keep up with 1000 msg/s)", labels: &[] },
    Metric { name: "robot_backend_mqtt_received_total", mtype: MType::Counter, help: "Telemetry messages received over MQTT", labels: &[] },
    Metric { name: "robot_backend_mqtt_dropped_total", mtype: MType::Counter, help: "Messages dropped due to MQTT validation failure (>0 means the payload contract is broken)", labels: &[] },
    Metric { name: "robot_backend_action_mqtt_received_total", mtype: MType::Counter, help: "VLA policy-action messages received over MQTT (robots/+/action)", labels: &[] },
    Metric { name: "robot_backend_action_mqtt_dropped_total", mtype: MType::Counter, help: "VLA policy-action messages dropped due to validation failure", labels: &[] },
    Metric { name: "robot_backend_ws_probe_alive", mtype: MType::Gauge, help: "Latest WS synthetic-probe result (1=handshake + received snapshot first frame / 0=failure)", labels: &[] },
    Metric { name: "robot_backend_ws_probe_failures_total", mtype: MType::Counter, help: "Cumulative WS synthetic-probe failures (>0 means the client-perspective realtime stream dropped)", labels: &[] },
    Metric { name: "robot_backend_ws_connections", mtype: MType::Gauge, help: "Current active /ws/robots connection count (including in-handshake connections not yet authenticated)", labels: &[] },
    Metric { name: "robot_backend_robots_online", mtype: MType::Gauge, help: "Online robot count (now - last_seen <= threshold, ISSUE #77; stopped reporters excluded)", labels: &[] },
    Metric { name: "robot_backend_history_latency_seconds", mtype: MType::Histogram, help: "/history endpoint processing time (incl. DB query, the data source for Drawer line chart + heatmap)", labels: &[] },
    Metric { name: "robot_backend_history_source_total", mtype: MType::Counter, help: "/history actual data source hit: cagg=continuous aggregate robot_history_10s / raw=raw table fallback", labels: &[("source", "cagg"), ("source", "raw")] },
    Metric { name: "robot_backend_ws_outbound_queue_depth", mtype: MType::Gauge, help: "Combined outbound backlog frame count across all /ws/robots connections (persistent >0 means a slow client)", labels: &[] },
    Metric { name: "robot_backend_ws_telemetry_frames_dropped_total", mtype: MType::Counter, help: "Telemetry frames dropped due to a full outbound queue (drop oldest, the backpressure cost of a slow client)", labels: &[] },
    Metric { name: "robot_backend_ws_alerts_deferred_total", mtype: MType::Counter, help: "Alert frames deferred because the outbound queue was full (should be 0; >0 means alert backlog, scale up or drop a connection)", labels: &[] },
    Metric { name: "robot_backend_ws_slow_client_evicted_total", mtype: MType::Counter, help: "Slow client connections actively closed due to a persistently full outbound queue or heartbeat timeout", labels: &[] },
    Metric { name: "robot_backend_ws_hub_frames_total", mtype: MType::Counter, help: "WS hub frames actually sent, split by kind (telemetry=one merged frame per robot / alert=sent individually)", labels: &[("kind", "telemetry"), ("kind", "alert")] },
    Metric { name: "robot_backend_ws_hub_coalesced_total", mtype: MType::Counter, help: "Telemetry frames merged away by the tick (received this tick - actually fanned out; >0 = keep-latest-per-robot in effect)", labels: &[] },
    Metric { name: "robot_backend_telemetry_buffered_total", mtype: MType::Counter, help: "Telemetry rows stored in the offline buffer (Redis list) after a DB write failure (awaiting replay)", labels: &[] },
    Metric { name: "robot_backend_telemetry_buffered_replayed_total", mtype: MType::Counter, help: "Telemetry rows replayed from the offline buffer and re-enqueued into the writer", labels: &[] },
];

const HISTORY_LATENCY_BUCKETS: &[f64] = &[0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5];

/// Render the Prometheus exposition (HELP + TYPE + samples), matching the text
/// format produced by prometheus_client.generate_latest().
///
/// prometheus_client strips the conventional `_total` suffix when emitting the
/// auto-generated `_created` gauge (e.g. `..._written_total` -> `..._written_created`).
fn created_name(name: &str) -> String {
    let base = name.strip_suffix("_total").unwrap_or(name);
    format!("{base}_created")
}

/// Live values for the WS-derived metrics (all other metrics stay at their
/// honest stateless-replica 0). See `hub::WsMetrics` / `Hub::client_stats`.
struct WsLive {
    connections: f64,
    queue_depth: f64,
    coalesced: f64,
    frames_telemetry: f64,
    frames_alert: f64,
    dropped: f64,
    deferred: f64,
}

impl Default for WsLive {
    fn default() -> Self {
        WsLive {
            connections: 0.0,
            queue_depth: 0.0,
            coalesced: 0.0,
            frames_telemetry: 0.0,
            frames_alert: 0.0,
            dropped: 0.0,
            deferred: 0.0,
        }
    }
}

/// Override the value for a labeled counter with the live WS value, if the metric
/// name is one the hub actually drives.
fn ws_counter_sample(name: &str, label_value: Option<&str>, ws: &WsLive) -> Option<String> {
    match name {
        "robot_backend_ws_hub_coalesced_total" => Some(format!("{}", ws.coalesced as i64)),
        "robot_backend_ws_telemetry_frames_dropped_total" => Some(format!("{}", ws.dropped as i64)),
        "robot_backend_ws_alerts_deferred_total" => Some(format!("{}", ws.deferred as i64)),
        "robot_backend_ws_hub_frames_total" => {
            let v = match label_value {
                Some("alert") => ws.frames_alert,
                _ => ws.frames_telemetry,
            } as i64;
            Some(format!("{}", v))
        }
        _ => None,
    }
}

/// Override the value for a gauge with the live WS value, if applicable.
fn ws_gauge_sample(name: &str, ws: &WsLive) -> Option<String> {
    match name {
        "robot_backend_ws_connections" => Some(format!("{:.1}", ws.connections)),
        "robot_backend_ws_outbound_queue_depth" => Some(format!("{:.1}", ws.queue_depth)),
        _ => None,
    }
}

fn render_metrics(online: i64, ws: &WsLive) -> String {
    let mut out = String::new();
    for m in METRICS {
        out.push_str("# HELP ");
        out.push_str(m.name);
        out.push(' ');
        out.push_str(m.help);
        out.push('\n');

        let t = match m.mtype {
            MType::Counter => "counter",
            MType::Gauge => "gauge",
            MType::Histogram => "histogram",
        };
        out.push_str("# TYPE ");
        out.push_str(m.name);
        out.push(' ');
        out.push_str(t);
        out.push('\n');

        match m.mtype {
            MType::Histogram => {
                for &b in HISTORY_LATENCY_BUCKETS {
                    out.push_str(m.name);
                    out.push_str("_bucket{le=\"");
                    out.push_str(&format!("{b}"));
                    out.push_str("\"} 0\n");
                }
                out.push_str(m.name);
                out.push_str("_bucket{le=\"+Inf\"} 0\n");
                out.push_str(m.name);
                out.push_str("_sum 0\n");
                out.push_str(m.name);
                out.push_str("_count 0\n");
                // prometheus_client also emits a `_created` gauge for histograms.
                let cn = created_name(m.name);
                out.push_str("# HELP ");
                out.push_str(&cn);
                out.push(' ');
                out.push_str(m.help);
                out.push('\n');
                out.push_str("# TYPE ");
                out.push_str(&cn);
                out.push_str(" gauge\n");
                out.push_str(&cn);
                out.push_str(" 0\n");
            }
            MType::Counter => {
                if m.labels.is_empty() {
                    let val = ws_counter_sample(m.name, None, ws).unwrap_or_else(|| "0".to_string());
                    out.push_str(m.name);
                    out.push(' ');
                    out.push_str(&val);
                    out.push('\n');
                } else {
                    for (k, v) in m.labels {
                        let val = ws_counter_sample(m.name, Some(v), ws)
                            .unwrap_or_else(|| "0".to_string());
                        out.push_str(m.name);
                        out.push('{');
                        out.push_str(k);
                        out.push_str("=\"");
                        out.push_str(v);
                        out.push_str("\"} ");
                        out.push_str(&val);
                        out.push('\n');
                    }
                }
                // prometheus_client emits a `_created` gauge for counters that have an
                // active series (with the conventional `_total` suffix stripped). We emit
                // it unconditionally so the monitoring contract is stable even before a
                // counter has been incremented on a given label (Python only emits it
                // once a series exists, which is non-deterministic across scrape timing).
                let cn = created_name(m.name);
                out.push_str("# HELP ");
                out.push_str(&cn);
                out.push(' ');
                out.push_str(m.help);
                out.push('\n');
                out.push_str("# TYPE ");
                out.push_str(&cn);
                out.push_str(" gauge\n");
                out.push_str(&cn);
                out.push_str(" 0\n");
            }
            MType::Gauge => {
                out.push_str(m.name);
                if m.name == "robot_backend_robots_online" {
                    // Prometheus gauges are formatted as floats (mirrors "0.0").
                    out.push_str(&format!(" {online}.0\n"));
                } else if let Some(val) = ws_gauge_sample(m.name, ws) {
                    out.push_str(&format!(" {val}\n"));
                } else {
                    out.push_str(" 0.0\n");
                }
            }
        }
    }
    out
}

async fn metrics(State(state): State<AppState>) -> impl axum::response::IntoResponse {
    let online = match online_count(&mut state.redis.clone()).await {
        Ok(n) => n,
        Err(e) => {
            tracing::warn!("metrics online_count failed: {e}");
            0
        }
    };
    let (conns, depth, _, _) = state.hub.client_stats();
    let m = state.hub.metrics();
    let (coalesced, frames_telemetry, frames_alert) = m.snapshot();
    let ws = WsLive {
        connections: conns as f64,
        queue_depth: depth as f64,
        coalesced: coalesced as f64,
        frames_telemetry: frames_telemetry as f64,
        frames_alert: frames_alert as f64,
        dropped: m.telemetry_dropped_total.load(Relaxed) as f64,
        deferred: m.alerts_deferred_total.load(Relaxed) as f64,
    };
    let body = render_metrics(online, &ws);
    (
        axum::http::StatusCode::OK,
        [(
            axum::http::header::CONTENT_TYPE,
            axum::http::HeaderValue::from_static(PROMETHEUS_CONTENT_TYPE),
        )],
        body,
    )
}

// --------------------------------------------------------------- /alerts
// Mirror of backend_ingestion/app/alerts/{router.py,repository.py}: paginated
// alert query (GET /alerts). Source of truth for columns is the explicit
// `_COLS` list in repository.py; we reproduce it verbatim. Filters are tri-state
// (None = no filter). Sort is `(time DESC, alert_id DESC)` to keep pagination
// boundaries stable. `detail` is a jsonb column: sqlx (with the "json" feature)
// already decodes it to a serde_json::Value, so we keep objects and drop
// everything else — exactly Python `_decode_detail`'s "return dict, else None".
// Auth (JWT) is deferred — spike validates data/contract parity, not the boundary.

const ALERT_COLS: &str = "alert_id, time, robot_id, rule_id, severity, message, detail, \
    ack, ack_at, ack_by, escalated, suppressed, suppressed_by";

#[derive(Clone)]
enum Param {
    B(bool),
    S(String),
}

/// Mirror of `repository.py:_build_filters`: assemble the WHERE clause + $n params.
/// Params are numbered contiguously from $1; limit/offset continue the numbering.
fn build_alerts_where(
    ack: Option<bool>,
    escalated: Option<bool>,
    suppressed: Option<bool>,
    robot_id: Option<String>,
) -> (String, Vec<Param>) {
    let mut clauses: Vec<String> = Vec::new();
    let mut params: Vec<Param> = Vec::new();
    if let Some(a) = ack {
        params.push(Param::B(a));
        clauses.push(format!("ack = ${}", params.len()));
    }
    if let Some(e) = escalated {
        params.push(Param::B(e));
        clauses.push(format!("escalated = ${}", params.len()));
    }
    if let Some(s) = suppressed {
        params.push(Param::B(s));
        clauses.push(format!("suppressed = ${}", params.len()));
    }
    if let Some(r) = robot_id {
        params.push(Param::S(r));
        clauses.push(format!("robot_id = ${}", params.len()));
    }
    let where_clause = if clauses.is_empty() {
        String::new()
    } else {
        format!(" WHERE {}", clauses.join(" AND "))
    };
    (where_clause, params)
}

#[derive(Deserialize)]
struct AlertQuery {
    ack: Option<bool>,
    escalated: Option<bool>,
    suppressed: Option<bool>,
    robot_id: Option<String>,
    limit: Option<i64>,
    offset: Option<i64>,
}

#[derive(Serialize)]
struct AlertItemOut {
    alert_id: i64,
    time: String,
    robot_id: String,
    rule_id: String,
    severity: i32,
    message: String,
    detail: Option<serde_json::Value>,
    ack: bool,
    ack_at: Option<String>,
    ack_by: Option<String>,
    escalated: bool,
    suppressed: bool,
    suppressed_by: Option<String>,
}

#[derive(Serialize)]
struct AlertPageOut {
    items: Vec<AlertItemOut>,
    total: i64,
    limit: i64,
    offset: i64,
}

/// Mirror of `repository.py:list_alerts`: same connection, same transaction,
/// two reads (list + count) so total and items never disagree.
async fn list_alerts_handler(
    State(state): State<AppState>,
    Query(q): Query<AlertQuery>,
) -> Result<Json<AlertPageOut>, (axum::http::StatusCode, Json<serde_json::Value>)> {
    let limit = q.limit.unwrap_or(50).clamp(1, 500);
    let offset = q.offset.unwrap_or(0).max(0);
    let (where_clause, params) =
        build_alerts_where(q.ack, q.escalated, q.suppressed, q.robot_id);

    let list_sql = format!(
        "SELECT {ALERT_COLS} FROM robot_alert{where_clause} \
         ORDER BY time DESC, alert_id DESC LIMIT ${} OFFSET ${}",
        params.len() + 1,
        params.len() + 2
    );
    let count_sql = format!("SELECT count(*) FROM robot_alert{where_clause}");

    let mut lq = sqlx::query(&list_sql);
    for p in &params {
        lq = match p {
            Param::B(b) => lq.bind(*b),
            Param::S(s) => lq.bind(s.clone()),
        };
    }
    lq = lq.bind(limit).bind(offset);
    let rows = lq
        .fetch_all(&state.pool)
        .await
        .map_err(|e| server_err(&e))?;

    let mut cq = sqlx::query(&count_sql);
    for p in &params {
        cq = match p {
            Param::B(b) => cq.bind(*b),
            Param::S(s) => cq.bind(s.clone()),
        };
    }
    let total = cq
        .fetch_one(&state.pool)
        .await
        .map_err(|e| server_err(&e))?
        .get::<i64, _>("count");

    let mut items = Vec::with_capacity(rows.len());
    for r in &rows {
        let time: DateTime<Utc> = r.get("time");
        let ack_at: Option<DateTime<Utc>> = r.get("ack_at");
        let detail_raw: Option<serde_json::Value> = r.try_get("detail").ok().flatten();
        let detail = match detail_raw {
            Some(serde_json::Value::Object(_)) => detail_raw,
            _ => None,
        };
        items.push(AlertItemOut {
            alert_id: r.get("alert_id"),
            time: iso_utc_z(time),
            robot_id: r.get("robot_id"),
            rule_id: r.get("rule_id"),
            severity: r.get("severity"),
            message: r.get("message"),
            detail,
            ack: r.get("ack"),
            ack_at: ack_at.map(iso_utc_z),
            ack_by: r.get("ack_by"),
            escalated: r.get("escalated"),
            suppressed: r.get("suppressed"),
            suppressed_by: r.get("suppressed_by"),
        });
    }

    Ok(Json(AlertPageOut {
        items,
        total,
        limit,
        offset,
    }))
}

fn env_f64(name: &str, default: f64) -> f64 {
    std::env::var(name)
        .ok()
        .and_then(|v| v.parse::<f64>().ok())
        .unwrap_or(default)
}

fn env_usize(name: &str, default: usize) -> usize {
    std::env::var(name)
        .ok()
        .and_then(|v| v.parse::<usize>().ok())
        .unwrap_or(default)
}

fn env_string(name: &str, default: &str) -> String {
    std::env::var(name).unwrap_or_else(|_| default.to_string())
}

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into()),
        )
        .init();

    let database_url = std::env::var("DATABASE_URL")
        .unwrap_or_else(|_| "postgresql://postgres:postgres@localhost:5432/robot_monitor".into());
    let redis_url =
        std::env::var("REDIS_URL").unwrap_or_else(|_| "redis://localhost:6379".into());

    let pool = PgPoolOptions::new()
        .max_connections(10)
        .connect(&database_url)
        .await
        .expect("postgres connect");
    let redis_client = redis::Client::open(redis_url.clone()).expect("redis client");
    let redis = ConnectionManager::new(redis_client)
        .await
        .expect("redis connection manager");

    // ---- WebSocket hub config (mirrors backend_ingestion/app/config.py) ----
    let jwt_secret = std::env::var("JWT_SECRET").unwrap_or_default();
    let hub_config = HubConfig {
        redis_url: redis_url.clone(),
        broadcast_interval_ms: env_f64("WS_BROADCAST_INTERVAL_MS", 100.0),
        queue_size: env_usize("WS_OUTBOUND_QUEUE_SIZE", 500),
        alert_slack: env_usize("WS_ALERT_OVERFLOW_SLACK", 64),
        close_timeout_s: env_f64("WS_OUTBOUND_CLOSE_TIMEOUT_S", 60.0),
        chan_stream_all: env_string("CHAN_STREAM_ALL", "robot:stream:all"),
        chan_alerts: env_string("CHAN_ALERTS", "robot:alerts"),
        chan_action_all: env_string("CHAN_ACTION_ALL", "robot:actions:all"),
    };
    let ws_metrics = Arc::new(WsMetrics::default());
    let hub = Hub::new(hub_config, ws_metrics);
    hub.clone().start();

    let state = AppState {
        pool,
        redis,
        hub: hub.clone(),
        jwt_secret,
    };
    let app = Router::new()
        .route("/healthz", get(healthz))
        .route("/robots", get(list_robots))
        .route("/supported-robots", get(supported_robots))
        .route("/robots/:robot_id/history", get(history))
        .route("/alerts", get(list_alerts_handler))
        .route("/metrics", get(metrics))
        .route("/ws/robots", get(ws::ws_handler))
        .with_state(state);

    let addr: std::net::SocketAddr = "0.0.0.0:8001".parse().expect("valid addr");
    tracing::info!("robot-monitor-backend-rs listening on http://{addr}");
    axum::serve(
        tokio::net::TcpListener::bind(addr).await.expect("bind"),
        app,
    )
    .await
    .expect("serve");
}

// ---------------------------------------------------------------------------
// Offline contract tests (no DB needed). These verify the two things that do NOT
// require a live Postgres/Redis: (1) the catalog `lookup()` mirror of
// app/catalog.py, and (2) the JSON shape of SupportedRobotsResponse matching the
// Python `schemas/api.py` field names — the frontend consumes these keys.
// Full data parity (live SQL rows -> structs) is validated separately by
// compare_spike2.py once the Docker stack is up.
#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{Map, Value};

    #[test]
    fn metrics_exposition_schema_matches_prometheus_format() {
        // content-type must equal prometheus_client's CONTENT_TYPE_LATEST (1.0.0).
        assert_eq!(
            PROMETHEUS_CONTENT_TYPE,
            "text/plain; version=1.0.0; charset=utf-8"
        );

        let body = render_metrics(42, &WsLive::default());

        // HELP + TYPE present for every declared metric.
        for m in METRICS {
            assert!(
                body.contains(&format!("# HELP {m}", m = m.name)),
                "missing HELP for {}",
                m.name
            );
            let t = match m.mtype {
                MType::Counter => "counter",
                MType::Gauge => "gauge",
                MType::Histogram => "histogram",
            };
            assert!(
                body.contains(&format!("# TYPE {m} {t}", m = m.name, t = t)),
                "missing TYPE for {}",
                m.name
            );
            // prometheus_client emits a `_created` gauge for counters and histograms,
            // with the `_total` suffix stripped from the name.
            let expect_created =
                matches!(m.mtype, MType::Counter) || matches!(m.mtype, MType::Histogram);
            if expect_created {
                let cn = created_name(m.name);
                assert!(
                    body.contains(&format!("# TYPE {cn} gauge", cn = cn)),
                    "missing _created TYPE for {} (expected {})",
                    m.name,
                    cn
                );
            }
        }

        // online gauge carries the injected live value (Prometheus float formatting).
        assert!(
            body.contains("robot_backend_robots_online 42.0"),
            "online value not injected"
        );

        // labeled counters emit each label value.
        assert!(body.contains("robot_backend_batch_flushes_total{reason=\"count\"} 0"));
        assert!(body.contains("robot_backend_history_source_total{source=\"cagg\"} 0"));
        assert!(body.contains("robot_backend_ws_hub_frames_total{kind=\"alert\"} 0"));

        // histogram helper lines.
        assert!(body.contains("robot_backend_history_latency_seconds_bucket{le=\"0.01\"} 0"));
        assert!(body.contains("robot_backend_history_latency_seconds_bucket{le=\"+Inf\"} 0"));
        assert!(body.contains("robot_backend_history_latency_seconds_sum 0"));
        assert!(body.contains("robot_backend_history_latency_seconds_count 0"));

        // every non-comment line is a well-formed `name[{labels}] value` sample.
        for line in body.lines() {
            if line.starts_with('#') {
                continue;
            }
            let mut parts = line.rsplitn(2, ' ');
            let v = parts.next().unwrap_or("");
            let _name = parts.next().unwrap_or("");
            assert!(
                v.parse::<f64>().is_ok(),
                "non-numeric sample value in line: {line}"
            );
        }
    }

    #[test]
    fn lookup_registered_model_matches_catalog() {
        let m = lookup("HUMANOID-24DOF");
        assert_eq!(m.model_type, "HUMANOID-24DOF");
        assert_eq!(m.display_name, "Humanoid 24-DOF");
        assert_eq!(m.form_factor, "bipedal humanoid");
        assert_eq!(m.dof, 24);
        assert_eq!(m.vendor, "RobotMonitor Platform");
        assert!(!m.generic);
        assert!(!m.description.is_empty());
    }

    #[test]
    fn lookup_unregistered_model_is_generic_placeholder() {
        let m = lookup("SEGWAY-S1");
        assert_eq!(m.model_type, "SEGWAY-S1");
        assert_eq!(m.display_name, "SEGWAY-S1"); // display_name mirrors raw id, no fabrication
        assert_eq!(m.form_factor, "—");
        assert_eq!(m.dof, 0);
        assert_eq!(m.vendor, "—");
        assert!(m.generic);
        assert!(m.description.contains("not yet registered"));
    }

    #[test]
    fn lookup_empty_model_is_unknown_placeholder() {
        let m = lookup("");
        assert_eq!(m.display_name, "Unknown model");
        assert!(m.generic);
    }

    #[test]
    fn parse_resolution_mirrors_python_rules() {
        // Acceptable integer forms.
        assert_eq!(parse_resolution("10s").unwrap(), 10);
        assert_eq!(parse_resolution("30s").unwrap(), 30);
        assert_eq!(parse_resolution("1m").unwrap(), 60);
        assert_eq!(parse_resolution(" 5s ").unwrap(), 5); // trim
        assert_eq!(parse_resolution("10S").unwrap(), 10); // case-insensitive
        // Sub-1s forbidden.
        assert!(parse_resolution("0s").is_err());
        assert!(parse_resolution("0m").is_err());
        assert!(parse_resolution("500ms").is_err());
        // Non-integer multiples rejected (no silent truncation) — Issue #74.
        assert!(parse_resolution("2.5s").is_err());
        assert!(parse_resolution("1.5m").is_err());
        // Missing / bad unit.
        assert!(parse_resolution("10").is_err());
        assert!(parse_resolution("abc").is_err());
        assert!(parse_resolution("").is_err());
    }

    #[test]
    fn build_alerts_where_mirrors_python_filters() {
        // Mirrors repository.py:_build_filters — contiguous $n numbering, tri-state.
        let (w, p) = build_alerts_where(Some(true), None, None, None);
        assert_eq!(w, " WHERE ack = $1");
        assert_eq!(p.len(), 1);
        let (w, p) = build_alerts_where(None, Some(false), Some(true), Some("robot-1".into()));
        assert_eq!(w, " WHERE escalated = $1 AND suppressed = $2 AND robot_id = $3");
        assert_eq!(p.len(), 3);
        let (w, p) = build_alerts_where(None, None, None, None);
        assert_eq!(w, "");
        assert_eq!(p.len(), 0);
    }

    #[test]
    fn supported_robots_response_json_shape_matches_python_schema() {
        let resp = SupportedRobotsResponse {
            models: vec![SupportedRobotOut {
                model_type: "HUMANOID-24DOF".into(),
                display_name: "Humanoid 24-DOF".into(),
                form_factor: "bipedal humanoid".into(),
                dof: 24,
                vendor: "RobotMonitor Platform".into(),
                description: "x".into(),
                generic: false,
                total: 100,
                online: 0,
                firmware_versions: vec!["1.0.0".into()],
                stations: 3,
            }],
            total_robots: 100,
        };
        let v: Value = serde_json::to_value(&resp).unwrap();
        let obj = v.as_object().unwrap();
        // top-level keys
        assert_eq!(obj.keys().cloned().collect::<Vec<_>>(), vec!["models", "total_robots"]);
        // per-model keys (order-independent)
        let model = obj["models"].as_array().unwrap()[0].clone();
        let model_obj = model.as_object().unwrap();
        let mut expected: Vec<&str> = vec![
            "model_type", "display_name", "form_factor", "dof", "vendor",
            "description", "generic", "total", "online", "firmware_versions", "stations",
        ];
        let mut got: Vec<&str> = model_obj.keys().map(|k| k.as_str()).collect();
        // JSON object key order is not semantically meaningful (frontend reads by name),
        // so compare as a set, not by order.
        expected.sort_unstable();
        got.sort_unstable();
        assert_eq!(got, expected, "JSON keys must match Python SupportedRobotOut");
        // types
        assert!(model_obj["dof"].is_i64());
        assert!(model_obj["generic"].is_boolean());
        assert!(model_obj["firmware_versions"].is_array());
        assert!(obj["total_robots"].is_i64());
        // ensure no extra/renamed keys leaked
        let _ = Map::new();
    }
}
