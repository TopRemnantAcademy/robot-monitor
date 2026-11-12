#!/usr/bin/env python3
"""Live WS parity check for the Rust /ws/robots spike vs the Python backend.

Connects to both endpoints with a valid HS256 JWT (audience fastapi-users:auth)
and compares, against the *same* Redis pub/sub events:

  1. first `snapshot` frame (type + robot set + full field deep-compare),
  2. auth rejection: missing / bad token must close with **exactly 1008** on both,
  3. wire parity for all three incremental kinds (alert / action / telemetry):
     the complete envelope **raw text** must be byte-identical, not merely
     JSON-equivalent. This is the regression guard for the canonicalization fix
     (Python `json.loads` -> `json.dumps(separators, ensure_ascii=False)` vs
     Rust forwarding the publisher's raw text, which leaked `\\uXXXX` escapes
     and whitespace),
  4. keep-latest coalescing: a burst on one robot_id must end with the newest
     frame on both stacks.

Run from inside the backend container (has websockets + redis-py + PyJWT):
    JWT_SECRET=... python verify_ws_parity.py
"""
import asyncio
import json
import os
import sys
import time
import urllib.request

import jwt
import redis
import websockets

PY = "ws://host.docker.internal:8000/ws/robots"
RS = "ws://host.docker.internal:8001/ws/robots"
PY_HTTP = "http://host.docker.internal:8000"
RS_HTTP = "http://host.docker.internal:8001"
REDIS_HOST = "host.docker.internal"
REDIS_PORT = 6379

CHAN_ALERTS = "robot:alerts"
CHAN_ACTION = "robot:actions:all"
CHAN_TELEM = "robot:stream:all"

SECRET = os.environ.get("JWT_SECRET", "")
if not SECRET:
    sys.exit("JWT_SECRET env required")

TOKEN = jwt.encode(
    {"sub": "ws-parity", "aud": "fastapi-users:auth",
     "exp": int(time.time()) + 3600},
    SECRET, algorithm="HS256",
)


async def collect(uri, token, seconds=2.0, wait_first=False):
    """Return (first_frame_dict_or_None, list_of_raw_frame_texts)."""
    raws = []
    first = None
    url = f"{uri}?token={token}"
    async with websockets.connect(url, max_size=None, open_timeout=5) as ws:
        if wait_first:
            raw = await asyncio.wait_for(ws.recv(), timeout=5)
            first = json.loads(raw)
            raws.append(raw)
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(0.05, end - time.monotonic()))
            except asyncio.TimeoutError:
                break
            raws.append(raw)
    return first, raws


async def expect_close(uri, token):
    """Return (close_code, error). A rejection MUST close with 1008."""
    url = f"{uri}?token={token}"
    try:
        async with websockets.connect(url, max_size=None, open_timeout=5) as ws:
            await asyncio.wait_for(ws.recv(), timeout=4)
        return None, "unexpectedly accepted (no close)"
    except websockets.exceptions.ConnectionClosed as e:
        return e.code, None
    except Exception as e:
        return None, f"{type(e).__name__}={e}"


def find_raw(raws, marker):
    for text in raws:
        if marker in text:
            return text
    return None


def decode(text):
    try:
        return json.loads(text)
    except Exception:
        return None


def coalesced_metric(http_base):
    """Read the hub's coalescing counter (diagnostic, see note in main())."""
    try:
        with urllib.request.urlopen(http_base + "/metrics", timeout=5) as r:
            body = r.read().decode()
        for line in body.splitlines():
            if line.startswith("robot_backend_ws_hub_coalesced_total "):
                return float(line.split()[1])
    except Exception:
        pass
    return None


