#!/bin/sh
# 契约门禁执行脚本（订单 18 T2）。
#
# 刻意用 `sh` 而不是 bash：CNB 的 script 本身就由 sh(dash) 执行
# （实测 `set -euo pipefail` 会报 `sh: 1: set: Illegal option -o pipefail`），
# 把逻辑放进 .sh 并显式用 sh 跑，可避免"本地 bash 能过、CI 挂掉"的错配。
# 也因此**只用 POSIX 语法**，不用 pipefail / [[ ]] / $'...'。
set -eu

echo "== 0) 环境自检 =="
node --version
python3 --version
python3 -m pip --version

echo
echo "== 1) 契约快照漂移 + breaking 检测（T3）=="
# --check 语义：非 breaking（加可选字段/改描述）只告警并 exit 0；
# breaking（删字段/改类型/删路径）exit 1。
python3 scripts/export-openapi.py --check docs/contracts/openapi.snapshot.json

echo
echo "== 2) 生成类型必须与入库产物一致（T1/T2 核心门禁）=="
# 前端依赖已在镜像构建期**全局安装**（见 ci/contract-gate.Dockerfile），
# 这里不再软链 node_modules —— 曾用 `ln -s /tmp/fe/node_modules ...` 省一次 npm ci，
# 真机失败（构建 cnb-pm7-1k4k4emnd）：.bin/ 里的 shim 是按**自身真实路径**相对回跳
# `../esbuild/bin/esbuild` 的，跨 /tmp 与 workspace 两个挂载点后相对路径失效。
cd frontend_dashboard
npm run gen:types
cd ..
git diff --exit-code -- frontend_dashboard/src/types/api.gen.ts

echo
echo "== 3) WS 信封 schema 的 ajv 校验（T4）=="
cd frontend_dashboard
# ⚠️ glob 必须**不加引号**：引号会让它不被 shell 展开、原样传给 node，
#   而 **Node 20 的 test runner 不展开 glob**（Node 22+ 才支持），
#   它把 `tests/*.test.mjs` 当字面路径交给 statSync：
#     lib/internal/test_runner/runner.js:136  resolve(process.argv[1])
#     lib/internal/test_runner/runner.js:92   statSync(path) → ENOENT
#     lib/internal/test_runner/runner.js:142  console.error(`Could not find '${err.path}'`)
#   真机报错原文：`Could not find '/workspace/frontend_dashboard/tests/*.test.mjs'`
#   （构建 cnb-pm7-1k4k4emnd）。本机 node 24 能过是因为 24 会展开 glob ——
#   典型「本地绿、CI 红」，别再把引号加回来。
node --test tests/*.test.mjs
cd ..

echo
echo "== 4) 类型检查（tsc --noEmit，0 错误才放行）=="
cd frontend_dashboard
npx tsc --noEmit
cd ..

echo
echo "== 契约门禁全部通过 =="
