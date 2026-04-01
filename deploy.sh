#!/bin/bash
# Deployment script for hyperliquid-trader-platform (Django + Postgres)
#
# Использование:
#   1. Скопируйте скрипт на сервер: scp deploy.sh root@<server_ip>:~/
#   2. На сервере: chmod +x deploy.sh && sudo ./deploy.sh
#   По умолчанию .venv каждый раз пересоздаётся. Быстрый прогон без пересоздания venv:
#     sudo env DEPLOY_REBUILD_VENV=0 ./deploy.sh
#   HTTPS (Let's Encrypt): при наличии PUBLIC_DOMAIN и отсутствии сертификата запускается certbot.
#     Почта по умолчанию в скрипте/.env; другая: sudo env CERTBOT_EMAIL=you@example.com ./deploy.sh
#     Отключить выпуск сертификата: sudo env DEPLOY_SKIP_LETSENCRYPT=1 ./deploy.sh
#
# Требуется на сервере:
#   - доступ root (sudo)
#   - SSH-ключ для GitHub (git@github.com), чтобы клонировать репозиторий
#     Если ключа нет: добавьте его в ~/.ssh/ или настройте deploy key в репозитории.

set -e
trap 'echo "ОШИБКА на строке $LINENO"' ERR

# === Конфигурация ===
REPO_DIR="hyperliquid-trader-platform"
PROJECT_DIR="/opt/$REPO_DIR"
GIT_REPO_URL="https://github.com/dashkordefi/hyperliquid-trader-platform.git"
APP_NAME="hyperliquid-trader"
VENV_NAME=".venv"
ENV_FILE="$PROJECT_DIR/.env"
SOCK_DIR="$PROJECT_DIR/run"
SOCK_FILE="$SOCK_DIR/$APP_NAME.sock"
LOG_ACCESS="/var/log/gunicorn-$APP_NAME-access.log"
LOG_ERROR="/var/log/gunicorn-$APP_NAME-error.log"

# Python: используем системный python3 (3.10/3.11/3.12)
PYTHON_CMD="python3"

# Postgres (локально на сервере)
PG_DB_NAME="hyperliquid_trader"
PG_DB_USER="hyperliquid_trader"

# Публичный домен (без https://): ALLOWED_HOSTS/CSRF подхватываются в config.settings через PUBLIC_DOMAIN.
PUBLIC_DOMAIN="${PUBLIC_DOMAIN:-kriptografiya.pro}"
# Попадает в .env и в certbot --email (переопределение: env CERTBOT_EMAIL=...).
CERTBOT_EMAIL="${CERTBOT_EMAIL:-bvkuchin@gmail.com}"
# 1 — не вызывать certbot (только HTTP).
DEPLOY_SKIP_LETSENCRYPT="${DEPLOY_SKIP_LETSENCRYPT:-0}"

# 1 / true / yes — удалить и заново создать .venv (и заново поставить зависимости). По умолчанию включено.
# Отключить: sudo env DEPLOY_REBUILD_VENV=0 ./deploy.sh
DEPLOY_REBUILD_VENV="${DEPLOY_REBUILD_VENV:-1}"

_env_is_truthy() {
    case "${1,,}" in
        1|true|yes|y|on) return 0 ;;
        *) return 1 ;;
    esac
}

# Имена для nginx server_name (apex + www), для пустого PUBLIC_DOMAIN — «_».
_deploy_nginx_server_names() {
    if [ -z "${PUBLIC_DOMAIN:-}" ]; then
        printf '%s' '_'
        return
    fi
    case "$PUBLIC_DOMAIN" in
        www.*) printf '%s %s' "$PUBLIC_DOMAIN" "${PUBLIC_DOMAIN#www.}" ;;
        *) printf '%s %s' "$PUBLIC_DOMAIN" "www.$PUBLIC_DOMAIN" ;;
    esac
}

# Каталог Let's Encrypt live/*/ если сертификат уже выпущен (пусто — нет).
_deploy_find_le_dir() {
    [ -n "${PUBLIC_DOMAIN:-}" ] || return 1
    local c
    local candidates=("$PUBLIC_DOMAIN")
    case "$PUBLIC_DOMAIN" in
        www.*) candidates+=("${PUBLIC_DOMAIN#www.}") ;;
        *) candidates+=("www.$PUBLIC_DOMAIN") ;;
    esac
    for c in "${candidates[@]}"; do
        if [ -f "/etc/letsencrypt/live/$c/fullchain.pem" ]; then
            echo "/etc/letsencrypt/live/$c"
            return 0
        fi
    done
    return 1
}

