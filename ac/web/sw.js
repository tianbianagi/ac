// Makes acc installable. It needs its server to do anything, so nothing is cached: requests go
// to the network, and when the server can't be reached the page says so instead of the browser's
// error screen.
const OFFLINE = `<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>acc</title>
<style>body{font:15px/1.55 -apple-system,system-ui,sans-serif;margin:0;height:100vh;display:grid;
place-items:center;text-align:center;background:#fbfaf8;color:#6f6a62}
@media (prefers-color-scheme:dark){body{background:#1b1a18;color:#9c968c}}
button{font:inherit;margin-top:12px;padding:5px 12px;border-radius:8px;border:1px solid #c8651b;
background:#c8651b;color:#fff}</style>
<div><div>Can't reach acc right now.</div><button onclick="location.reload()">Try again</button></div>`;

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
self.addEventListener("fetch", (event) => {
  if (event.request.mode !== "navigate") return;
  event.respondWith(fetch(event.request).catch(() =>
    new Response(OFFLINE, {headers: {"Content-Type": "text/html; charset=utf-8"}})));
});
