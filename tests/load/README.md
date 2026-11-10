# tests/load — decidable load testing (Order 23 / I-16)

> **One-liner**: Replace emqtt-bench's "just emit numbers" with k6 + xk6-mqtt, so that "connection success rate / publish latency / WS receive rate" become machine-decidable `check()` assertions that feed directly into the k6 exit code, instead of making a human read a string of numbers and judge for themselves.

---

## 1. Why replace emqtt-bench

| | emqtt-bench (Erlang) | This directory (k6 + xk6-mqtt) |
|---|---|---|
| Output | Just emit numbers (throughput, latency) | Numbers **+ `check()` PASS/FAIL** |
| Judgment | Human reads the chart to set thresholds | Thresholds written into `thresholds`, **non-zero exit code on miss** |
| CI usability | Needs OTP 27.2+ to compile | Official prebuilt binary, or one `xk6 build` command |
| Assertion power | Weak | Arbitrary JS logic (field types, message content, semantics) |

Basis: `external/INTEGRATION-PLAN.md` §8.6 (I-16), `external/SOURCES.md` §5 (N7/N8). `R-07 §6.1` explicitly says "forbid using 'no errors in logs' as a substitute for a pass judgment" — and emqtt-bench's numbers fall exactly into that "no errors in logs" category.

Note: **Do not replace `simulator/mock_fleet.py`** (explicit in Order 23, same in `INTEGRATION-PLAN.md` §8.6). mock_fleet is the only producer of the real 24-joint payload; this directory only does **load testing and judgment**.

---

## 2. File list

| File | Role |
|---|---|
| `payload.js` | Payload generator **structurally identical field-by-field** to `mock_fleet.py` (24 joints) |
| `config.js` | Load parameters + judgment thresholds (all overridable via env vars) |
| `mqtt_pub.js` | MQTT load core: N units × 10Hz publish + `check()` assertions (**requires xk6-mqtt**) |
| `ws_watch.js` | WS receive-rate / snapshot size / field-type assertions (**official k6 suffices**) |
| `run.sh` | One-click entry: prerequisites → load → probe → summary (distinguishes env errors / judgment failures) |
| `test_payload_contract.py` | **Unit test**: field-by-field diff of `payload.js` against `mock_fleet.py` |
| `last-run.json` / `last-ws-run.json` | Most recent run's judgment summary (`handleSummary` output) |

---

## 3. Getting k6 with xk6-mqtt (pick one of three)

Official k6 **does not include** the `k6/x/mqtt` module; running it directly errors with `unknown module: k6/x/mqtt`.

```bash
# A) Prebuilt artifact (fastest; note the asset URL has a version, not /latest/)
curl -sL https://github.com/grafana/xk6-mqtt/releases/download/v0.3.3/xk6-mqtt_v0.3.3_linux_amd64.tar.gz \
  | tar xz -C /tmp && mv /tmp/xk6-mqtt_v0.3.3_linux_amd64/k6 ./k6-mqtt

# B) Build it yourself (needs Go)
go install go.k6.io/xk6/cmd/xk6@latest
xk6 build --with github.com/grafana/xk6-mqtt@latest --output ./k6-mqtt

# C) Reuse an existing one
K6_BIN=/path/to/k6-mqtt bash tests/load/run.sh
```

Actually-measured binary self-reported extensions (verified locally):
```
k6 v2.3.0 (go1.27.1, linux/amd64)
Extensions:
  github.com/grafana/xk6-mqtt v0.3.3, k6/x/mqtt [js]
```

---

## 4. How to run

```bash
# Full (MQTT load + WS probe) —— requires broker and backend both running
bash tests/load/run.sh

# Small smoke test (not 100 units)
ROBOTS=20 DURATION_S=10 bash tests/load/run.sh

# Only MQTT, or only WS
bash tests/load/run.sh --mqtt-only
WS_TOKEN=<jwt> bash tests/load/run.sh --ws-only

# Only probe the environment, start no load
bash tests/load/run.sh --preflight-only
```

**Exit codes** (continuing the `test_integration.sh` convention, R-07 §6.3):

| Code | Meaning |
|---|---|
| 0 | All judgments passed |
| 1 | A **judgment failure** exists (threshold/check red) —— code or config problem |
| 2 | **Environment not ready** (no k6 binary / broker not up / backend not up) → related items marked "unverified" |

### Run a single script directly with k6

