#!/usr/bin/env bash
# 一键跑全部前端集成验证。前置：npm install
set -uo pipefail
cd "$(dirname "$0")/.."
FAIL=0

echo "═══ 1/7 build（tsc --noEmit + vite build）═══"
npm run build >/dev/null 2>&1 && echo "  ✅ RC=0" || { echo "  ❌ build 失败"; FAIL=1; }

echo "═══ 2/7 WS 洪流 + 三分支 + 100ms 节流 ═══"
E2E_WS_PORT=8123 node .e2e/build.mjs >/dev/null 2>&1
node .e2e/run.mjs >/tmp/t1.log 2>&1 && echo "  ✅ 通过" || { echo "  ❌ 失败"; tail -20 /tmp/t1.log; FAIL=1; }

echo "═══ 3/7 指数退避重连 ═══"
E2E_WS_PORT=8124 node .e2e/build.mjs >/dev/null 2>&1
node .e2e/run_reconnect.mjs >/tmp/t2.log 2>&1 && echo "  ✅ 通过" || { echo "  ❌ 失败"; tail -20 /tmp/t2.log; FAIL=1; }

echo "═══ 4/7 Drawer REST → ECharts ═══"
E2E_API_BASE=http://localhost:8201 node .e2e/build.mjs >/dev/null 2>&1
node .e2e/run_drawer.mjs >/tmp/t3.log 2>&1 && echo "  ✅ 通过" || { echo "  ❌ 失败"; tail -20 /tmp/t3.log; FAIL=1; }

echo "═══ 5/7 告警行增量渲染（memo 生效，不整表重建）═══"
node .e2e/run_alert_rows.mjs >/tmp/t4.log 2>&1 && echo "  ✅ 通过" || { echo "  ❌ 失败"; tail -20 /tmp/t4.log; FAIL=1; }

echo "═══ 6/7 订单26：错误边界 + react-query 缓存/重试 ═══"
E2E_API_BASE=http://localhost:8202 node .e2e/build.mjs >/dev/null 2>&1
node .e2e/run_order26.mjs >/tmp/t6.log 2>&1 && echo "  ✅ 通过" || { echo "  ❌ 失败"; tail -20 /tmp/t6.log; FAIL=1; }

echo "═══ 7/7 无数据 → 100 张离线占位卡 ═══"
# 需先起 preview 服务（预览构建产物）
(npx vite preview --port 4173 >/tmp/preview.log 2>&1 & echo $! >/tmp/preview.pid)
for _ in $(seq 1 30); do curl -sf -o /dev/null http://127.0.0.1:4173/ && break; sleep 1; done
(node .e2e/run_offline.mjs >/tmp/t5.log 2>&1) && echo "  ✅ 通过" || { echo "  ❌ 失败"; tail -20 /tmp/t5.log; FAIL=1; }
kill "$(cat /tmp/preview.pid)" 2>/dev/null || true

echo
[ "$FAIL" = 0 ] && echo "全部通过 ✅" || echo "存在失败 ❌"
exit "$FAIL"
