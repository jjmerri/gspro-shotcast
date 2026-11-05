// The stats page's alerts (docs/tracking.md#alerts): shows the booth's pushes, and opens the page at
// the alert's link when one is tapped. It doesn't touch the page's requests.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil(self.clients.claim()));

self.addEventListener("push", e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (err) { d = { body: e.data ? e.data.text() : "" }; }
  e.waitUntil(self.registration.showNotification(d.title || "Shotcast", {
    body: d.body || "", tag: d.tag || undefined, icon: "icons/icon-192.png", badge: "icons/icon-192.png",
    data: { url: d.url || "" } }));
});

self.addEventListener("notificationclick", e => {
  e.notification.close();
  const link = (e.notification.data && e.notification.data.url) || "";
  const url = new URL(link.startsWith("#") ? "./" + link : "./", self.registration.scope).href;
  e.waitUntil((async () => {
    const open = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const c of open) {
      if (!c.url.startsWith(self.registration.scope)) continue;
      try { await c.focus(); } catch (err) {}
      c.postMessage({ open: link });   // the page follows the link itself (its hash routes)
      return;
    }
    await self.clients.openWindow(url);
  })());
});
