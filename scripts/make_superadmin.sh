#!/usr/bin/env bash
# RobotMonitor super-admin (role=admin + is_superuser=true) creation script
#
# Prerequisites:
#   docker compose --env-file .env -f infra/docker-compose.yml -f infra/docker-compose.app.yml up -d --build
#   -> backend(:8000) + timescaledb ready
#
# Usage:
#   ./scripts/make_superadmin.sh                         # use ADMIN_EMAIL/ADMIN_PASSWORD from .env (gitignored)
#   ADMIN_EMAIL=foo@bar.com ADMIN_PASSWORD='x' ./scripts/make_superadmin.sh
#   (interactive input) ./scripts/make_superadmin.sh    # prompt if ADMIN_PASSWORD is empty
#
# Behavior:
#   1) POST /auth/register -> created as viewer (fastapi-users bcrypt-hashes; register fixes role to viewer)
#   2) SQL UPDATE -> promote to role='admin', is_superuser=TRUE (official way: app/auth/router.py:12-14)
#   3) SELECT to verify
#
# Note: the password is never recorded in source (.env is gitignored, or injected only via env at runtime).
set -euo pipefail

cd "$(dirname "$0")/.."

ADMIN_EMAIL="${ADMIN_EMAIL:-piaoyhyh@gmail.com}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-}"
BASE_URL="${BASE_URL:-http://localhost:8000}"
COMPOSE_FILES=(-f infra/docker-compose.yml -f infra/docker-compose.app.yml)

# Password resolution: env -> .env (gitignored) -> interactive
if [[ -z "$ADMIN_PASSWORD" && -f .env ]]; then
  ADMIN_PASSWORD="$(grep -E '^ADMIN_PASSWORD=' .env | head -1 | cut -d= -f2-)"
fi
if [[ -z "$ADMIN_PASSWORD" ]]; then
  read -r -s -p "Enter admin password: " ADMIN_PASSWORD
  echo
fi
if [[ -z "$ADMIN_PASSWORD" ]]; then
  echo "ADMIN_PASSWORD is empty. Aborting." >&2
  exit 1
fi

DB_PSQL() {
  docker compose "${COMPOSE_FILES[@]}" exec -T timescaledb \
    psql -v ON_ERROR_STOP=1 -U postgres -d robot_monitor "$@"
}

echo ">>> [1/3] register POST $BASE_URL/auth/register  (email=$ADMIN_EMAIL)"
REG=$(curl -s -w "\n%{http_code}" -X POST "$BASE_URL/auth/register" \
  -H 'Content-Type: application/json' \
  -d "{\"email\":\"$ADMIN_EMAIL\",\"password\":\"$ADMIN_PASSWORD\"}")
REG_CODE=$(printf '%s' "$REG" | tail -1)
REG_JSON=$(printf '%s' "$REG" | sed '$d')
echo "    response($REG_CODE): $REG_JSON"
case "$REG_CODE" in
  201) echo "    registered (viewer)" ;;
  409|400) echo "    already exists/duplicate -> skip register step (promote only)" ;;
  *) echo "    registration failed ($REG_CODE). Check that backend is up and DB is ready. Aborting." >&2; exit 1 ;;
esac

echo ">>> [2/3] promote to super-admin (role=admin, is_superuser=true)"
if DB_PSQL -c "UPDATE robot_user SET role='admin', is_superuser=TRUE WHERE email='$ADMIN_EMAIL';"; then
  echo "    promotion done"
else
  echo "    promotion SQL failed. Run manually:" >&2
  echo "    UPDATE robot_user SET role='admin', is_superuser=TRUE WHERE email='$ADMIN_EMAIL';" >&2
  exit 1
fi

echo ">>> [3/3] verify"
DB_PSQL -c "SELECT id, email, role, is_superuser, is_active FROM robot_user WHERE email='$ADMIN_EMAIL';"

echo
echo "Done. Browser http://localhost:5173/ -> login (email=$ADMIN_EMAIL, that password)"
echo "Login uses username=email. curl test:"
echo "  curl -X POST $BASE_URL/auth/jwt/login -d \"username=$ADMIN_EMAIL&password=$ADMIN_PASSWORD\""