# === Проверка прав ===
if [ "$EUID" -ne 0 ]; then
    echo "ОШИБКА: Запустите скрипт с правами root: sudo ./deploy.sh"
    exit 1
fi

echo "==== 1. Установка системных пакетов ===="
export DEBIAN_FRONTEND=noninteractive
apt update
apt install -y \
  python3 python3-pip python3-venv \
  nginx git curl ca-certificates \
  postgresql postgresql-contrib \
  libpq-dev \
  certbot python3-certbot-nginx

echo "==== 2. Проверка Python ===="
if ! command -v $PYTHON_CMD &>/dev/null; then
    echo "ОШИБКА: python3 не найден"
    exit 1
fi
$PYTHON_CMD --version

echo "==== 3. Подготовка директорий ===="
mkdir -p /opt
cd /opt

echo "==== 4. Пользователь appuser ===="
if ! id "appuser" &>/dev/null; then
    useradd -m -s /bin/bash appuser
fi

echo "==== 5. Клонирование репозитория ===="
if [ -d "$PROJECT_DIR/.git" ]; then
    echo "Репозиторий уже существует, делаем git fetch + reset --hard origin/main"
    # После первого деплоя каталог принадлежит appuser; git от root даёт «dubious ownership».
    chown -R appuser:appuser "$PROJECT_DIR"
    sudo -u appuser -H git -C "$PROJECT_DIR" fetch --all --prune
    sudo -u appuser -H git -C "$PROJECT_DIR" reset --hard origin/main
else
    rm -rf "$PROJECT_DIR"
    if ! git clone "$GIT_REPO_URL" "$PROJECT_DIR"; then
        echo "ОШИБКА: не удалось клонировать репозиторий."
        echo "Проверьте доступ к репозиторию (URL/токен/SSH)."
        exit 1
    fi
    cd "$PROJECT_DIR"
fi

# Дальше все относительные пути (.venv и т.д.) — внутри каталога проекта.
cd "$PROJECT_DIR"

chown -R appuser:appuser "$PROJECT_DIR"
chmod -R 755 "$PROJECT_DIR"

echo "==== 6. Виртуальное окружение и зависимости ===="
if _env_is_truthy "$DEPLOY_REBUILD_VENV"; then
    echo "DEPLOY_REBUILD_VENV: пересоздаём $VENV_NAME (--clear)"
    sudo -u appuser $PYTHON_CMD -m venv --clear "$PROJECT_DIR/$VENV_NAME"
elif [ ! -x "$PROJECT_DIR/$VENV_NAME/bin/python" ]; then
    echo "Создаём $VENV_NAME"
    sudo -u appuser $PYTHON_CMD -m venv "$PROJECT_DIR/$VENV_NAME"
else
    echo "Виртуальное окружение уже есть; создание пропущено (DEPLOY_REBUILD_VENV=0)"
fi
sudo -u appuser bash -c "cd $PROJECT_DIR && source $VENV_NAME/bin/activate && pip install --upgrade pip && pip install -r requirements.txt"

echo "==== 7. Настройка Postgres (локально) ===="
systemctl enable --now postgresql
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='${PG_DB_USER}'" | grep -q 1; then
    PG_DB_PASSWORD="$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 32)"
    sudo -u postgres psql -c "CREATE USER ${PG_DB_USER} WITH PASSWORD '${PG_DB_PASSWORD}';"
else
    PG_DB_PASSWORD=""
fi
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='${PG_DB_NAME}'" | grep -q 1; then
    sudo -u postgres psql -c "CREATE DATABASE ${PG_DB_NAME} OWNER ${PG_DB_USER};"
fi

