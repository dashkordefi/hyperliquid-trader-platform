/**
 * Прокси к https://api.hyperunit.xyz — обходит 403 с IP датацентров (Render и др.).
 */
const UPSTREAM = "https://api.hyperunit.xyz";

export default {
  async fetch(request) {
    const url = new URL(request.url);
    const target = UPSTREAM + url.pathname + url.search;

    const headers = new Headers();
    headers.set(
      "User-Agent",
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    );
    headers.set("Accept", "application/json");
    headers.set("Origin", "https://app.hyperliquid.xyz");
    headers.set("Referer", "https://app.hyperliquid.xyz/");

    return fetch(target, {
      method: request.method,
      headers,
      body: request.method !== "GET" && request.method !== "HEAD" ? request.body : undefined,
    });
  },
};