```bash
./k6-mqtt run tests/load/mqtt_pub.js          # default 100 units × 10Hz × 30s
./k6-mqtt run tests/load/ws_watch.js          # WS probe (official k6 also works)
```

### Key environment variables

| Variable | Default | Description |
|---|---|---|
| `ROBOTS` | `100` | Number of robot units (`spec/00 §1`) |
| `PUBLISH_HZ` | `10` | Publishes per unit per second (`spec/00 §1`) |
| `DURATION_S` | `30` | Steady-state duration (`R-07 §2` uses 30s) |
| `RAMP_S` | `5` | Ramp-up duration (avoid misjudging a connection storm) |
| `BROKER_URL` | `mqtt://localhost:1883` | xk6-mqtt accepts `mqtt://` URL |
| `WS_URL` | `ws://localhost:8000/ws/robots` | Backend WS endpoint |
| `WS_TOKEN` | empty | JWT. **From Order 15, WS requires `?token=`**; empty is rejected with close 1008 |
| `K6_BIN` | `./k6-mqtt` | Path to k6 with the extension |
| `TH_PUB_P95_MS` etc | see `config.js` | Judgment threshold overrides |

Get a JWT (when backend is running):

```bash
curl -s -X POST http://localhost:8000/auth/jwt/login \
  -d 'username=<email>&password=<pwd>' | python3 -c 'import json,sys;print(json.load(sys.stdin)["access_token"])'
```

---

## 5. The three hard assertions (corresponding to R-07/R-08)

| # | Assertion | Source | Threshold | Where implemented |
|---|---|---|---|---|
| ① | **Connection success rate** | `R-07 §1` readiness criterion | ≥ 0.99 | `mqtt_pub.js`'s `'MQTT connection established'` + `thresholds.checks` |
| ② | **Publish latency** | `R-07 §3` | p95 < 50ms, p99 < 200ms | `mqtt_pub.js`'s `pub_latency_ms` Trend |
| ③ | **WS receive rate / first frame** | `R-08 C4/C5` | snapshot ≥ 100 units; numeric-field rate ≥ 0.99 | `ws_watch.js`'s `ws_snapshot_robots` / `ws_telemetry_fields_ok` |

**Each is hooked to `thresholds`**, so a miss gives a non-zero k6 exit code. This point is **reverse-verified** (see §7 "assertion validity") — writing only `check()` without thresholds would judge red as pass.

### How the payload contract guarantees "reuse mock_fleet's real 24-joint structure"

`payload.js`'s joint names and fields were copied from `simulator/mock_fleet.py`, but "copying" is not trustworthy, so there is `test_payload_contract.py`:

```bash
python tests/load/test_payload_contract.py     # or python -m pytest tests/load/
```

It uses `ast` to **parse** `mock_fleet.py` (not regex guessing), then **actually runs** `payload.js` with node, asserting item by item: 24 joint key names and order, top-level field set, `robot_id` regex, three joint-dict key consistency, `max_joint_temp == max(joint_temperatures)`, and message size on the same order of magnitude.

---

## 6. Local measured evidence (2026-10-11)

Environment: in-container Linux amd64; broker is a local `aedes` (MQTT 3.1.1), WS server is a `ws` server reproduced from the backend contract (three-part envelope + `?token=` rejection semantics). k6 = official prebuilt `xk6-mqtt v0.3.3` binary.

### 6.1 Full scale: 100 units × 10Hz × 30s

```
[load] broker=mqtt://localhost:1883 robots=100 hz=10 vus=100 duration=30s ramp=5s
       target=1000 msg/s expected=30000
[load] judgment summary (checks rate=1.0000, pass=285 fail=0)
```

| Metric | Measured | Target (R-07 §3) | Judgment |
|---|---|---|---|
| check pass rate | **285/285 = 1.0000** | — | PASS |
| Publish throughput | **35696 in 30s = 1190 msg/s** | ≥ 1000 msg/s | PASS |
| Publish latency p95 | **1 ms** | < 50 ms | PASS |
| Publish latency p99 | **1 ms** | < 200 ms | PASS |
| Publish latency max | 12 ms | — | observation |

> `1189.9 msg/s` being slightly above 1000 is normal: beyond the steady-state window it also includes the tail of the ramp, so `EXPECTED_PUBLISHES` is used for order-of-magnitude validation, and `checks` for each VU's per-rhythm pass rate. **This is not "loosening the threshold"** (R-07 §6.2) — R-07's ≥1000 msg/s criterion is a lower bound.

### 6.2 WS probe

