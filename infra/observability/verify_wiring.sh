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
# ISSUE #72：唯一规范前缀是 robot_backend_*，且规则文件里**不得**残留 robot_monitor_*。
# 这里同时校验「被引用的量在 metrics.py 里有定义」与「规则没引用已废弃的前缀」。
for m in robot_backend_queue_depth robot_backend_mqtt_dropped_total \
         robot_backend_telemetry_written_total \
         robot_backend_ws_probe_alive robot_backend_ws_probe_failures_total; do
  if grep -rq "$m" backend_ingestion/app/metrics.py; then ok "$m (backend)"; else bad "$m 未定义"; fi
done
for m in robot_alert_emitted_total robot_alert_robots_online_gauge \
         robot_alert_store_errors_total; do
  if grep -rq "$m" alert_engine/app/metrics.py; then ok "$m (alert)"; else bad "$m 未定义"; fi
done

# 探针指标必须与连接数指标分开定义（ISSUE #58）：探针（客户端视角）与连接数
# （服务端视角）语义不同，不得互相覆盖。ISSUE #72 后连接数指标改名为
# robot_backend_ws_connections 且确实定义在 metrics.py 中。
# -z 是必要的：`Gauge(` 与指标名字面量跨两行（代码格式如此），逐行 grep 看不到。
# 这不是"为了过检查而改检查"——检查的意图是"连接数指标确实定义在 metrics.py 中"，
# 跨行匹配才是对该意图的正确表达。
if grep -q "robot_backend_ws_probe_alive" backend_ingestion/app/metrics.py \
   && grep -qzoE 'Gauge\(\s*"robot_backend_ws_connections"' backend_ingestion/app/metrics.py; then
  ok "ws_probe 指标与 ws_connections 指标分离（且连接数在 metrics.py 中）"
else
  bad "ws_probe / ws_connections 指标定义混淆"
fi

# ISSUE #72：双轨命名统一后，代码/规则/抓取配置里**不得**再注册/引用 robot_monitor_* 指标。
# 判定形态：Prometheus 指标名必然出现在**引号内**（`"robot_monitor_x"`）或规则表达式里。
# 注释里以反引号包裹的迁移记录（`` `robot_monitor_ws_connections` ``）不算引用 —— 那是
# 记录"旧名已作废"，正是希望保留的信息。故这里只匹配「双引号/单引号包裹」的形态。
dual=0
for f in backend_ingestion/app/metrics.py backend_ingestion/app/main.py \
         backend_ingestion/app/services/mqtt_subscriber.py \
         backend_ingestion/app/services/batch_writer.py \
         infra/observability/rules/robot-alerts.yml infra/observability/prometheus.yml; do
  if grep -qE '["'"'"']robot_monitor_[a-z_]+["'"'"']' "$f" \
     || grep -qE '^\s*(expr|-[[:space:]]+alert):.*robot_monitor_' "$f"; then
    bad "$f 仍引用 robot_monitor_*（ISSUE #72 已作废该前缀）"; dual=1
  fi
done
[ "$dual" -eq 0 ] && ok "无 robot_monitor_* 残留（代码/规则/抓取配置）"

# 订单 32 / ISSUE #100：把上面那条「无 robot_monitor_* 残留」从**6 个硬编码文件**
# 扩成**全仓扫描**，并同时卡住「改前存在的旧名是否真的消失」。
#
# 为什么要扩：原来的 for 列表是手工维护的 —— 新加一个文件引用了旧前缀，
# 巡检器**看不见**（列表外）。这正是 ISSUE #72 复发过的形态：三个回收分支各自
# 新增引用，谁都以为巡检覆盖到了。列表可扩，遗漏不可见。
#
# 判定形态与上面一致（引号内指标名 / 规则表达式），故迁移记录的反引号写法不算违规。
# 白名单（历史留档 + 本单调度的对照清单）：
#   orders/**             —— 派工单与 LEDGER 的历史记录，逐字留档是纪律
#   backend_ingestion/tests/test_observability.py —— FORBIDDEN_MONITOR_PREFIX 守卫常量
#   infra/observability/verify_wiring.sh          —— 本脚本内的检测正则（自指）
dual_all=0
for f in $(grep -rl "robot_monitor_" \
              backend_ingestion alert_engine infra docs \
              --include="*.py" --include="*.json" --include="*.yml" \
              --include="*.yaml" --include="*.md" --include="*.sh" 2>/dev/null \
           | grep -v "test_observability.py" | grep -v "verify_wiring.sh"); do
  if grep -qE '["'"'"']robot_monitor_[a-z_]+["'"'"']' "$f" \
     || grep -qE '^\s*(expr|-[[:space:]]+alert):.*robot_monitor_' "$f"; then
    bad "$f 仍引用 robot_monitor_*（全仓扫描；订单 32）"; dual_all=1
  fi
