# Hyperunit proxy (Cloudflare Worker)

Если в браузере Cloudflare **не даёт править код** в шаблоне Hello World, деплой делается **с компьютера** через Wrangler (официальный CLI). Node ставится один раз с сайта — отдельный `npm create` не обязателен.

## 1. Установить Node.js

Скачай **LTS** с https://nodejs.org и установи. Закрой и снова открой терминал.

Проверка:

```bash
node -v
npx --version
```

## 2. Перейти в эту папку

Из корня репозитория:

```bash
cd scripts/hyperunit-proxy-wrangler
```

## 3. Войти в Cloudflare

```bash
npx wrangler@latest login
```

Откроется браузер — разреши доступ аккаунту Cloudflare.

## 4. Задеплоить Worker

```bash
npx wrangler@latest deploy
```

В конце будет URL вида `https://hyperunit-proxy.<твой-поддомен>.workers.dev`.  
Если имя `hyperunit-proxy` уже занято — переименуй в `wrangler.toml` поле `name` и снова `deploy`.

## 5. Render

В Environment сервиса:

`HYPERUNIT_MAINNET_API_URL=https://<то-что-вывел-wrangler>.workers.dev`  

без слэша в конце.

## 6. Проверка

В браузере:

`https://<твой-worker>.workers.dev/gen/ethereum/hyperliquid/eth/0xТВОЙ_HL_АДРЕС`

Должен быть JSON с `"status":"OK"`.

## Testnet

В `src/index.js` замени `UPSTREAM` на `https://api.hyperunit-testnet.xyz`, задеплой второй воркер (другое `name` в `wrangler.toml`) и задай `HYPERUNIT_TESTNET_API_URL`.