```
[ws] judgment summary (checks rate=1.0000, pass=84 fail=0, messages=80)
  [PASS] WS probe: checks passed 84/84 (fail rate 0% <= 5%)
```

### 6.3 run.sh full chain

```
==> summary
  passed 4 / judgment failed 0 / env errors 0 / unverified 0
  —— passed ——
     * MQTT load
     * k6 thresholds (MQTT)
     * WS probe
     * k6 thresholds (WS)
  conclusion: all passed
```

---

## 7. Assertion validity (**reverse verification**, required by repo discipline)

Writing a test doesn't count; you must prove "it really goes red". Three reverse-verification scenarios were actually run:

| Scenario | Construct | Result |
|---|---|---|
| **Bad WS server** | snapshot sends only 50 units + stringifies `status_code`/`battery_pct` | `checks rate=0.6000 (3 pass / 2 fail)`, **k6 exit code 99** |
| **No token** | `WS_TOKEN=` left empty (Order 15 onward closes 1008) | `setup()` throws, **exit code 107**, prints only 1 explanation (no log spam) |
| **Broker not up** | stop broker then `run.sh --mqtt-only` | classified as **env error**, **exit code 2**, MQTT item marked "unverified" |

### Two real defects exposed and fixed by reverse verification

1. **Writing `check()` without `thresholds` → judges red as pass.**
   The first version of `ws_watch.js` only declared `ws_up` / `ws_telemetry_fields_ok` thresholds; `checks` was not hooked. Measured: on the bad server ("50 units + stringified fields"), k6 still printed `ws_probe ✓` and **ended with exit code 0**. Only after adding `checks: ['rate>=…']` did it become 99.
2. **Rejection-triggered reconnect storm floods the summary.**
   Putting `check()` inside `socket.on('error')` / `on('close')` re-triggers it on every k6 auto-reconnect: measured one 2s session blew up to **43647 fails**, diluting the pass rate to 0.25 and making the summary meaningless. Fix: final assertions settle exactly once on the **first close** (`settled` flag), and "no token" aborts in `setup()` instead of entering the storm.

---

## 8. Unverified / boundaries (honestly labeled)

| Item | Why unverified | What's needed to verify |
|---|---|---|
| **Real EMQX broker** | No Docker locally; load test used a local `aedes` MQTT 3.1.1 broker | Run after `docker compose -f infra/docker-compose.yml up -d`, and check EMQX's connection-count / dropped metrics via `emqx ctl` |
| **Real backend `/ws/robots`** | Same as above; WS used a reproduced substitute server | Bring up `backend_ingestion` and obtain a real JWT (`WS_TOKEN`) |
| **End-to-end landing rows ≈ 30000** | Needs real TimescaleDB SQL counting | This is `test_integration.sh`'s job (`R-08 F3`), not repeated here |
| **Frontend render ≈10 times/sec (R-08 E4)** | 100ms throttle is **browser-internal** behavior | Needs Playwright / real browser counting; `ws_watch.js` only judges **server→client delivery** |
| **`xk6-mqtt` vs EMQX 5.6 protocol details** | Never run against real EMQX | Same as above — bring up EMQX then re-run |
| **The "truth" of publish latency** | This item measures the synchronous cost of **writing to the socket** (qos=0) | R-07 §3's P95<200ms is the **batch landing cost**, different scope, don't mix them |

Note: two more scope reminders:

- **`noConnectionReuse` / connection model**: each VU opens one MQTT connection. 100 VU = 100 connections. Before dialing `ROBOTS` into the thousands, confirm the broker's `max_connections`.
- **Publish timing semantics**: `client.publish()` is the synchronous version (`mqtt/client_publish.go:22`, internal `token.Wait()`), so `pub_latency_ms` is the cost of "write to socket + wait for token completion", excluding broker persistence and backend consumption.

---

## 9. Relationship with other tests (don't duplicate runs)

| Layer | Where | What it judges |
|---|---|---|
| payload structure contract | `tests/load/test_payload_contract.py` | `payload.js` ↔ `mock_fleet.py` field-by-field |
| **Load judgment** | **This directory** | Connection success rate / publish latency / WS delivery rate (`R-07`, `R-08 C1-C5`) |
| End-to-end landing | `test_integration.sh` | SQL truth count ≈ 30000 rows (`R-08 F3`) |
| schema contract | `tests/test_schema_contract.py` | `init.sql` ↔ ORM |
| Frontend behavior | `frontend_dashboard`'s type / build checks | throttle, render count (`R-08 E1-E10`) |
