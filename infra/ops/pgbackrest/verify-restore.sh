#!/usr/bin/env bash
# L3 恢复真实性校验：把最新备份**真实恢复**到临时目录，启动只读实例，
# 与源库比对 行数 / 最新时间戳。被 verify.sh --scratch 调用（也可单独跑）。
#
# 为什么值得单独做一层：
#   `verify` 只证明「仓库内文件的校验和没坏」，不证明「这份备份能还原成能用的库」。
#   本层是唯一能覆盖「恢复后数据是否真的回来了」的证据 —— 订单 31 §T2 明确要求
#   「恢复后行数/最新时间戳比对，禁止 echo OK 式假验证」。
#
# 用法：
#   verify-restore.sh [--help]
#
# 环境变量（默认值面向 compose：仓库边车内执行）：
#   PGBACKREST_STANZA      默认 robot_monitor
#   SRC_PSQL_ARGS          源库 psql 连接参数（默认 "-h timescaledb -U postgres -d robot_monitor"）
#   SCRATCH_DIR            恢复目标空目录（默认 /tmp/pbr-verify-restore，自动创建）
#   SCRATCH_PORT           恢复实例端口（默认 55432）
#   VERIFY_TABLES          要比对的表（默认 "robot_telemetry robot_metadata robot_alert"）
#
# 退出码：0 一致；1 不一致；2 用法错误；3 前置条件缺失（无 pgbackrest / 无源库连接）
#
# ⚠️ 已知边界：本脚本要求恢复目标目录**可写**且不与生产 PGDATA 冲突。
#    它**不会**碰生产数据：--pg1-path 一律指向 SCRATCH_DIR。
set -euo pipefail

STANZA="${PGBACKREST_STANZA:-robot_monitor}"
SCRATCH_DIR="${SCRATCH_DIR:-/tmp/pbr-verify-restore}"
SCRATCH_PORT="${SCRATCH_PORT:-55432}"
SRC_PSQL_ARGS="${SRC_PSQL_ARGS:--h timescaledb -U postgres -d robot_monitor}"
VERIFY_TABLES="${VERIFY_TABLES:-robot_telemetry robot_metadata robot_alert}"

log()  { printf '[dr-verify-restore] %s\n' "$*"; }
die()  { printf '[dr-verify-restore] ERROR: %s\n' "$*" >&2; exit "${2:-1}"; }

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
fi

command -v pgbackrest >/dev/null 2>&1 || die "PATH 内找不到 pgbackrest 二进制" 3
command -v psql       >/dev/null 2>&1 || die "PATH 内找不到 psql（源库比对需要）" 3

# 源库必须可达 —— 否则「比对」无从谈起，明确报缺条件而不是假装通过。
if ! psql $SRC_PSQL_ARGS -tAc 'SELECT 1' >/dev/null 2>&1; then
  die "源库不可达（psql $SRC_PSQL_ARGS 失败）。本层需要能读源库做基线比对。" 3
fi

# 1) 准备空目录（幂等：已存在则清空）
if [ -e "$SCRATCH_DIR" ] && [ ! -d "$SCRATCH_DIR" ]; then
  die "SCRATCH_DIR=$SCRATCH_DIR 存在但不是目录" 2
