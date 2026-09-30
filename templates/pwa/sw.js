{% load static %}// Service worker for the installable app. Pages are always fetched live
// (they show per-user points and forms); only static assets are cached, and
// a small offline page is shown when the network is unreachable.
const VERSION = "v2";
const CACHE = "winsports-" + VERSION;
const OFFLINE_URL = "{% url 'offline' %}";
const PRECACHE = [
  OFFLINE_URL,
  "{% static 'predictions/icons/icon-192.png' %}",
  "{% static 'predictions/icons/favicon-32.png' %}",
  "https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css",
  "https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js",
];
const STATIC_PREFIX = "{{ static_url }}";
const CDN_PREFIX = "https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/";

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(PRECACHE)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((key) => key.startsWith("winsports-") && key !== CACHE)
          .map((key) => caches.delete(key))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;

  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request).catch(() => caches.match(OFFLINE_URL))
    );
    return;
  }

  const url = new URL(request.url);
  const isStatic = url.origin === self.location.origin && url.pathname.startsWith(STATIC_PREFIX);
  if (isStatic || request.url.startsWith(CDN_PREFIX)) {
    event.respondWith(
      caches.match(request).then((cached) => cached || fetch(request).then((response) => {
        if (response.ok) {
          const copy = response.clone();
          caches.open(CACHE).then((cache) => cache.put(request, copy));
        }
        return response;
      }))
    );
  }
});
