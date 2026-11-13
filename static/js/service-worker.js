var CACHE_NAME = 'avatar-store-v4';
var APP_ASSETS = [
    '/static/manifest.webmanifest',
    '/static/css/main.css',
    '/static/js/cart.js',
    '/static/js/product-detail.js',
    '/static/js/checkout.js',
    '/static/js/order-status.js',
    '/static/js/splash.js',
    '/static/js/pwa.js',
    '/static/images/avatar-mark.svg',
    '/static/images/icon-192.png',
    '/static/images/icon-512.png',
    '/static/offline.html'
];

self.addEventListener('install', function (event) {
    event.waitUntil(caches.open(CACHE_NAME).then(function (cache) {
        return cache.addAll(APP_ASSETS);
    }));
    self.skipWaiting();
});

self.addEventListener('activate', function (event) {
    event.waitUntil(caches.keys().then(function (keys) {
        return Promise.all(keys.filter(function (key) {
            return key.indexOf('avatar-store-') === 0 && key !== CACHE_NAME;
        }).map(function (key) { return caches.delete(key); }));
    }));
    self.clients.claim();
});

self.addEventListener('fetch', function (event) {
    var request = event.request;
    var url = new URL(request.url);
    if (request.method !== 'GET' || url.origin !== self.location.origin) return;

    if (url.pathname.indexOf('/static/') === 0) {
        event.respondWith(caches.match(request).then(function (cached) {
            return cached || fetch(request).then(function (response) {
                if (response.ok) {
                    var copy = response.clone();
                    caches.open(CACHE_NAME).then(function (cache) { cache.put(request, copy); });
                }
                return response;
            });
        }));
        return;
    }

    if (request.mode === 'navigate') {
        event.respondWith(fetch(request).catch(function () {
            return caches.match('/static/offline.html');
        }));
    }
});
