// Stash service worker: shows push notifications and sends taps back to the app. Put this file in your frontend's public/ folder (it must be served at /sw.js).
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil(self.clients.claim()));

self.addEventListener("push", e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch { d = { title: "Stash", body: e.data ? e.data.text() : "" }; }
  e.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    wins.forEach(w => w.postMessage({ type: "push" }));  // an open app refreshes its alerts and balance
    await self.registration.showNotification(d.title || "Stash", {  // always show one: iPhones and Chrome require it
      body: d.body || "", tag: d.tag || undefined, data: { url: d.url || "/" }, icon: "/apple-touch-icon.png", badge: "/apple-touch-icon.png",
    });
  })());
});

self.addEventListener("notificationclick", e => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || "/";
  e.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    if (wins[0]) { await wins[0].focus(); wins[0].postMessage({ type: "open", url }); }  // the app opens the screen the notification points at
    else await self.clients.openWindow(url);
  })());
});
