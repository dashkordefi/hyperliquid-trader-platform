# Hyperunit proxy (Cloudflare Worker)

Запросы с IP VPS/хостинга к `https://api.hyperunit.xyz` часто получают **403** (Cloudflare). Worker выполняет **исходящий** запрос к Hyperunit **из сети Cloudflare**, поэтому upstream обычно отвечает 200.

## 1. Установить Node.js LTS

Скачай с https://nodejs.org , установи, перезапусти терминал.

```bash
node -v
npx --version
```

## 2. Деплой с компьютера (Wrangler)

Из корня репозитория:

```bash
cd scripts/hyperunit-proxy-wrangler
npx wrangler@latest login
npx wrangler@latest deploy
```

В конце будет URL вида `https://hyperunit-proxy.<поддомен>.workers.dev`.

Если имя `hyperunit-proxy` занято — измени `name` в `wrangler.toml` и снова `deploy`.

## 3. Проверка в браузере

Открой (подставь свой HL-адрес):

`https://<твой-worker>.workers.dev/gen/ethereum/hyperliquid/eth/0xТВОЙ_HL_АДРЕС`

Должен вернуться JSON с `"status":"OK"` и полем `address`.

## 4. Переменные окружения приложения (Django)

Без завершающего слэша.

**Вариант A** — все обращения к Hyperunit mainnet идут через Worker:

`HYPERUNIT_MAINNET_API_URL=https://<твой-worker>.workers.dev`

**Вариант B** — базовый URL оставить официальным, при 403 с прямого API повторить через Worker:

`HYPERUNIT_MAINNET_PROXY_URL=https://<твой-worker>.workers.dev`

После изменения env перезапусти процесс (gunicorn, контейнер и т.д.).

## 5. Testnet

1. Скопируй папку воркера или создай второй Worker с другим `name` в `wrangler.toml`.
2. В `src/index.js` замени `UPSTREAM` на `https://api.hyperunit-testnet.xyz`.
3. `npx wrangler@latest deploy`.
4. В приложении: `HYPERUNIT_TESTNET_API_URL=...` или `HYPERUNIT_TESTNET_PROXY_URL=...`.

## Лимиты и аккаунт

На бесплатном плане Cloudflare есть лимиты запросов к Workers; для личного/небольшого приложения обычно достаточно. Нужен аккаунт Cloudflare (бесплатный).

Если Worker отвечает 403 — проверь, что `UPSTREAM` в `index.js` совпадает с нужной сетью (mainnet vs testnet).
