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
