#!/usr/bin/env bash
# 一键跑全部前端集成验证。前置：npm install
set -uo pipefail
cd "$(dirname "$0")/.."
FAIL=0

echo "═══ 1/5 build（tsc --noEmit + vite build）═══"
npm run build >/dev/null 2>&1 && echo "  ✅ RC=0" || { echo "  ❌ build 失败"; FAIL=1; }

echo "═══ 2/5 WS 洪流 + 三分支 + 100ms 节流 ═══"
E2E_WS_PORT=8123 node .e2e/build.mjs >/dev/null 2>&1
node .e2e/run.mjs >/tmp/t1.log 2>&1 && echo "  ✅ 通过" || { echo "  ❌ 失败"; tail -20 /tmp/t1.log; FAIL=1; }

echo "═══ 3/5 指数退避重连 ═══"
E2E_WS_PORT=8124 node .e2e/build.mjs >/dev/null 2>&1
node .e2e/run_reconnect.mjs >/tmp/t2.log 2>&1 && echo "  ✅ 通过" || { echo "  ❌ 失败"; tail -20 /tmp/t2.log; FAIL=1; }

echo "═══ 4/5 Drawer REST → ECharts ═══"
E2E_API_BASE=http://localhost:8201 node .e2e/build.mjs >/dev/null 2>&1
node .e2e/run_drawer.mjs >/tmp/t3.log 2>&1 && echo "  ✅ 通过" || { echo "  ❌ 失败"; tail -20 /tmp/t3.log; FAIL=1; }

echo "═══ 5/5 无数据 → 100 张离线占位卡 ═══"
(node .e2e/run_offline.mjs >/tmp/t4.log 2>&1) && echo "  ✅ 通过" || { echo "  ❌ 失败"; tail -20 /tmp/t4.log; FAIL=1; }

echo
[ "$FAIL" = 0 ] && echo "全部通过 ✅" || echo "存在失败 ❌"
exit "$FAIL"
