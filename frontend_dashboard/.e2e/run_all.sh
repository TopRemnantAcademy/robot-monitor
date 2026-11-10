#!/usr/bin/env bash
# Run all frontend integration checks in one shot. Prerequisite: npm install
set -uo pipefail
cd "$(dirname "$0")/.."
FAIL=0

echo "═══ 1/7 build (tsc --noEmit + vite build) ═══"
npm run build >/dev/null 2>&1 && echo "  ✅ RC=0" || { echo "  ❌ build failed"; FAIL=1; }

echo "═══ 2/7 WS flood + three branches + 100ms throttle ═══"
E2E_WS_PORT=8123 node .e2e/build.mjs >/dev/null 2>&1
node .e2e/run.mjs >/tmp/t1.log 2>&1 && echo "  ✅ passed" || { echo "  ❌ failed"; tail -20 /tmp/t1.log; FAIL=1; }

echo "═══ 3/7 exponential-backoff reconnect ═══"
E2E_WS_PORT=8124 node .e2e/build.mjs >/dev/null 2>&1
node .e2e/run_reconnect.mjs >/tmp/t2.log 2>&1 && echo "  ✅ passed" || { echo "  ❌ failed"; tail -20 /tmp/t2.log; FAIL=1; }

echo "═══ 4/7 Drawer REST to ECharts ═══"
E2E_API_BASE=http://localhost:8201 node .e2e/build.mjs >/dev/null 2>&1
node .e2e/run_drawer.mjs >/tmp/t3.log 2>&1 && echo "  ✅ passed" || { echo "  ❌ failed"; tail -20 /tmp/t3.log; FAIL=1; }

echo "═══ 5/7 alert-row incremental rendering (memo works, no full-table rebuild) ═══"
node .e2e/run_alert_rows.mjs >/tmp/t4.log 2>&1 && echo "  ✅ passed" || { echo "  ❌ failed"; tail -20 /tmp/t4.log; FAIL=1; }

echo "═══ 6/7 Order 26: error boundary + react-query cache/retry ═══"
E2E_API_BASE=http://localhost:8202 node .e2e/build.mjs >/dev/null 2>&1
node .e2e/run_order26.mjs >/tmp/t6.log 2>&1 && echo "  ✅ passed" || { echo "  ❌ failed"; tail -20 /tmp/t6.log; FAIL=1; }

echo "═══ 7/7 no data to 100 offline placeholder cards ═══"
# Must start the preview server first (to preview the built output)
(npx vite preview --port 4173 >/tmp/preview.log 2>&1 & echo $! >/tmp/preview.pid)
for _ in $(seq 1 30); do curl -sf -o /dev/null http://127.0.0.1:4173/ && break; sleep 1; done
(node .e2e/run_offline.mjs >/tmp/t5.log 2>&1) && echo "  ✅ passed" || { echo "  ❌ failed"; tail -20 /tmp/t5.log; FAIL=1; }
kill "$(cat /tmp/preview.pid)" 2>/dev/null || true

echo
[ "$FAIL" = 0 ] && echo "All passed ✅" || echo "Some failed ❌"
exit "$FAIL"
