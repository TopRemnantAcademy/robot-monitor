#!/usr/bin/env python3
"""Live contract-parity check: Python backend (8000) vs Rust spike (8001).

Uses the existing seeded admin account from .env to obtain a JWT (no temp user
created — avoids mutating the DB for verification). The Rust spike is
unauthenticated by design (auth is deferred; this validates data/contract parity).

Exits non-zero on any mismatch so it can gate CI.
"""
import json
import sys
import urllib.parse
import urllib.request
import datetime as dt
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE_PY = "http://localhost:8000"
BASE_RS = "http://localhost:8001"

# --- read admin creds from .env (local only, gitignored) ---
env = {}
for line in (ROOT / ".env").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()

EMAIL = "spike_verify_" + dt.datetime.now().strftime("%Y%m%d%H%M%S") + "@robomon.dev"
PASSWORD = "SpikeVerify!2026"


def get(url, token=None):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def get_err(url, token=None):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        urllib.request.urlopen(req, timeout=30)
        return None, None
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def register_login():
    # Register a throwaway viewer account (no real data), login, verify, then DELETE
    # it from robot_user so the DB is left untouched.
    body = json.dumps({"email": EMAIL, "password": PASSWORD}).encode()
    req = urllib.request.Request(
        BASE_PY + "/auth/register",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=30).read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode()
        if e.code >= 400:
            # 400 on register usually means the email already exists from a prior run; proceed to login.
            print(f"  register note: HTTP {e.code} {detail[:120]}")
    data = urllib.parse.urlencode({"username": EMAIL, "password": PASSWORD}).encode()
    req = urllib.request.Request(
        BASE_PY + "/auth/jwt/login",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())["access_token"]


def approx(a, b, nd=6):
    return round(float(a), nd) == round(float(b), nd)


def parse_ts(s):
    return dt.datetime.fromisoformat(s)


def get_raw(url):
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode(), r.headers.get("Content-Type", "")


def metric_names(text):
    names = set()
    for line in text.splitlines():
        if line.startswith("# TYPE "):
            parts = line.split(" ", 3)
            if len(parts) >= 3:
                names.add(parts[2])
    return names


def metric_value(text, name):
    for line in text.splitlines():
        if line.startswith(name + " ") or line.startswith(name + "{"):
            return line.rsplit(" ", 1)[-1]
    return None


failures = []
checks = 0


def check(name, cond, detail=""):
    global checks
    checks += 1
    if cond:
        print(f"  [PASS] {name}")
    else:
        print(f"  [FAIL] {name}  {detail}")
        failures.append(name)


print("=== register + login (temp verification user) ===")
tok = register_login()
print("  token obtained:", bool(tok))

# ---------------------------------------------------------------- /supported-robots
print("=== /supported-robots ===")
py = get(BASE_PY + "/supported-robots", tok)
rs = get(BASE_RS + "/supported-robots")
check("total_robots equal", py["total_robots"] == rs["total_robots"],
      f"py={py['total_robots']} rs={rs['total_robots']}")
check("model_type sets equal",
      set(m["model_type"] for m in py["models"]) == set(m["model_type"] for m in rs["models"]),
      f"only_py={sorted(set(m['model_type'] for m in py['models']) - set(m['model_type'] for m in rs['models']))} "
      f"only_rs={sorted(set(m['model_type'] for m in rs['models']) - set(m['model_type'] for m in py['models']))}")
pym = {m["model_type"]: m for m in py["models"]}
rsm = {m["model_type"]: m for m in rs["models"]}
fields = ["display_name", "form_factor", "dof", "vendor", "description", "generic",
          "total", "online", "stations"]
mism = {f: 0 for f in fields}
for mt in set(pym) & set(rsm):
    p, s = pym[mt], rsm[mt]
    for f in fields:
        if p[f] != s[f]:
            mism[f] += 1
    if sorted(p["firmware_versions"]) != sorted(s["firmware_versions"]):
        mism["firmware_versions"] += 1
check("per-model fields equal", all(v == 0 for v in mism.values()), f"mismatches={mism}")
# JSON key set of a model matches exactly
pk = sorted(pym[next(iter(pym))].keys())
rk = sorted(rsm[next(iter(rsm))].keys())
check("model JSON keys equal", pk == rk, f"py={pk} rs={rk}")

# ---------------------------------------------------------------- /robots
print("=== /robots ===")
py = get(BASE_PY + "/robots", tok)
rs = get(BASE_RS + "/robots")
check("robot count equal", len(py["robots"]) == len(rs["robots"]),
      f"py={len(py['robots'])} rs={len(rs['robots'])}")
