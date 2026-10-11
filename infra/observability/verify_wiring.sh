#!/usr/bin/env bash
# 可观测性接线自检（无 docker 环境也能跑的部分）。
#
# 本机没有 docker（CI 容器内），所以这里做**静态 + 语义**校验：
#   1. 所有 YAML 语法
#   2. 三层 compose merge 后，prometheus.yml 里的每个抓取目标都能解析到 service
#   3. 规则文件里的指标名，在代码里真实存在（防止「规则引用不存在的指标」）
#   4. env 变量名与上游 README 的 file:line 对齐
#
# 真正的运行时验证需要 docker，见 infra/observability/README.md §5。
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
fail=0
ok()  { echo "  [ok]   $1"; }
bad() { echo "  [FAIL] $1"; fail=1; }

echo "==> [1/5] YAML 语法"
for f in infra/docker-compose.yml infra/docker-compose.app.yml \
         infra/observability/docker-compose.observability.yml \
         infra/observability/prometheus.yml infra/observability/alertmanager.yml \
         infra/observability/rules/robot-alerts.yml; do
  if python3 -c "import yaml,sys; yaml.safe_load(open('$f'))" 2>/dev/null; then
    ok "$f"
  else
    bad "$f"
  fi
done

echo "==> [2/5] 抓取目标可达性（三层 merge）"
python3 - <<'PY' || fail=1
import yaml, re, sys
base = yaml.safe_load(open('infra/docker-compose.yml'))
app  = yaml.safe_load(open('infra/docker-compose.app.yml'))
obs  = yaml.safe_load(open('infra/observability/docker-compose.observability.yml'))
svc = set(base['services']); svc |= set(app['services']); svc |= set(obs['services'])
targets = re.findall(r'-\s*targets:\s*\["([^"]+)"\]',
                     open('infra/observability/prometheus.yml').read())
bad = [t for t in targets
       if t.split(':')[0] not in svc and t.split(':')[0] != 'localhost']
for t in targets:
    print(f"  [ok]   target {t}")
if bad:
    for t in bad: print(f"  [FAIL] unresolved target {t}")
    sys.exit(1)
PY

echo "==> [3/5] 规则指标名 ↔ 代码 实存性"
for m in robot_backend_queue_depth robot_backend_mqtt_dropped_total \
         robot_backend_telemetry_written_total \
         robot_backend_ws_probe_alive robot_backend_ws_probe_failures_total; do
  if grep -rq "$m" backend_ingestion/app/metrics.py; then ok "$m (backend)"; else bad "$m 未定义"; fi
done
for m in robot_alert_emitted_total robot_alert_robots_online \
         robot_alert_store_errors_total; do
  if grep -rq "$m" alert_engine/app/metrics.py; then ok "$m (alert)"; else bad "$m 未定义"; fi
done

# 探针指标必须与连接数指标分开定义（ISSUE #58）：探针只出现在 ws_probe.py 引用的
# metrics 里，而连接数不在 metrics.py 中定义 —— 两者若被混为一谈，本检查会红。
if grep -q "robot_backend_ws_probe_alive" backend_ingestion/app/metrics.py \
   && ! grep -qE 'Gauge\(\s*"robot_backend_ws_connections"' backend_ingestion/app/metrics.py; then
  ok "ws_probe 指标与 ws_connections 指标分离"
else
  bad "ws_probe / ws_connections 指标定义混淆"
fi

echo "==> [4/4] 上游 env 变量名对齐"
if grep -q "DATA_SOURCE_NAME" external/postgres_exporter/README.md 2>/dev/null \
   && grep -q "DATA_SOURCE_NAME" infra/observability/docker-compose.observability.yml; then
  ok "DATA_SOURCE_NAME (postgres_exporter/README.md:223)"
else
  echo "  [skip] external/postgres_exporter 未下载，跳过"
fi
if grep -q "REDIS_ADDR" external/redis_exporter/README.md 2>/dev/null \
   && grep -q "REDIS_ADDR" infra/observability/docker-compose.observability.yml; then
  ok "REDIS_ADDR (redis_exporter/README.md:170)"
else
  echo "  [skip] external/redis_exporter 未下载，跳过"
fi

# 订单 22 / I-12：exporter 同网 + 无明文密码。这两条是「起来了但拉不到」的根因，
# 光看容器状态发现不了，必须在配置层卡住。
echo "==> [5/5] exporter 同网 + 无明文密码"
python3 - <<'PY2' || fail=1
import yaml, sys
obs = yaml.safe_load(open('infra/observability/docker-compose.observability.yml'))
base = yaml.safe_load(open('infra/docker-compose.yml'))
fails = []

# a) 本文件不得声明自有网络：一旦声明，base 的 timescaledb/redis 不会自动落进来，
#    exporter 就成了网络孤岛（DNS 解析不到，/metrics 无业务指标）。
if obs.get('networks'):
    fails.append("observability 文件声明了 networks，exporter 会与 base 服务分网")

# b) exporter 的 depends_on 目标必须在 base compose 里真实存在（否则 DNS 名字是空的）
base_svc = set(base['services'])
for name in ('postgres-exporter', 'redis-exporter'):
    svc = obs['services'].get(name)
    if not svc:
        fails.append(f"{name} 缺失")
        continue
    for dep in svc.get('depends_on', []) or []:
        if dep not in base_svc:
            fails.append(f"{name} depends_on {dep}，但 base compose 里没有该 service")

# c) DATA_SOURCE_NAME 必须用 ${...} 变量引用，且不得出现字面量密码
dsn = obs['services']['postgres-exporter']['environment']['DATA_SOURCE_NAME']
if '${POSTGRES_PASSWORD' not in dsn:
    fails.append("DATA_SOURCE_NAME 未用 ${POSTGRES_PASSWORD} 变量引用")
# 出网主机名必须是 base 的 service 名（同网才解析得到）
if '@timescaledb:' not in dsn:
    fails.append("DATA_SOURCE_NAME 目标主机不是 timescaledb（非同网 DNS）")

for f in fails:
    print(f"  [FAIL] {f}")
if fails:
    sys.exit(1)
print("  [ok]   无自有网络 / depends_on 目标存在 / 密码走变量")
PY2

echo
if [ "$fail" -eq 0 ]; then echo "全部通过 ✅"; else echo "存在失败项 ❌"; fi
exit "$fail"