async def main():
    report = {"checks": []}
    stamp = str(int(time.time() * 1000))

    # ---------------------------------------------------------------- 1) snapshot
    py_first, _ = await collect(PY, TOKEN, seconds=0.3, wait_first=True)
    rs_first, _ = await collect(RS, TOKEN, seconds=0.3, wait_first=True)
    snap_detail = {}
    if py_first is None or rs_first is None:
        snap_ok = False
        snap_detail = {"py_first_none": py_first is None, "rs_first_none": rs_first is None}
    else:
        py_robots = py_first.get("data", {}).get("robots", [])
        rs_robots = rs_first.get("data", {}).get("robots", [])
        snap_detail = {
            "py_count": len(py_robots), "rs_count": len(rs_robots),
            "type_equal": py_first.get("type") == rs_first.get("type") == "snapshot",
            "ids_equal": [r["robot_id"] for r in py_robots] == [r["robot_id"] for r in rs_robots],
            "full_list_equal": py_robots == rs_robots,
        }
        snap_ok = all(v for k, v in snap_detail.items() if k.endswith("_equal"))
        if not snap_detail["full_list_equal"]:
            for a, b in zip(py_robots, rs_robots):
                if a != b:
                    snap_detail["first_diff"] = {"py": a, "rs": b}
                    break
    report["checks"].append({"name": "snapshot_parity", "pass": snap_ok, "detail": snap_detail})

    # ---------------------------------------------------------------- 2) auth (strict 1008)
    auth = {}
    auth_ok = True
    for label, uri, tok in [
        ("py_no_token", PY, ""), ("py_bad_token", PY, "garbage.token.here"),
        ("rs_no_token", RS, ""), ("rs_bad_token", RS, "garbage.token.here"),
    ]:
        code, err = await expect_close(uri, tok)
        auth[label] = f"code={code}" if err is None else err
        auth_ok = auth_ok and err is None and code == 1008
    report["checks"].append({"name": "auth_rejection_1008", "pass": auth_ok, "detail": auth})

    # ---------------------------------------------------------------- 3) wire parity for 3 kinds
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT)
    # Deliberately NON-canonical payloads:
    #  - alert: default json.dumps (ensure_ascii=True) -> \uXXXX escapes + non-ASCII
    #  - action: compact, ASCII
    #  - telemetry: separators with spaces -> stray whitespace
    marker_alert = f"parity-alert-{stamp}"
    marker_action = f"parity-action-{stamp}"
    marker_telem = f"parity-telem-{stamp}"
    alert_payload = json.dumps({
        "robot_id": "humanoid-001", "rule_id": "ws-parity-rule", "severity": "warning",
        "message": "电机温度过高 " + marker_alert, "ts": stamp, "alert_id": marker_alert,
        "escalated": False, "data": None,
    }, separators=(",", ":"))
    action_payload = json.dumps({
        "robot_id": "humanoid-002", "policy_version": "v1", "trial_id": marker_action,
        "action": [0.1, 0.2, 0.3], "goal": "stand", "ts": stamp,
    }, separators=(",", ":"))
    telem_payload = json.dumps({
        "robot_id": "humanoid-003", "status_code": 1, "battery_pct": 88.5,
        "max_joint_temp": 41.25, "note": marker_telem, "ts": stamp,
    }, separators=(", ", ": "))

    py_task = asyncio.create_task(collect(PY, TOKEN, seconds=2.0, wait_first=True))
    rs_task = asyncio.create_task(collect(RS, TOKEN, seconds=2.0, wait_first=True))
    await asyncio.sleep(0.5)  # both sockets accepted + registered on the hub
    r.publish(CHAN_ALERTS, alert_payload)
    r.publish(CHAN_ACTION, action_payload)
    r.publish(CHAN_TELEM, telem_payload)

    # ---------------------------------------------------------------- 4) coalescing burst
    # Latest-value-wins: 5 frames on one robot_id inside one 100ms beat.
    burst_marker = f"parity-burst-{stamp}"
    before_py, before_rs = coalesced_metric(PY_HTTP), coalesced_metric(RS_HTTP)
    for seq in range(5):
        r.publish(CHAN_TELEM, json.dumps({
            "robot_id": "humanoid-004", "status_code": 1, "seq": seq,
            "marker": burst_marker, "ts": stamp,
        }, separators=(",", ":")))
    _, py_raws = await py_task
    _, rs_raws = await rs_task
    after_py, after_rs = coalesced_metric(PY_HTTP), coalesced_metric(RS_HTTP)

    wire = {}
    wire_ok = True
    for label, kind, marker in [
        ("alert", "alert", marker_alert),
        ("action", "action", marker_action),
        ("telemetry", "telemetry", marker_telem),
    ]:
        py_text = find_raw(py_raws, marker)
        rs_text = find_raw(rs_raws, marker)
        py_obj, rs_obj = decode(py_text), decode(rs_text)
        entry = {
            "py_seen": py_text is not None, "rs_seen": rs_text is not None,
            "py_kind": py_obj.get("type") if isinstance(py_obj, dict) else None,
            "rs_kind": rs_obj.get("type") if isinstance(rs_obj, dict) else None,
            "raw_equal": py_text is not None and py_text == rs_text,
            "data_equal": bool(
                isinstance(py_obj, dict) and isinstance(rs_obj, dict)
                and py_obj.get("data") == rs_obj.get("data")
            ),
        }
        if not entry["raw_equal"]:
            entry["py_raw"] = py_text
            entry["rs_raw"] = rs_text
        ok = (
            entry["py_seen"] and entry["rs_seen"]
            and entry["py_kind"] == entry["rs_kind"] == kind
            and entry["raw_equal"] and entry["data_equal"]
        )
        wire_ok = wire_ok and ok
        wire[label] = entry
    report["checks"].append({"name": "wire_parity_alert_action_telemetry", "pass": wire_ok, "detail": wire})

    def last_burst(raws):
        found = None
        for text in raws:
            if burst_marker not in text:
                continue
            obj = decode(text)
            if isinstance(obj, dict) and obj.get("type") == "telemetry":
                found = obj
        return found

    py_last, rs_last = last_burst(py_raws), last_burst(rs_raws)
    py_seq = py_last.get("data", {}).get("seq") if py_last else None
    rs_seq = rs_last.get("data", {}).get("seq") if rs_last else None
    # Diagnostic only (not a hard assertion): a burst split across two 100ms
    # beats may legitimately produce zero coalescing on one stack, so asserting
    # a counter delta would be flaky. The newest-value invariant is the contract.
    coalesce_detail = {
        "py_last_seq": py_seq, "rs_last_seq": rs_seq,
        "last_frame_equal": py_last is not None and py_last == rs_last,
        "coalesced_before": {"py": before_py, "rs": before_rs},
        "coalesced_after": {"py": after_py, "rs": after_rs},
    }
    coalesce_ok = py_seq == rs_seq == 4 and coalesce_detail["last_frame_equal"]
    report["checks"].append({"name": "keep_latest_coalescing", "pass": coalesce_ok, "detail": coalesce_detail})

    passed = sum(1 for c in report["checks"] if c["pass"])
    total = len(report["checks"])
    report["summary"] = f"{passed}/{total} checks passed"
    print(json.dumps(report, indent=2, ensure_ascii=False))
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    asyncio.run(main())
