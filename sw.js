self.addEventListener('install', (event) => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  return self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  // Alapértelmezett hálózati kérés átengedése
  event.respondWith(fetch(event.request));
});
