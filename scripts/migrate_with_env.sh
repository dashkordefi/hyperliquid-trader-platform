#!/usr/bin/env bash
# Миграции в ту же БД, что видит Gunicorn (через .env в корне проекта).
# Запуск на сервере: chmod +x scripts/migrate_with_env.sh && ./scripts/migrate_with_env.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
ENV_FILE="$ROOT/.env"
if [ ! -f "$ENV_FILE" ]; then
    echo "ОШИБКА: нет $ENV_FILE"
    exit 1
fi

set -a
eval "$(python3 "$ROOT/scripts/envtool.py" export "$ENV_FILE")"
set +a

if [ -z "${DATABASE_URL:-}" ]; then
    echo "ОШИБКА: в .env нет DATABASE_URL — migrate создаст только sqlite, Postgres останется пустым."
    exit 1
fi

if [ ! -x "$ROOT/.venv/bin/python" ]; then
    echo "ОШИБКА: нет $ROOT/.venv (сначала deploy.sh или python -m venv .venv)"
    exit 1
fi

# shellcheck disable=SC1091
source "$ROOT/.venv/bin/activate"
export DJANGO_SETTINGS_MODULE="${DJANGO_SETTINGS_MODULE:-config.settings}"

python manage.py migrate --noinput
python manage.py migrate --check
python manage.py init_roles
python manage.py shell -c "from django.db import connection; t=connection.introspection.table_names(); assert 'auth_user' in t, 'auth_user отсутствует — см. DATABASE_URL'"

echo "Миграции применены; перезапустите: sudo systemctl restart hyperliquid-trader"