fi
mkdir -p "$SCRATCH_DIR"
# --delta 在非空目录上需要 backup.manifest；这里要的是干净恢复，故清空后走全量。
rm -rf "${SCRATCH_DIR:?}"/* "${SCRATCH_DIR:?}"/.[!.]* 2>/dev/null || true
log "恢复目标：$SCRATCH_DIR（已清空）"

# 2) 记录源库基线（**先取基线再恢复**，避免漂移）
declare -A SRC_COUNT SRC_MAXTS
for t in $VERIFY_TABLES; do
  SRC_COUNT[$t]="$(psql $SRC_PSQL_ARGS -tAc "SELECT count(*) FROM $t" 2>/dev/null || echo "")"
  [ -n "${SRC_COUNT[$t]}" ] || die "源库表 $t 不可读（表名错/权限不足）" 3
  # 时间戳列只有带 time 列的表才有
  SRC_MAXTS[$t]="$(psql $SRC_PSQL_ARGS -tAc "SELECT coalesce(max(time)::text,'') FROM $t" 2>/dev/null || echo "")"
  log "基线 $t：count=${SRC_COUNT[$t]} max_time=${SRC_MAXTS[$t]:-n/a}"
done

# 3) 真实恢复到临时目录（不加 --delta：干净目标，要的是可独立启动的库）
log "执行 pgbackrest restore 到临时目录（真实落盘）"
pgbackrest --stanza="$STANZA" --pg1-path="$SCRATCH_DIR" \
  --log-level-console=info restore

[ -f "$SCRATCH_DIR/PG_VERSION" ] || die "恢复后 $SCRATCH_DIR 内没有 PG_VERSION —— 恢复未产生有效数据目录" 1
log "恢复产物校验：PG_VERSION=$(cat "$SCRATCH_DIR/PG_VERSION")，文件数=$(find "$SCRATCH_DIR" -type f | wc -l)"

# 4) 启动只读实例并比对
#    ⚠️ restore 默认会写 recovery 配置；本层只需读数据，故以 -P（只读）与独立端口启动。
#    postgres -c port 覆盖端口，避免与生产 5432 冲突。
# pg 二进制目录：优先 pg_config（官方 PG 自带，跨发行版/自编译都可靠），
# 其次 psql 所在目录，均无则报缺条件。
if command -v pg_config >/dev/null 2>&1; then
  PG_BIN="$(pg_config --bindir)"
else
  PG_BIN="$(dirname "$(command -v psql)")"
fi
[ -x "$PG_BIN/pg_ctl" ] || die "找不到 pg_ctl（PG_BIN=$PG_BIN）；恢复实例需要 PG 客户端工具集" 3
SOCK_DIR="$(mktemp -d)"
LOG_FILE="$(mktemp)"
STARTED=no
cleanup() {
  if [ "$STARTED" = "yes" ]; then
    "$PG_BIN/pg_ctl" -D "$SCRATCH_DIR" -m immediate stop >/dev/null 2>&1 || true
  fi
  rm -rf "$SOCK_DIR" "$LOG_FILE" 2>/dev/null || true
}
trap cleanup EXIT

# Debian 系把 postgresql.conf 放在 /etc/postgresql/<ver>/main/（PGDATA 之外），
# 这类布局下 restore 出来的目录**没有** postgresql.conf，实例起不来。
# 生产 compose 的 PGDATA 自带 postgresql.conf，不存在此问题。
if [ ! -f "$SCRATCH_DIR/postgresql.conf" ]; then
  if [ -n "${PGDATA_CONF_SRC:-}" ] && [ -f "$PGDATA_CONF_SRC" ]; then
    cp "$PGDATA_CONF_SRC" "$SCRATCH_DIR/postgresql.conf"
    log "补入 postgresql.conf（来自 PGDATA_CONF_SRC=$PGDATA_CONF_SRC）"
    # Debian split-config：conf.d 是 include_dir；data_directory 指向生产路径。
    # 二者对临时实例都是干扰，就地修正 —— 只改 scratch 目录内的副本，不碰源配置。
    grep -qE "^[[:space:]]*include_dir" "$SCRATCH_DIR/postgresql.conf" \
      && mkdir -p "$SCRATCH_DIR/conf.d"
    sed -i "s|^[[:space:]]*data_directory[[:space:]]*=.*|data_directory = '$SCRATCH_DIR'|" \
      "$SCRATCH_DIR/postgresql.conf"
  else
    log "警告：$SCRATCH_DIR 内无 postgresql.conf，且未提供 PGDATA_CONF_SRC。"
    log "      Debian split-config 布局需要它；生产 PGDATA 自带，无需设置。"
  fi
fi

log "启动恢复实例（端口 $SCRATCH_PORT，socket $SOCK_DIR）"
if ! "$PG_BIN/pg_ctl" -D "$SCRATCH_DIR" \
       -o "-p $SCRATCH_PORT -c unix_socket_directories=$SOCK_DIR -c listen_addresses=''" \
       -l "$LOG_FILE" start; then
  log "启动失败，日志尾部："
  tail -20 "$LOG_FILE" >&2
  die "恢复实例无法启动 —— 备份不可用（这本身就是失败证据）" 1
fi
STARTED=yes

# 等 pg_isready
for _ in $(seq 1 30); do
  if "$PG_BIN/pg_isready" -h "$SOCK_DIR" -p "$SCRATCH_PORT" >/dev/null 2>&1; then break; fi
  sleep 1
done
"$PG_BIN/pg_isready" -h "$SOCK_DIR" -p "$SCRATCH_PORT" >/dev/null 2>&1 \
  || { tail -20 "$LOG_FILE" >&2; die "恢复实例未就绪（pg_isready 超时）" 1; }

# 5) 逐表比对
FAILED=0
for t in $VERIFY_TABLES; do
  rcount="$(psql -h "$SOCK_DIR" -p "$SCRATCH_PORT" -U postgres -d robot_monitor -tAc "SELECT count(*) FROM $t" 2>/dev/null || echo "")"
  if [ "$rcount" != "${SRC_COUNT[$t]}" ]; then
    printf '[dr-verify-restore] FAIL: 表 %s 行数不一致 源=%s 恢复=%s\n' "$t" "${SRC_COUNT[$t]}" "$rcount" >&2
    FAILED=1
  else
    log "OK 表 $t 行数一致：$rcount"
  fi
  rmax="$(psql -h "$SOCK_DIR" -p "$SCRATCH_PORT" -U postgres -d robot_monitor -tAc "SELECT coalesce(max(time)::text,'') FROM $t" 2>/dev/null || echo "")"
  if [ -n "${SRC_MAXTS[$t]}" ] && [ "$rmax" != "${SRC_MAXTS[$t]}" ]; then
    printf '[dr-verify-restore] FAIL: 表 %s 最新时间戳不一致 源=%s 恢复=%s\n' "$t" "${SRC_MAXTS[$t]}" "$rmax" >&2
    FAILED=1
  elif [ -n "${SRC_MAXTS[$t]}" ]; then
    log "OK 表 $t max(time) 一致：$rmax"
  fi
done

# 6) TimescaleDB 扩展表（若装了 timescaledb）—— 物理备份下超表目录一致性
ext_ver="$(psql -h "$SOCK_DIR" -p "$SCRATCH_PORT" -U postgres -d robot_monitor -tAc \
  "SELECT extversion FROM pg_extension WHERE extname='timescaledb'" 2>/dev/null || echo "")"
if [ -n "$ext_ver" ]; then
  chunk_n="$(psql -h "$SOCK_DIR" -p "$SCRATCH_PORT" -U postgres -d robot_monitor -tAc \
    "SELECT count(*) FROM timescaledb_information.chunks" 2>/dev/null || echo "?")"
  log "恢复库 timescaledb=${ext_ver}，chunk 数=${chunk_n}"
fi

[ "$FAILED" -eq 0 ] || die "恢复真实性比对失败" 1
log "恢复真实性全部通过：源库与恢复库 行数 / 最新时间戳 一致。"
