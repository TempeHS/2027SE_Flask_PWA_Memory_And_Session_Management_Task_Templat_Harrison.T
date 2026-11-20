/**
 * Service worker: lets the app install as a PWA and show an offline page.
 * README Step 10 changes what it caches and when old caches are deleted.
 */

const CACHE_NAME = "canteen-v1";
const STATIC_ASSETS = [
  "/offline.html",
  "/static/css/bootstrap.min.css",
  "/static/css/style.css",
  "/static/js/bootstrap.bundle.min.js",
  "/static/js/app.js",
  "/static/images/logo.svg",
  "/static/images/favicon.png",
  "/static/manifest.json",
  "/static/icons/icon-192x192.png",
  "/static/icons/icon-512x512.png",
];

/**
 * Install: download and cache the static files the app needs offline.
 */
self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE_NAME)
      .then((cache) => cache.addAll(STATIC_ASSETS))
      .then(() => self.skipWaiting()),
  );
});

/**
 * Activate: runs when a new version of this file takes over.
 * README Step 10 makes it delete caches left by older versions.
 */
self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

/**
 * Fetch: runs for every request a page makes.
 * README Step 10 limits caching to static files and the offline page.
 */
self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") return;
  event.respondWith(
    fetch(event.request)
      .then((response) => {
        const copy = response.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
        return response;
      })
      .catch(() =>
        caches
          .match(event.request)
          .then((cached) => cached || caches.match("/offline.html")),
      ),
  );
});
