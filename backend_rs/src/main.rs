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
//! Auth (JWT) is intentionally deferred — this spike validates data/contract
//! parity, not the security boundary.

use std::collections::HashMap;
use std::time::{SystemTime, UNIX_EPOCH};

use axum::extract::{Path, Query, State};
use axum::{routing::get, Json, Router};
use chrono::{DateTime, Utc};
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
}

#[derive(Serialize)]
struct RobotTelemetryOut {
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

fn base_ids() -> Vec<String> {
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
    let redis_client = redis::Client::open(redis_url).expect("redis client");
    let redis = ConnectionManager::new(redis_client)
        .await
        .expect("redis connection manager");

    let state = AppState { pool, redis };
    let app = Router::new()
        .route("/healthz", get(healthz))
        .route("/robots", get(list_robots))
        .route("/supported-robots", get(supported_robots))
        .route("/robots/:robot_id/history", get(history))
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
