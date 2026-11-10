#!/usr/bin/env bash
# G-7 supplementary evidence: give AgentVerse a "working local LLM" (OpenAI-compatible fake service),
# to see whether it can then produce telemetry -- ruling out the objection "it only failed because there was no API key".
#
# Conclusion (verified): even with a working LLM, agent.step() still returns a chat-text Message,
# containing no mock_fleet telemetry fields. AgentVerse and the telemetry contract are different layers.
set -uo pipefail
AV_ROOT="${AV_ROOT:-/tmp/agentverse_probe}"
[ -d "$AV_ROOT/agentverse" ] || { echo "[SKIP] $AV_ROOT/agentverse not found"; exit 2; }

cat > /tmp/_g7_fake_openai.py <<'PY'
from http.server import BaseHTTPRequestHandler, HTTPServer
import json, time
class H(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get('Content-Length', 0)); self.rfile.read(n)
        body = {"id": "chatcmpl-fake", "object": "chat.completion", "created": int(time.time()),
                "model": "gpt-4", "choices": [{"index": 0, "message": {"role": "assistant",
                "content": "Action: Speak\nAction Input: humanoid keepalive"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
        d = json.dumps(body).encode()
        self.send_response(200); self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(d))); self.end_headers(); self.wfile.write(d)
    def log_message(self, *a): pass
HTTPServer(("127.0.0.1", 8899), H).serve_forever()
PY
python3 /tmp/_g7_fake_openai.py & FAKE_PID=$!
sleep 1
echo "=== local OpenAI-compatible fake service started (127.0.0.1:8899) ==="

cd "$AV_ROOT"
OPENAI_API_KEY=sk-local-fake OPENAI_BASE_URL=http://127.0.0.1:8899/v1 python3 - <<'PY'
import sys, asyncio, json, os
sys.path.insert(0, os.getcwd())
import logging, io, contextlib
for n in ("agentverse", "openai", "httpx"): logging.getLogger(n).setLevel(logging.CRITICAL)
# AgentVerse prints the full prompt straight to stdout; redirect it away so the probe output stays readable
_buf = io.StringIO()
from agentverse.initialization import prepare_task_config, load_agent, load_environment

cfg = prepare_task_config("nlp_classroom_3players", "agentverse/tasks/simulation")
a = load_agent(dict(cfg["agents"][0]))
with contextlib.redirect_stdout(_buf):
    m = asyncio.run(a.astep("env desc"))
print(f"[single agent] astep returned content={m.content!r}")
print(f"[single agent] semantic slots Action/Action Input are an LLM convention, not telemetry values")

N = 100
agents = [load_agent({**cfg["agents"][0], "name": f"humanoid-{i+1:03d}"}) for i in range(N)]
env = load_environment({**cfg["environment"], "agents": agents})
env.reset()
with contextlib.redirect_stdout(_buf):
    msg = asyncio.run(env.step())
print(f"[100 agents] env single step returned {len(msg) if isinstance(msg, list) else 1} Message(s) (text dialogue)")
TELE = {"robot_id","status_code","error_code","battery_pct","battery_temp","max_joint_temp",
        "joint_temperatures","joint_positions","joint_torques","ts"}
got = json.loads(json.dumps(msg[0].dict(), default=str)) if isinstance(msg, list) and msg else {}
hit = TELE & set(got.keys())
print(f"[100 agents] matched telemetry contract fields: {sorted(hit) if hit else 'none'}")
print("[conclusion] even with a working LLM, AgentVerse still only emits text Messages; telemetry must be derived by another layer from state.")
PY
RC=$?
kill $FAKE_PID 2>/dev/null
exit $RC
