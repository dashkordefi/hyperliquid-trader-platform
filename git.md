# Обновление кода на сервере (`/opt/hyperliquid-trader-platform`)

Каталог после **`deploy.sh`** принадлежит пользователю **`appuser`**, файл **`.env`** — режим **600** (читает/пишет только `appuser`). Под своим логином (`bkuchin` и т.д.) не запускайте **`git …`** и не трогайте **`.env`** без **`sudo -u appuser`** (или `sudo`), иначе `Permission denied` на `.git/FETCH_HEAD` или на `.env`.

```bash
cd /opt/hyperliquid-trader-platform
sudo -u appuser git fetch --all --prune
sudo -u appuser git reset --hard origin/main
sudo -u appuser bash -lc 'cd /opt/hyperliquid-trader-platform && source .venv/bin/activate && pip install -r requirements.txt'
sudo -u appuser bash /opt/hyperliquid-trader-platform/scripts/migrate_with_env.sh
sudo systemctl restart hyperliquid-trader
```

Скрипт лучше вызывать как **`bash …/migrate_with_env.sh`**, а не прямым путём: так не нужен бит **+x** и не ломается окончание строк (**CRLF** с Windows даёт `command not found` на shebang). В репозитории задан **`.gitattributes`** (`*.sh` → LF). Уже на сервере один раз: `sed -i 's/\r$//' scripts/migrate_with_env.sh`

## Ошибка `relation "auth_user" does not exist`

В **Postgres**, к которому подключается Gunicorn, нет таблиц Django. Чаще всего `migrate` когда‑то запускали **без** подхваченного `DATABASE_URL` (тогда таблицы создаются в локальном `db.sqlite3`, а не в Postgres).

**Исправление:** после обновления кода выполните (или всегда используйте скрипт в начале файла):

```bash
sudo -u appuser bash /opt/hyperliquid-trader-platform/scripts/migrate_with_env.sh
sudo systemctl restart hyperliquid-trader
```

Убедитесь, что в `/opt/hyperliquid-trader-platform/.env` задан корректный **`DATABASE_URL`** (не плейсхолдер `<SET_PASSWORD>`).

## Ошибка `.env: syntax error near unexpected token` (при `source .env`)

`SECRET_KEY` и др. с символами `) & ! # $` ломают обычный `source .env`. Используется **`scripts/envtool.py`**. После обновления кода выполните **`deploy.sh`** или вручную:

```bash
sudo -u appuser python3 /opt/hyperliquid-trader-platform/scripts/envtool.py materialize /opt/hyperliquid-trader-platform/.env
```

## `local changes would be overwritten by merge`

На сервере изменены файлы относительно Git. Если правки не нужны, не используйте `git pull`, а сбросьте к удалённой ветке:

```bash
cd /opt/hyperliquid-trader-platform
sudo -u appuser git fetch --all --prune
sudo -u appuser git reset --hard origin/main
```

## Логи

```bash
sudo journalctl -u hyperliquid-trader -n 80 --no-pager
```