echo "==== 8. Генерация SECRET_KEY и создание/обновление .env ===="
SECRET_KEY="$(sudo -u appuser bash -c "cd $PROJECT_DIR && source $VENV_NAME/bin/activate && python -c \"from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())\"")"
SERVER_IP=$(curl -s http://checkip.amazonaws.com 2>/dev/null || hostname -I | awk '{print $1}' || echo "127.0.0.1")

if [ -f "$ENV_FILE" ]; then
    echo "Найден существующий .env — не перетираем целиком, добавим недостающие ключи."
else
    touch "$ENV_FILE"
fi

ensure_env_line() {
    local key="$1"
    local value="$2"
    if grep -qE "^${key}=" "$ENV_FILE"; then
        return 0
    fi
    # systemd + bash: без «голого» значения — иначе ) & ! # $ ломают source и иногда systemd
    python3 "$PROJECT_DIR/scripts/envtool.py" append "$ENV_FILE" "$key" "$value"
}

ensure_env_line "DJANGO_SETTINGS_MODULE" "config.settings"
ensure_env_line "SECRET_KEY" "$SECRET_KEY"
ensure_env_line "DEBUG" "False"
if [ -n "$PUBLIC_DOMAIN" ]; then
    ensure_env_line "PUBLIC_DOMAIN" "$PUBLIC_DOMAIN"
    ensure_env_line "PUBLIC_URL" "https://$PUBLIC_DOMAIN"
    _ah="$SERVER_IP,localhost,127.0.0.1,$PUBLIC_DOMAIN"
    _csrf="http://$SERVER_IP,http://localhost,http://127.0.0.1,https://$SERVER_IP,https://$PUBLIC_DOMAIN"
    case "$PUBLIC_DOMAIN" in www.*) ;; *)
        _ah="$_ah,www.$PUBLIC_DOMAIN"
        _csrf="$_csrf,https://www.$PUBLIC_DOMAIN"
        ;;
    esac
    ensure_env_line "ALLOWED_HOSTS" "$_ah"
    ensure_env_line "CSRF_TRUSTED_ORIGINS" "$_csrf"
else
    ensure_env_line "ALLOWED_HOSTS" "$SERVER_IP,localhost,127.0.0.1"
    ensure_env_line "CSRF_TRUSTED_ORIGINS" "http://$SERVER_IP,http://localhost,http://127.0.0.1,https://$SERVER_IP"
fi
if [ -n "$PG_DB_PASSWORD" ]; then
    ensure_env_line "DATABASE_URL" "postgres://${PG_DB_USER}:${PG_DB_PASSWORD}@127.0.0.1:5432/${PG_DB_NAME}"
else
    ensure_env_line "DATABASE_URL" "postgres://${PG_DB_USER}:<SET_PASSWORD>@127.0.0.1:5432/${PG_DB_NAME}"
    echo "ВНИМАНИЕ: пароль пользователя Postgres уже существует; обновите DATABASE_URL в $ENV_FILE вручную."
fi
ensure_env_line "DATABASE_SSL_REQUIRE" "false"
if [ -n "${CERTBOT_EMAIL:-}" ]; then
    ensure_env_line "CERTBOT_EMAIL" "$CERTBOT_EMAIL"
fi

chown appuser:appuser "$ENV_FILE"
chmod 600 "$ENV_FILE"
# Привести все строки к виду KEY="..." (исправляет старые SECRET_KEY без кавычек)
python3 "$PROJECT_DIR/scripts/envtool.py" materialize "$ENV_FILE"
chown appuser:appuser "$ENV_FILE"
echo ".env готов (ALLOWED_HOSTS и DATABASE_URL настроены)"

echo "==== 9. Миграции, статика, роли (как в build.sh) ===="
sudo -u appuser bash <<ENVEOF
set -a
$(python3 "$PROJECT_DIR/scripts/envtool.py" export "$ENV_FILE")
set +a
cd "$PROJECT_DIR" && source "$VENV_NAME/bin/activate" && python manage.py collectstatic --noinput
ENVEOF
chmod +x "$PROJECT_DIR/scripts/migrate_with_env.sh"
chown appuser:appuser "$PROJECT_DIR/scripts/migrate_with_env.sh" 2>/dev/null || true
# Один путь с .env: иначе migrate без DATABASE_URL бьёт в sqlite, Postgres пустой → relation auth_user does not exist
# Явно через bash: CRLF/shebang на сервере не даёт «command not found»
sudo -u appuser bash "$PROJECT_DIR/scripts/migrate_with_env.sh"
# Чтобы nginx (www-data) мог отдавать статику из STATIC_ROOT
chmod -R o+rX "$PROJECT_DIR/staticfiles" 2>/dev/null || true
chmod o+x /opt "/opt/$REPO_DIR" 2>/dev/null || true

