#!/usr/bin/env bash
# RobotMonitor 최고 관리자(role=admin + is_superuser=true) 생성 스크립트
#
# 사전조건:
#   docker compose --env-file .env -f infra/docker-compose.yml -f infra/docker-compose.app.yml up -d --build
#   → backend(:8000) + timescaledb 준비 완료
#
# 사용법:
#   ./scripts/make_superadmin.sh                         # .env(gitignored)의 ADMIN_EMAIL/ADMIN_PASSWORD 사용
#   ADMIN_EMAIL=foo@bar.com ADMIN_PASSWORD='x' ./scripts/make_superadmin.sh
#   (대화형 입력) ./scripts/make_superadmin.sh           # ADMIN_PASSWORD 비어있으면 프롬프트
#
# 동작:
#   1) POST /auth/register  → viewer 로 생성(fastapi-users 가 bcrypt 해시, role 은 register 가 viewer 고정)
#   2) SQL UPDATE           → role='admin', is_superuser=TRUE 로 승격(공식 방식: app/auth/router.py:12-14)
#   3) 검증 SELECT
#
# 주의: 비밀번호는 소스에 기록되지 않음(.env 는 gitignored, 또는 실행 시 env 로만 주입).
set -euo pipefail

cd "$(dirname "$0")/.."

ADMIN_EMAIL="${ADMIN_EMAIL:-piaoyhyh@gmail.com}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-}"
BASE_URL="${BASE_URL:-http://localhost:8000}"
COMPOSE_FILES=(-f infra/docker-compose.yml -f infra/docker-compose.app.yml)

# 비밀번호 결정: env → .env(gitignored) → 대화형
if [[ -z "$ADMIN_PASSWORD" && -f .env ]]; then
  ADMIN_PASSWORD="$(grep -E '^ADMIN_PASSWORD=' .env | head -1 | cut -d= -f2-)"
fi
if [[ -z "$ADMIN_PASSWORD" ]]; then
  read -r -s -p "관리자 비밀번호 입력: " ADMIN_PASSWORD
  echo
fi
if [[ -z "$ADMIN_PASSWORD" ]]; then
  echo "ADMIN_PASSWORD 가 비어있습니다. 중단." >&2
  exit 1
fi

DB_PSQL() {
  docker compose "${COMPOSE_FILES[@]}" exec -T timescaledb \
    psql -v ON_ERROR_STOP=1 -U postgres -d robot_monitor "$@"
}

echo ">>> [1/3] 등록 POST $BASE_URL/auth/register  (email=$ADMIN_EMAIL)"
REG=$(curl -s -w "\n%{http_code}" -X POST "$BASE_URL/auth/register" \
  -H 'Content-Type: application/json' \
  -d "{\"email\":\"$ADMIN_EMAIL\",\"password\":\"$ADMIN_PASSWORD\"}")
REG_CODE=$(printf '%s' "$REG" | tail -1)
REG_JSON=$(printf '%s' "$REG" | sed '$d')
echo "    응답($REG_CODE): $REG_JSON"
case "$REG_CODE" in
  201) echo "    등록됨(viewer)" ;;
  409|400) echo "    이미 존재/중복 → 등록 단계 건너뜀(승격만 진행)" ;;
  *) echo "    등록 실패($REG_CODE). backend 가 뜨고 DB 가 준비됐는지 확인하세요. 중단." >&2; exit 1 ;;
esac

echo ">>> [2/3] 최고 관리자 승격 (role=admin, is_superuser=true)"
if DB_PSQL -c "UPDATE robot_user SET role='admin', is_superuser=TRUE WHERE email='$ADMIN_EMAIL';"; then
  echo "    승격 완료"
else
  echo "    승격 SQL 실패. 수동 실행:" >&2
  echo "    UPDATE robot_user SET role='admin', is_superuser=TRUE WHERE email='$ADMIN_EMAIL';" >&2
  exit 1
fi

echo ">>> [3/3] 검증"
DB_PSQL -c "SELECT id, email, role, is_superuser, is_active FROM robot_user WHERE email='$ADMIN_EMAIL';"

echo
echo "완료. 브라우저 http://localhost:5173/ → 로그인 (email=$ADMIN_EMAIL, 해당 비밀번호)"
echo "로그인은 username=email 방식. curl 테스트:"
echo "  curl -X POST $BASE_URL/auth/jwt/login -d \"username=$ADMIN_EMAIL&password=$ADMIN_PASSWORD\""
