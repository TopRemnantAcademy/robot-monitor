#!/usr/bin/env bash
# G-7 补充实证：给 AgentVerse 一个"能用的本地 LLM"（OpenAI 兼容假服务），
# 看它是否就能产出遥测 —— 排除"只是因为没 API key 所以失败"的质疑。
#
# 结论（实测）：即便 LLM 可用，agent.step() 返回的仍是聊天文本 Message，
# 不含任何 mock_fleet 遥测字段。AgentVerse 与遥测契约是不同层。
set -uo pipefail
AV_ROOT="${AV_ROOT:-/tmp/agentverse_probe}"
[ -d "$AV_ROOT/agentverse" ] || { echo "[SKIP] 未找到 $AV_ROOT/agentverse"; exit 2; }

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
echo "=== 本地 OpenAI 兼容假服务已起 (127.0.0.1:8899) ==="

cd "$AV_ROOT"
OPENAI_API_KEY=sk-local-fake OPENAI_BASE_URL=http://127.0.0.1:8899/v1 python3 - <<'PY'
import sys, asyncio, json, os
sys.path.insert(0, os.getcwd())
import logging, io, contextlib
for n in ("agentverse", "openai", "httpx"): logging.getLogger(n).setLevel(logging.CRITICAL)
# AgentVerse 会把完整 prompt 直接 print 到 stdout，这里重定向掉以保持探针输出可读
_buf = io.StringIO()
from agentverse.initialization import prepare_task_config, load_agent, load_environment

cfg = prepare_task_config("nlp_classroom_3players", "agentverse/tasks/simulation")
a = load_agent(dict(cfg["agents"][0]))
with contextlib.redirect_stdout(_buf):
    m = asyncio.run(a.astep("env desc"))
print(f"[单 agent] astep 返回 content={m.content!r}")
print(f"[单 agent] 语义槽 Action/Action Input 是 LLM 约定，不是遥测数值")

N = 100
agents = [load_agent({**cfg["agents"][0], "name": f"humanoid-{i+1:03d}"}) for i in range(N)]
env = load_environment({**cfg["environment"], "agents": agents})
env.reset()
with contextlib.redirect_stdout(_buf):
    msg = asyncio.run(env.step())
print(f"[100 agent] 环境单步返回 {len(msg) if isinstance(msg, list) else 1} 条 Message（文本对话）")
TELE = {"robot_id","status_code","error_code","battery_pct","battery_temp","max_joint_temp",
        "joint_temperatures","joint_positions","joint_torques","ts"}
got = json.loads(json.dumps(msg[0].dict(), default=str)) if isinstance(msg, list) and msg else {}
hit = TELE & set(got.keys())
print(f"[100 agent] 命中遥测契约字段: {sorted(hit) if hit else '无'}")
print("[结论] LLM 可用时 AgentVerse 仍只产文本 Message；遥测需另一层从状态派生。")
PY
RC=$?
kill $FAKE_PID 2>/dev/null
exit $RC