echo "==== 10. Каталог run и логи ===="
mkdir -p "$SOCK_DIR"
# Чтобы сокет наследовал группу www-data (для nginx), задаём группу и setgid на каталог
chown appuser:www-data "$SOCK_DIR"
chmod g+s "$SOCK_DIR"
touch "$LOG_ACCESS" "$LOG_ERROR"
chown appuser:appuser "$LOG_ACCESS" "$LOG_ERROR"
chmod 644 "$LOG_ACCESS" "$LOG_ERROR"
# appuser в группе www-data для доступа nginx к сокету
usermod -aG www-data appuser 2>/dev/null || true

echo "==== 11. Systemd unit для Gunicorn ===="
cat > "/etc/systemd/system/${APP_NAME}.service" <<SVCEOF
[Unit]
Description=Gunicorn daemon for $APP_NAME
After=network.target postgresql.service

[Service]
User=appuser
Group=appuser
WorkingDirectory=$PROJECT_DIR
Environment="PATH=$PROJECT_DIR/$VENV_NAME/bin"
Environment="DJANGO_SETTINGS_MODULE=config.settings"
EnvironmentFile=$ENV_FILE
Umask=0007
ExecStart=$PROJECT_DIR/$VENV_NAME/bin/gunicorn --access-logfile $LOG_ACCESS --error-logfile $LOG_ERROR --workers 3 --bind unix:$SOCK_FILE config.wsgi:application
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SVCEOF

systemctl daemon-reload

echo "==== 12. Диагностика Django ===="
sudo -u appuser bash <<ENVEOF
set -a
$(python3 "$PROJECT_DIR/scripts/envtool.py" export "$ENV_FILE")
set +a
cd "$PROJECT_DIR" && source "$VENV_NAME/bin/activate" && python manage.py check
ENVEOF

echo "==== 13. Запуск Gunicorn ===="
systemctl stop "$APP_NAME" 2>/dev/null || true
systemctl start "$APP_NAME"
systemctl enable "$APP_NAME"

echo "Ожидание создания сокета..."
for i in $(seq 1 10); do
    if [ -S "$SOCK_FILE" ]; then
        echo "Сокет создан."
        break
    fi
    if ! systemctl is-active --quiet "$APP_NAME"; then
        echo "Сервис не запущен. Логи:"
        journalctl -u "$APP_NAME" --no-pager -n 30
        exit 1
    fi
    sleep 2
done

if [ ! -S "$SOCK_FILE" ]; then
    echo "ОШИБКА: сокет не создан. Логи:"
    journalctl -u "$APP_NAME" --no-pager -n 50
    exit 1
fi

# Права на сокет для nginx (на случай если setgid не сработал)
chown appuser:www-data "$SOCK_FILE" 2>/dev/null || true
chmod 660 "$SOCK_FILE" 2>/dev/null || true

echo "==== 14. Nginx ===="
mkdir -p /var/www/certbot
chown www-data:www-data /var/www/certbot
chmod 755 /var/www/certbot

NGINX_SERVER_NAMES="$(_deploy_nginx_server_names)"
LE_DIR="$(_deploy_find_le_dir || true)"

SSL_DHPARAM_LINE=""
if [ -f /etc/letsencrypt/ssl-dhparams.pem ]; then
    SSL_DHPARAM_LINE="ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;"
fi

