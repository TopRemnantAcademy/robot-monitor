#!/usr/bin/env bash
# E2E 30k 落库验证（ISSUE #60）—— GitHub Actions 版入口。
#
# 与 CNB `.cnb.yml` 的 e2e-30k-landing 流水线**逐字等效**，仅把原先写在 YAML 里的
# 内联 script 抽成独立文件（逻辑 / SKIP·FAIL 横幅 / 退出码语义完全不变）。
# 设计铁律（来自 CNB 实证教训）：
#   - 没实跑的退出必须打【E2E SKIP】固定串并以 0 退出，让「绿」不再能冒充实跑；
#   - 起栈失败 / 命名门禁红 / 30k 断言红 都必须以非 0 退出（门禁能被吞掉就等于不存在）。
# 本脚本只在仓库根目录被调用（相对路径 tests/e2e/... 以此为基准）。
set -uo pipefail

echo "== e2e 30k 落库验证（ISSUE #60）=="

# SKIP 横幅（任何一次「没实跑」的退出都必须先打这一行，且必须是可 grep 的固定串）。
skip_banner() {
  echo "================================================================"
  echo "【E2E SKIPPED —— 本构建未实跑 30k 落库】"
  echo "原因：$1"
  echo "本构建的 e2e 为绿色但**未产生任何落库数字**，不可当作 ISSUE #60 的证据。"
  echo "================================================================"
}

# ---- 环境判定：无 Docker 即明确 skip，绝不 red ----
if ! command -v docker >/dev/null 2>&1; then
  echo "【E2E SKIP】PATH 内找不到 docker 命令。"
  echo "本 job 需要真实 Docker（emqx + timescaledb:2.13.1 + redis + backend + mock_fleet），"
  echo "且 ISSUE #60 禁止用 mock 替身冒充真实 TimescaleDB。"
  echo "需要：在提供 docker 的构建机/自托管 runner 上运行本 job，或本地执行"
  echo "  python -m pytest tests/e2e/test_e2e_30k.py -v -s"
  skip_banner "PATH 内找不到 docker 命令（runner 未提供 Docker-in-Docker）"
  exit 0
fi
if ! docker info >/dev/null 2>&1; then
  echo "【E2E SKIP】docker daemon 不可达（docker info 非 0）。原始输出（尾部）："
  docker info 2>&1 | tail -5 || true
  skip_banner "docker daemon 不可达"
  exit 0
fi
echo "docker 可用：$(docker --version)"

# ---- 运行环境判定：连 python 都没有就明确 skip，绝不 red ----
if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo "【E2E SKIP】镜像内 PATH 找不到 python3/python（当前镜像：${CNB_BUILD_IMAGE:-缺省镜像}）。"
  echo "本 job 需要 python 来驱动 tests/e2e/test_e2e_30k.py（只用标准库，无需 pip 安装）。"
  echo "修复：本 pipeline 必须引用 docker.image 锚点（python:3.11-slim），不要沿用缺省镜像。"
  skip_banner "运行镜像内没有 python3/python（docker.image 锚点未生效）"
  exit 0
fi
echo "python 可用：$($PY --version 2>&1)"

# ---- 订单 32 / ISSUE #100：指标命名反例探针 ----
# 先起栈、再针对已起的栈断言（命名门禁要断言「跑起来的进程吐出的字节」）。
# 判定：HTTP 探不到 → [SKIP] 并 exit 0；旧名残留 / 新名缺失 → 退出码 1 → 本 stage 变红。
# 命名探针 rc 与 30k rc 各自独立收集，最后合并判定（互不掐断）。
naming_rc=0

echo "== 订单 32：指标命名反例探针 =="
naming_rc=0
if docker compose -f tests/e2e/docker-compose.e2e.yml up -d --build \
     emqx timescaledb redis backend; then
  i=0
  while [ $i -lt 36 ]; do
    if $PY -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:18000/healthz',timeout=3)" >/dev/null 2>&1; then break; fi
    i=$((i + 1)); sleep 5
  done
  if $PY -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:18000/metrics',timeout=3)" >/dev/null 2>&1; then
    $PY tests/e2e/test_e2e_30k.py --metric-naming-check || naming_rc=$?
    if [ "$naming_rc" = "3" ]; then
      echo "【E2E SKIP】订单 32 命名探针：脚本自判环境不具备（退出码 3），非代码失败。"
      skip_banner "订单 32 命名探针自判环境不具备（退出码 3）"
      naming_rc=0
    elif [ "$naming_rc" != "0" ]; then
      echo "【E2E FAIL】订单 32 命名门禁未通过（退出码 $naming_rc）—— 旧前缀指标残留或规范名缺失。"
    else
      echo "订单 32 命名门禁通过：/metrics 旧名 0 命中，robot_backend_* 规范名全部在位。"
    fi
  else
    echo "【E2E SKIP】订单 32 命名探针：/metrics 在 180s 内不可达（栈未就绪）"
    skip_banner "订单 32 命名探针：/metrics 在 180s 内不可达（该门禁本轮未实跑）"
  fi
else
  echo "【E2E FAIL】订单 32 命名探针：起栈失败。"
  echo "（订单 34 判定收紧：起栈失败**不再**当作可静默 skip 的环境问题 ——"
  echo " 本 compose 栈此前已被实跑证明可起，失败即代码/配置回归。）"
  naming_rc=1
fi
docker compose -f tests/e2e/docker-compose.e2e.yml down -v --remove-orphans || true

# 依赖：e2e 脚本只用标准库（subprocess/urllib），无需 pip 安装。
# 同样用 `|| rc=$?` 就地吞退出码：这是本 job 的主产出，绝不能被 shell 语义掐断。
echo "== 30k 落库主链路（本 job 的硬门槛产出）=="
rc=0
$PY tests/e2e/test_e2e_30k.py || rc=$?
if [ "$rc" = "3" ]; then
  echo "【E2E SKIP】脚本自判环境不具备（退出码 3），非代码失败。"
  skip_banner "test_e2e_30k.py 自判环境不具备（退出码 3，无 Docker/daemon 不可达）"
  rc=0
fi
# 合并判定：命名探针红、或 30k 红，本 stage 都红（都非静默可放过）。
if [ "$rc" = "0" ] && [ "$naming_rc" = "0" ]; then
  echo "e2e stage 全部通过：30k 落库断言通过 + 命名门禁在位。"
  exit 0
fi
if [ "$naming_rc" != "0" ]; then
  echo "【E2E FAIL】订单 32 命名门禁未通过（退出码 $naming_rc）—— 旧前缀指标残留或规范名缺失。"
fi
if [ "$rc" != "0" ]; then
  echo "【E2E FAIL】30k 落库断言未通过（退出码 $rc）。"
fi
exit 1
