#!/usr/bin/env bash
# 灾备交付物**静态**自检（无需 Docker）—— 在 CI/本地都能跑
#
# 定位（订单 31 后收窄，避免与 verify.sh 混淆）：
#   本脚本只校验「**文件形态**对不对」：YAML 能否解析、配置结构是否合法、
#   有没有把密钥写进仓库、脚本语法是否成立。
#   **它不判定备份能不能恢复** —— 那是 `verify.sh`（L1/L2/L3 真断言）的职责。
#   两者互补：本脚本保证「配置不会因语法问题起不来」，verify.sh 保证「备份真的可用」。
set -euo pipefail
cd "$(dirname "$0")"

fail=0
ok()   { printf '  \033[32mOK\033[0m   %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$*"; fail=$((fail+1)); }

echo "== 1. YAML 可解析 =="
for f in docker-compose.dr.yml postgres-exporter-queries.yaml; do
  if python3 -c "import yaml,sys; yaml.safe_load(open('$f'))" 2>/dev/null; then
    ok "$f"
  else
    bad "$f YAML 解析失败"
  fi
done

echo "== 2. pgbackrest.conf 结构（section + key=value）=="
for f in pgbackrest.conf pgbackrest-db.conf; do
  if python3 - "$f" <<'PY'
import sys
for i,line in enumerate(open(sys.argv[1],encoding="utf-8"),1):
    st=line.strip()
    if not st or st.startswith("#"): continue
    if st.startswith("["):
        assert st.endswith("]"), f"line {i} section not closed"
    else:
        assert "=" in st, f"line {i} missing ="
PY
  then ok "$f 结构合法"; else bad "$f 结构非法"; fi
done

echo "== 3. 密钥不明文 =="
if grep -RE 'cipher-pass=[A-Za-z0-9+/]{16,}' pgbackrest.conf pgbackrest-db.conf; then
  bad "检测到疑似明文 cipher-pass"
else
  ok "无明文 cipher-pass（仅 \${PGBACKREST_CIPHER_PASS} 占位）"
fi

echo "== 4. shell 脚本语法（含订单 31 的 T2 三入口）=="
for s in run-backup.sh backup.sh restore.sh verify.sh verify-restore.sh; do
  if [ ! -f "$s" ]; then bad "$s 不存在"; continue; fi
  if bash -n "$s" 2>/dev/null; then ok "$s"; else bad "$s 语法错误"; fi
  # 订单 31 §禁止项 2：不得用 `|| true` / `continue-on-error` 兜底**主命令**失败。
  # 判定收紧到「与 pgbackrest 或脚本自身判定命令同行的兜底」——注释里的示例、
  # 以及 trap 里的 best-effort 清理（`rm || true`）、`grep -c` 不匹配时需要的
  # `|| true`（否则 set -e 会把"零匹配"误判为失败）都不算违规。
  hits="$(grep -nE '\|\|[[:space:]]*true|continue-on-error' "$s" 2>/dev/null \
          | grep -vE '^[0-9]+:[[:space:]]*#' \
          | grep -E 'pgbackrest|verify\.sh|backup\.sh|restore\.sh' || true)"
  if [ -n "$hits" ]; then
    printf '  \033[31mFAIL\033[0m %s 对主命令做了失败兜底：\n%s\n' "$s" "$hits"
    fail=$((fail+1))
  fi
done

echo "== 5. T2 三入口存在且可执行 =="
for s in backup.sh restore.sh verify.sh; do
  if [ -x "$s" ]; then ok "$s 可执行"; else bad "$s 不可执行（chmod +x）"; fi
done

echo "== 6. compose 引用脚本与文件齐备 =="
# overlay 里挂载的每个 ./ops/pgbackrest/xxx 都必须在磁盘上存在 —— 少一个，
# 容器起得来但该入口是空文件/挂载失败（静默失效）。
missing=0
for ref in $(grep -oE '\./ops/pgbackrest/[A-Za-z0-9._-]+' docker-compose.dr.yml | sort -u); do
  if [ ! -e "${ref#./ops/pgbackrest/}" ]; then
    bad "compose 引用了不存在的文件：$ref"; missing=1
  fi
done
[ "$missing" -eq 0 ] && ok "compose 引用的脚本/配置全部存在"

echo "== 7. shellcheck（若已安装）=="
if command -v shellcheck >/dev/null 2>&1; then
  if shellcheck -S warning run-backup.sh backup.sh restore.sh verify.sh verify-restore.sh; then
    ok "shellcheck 通过"
  else
    bad "shellcheck 报告问题"
  fi
else
  echo "  SKIP shellcheck 未安装"
fi

echo
if [ "$fail" -eq 0 ]; then
  echo "静态检查全部通过。"
  echo "⚠️  运行时真实性判定请跑 verify.sh（L1 校验和 / L2 新鲜度 / L3 恢复比对）。"
else
  echo "$fail 项未通过。"
  exit 1
fi
