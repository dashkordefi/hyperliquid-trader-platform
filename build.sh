#!/usr/bin/env bash
set -euo pipefail
pip install -r requirements.txt
python manage.py collectstatic --noinput
python manage.py migrate --noinput
python manage.py init_roles || true
