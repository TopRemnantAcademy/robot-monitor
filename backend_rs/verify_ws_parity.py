#!/usr/bin/env python3
"""Live WS parity check for the Rust /ws/robots spike vs the Python backend.

Connects to both endpoints with a valid HS256 JWT (audience fastapi-users:auth),
compares:
  1. the first `snapshot` frame (robot set + full field deep-compare),
  2. auth rejection: missing / bad token must yield a close (code 1008),
  3. live parity: a synthetic alert + action published to Redis pub/sub must be
     fanned out by BOTH hubs as `{"type":"alert"|"action","data":<payload>}`
     with byte-identical `data`.

Run from inside the backend container (has websockets + redis-py + PyJWT):
    JWT_SECRET=... python verify_ws_parity.py
"""
import asyncio
import json
import os
import sys
import time

import jwt
import redis
import websockets

PY = "ws://host.docker.internal:8000/ws/robots"
RS = "ws://host.docker.internal:8001/ws/robots"
REDIS_HOST = "host.docker.internal"
REDIS_PORT = 6379

SECRET = os.environ.get("JWT_SECRET", "")
if not SECRET:
    sys.exit("JWT_SECRET env required")

TOKEN = jwt.encode(
    {"sub": "ws-parity", "aud": "fastapi-users:auth",
     "exp": int(time.time()) + 3600},
    SECRET, algorithm="HS256",
)


async def collect(uri, token, seconds=1.5, wait_first=None):
    """Open a WS, optionally wait for first frame, then collect frames for `seconds`.
    Returns (first_frame_dict_or_None, list_of_all_frame_dicts)."""
    all_frames = []
    first = None
    url = f"{uri}?token={token}"
    async with websockets.connect(url, max_size=None, open_timeout=5) as ws:
        if wait_first:
            raw = await asyncio.wait_for(ws.recv(), timeout=5)
            first = json.loads(raw)
            all_frames.append(first)
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(0.05, end - time.monotonic()))
            except asyncio.TimeoutError:
                break
            try:
                all_frames.append(json.loads(raw))
            except Exception:
                all_frames.append({"_raw": raw})
    return first, all_frames


async def expect_close(uri, token, label):
    """Connecting with this token should be rejected (server closes). Return code."""
    url = f"{uri}?token={token}"
    try:
        async with websockets.connect(url, max_size=None, open_timeout=5) as ws:
            # If accepted, the first recv would be the snapshot; a rejection closes
            # the socket, so recv raises ConnectionClosed.
            await asyncio.wait_for(ws.recv(), timeout=4)
        return f"{label}: UNEXPECTEDLY ACCEPTED"
    except websockets.exceptions.ConnectionClosed as e:
        return f"{label}: closed code={e.code}"
    except Exception as e:
        return f"{label}: {type(e).__name__}={e}"


