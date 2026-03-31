#!/usr/bin/env bash
set -euo pipefail
pip install -r requirements.txt
python manage.py collectstatic --noinput
python manage.py migrate --noinput
# Группы ролей (compliance_approver и т.д.) — без них в админке пустой список Groups.
# Раньше было init_roles || true — ошибка глоталась, деплой зелёный, а ролей в БД нет.
python manage.py init_roles