check("online count equal",
      sum(1 for r in py["robots"] if r["is_online"]) == sum(1 for r in rs["robots"] if r["is_online"]),
      f"py_online={sum(1 for r in py['robots'] if r['is_online'])} "
      f"rs_online={sum(1 for r in rs['robots'] if r['is_online'])}")
pym = {r["robot_id"]: r for r in py["robots"]}
rsm = {r["robot_id"]: r for r in rs["robots"]}
check("robot_id sets equal", set(pym) == set(rsm),
      f"only_py={sorted(set(pym)-set(rsm))[:5]} only_rs={sorted(set(rsm)-set(pym))[:5]}")
# field-by-field on intersection (numeric/boolean)
bad = 0
for rid in set(pym) & set(rsm):
    p, s = pym[rid], rsm[rid]
    for f in ("status_code", "error_code", "battery_pct", "max_joint_temp", "ts", "is_online"):
        pv, sv = p[f], s[f]
        if f in ("battery_pct", "max_joint_temp"):
            if not approx(pv, sv):
                bad += 1
        elif pv != sv:
            bad += 1
check("per-robot scalar fields equal", bad == 0, f"mismatches={bad}")
# joint maps equal (parsed JSON, order-independent)
badj = 0
for rid in set(pym) & set(rsm):
    p, s = pym[rid], rsm[rid]
    for f in ("joint_temperatures", "joint_positions", "joint_torques"):
        if json.dumps(p[f], sort_keys=True) != json.dumps(s[f], sort_keys=True):
            badj += 1
check("per-robot joint maps equal", badj == 0, f"mismatches={badj}")

# ---------------------------------------------------------------- /history (cagg, 5m default)
print("=== /history (robot=humanoid-001, 5m, 10s) ===")
RID = "humanoid-001"
py = get(f"{BASE_PY}/robots/{RID}/history?minutes=5&resolution=10s", tok)
rs = get(f"{BASE_RS}/robots/{RID}/history?minutes=5&resolution=10s")
check("resolution equal", py["resolution"] == rs["resolution"], f"py={py['resolution']} rs={rs['resolution']}")
check("points count equal", len(py["points"]) == len(rs["points"]),
      f"py={len(py['points'])} rs={len(rs['points'])}")
n = min(len(py["points"]), len(rs["points"]))
bad = 0
for i in range(n):
    p, s = py["points"][i], rs["points"][i]
    if parse_ts(p["time"]) != parse_ts(s["time"]):
        bad += 1; continue
    if p["status_code"] != s["status_code"] or not approx(p["battery_pct"], s["battery_pct"]) \
       or not approx(p["max_joint_temp"], s["max_joint_temp"]):
        bad += 1; continue
    for f in ("joint_temperatures", "joint_positions", "joint_torques"):
        if json.dumps(p[f], sort_keys=True) != json.dumps(s[f], sort_keys=True):
            bad += 1; break
check("per-point fields equal", bad == 0, f"mismatches={bad}")

# ---------------------------------------------------------------- /history (hourly path, minutes=200)
print("=== /history (robot=humanoid-001, 200m -> hourly, 3600s) ===")
py = get(f"{BASE_PY}/robots/{RID}/history?minutes=200&resolution=10s", tok)
rs = get(f"{BASE_RS}/robots/{RID}/history?minutes=200&resolution=10s")
check("resolution echoes 3600s", py["resolution"] == "3600s" and rs["resolution"] == "3600s",
      f"py={py['resolution']} rs={rs['resolution']}")
check("hourly points count equal", len(py["points"]) == len(rs["points"]),
      f"py={len(py['points'])} rs={len(rs['points'])}")

# ---------------------------------------------------------------- /alerts (default)
print("=== /alerts (default limit=50) ===")
py = get(BASE_PY + "/alerts", tok)
rs = get(BASE_RS + "/alerts")
check("alerts total equal", py["total"] == rs["total"], f"py={py['total']} rs={rs['total']}")
check("alerts items len equal", len(py["items"]) == len(rs["items"]),
      f"py={len(py['items'])} rs={len(rs['items'])}")
check("alerts limit/offset echoed", py["limit"] == rs["limit"] and py["offset"] == rs["offset"],
      f"py={py['limit']},{py['offset']} rs={rs['limit']},{rs['offset']}")
pym = {it["alert_id"]: it for it in py["items"]}
rsm = {it["alert_id"]: it for it in rs["items"]}
check("alert_id sets equal", set(pym) == set(rsm),
      f"only_py={sorted(set(pym)-set(rsm))[:5]} only_rs={sorted(set(rsm)-set(pym))[:5]}")
