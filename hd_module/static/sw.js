/* HD Smart Monitoring — Service Worker (PWA)  v3.0 (14 Nov 2026)
 *
 * Why v3: the installed desktop app stopped opening. v2 pre-cached HTML pages
 * (cache.addAll of /hd/dashboard …) — when the user was logged out those
 * requests were 302 → /hd/login and the *redirected* response was stored.
 * Serving a redirected response to a later navigation is a network error in
 * Chrome/Edge ("redirected response was used for a request whose redirect
 * mode is not follow") → blank window / app would not open. It also served
 * stale HTML first (stale-while-revalidate) after every deploy.
 *
 * Rules now:
 *   - navigations (HTML)  → network only, offline → small inline page
 *   - /hd/api/            → network only (clinical data must never be stale)
 *   - /hd/static/ assets  → cache-first with background refresh, never redirected responses
 *   - everything else     → network
 */
const CACHE_NAME = 'hd-monitor-v3.0';

self.addEventListener('install', (e) => {
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then(keys => Promise.all(keys.map(k => caches.delete(k))))   // drop every old cache (v2.x)
      .then(() => self.clients.claim())
  );
});

const OFFLINE_HTML = `<!doctype html><html lang="th"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ออฟไลน์ — HD Smart Monitoring</title>
<body style="font-family:system-ui,sans-serif;background:#0d1117;color:#e6edf3;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;text-align:center;padding:24px">
<div><div style="font-size:48px">📡</div><h2 style="margin:8px 0">ไม่มีการเชื่อมต่ออินเทอร์เน็ต</h2>
<p style="color:#9fb0c3">HD Smart Monitoring ต้องเชื่อมต่อเซิร์ฟเวอร์ · บันทึกที่กรอกค้างไว้ในหน้า Session ยังอยู่ในเครื่องและจะส่งเองเมื่อเน็ตกลับมา</p>
<button onclick="location.reload()" style="margin-top:12px;padding:10px 18px;border:0;border-radius:8px;background:#2980b9;color:#fff;font-size:16px">ลองใหม่</button></div></body></html>`;

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // 1) Page navigations: always from the network (login redirects, fresh templates)
  if (req.mode === 'navigate') {
    e.respondWith(fetch(req).catch(() => new Response(OFFLINE_HTML, {status: 503, headers: {'Content-Type': 'text/html; charset=utf-8'}})));
    return;
  }
  // 2) API: network only
  if (url.pathname.startsWith('/hd/api/')) return;

  // 3) Static assets under /hd/static/: cache-first, refresh in background
  if (url.pathname.startsWith('/hd/static/') && !url.pathname.endsWith('sw.js')) {
    e.respondWith(
      caches.open(CACHE_NAME).then(async (cache) => {
        const cached = await cache.match(req);
        const refresh = fetch(req).then(resp => {
          if (resp && resp.ok && !resp.redirected) cache.put(req, resp.clone());
          return resp;
        }).catch(() => cached);
        return cached || refresh;
      })
    );
  }
  // 4) anything else → default network behaviour
});
