/**
 * Прокси к https://api.hyperunit.xyz — обходит 403 с IP датацентров (VPS, Render и др.).
 * Исходящий fetch к upstream выполняется с сети Cloudflare, не с IP вашего сервера.
 */
const UPSTREAM = "https://api.hyperunit.xyz";

function corsHeaders() {
  return {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Accept",
    "Access-Control-Max-Age": "86400",
  };
}

export default {
  async fetch(request) {
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: corsHeaders() });
    }

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

    let upstream;
    try {
      upstream = await fetch(target, {
        method: request.method,
        headers,
        body:
          request.method !== "GET" && request.method !== "HEAD"
            ? request.body
            : undefined,
      });
    } catch (e) {
      return new Response(
        JSON.stringify({ error: "upstream_fetch_failed", message: String(e) }),
        {
          status: 502,
          headers: { "Content-Type": "application/json", ...corsHeaders() },
        }
      );
    }

    const out = new Headers(upstream.headers);
    for (const [k, v] of Object.entries(corsHeaders())) {
      out.set(k, v);
    }
    return new Response(upstream.body, {
      status: upstream.status,
      statusText: upstream.statusText,
      headers: out,
    });
  },
};