done
[ "$dual_all" -eq 0 ] && ok "全仓扫描：无 robot_monitor_* 活跃引用（代码/规则/配置/文档）"

echo "==> [3.6/5] 订单 32：改名前后对照（旧名必须消失、新名必须存在）"
# 旧名清单 = 订单 32 T1 的改前 grep 清单里**曾被注册为指标**的那些。
# 这里在**代码字面量**层面断言它们已不存在（注释里的迁移记录仍允许保留）。
for old in robot_monitor_batch_queue_size robot_monitor_batch_flush_total \
           robot_monitor_batch_rows_total robot_monitor_mqtt_messages_total \
           robot_monitor_ws_connections robot_monitor_history_latency_seconds; do
  if grep -qE '["'"'"']'"$old"'["'"'"']' backend_ingestion/app/metrics.py; then
    bad "旧名仍被注册：$old"
  else
    ok "旧名已消失：$old"
  fi
done
# 新名清单（后端 7 个 + 告警引擎 1 个改名）
for m in robot_backend_queue_depth robot_backend_batch_flushes_total \
         robot_backend_telemetry_written_total robot_backend_mqtt_received_total \
         robot_backend_ws_connections robot_backend_history_latency_seconds; do
  if grep -qE '["'"'"']'"$m"'["'"'"']' backend_ingestion/app/metrics.py; then
    ok "新名在位：$m"
  else
    bad "新名缺失：$m"
  fi
done
if grep -qE '["'"'"']robot_alert_robots_online_gauge["'"'"']' alert_engine/app/metrics.py \
   && ! grep -qE '["'"'"']robot_alert_robots_online["'"'"']' alert_engine/app/metrics.py; then
  ok "告警引擎在线台数改名：robot_alert_robots_online → robot_alert_robots_online_gauge"
else
  bad "告警引擎在线台数改名未生效（robot_alert_robots_online_gauge）"
fi

echo "==> [3.5/5] 备份指标接线（订单 31 §T3）"
# 规则里引用的备份指标，必须能在「视图 + 查询定义」里找到来源，否则是引用空气。
for m in pgbackrest_backup_last_success_timestamp_seconds \
         pgbackrest_backup_last_errored pgbackrest_stanza_status_code; do
  # 规则里必须引用
  if ! grep -q "$m" infra/observability/rules/robot-alerts.yml; then
    bad "$m 未被任何规则引用"; continue
  fi
  # 来源：指标名必须在 queries.yaml 的 metrics 定义里出现
  if grep -q "$m" infra/ops/pgbackrest/postgres-exporter-queries.yaml; then
    ok "$m（规则引用 + queries.yaml 有定义）"
  else
    bad "$m 在规则里被引用，但 queries.yaml 里找不到定义"
  fi
done
# exporter 必须真的挂上自定义查询文件，否则视图再全也导不出来
if grep -q "extend.query-path=/etc/postgres-exporter/queries.yaml" \
      infra/observability/docker-compose.observability.yml \
   && grep -q "postgres-exporter-queries.yaml:/etc/postgres-exporter/queries.yaml" \
      infra/observability/docker-compose.observability.yml; then
  ok "postgres-exporter 已挂载 --extend.query-path"
else
  bad "postgres-exporter 未挂载自定义查询（备份指标不会被导出）"
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