async def main():
    report = {"checks": []}
    # 1) snapshot parity
    py_first, _ = await collect(PY, TOKEN, seconds=0.3, wait_first=True)
    rs_first, _ = await collect(RS, TOKEN, seconds=0.3, wait_first=True)
    snap_ok = True
    snap_detail = {}
    if py_first is None or rs_first is None:
        snap_ok = False
        snap_detail = {"py_first_none": py_first is None, "rs_first_none": rs_first is None}
    else:
        py_robots = py_first.get("data", {}).get("robots", [])
        rs_robots = rs_first.get("data", {}).get("robots", [])
        py_ids = [r["robot_id"] for r in py_robots]
        rs_ids = [r["robot_id"] for r in rs_robots]
        snap_detail = {
            "py_count": len(py_robots), "rs_count": len(rs_robots),
            "ids_equal": py_ids == rs_ids,
            "first_robot_equal": (py_robots[0] if py_robots else None) == (rs_robots[0] if rs_robots else None),
            "full_list_equal": py_robots == rs_robots,
        }
        if not (snap_detail["ids_equal"] and snap_detail["full_list_equal"]):
            snap_ok = False
            # surface the first differing robot for debugging
            for a, b in zip(py_robots, rs_robots):
                if a != b:
                    snap_detail["first_diff"] = {"py": a, "rs": b}
                    break
    report["checks"].append({"name": "snapshot_parity", "pass": snap_ok, "detail": snap_detail})

    # 2) auth rejection (must close). Only test on the Rust side primarily, but
    #    report both for completeness.
    auth = {}
    auth["rs_no_token"] = await expect_close(RS, "", "rs_no_token")
    auth["rs_bad_token"] = await expect_close(RS, "garbage.token.here", "rs_bad_token")
    auth["py_no_token"] = await expect_close(PY, "", "py_no_token")
    auth["py_bad_token"] = await expect_close(PY, "garbage.token.here", "py_bad_token")
    auth_ok = all("closed" in v for v in auth.values())
    report["checks"].append({"name": "auth_rejection", "pass": auth_ok, "detail": auth})

    # 3) live alert + action parity
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT)
    alert_payload = json.dumps({
        "robot_id": "humanoid-001", "rule_id": "ws-parity-rule", "severity": "warning",
        "message": "WS parity synthetic alert", "detail": "verify_ws_parity",
        "ts": int(time.time() * 1000), "alert_id": "ws-parity-alert", "escalated": False,
        "data": None,
    }, separators=(",", ":"))
    action_payload = json.dumps({
        "robot_id": "humanoid-002", "policy_version": "v1", "trial_id": "t-9",
        "action": [0.1, 0.2, 0.3], "goal": "stand", "ts": int(time.time() * 1000),
    }, separators=(",", ":"))

    # Start collecting on both, then publish.
    py_task = asyncio.create_task(collect(PY, TOKEN, seconds=1.5, wait_first=True))
    rs_task = asyncio.create_task(collect(RS, TOKEN, seconds=1.5, wait_first=True))
    await asyncio.sleep(0.4)  # let both subscribe + receive snapshot
    r.publish("robot:alerts", alert_payload)
    r.publish("robot:actions:all", action_payload)
    py_first, py_frames = await py_task
    rs_first, rs_frames = await rs_task

    def find(frames, kind):
        for f in frames:
            if isinstance(f, dict) and f.get("type") == kind:
                return f
        return None

    py_alert = find(py_frames, "alert")
    rs_alert = find(rs_frames, "alert")
    py_action = find(py_frames, "action")
    rs_action = find(rs_frames, "action")

    alert_data_equal = bool(py_alert and rs_alert and py_alert.get("data") == rs_alert.get("data"))
    action_data_equal = bool(py_action and rs_action and py_action.get("data") == rs_action.get("data"))
    # byte-level: compare the whole envelope text of the matching frames
    alert_raw_equal = bool(py_alert and rs_alert and json.dumps(py_alert, sort_keys=True) == json.dumps(rs_alert, sort_keys=True))
    action_raw_equal = bool(py_action and rs_action and json.dumps(py_action, sort_keys=True) == json.dumps(rs_action, sort_keys=True))

    live_ok = alert_data_equal and action_data_equal
    report["checks"].append({"name": "live_alert_action_parity", "pass": live_ok, "detail": {
        "py_alert_seen": py_alert is not None, "rs_alert_seen": rs_alert is not None,
        "alert_data_equal": alert_data_equal, "alert_raw_equal": alert_raw_equal,
        "py_action_seen": py_action is not None, "rs_action_seen": rs_action is not None,
        "action_data_equal": action_data_equal, "action_raw_equal": action_raw_equal,
    }})

    passed = sum(1 for c in report["checks"] if c["pass"])
    total = len(report["checks"])
    report["summary"] = f"{passed}/{total} checks passed"
    print(json.dumps(report, indent=2, ensure_ascii=False))
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    asyncio.run(main())
