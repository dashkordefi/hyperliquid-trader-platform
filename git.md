# Обновление кода на сервере (`/opt/hyperliquid-trader-platform`)

```bash
cd /opt/hyperliquid-trader-platform
sudo -u appuser git fetch --all --prune
sudo -u appuser git reset --hard origin/main
sudo -u appuser bash -lc 'cd /opt/hyperliquid-trader-platform && source .venv/bin/activate && pip install -r requirements.txt'
sudo -u appuser /opt/hyperliquid-trader-platform/scripts/migrate_with_env.sh
sudo systemctl restart hyperliquid-trader
```

## Ошибка `relation "auth_user" does not exist`

В **Postgres**, к которому подключается Gunicorn, нет таблиц Django. Чаще всего `migrate` когда‑то запускали **без** подхваченного `DATABASE_URL` (тогда таблицы создаются в локальном `db.sqlite3`, а не в Postgres).

**Исправление:** после `git pull` выполните (или всегда используйте скрипт выше):

```bash
sudo -u appuser /opt/hyperliquid-trader-platform/scripts/migrate_with_env.sh
sudo systemctl restart hyperliquid-trader
```

Убедитесь, что в `/opt/hyperliquid-trader-platform/.env` задан корректный **`DATABASE_URL`** (не плейсхолдер `<SET_PASSWORD>`).

## Логи

```bash
sudo journalctl -u hyperliquid-trader -n 80 --no-pager
```
