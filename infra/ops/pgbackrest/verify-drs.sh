#!/usr/bin/env bash
# 灾备交付物静态自检（无需 Docker）—— 在 CI/本地都能跑
#
# 校验「语法层面」的正确性；运行时验证（stanza-create/backup/restore）见 DR-RUNBOOK §6。
set -euo pipefail
cd "$(dirname "$0")"

fail=0
ok()   { printf '  \033[32mOK\033[0m   %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$*"; fail=$((fail+1)); }

echo "== 1. YAML 可解析 =="
if python3 -c "import yaml,sys; yaml.safe_load(open('docker-compose.dr.yml'))" 2>/dev/null; then
  ok "docker-compose.dr.yml"
else
  bad "docker-compose.dr.yml YAML 解析失败"
fi

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

echo "== 4. shell 脚本语法 =="
for s in run-backup.sh restore.sh; do
  if bash -n "$s" 2>/dev/null; then ok "$s"; else bad "$s 语法错误"; fi
done

echo "== 5. shellcheck（若已安装）=="
if command -v shellcheck >/dev/null 2>&1; then
  if shellcheck -S warning run-backup.sh restore.sh; then ok "shellcheck 通过"; else bad "shellcheck 报告问题"; fi
else
  echo "  SKIP shellcheck 未安装"
fi

echo
if [ "$fail" -eq 0 ]; then
  echo "全部静态检查通过。运行时验证请在 Docker 环境按 DR-RUNBOOK §4/§6 执行。"
else
  echo "$fail 项未通过。"
  exit 1
fi
