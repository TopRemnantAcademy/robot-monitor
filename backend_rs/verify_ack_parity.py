#!/usr/bin/env python3
"""Live parity for POST /alerts/{id}/ack | /unack: Python (8000) vs Rust (8001).

The two stacks SHARE the same `robot_alert` table (dual-writer). The `changed`
field is inherently sequencing-dependent: whichever stack writes first flips the
row (changed=True) and the second writer sees an already-flipped row
(changed=False). So `changed` must be verified PER STACK in ISOLATION — each
stack's idempotency cycle is run from a freshly-reset (unacked) row, and the two
cycles' `changed` patterns + final states are compared. The shared-state state
fields (ack / ack_by / ack_at) are compared for the first-ack result.

A throwaway viewer is registered, promoted to admin via the local TimescaleDB
container, used for the test, then deleted at exit (admin count returns to 0).

Exits non-zero on any mismatch so it can gate CI.
"""
import atexit
import json
import subprocess
import sys
import urllib.parse
import urllib.request
import datetime as dt

BASE_PY = "http://localhost:8000"
BASE_RS = "http://localhost:8001"
EMAIL = "ack_verify_" + dt.datetime.now().strftime("%Y%m%d%H%M%S") + "@robomon.dev"
PASSWORD = "AckVerify!2026"

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


def cleanup():
    sql = f"DELETE FROM robot_user WHERE email = '{EMAIL}';"
    try:
        out = subprocess.run(
            ["docker", "exec", "robot-monitor-timescaledb", "psql",
             "-U", "postgres", "-d", "robot_monitor", "-c", sql],
            capture_output=True, text=True, timeout=30,
        )
        if out.returncode == 0:
            print(f"[cleanup] removed temp admin {EMAIL}")
        else:
            print(f"[cleanup] FAILED: {out.stderr.strip()[:200]}", file=sys.stderr)
    except Exception as e:
        print(f"[cleanup] FAILED: {e}", file=sys.stderr)


atexit.register(cleanup)


def req(method, url, token=None, body=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def reset_alert(aid):
    sql = f"UPDATE robot_alert SET ack=FALSE, ack_at=NULL, ack_by=NULL WHERE alert_id={aid};"
    subprocess.run(
        ["docker", "exec", "robot-monitor-timescaledb", "psql",
         "-U", "postgres", "-d", "robot_monitor", "-c", sql],
        capture_output=True, text=True, timeout=30,
    )


def register_login():
    body = json.dumps({"email": EMAIL, "password": PASSWORD}).encode()
    r = urllib.request.Request(BASE_PY + "/auth/register", data=body,
                               headers={"Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(r, timeout=30).read()
    except urllib.error.HTTPError as e:
        print(f"  register note: HTTP {e.code}")
    data = urllib.parse.urlencode({"username": EMAIL, "password": PASSWORD}).encode()
    r = urllib.request.Request(BASE_PY + "/auth/jwt/login", data=data,
                               headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    with urllib.request.urlopen(r, timeout=30) as resp:
        return json.loads(resp.read())["access_token"]


def promote_admin():
    sql = f"UPDATE robot_user SET role='admin', is_active=true WHERE email='{EMAIL}';"
    out = subprocess.run(
        ["docker", "exec", "robot-monitor-timescaledb", "psql",
         "-U", "postgres", "-d", "robot_monitor", "-c", sql],
        capture_output=True, text=True, timeout=30,
    )
    if out.returncode != 0:
        raise RuntimeError(f"promote failed: {out.stderr}")
    print(f"  promoted {EMAIL} to admin")


def idempotency_cycle(stack_base, tok, aid, label):
    """Run a full ack/unack cycle from a reset (unacked) row; return first-ack result."""
    reset_alert(aid)
    s1, r1 = req("POST", f"{stack_base}/alerts/{aid}/ack", tok, {})
    check(f"{label} ack status 200", s1 == 200, f"status={s1}")
    check(f"{label} ack changed==True", r1.get("changed") is True, f"changed={r1.get('changed')}")
    check(f"{label} ack ack==True", r1.get("ack") is True)
    check(f"{label} ack ack_by set", bool(r1.get("ack_by")))

    s2, r2 = req("POST", f"{stack_base}/alerts/{aid}/ack", tok, {})
    check(f"{label} re-ack changed==False", r2.get("changed") is False, f"changed={r2.get('changed')}")

    s3, r3 = req("POST", f"{stack_base}/alerts/{aid}/unack", tok, {})
    check(f"{label} unack status 200", s3 == 200, f"status={s3}")
    check(f"{label} unack changed==True", r3.get("changed") is True, f"changed={r3.get('changed')}")
    check(f"{label} unack ack==False", r3.get("ack") is False)

    s4, r4 = req("POST", f"{stack_base}/alerts/{aid}/unack", tok, {})
    check(f"{label} re-unack changed==False", r4.get("changed") is False, f"changed={r4.get('changed')}")
    return r1


def main():
    print("=== register + login (temp viewer) ===")
    tok = register_login()
    print("  token obtained:", bool(tok))
    print("=== promote to admin ===")
    promote_admin()

    st, alerts = req("GET", BASE_PY + "/alerts?limit=1", tok)
    aid = alerts["items"][0]["alert_id"]
    print(f"  using alert_id={aid}")

    print("=== isolated idempotency cycle: Python ===")
    r1_py = idempotency_cycle(BASE_PY, tok, aid, "py")
    print("=== isolated idempotency cycle: Rust ===")
    r1_rs = idempotency_cycle(BASE_RS, tok, aid, "rs")

    print("=== first-ack STATE parity (py vs rs) ===")
    check("ack==True both", r1_py.get("ack") is True and r1_rs.get("ack") is True)
    check("ack_by equal", r1_py.get("ack_by") == r1_rs.get("ack_by"),
          f"py={r1_py.get('ack_by')} rs={r1_rs.get('ack_by')}")
    check("ack_at present + Z-formatted both",
          bool(r1_py.get("ack_at")) and bool(r1_rs.get("ack_at")) and
          r1_py["ack_at"].endswith("Z") and r1_rs["ack_at"].endswith("Z"),
          f"py={r1_py.get('ack_at')} rs={r1_rs.get('ack_at')}")
    # Both cycles end unacked (reset between), so final-state parity is trivially equal.

    print("=== 404 on missing alert (both) ===")
    p4, _ = req("POST", f"{BASE_PY}/alerts/999999999/ack", tok, {})
    r4, _ = req("POST", f"{BASE_RS}/alerts/999999999/ack", tok, {})
    check("missing -> 404 both", p4 == 404 and r4 == 404, f"py={p4} rs={r4}")

    print("=== auth boundary: no token -> 401 (both) ===")
    pn, _ = req("POST", f"{BASE_PY}/alerts/{aid}/ack", None, {})
    rn, _ = req("POST", f"{BASE_RS}/alerts/{aid}/ack", None, {})
    check("no token -> 401 both", pn == 401 and rn == 401, f"py={pn} rs={rn}")

    # leave the alert in its pre-test state (unacked) for cleanliness
    reset_alert(aid)

    print(f"\n=== SUMMARY: {checks - len(failures)}/{checks} passed, {len(failures)} failed ===")
    if failures:
        print("FAILED:", failures)
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