_write_nginx_site() {
    local outfile="/etc/nginx/sites-available/${APP_NAME}"
    if [ -n "$LE_DIR" ]; then
        echo "Пишем nginx с HTTPS (сертификат: $LE_DIR)"
        cat >"$outfile" <<EOF
server {
    listen 80;
    server_name ${NGINX_SERVER_NAMES};

    location ^~ /.well-known/acme-challenge/ {
        root /var/www/certbot;
        default_type text/plain;
    }

    location / {
        return 301 https://\$host\$request_uri;
    }
}

server {
    listen 443 ssl http2;
    server_name ${NGINX_SERVER_NAMES};

    ssl_certificate ${LE_DIR}/fullchain.pem;
    ssl_certificate_key ${LE_DIR}/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ${SSL_DHPARAM_LINE}

    client_max_body_size 10M;

    location = /favicon.ico { access_log off; log_not_found off; }

    location /static/ {
        alias $PROJECT_DIR/staticfiles/;
        expires 30d;
        add_header Cache-Control "public, immutable";
    }

    location / {
        proxy_pass http://unix:$SOCK_FILE;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_http_version 1.1;
        proxy_buffering off;
    }
}
EOF
    else
        echo "Пишем nginx только HTTP (certbot добавит HTTPS при первом выпуске)"
        cat >"$outfile" <<EOF
server {
    listen 80;
    server_name ${NGINX_SERVER_NAMES};

    client_max_body_size 10M;

    location ^~ /.well-known/acme-challenge/ {
        root /var/www/certbot;
        default_type text/plain;
    }

    location = /favicon.ico { access_log off; log_not_found off; }

    location /static/ {
        alias $PROJECT_DIR/staticfiles/;
        expires 30d;
        add_header Cache-Control "public, immutable";
    }

    location / {
        proxy_pass http://unix:$SOCK_FILE;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_http_version 1.1;
        proxy_buffering off;
    }
}
EOF
    fi
}

_write_nginx_site

ln -sf "/etc/nginx/sites-available/${APP_NAME}" /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

echo "==== 15. Let's Encrypt (HTTPS, если сертификата ещё нет) ===="
if [ -z "${PUBLIC_DOMAIN:-}" ] || [ "$NGINX_SERVER_NAMES" = "_" ]; then
    echo "PUBLIC_DOMAIN не задан — certbot пропущен."
elif [ -n "$LE_DIR" ]; then
    echo "Сертификат уже есть ($LE_DIR), выпуск не требуется."
elif _env_is_truthy "$DEPLOY_SKIP_LETSENCRYPT"; then
    echo "DEPLOY_SKIP_LETSENCRYPT=1 — certbot пропущен."
else
    echo "Запуск certbot (нужны DNS на этот сервер и доступность :80 с интернета)..."
    _certbot_args=(
        certbot
        --nginx
        --non-interactive
        --agree-tos
        --redirect
    )
    if [ -n "${CERTBOT_EMAIL:-}" ]; then
        _certbot_args+=(--email "$CERTBOT_EMAIL")
    else
        _certbot_args+=(--register-unsafely-without-email)
    fi
    for _d in $NGINX_SERVER_NAMES; do
        _certbot_args+=(-d "$_d")
    done
    set +e
    "${_certbot_args[@]}"
    _crb=$?
    set -e
    if [ "$_crb" -ne 0 ]; then
        echo "ВНИМАНИЕ: certbot завершился с кодом $_crb. Проверьте DNS, 80 порт и логи: /var/log/letsencrypt/letsencrypt.log"
    else
        LE_DIR="$(_deploy_find_le_dir || true)"
        if [ -n "$LE_DIR" ]; then
            if [ -f /etc/letsencrypt/ssl-dhparams.pem ]; then
                SSL_DHPARAM_LINE="ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;"
            else
                SSL_DHPARAM_LINE=""
            fi
            _write_nginx_site
        fi
        systemctl enable --now certbot.timer 2>/dev/null || true
        nginx -t
        systemctl reload nginx
    fi
fi

echo "==== 16. Статус ===="
systemctl status "$APP_NAME" --no-pager || true
systemctl status nginx --no-pager || true

echo ""
echo "==== Готово ===="
if [ -n "${LE_DIR:-}" ]; then
    echo "Приложение: https://${PUBLIC_DOMAIN}/ (и http://$SERVER_IP/ → редирект на HTTPS)"
else
    echo "Приложение: http://$SERVER_IP/  (или http://${PUBLIC_DOMAIN}/ — после успешного certbot будет HTTPS)"
fi
echo "Логи приложения: journalctl -u $APP_NAME -f"
echo "Создать суперпользователя: sudo -u appuser bash -c 'set -a; eval \"\$(python3 $PROJECT_DIR/scripts/envtool.py export $ENV_FILE)\"; set +a; cd $PROJECT_DIR && source $VENV_NAME/bin/activate && python manage.py createsuperuser'"
