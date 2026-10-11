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
# 前端依赖已在镜像构建期 `npm ci` 到 /tmp/fe；这里把工作区源码接上那个 node_modules，
# 避免运行时重复 npm ci（也避免在只读/网络受限时失败）。
if [ -d /tmp/fe/node_modules ]; then
  rm -rf frontend_dashboard/node_modules
  ln -s /tmp/fe/node_modules frontend_dashboard/node_modules
fi
cd frontend_dashboard
npm run gen:types
cd ..
git diff --exit-code -- frontend_dashboard/src/types/api.gen.ts

echo
echo "== 3) WS 信封 schema 的 ajv 校验（T4）=="
cd frontend_dashboard
node --test "tests/*.test.mjs"
cd ..

echo
echo "== 4) 类型检查（tsc --noEmit，0 错误才放行）=="
cd frontend_dashboard
npx tsc --noEmit
cd ..

echo
echo "== 契约门禁全部通过 =="