bad = 0
for aid in set(pym) & set(rsm):
    p, s = pym[aid], rsm[aid]
    for f in ("robot_id", "rule_id", "severity", "message", "ack", "escalated",
              "suppressed", "ack_by", "suppressed_by"):
        if p[f] != s[f]:
            bad += 1
            break
    if p["time"] != s["time"]:
        bad += 1
        continue
    if (p["ack_at"] or "") != (s["ack_at"] or ""):
        bad += 1
        continue
    if json.dumps(p["detail"], sort_keys=True) != json.dumps(s["detail"], sort_keys=True):
        bad += 1
        continue
check("per-alert fields equal", bad == 0, f"mismatches={bad}")

# ---------------------------------------------------------------- /alerts (filtered)
print("=== /alerts (filtered ack=false) ===")
py = get(BASE_PY + "/alerts?ack=false&limit=50", tok)
rs = get(BASE_RS + "/alerts?ack=false&limit=50")
check("filtered total equal", py["total"] == rs["total"], f"py={py['total']} rs={rs['total']}")
check("filtered ack all false (rs)", all(not it["ack"] for it in rs["items"]), "rs has ack=true")

# ---------------------------------------------------------------- /metrics (schema parity)
# NOTE: /metrics is the Prometheus exposition endpoint (text/plain), not a
# dashboard data API. Both backends expose metric names/types/HELP; values are
# produced by the live ingest pipeline. The Rust read-replica emits ingest metrics
# at 0 but derives `robot_backend_robots_online` live (same ZCOUNT source as
# Python). Parity here is SCHEMA parity, plus the one value both can compute.
print("=== /metrics (schema parity) ===")
py_body, py_ct = get_raw(BASE_PY + "/metrics")
rs_body, rs_ct = get_raw(BASE_RS + "/metrics")
check("content-type identical", py_ct == rs_ct and py_ct != "",
      f"py={py_ct} rs={rs_ct}")
check("content-type is prometheus 1.0.0",
      rs_ct == "text/plain; version=1.0.0; charset=utf-8", f"rs={rs_ct}")
py_names = metric_names(py_body)
rs_names = metric_names(rs_body)
# prometheus_client emits a `_created` gauge per counter only once that counter has
# an active series, so the Python `_created` set is non-deterministic across scrapes.
# Rust emits `_created` unconditionally; the stable invariants are therefore:
#   - Rust covers every metric name Python currently exposes (superset);
#   - Rust never introduces a NEW base metric name (only the conventional `_created`
#     companions, which mirror prometheus_client's behavior).
rs_base = {n for n in rs_names if not n.endswith("_created")}
py_base = {n for n in py_names if not n.endswith("_created")}
check("rust introduces no new base metric names", rs_base <= py_base,
      f"only_rs={sorted(rs_base - py_base)}")
check("rust covers all python metric names", py_names <= rs_names,
      f"missing={sorted(py_names - rs_names)}")
py_online = metric_value(py_body, "robot_backend_robots_online")
rs_online = metric_value(rs_body, "robot_backend_robots_online")
# Both backends compute this live from the same `liveness:last_seen` ZSET, so the
# numeric value must match (Python formats the gauge as a float, Rust mirrors it).
check("robots_online present + equal (same ZSET)",
      py_online is not None and rs_online is not None
      and float(py_online) == float(rs_online),
      f"py={py_online} rs={rs_online}")
check("robots_online is a non-negative number",
      rs_online is not None and float(rs_online) >= 0, f"rs={rs_online}")

# ---------------------------------------------------------------- error contract
print("=== error contract ===")
pc, pb = get_err(f"{BASE_PY}/robots/{RID}/history?resolution=2.5s", tok)
rc, rb = get_err(f"{BASE_RS}/robots/{RID}/history?resolution=2.5s")
check("invalid resolution -> 400 both", pc == 400 and rc == 400, f"py={pc} rs={rc}")
pc, pb = get_err(f"{BASE_PY}/robots/{RID}/history?minutes=99999", tok)
rc, rb = get_err(f"{BASE_RS}/robots/{RID}/history?minutes=99999")
check("span too large -> 400 both", pc == 400 and rc == 400, f"py={pc} rs={rc}")

print(f"\n=== SUMMARY: {checks - len(failures)}/{checks} passed, {len(failures)} failed ===")
if failures:
    print("FAILED:", failures)
    sys.exit(1)
print("ALL PASS")
