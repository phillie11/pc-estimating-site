{% load static %}/* Fire roll call service worker (safety/views.py roll_call_sw). It only ever handles the roll call page,
   and the stylesheets and script that page loads: network first for the page, so the list is current when
   there is a signal, and the last copy when there isn't. */
'use strict';
var CACHE = '{{ cache|escapejs }}';
var PAGE = '{{ page|escapejs }}';
var ASSETS = ['{% static "css/app.css" %}', '{% static "fonts/fonts.css" %}', '{% static "safety/safety.css" %}',
              '{% static "safety/safety.js" %}', '{% static "js/app.js" %}'];

self.addEventListener('install', function (event) {
  event.waitUntil(caches.open(CACHE).then(function (cache) { return cache.addAll(ASSETS); })
    .then(function () { return self.skipWaiting(); }));
});

self.addEventListener('activate', function (event) {
  event.waitUntil(self.clients.claim());
});

self.addEventListener('fetch', function (event) {
  var request = event.request;
  if (request.method !== 'GET') return;
  var url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname === PAGE) {
    event.respondWith(fetch(request).then(function (response) {
      if (response.ok && !response.redirected) {
        var copy = response.clone();
        caches.open(CACHE).then(function (cache) { cache.put(PAGE, copy); });
      }
      return response;
    }).catch(function () {
      return caches.match(PAGE).then(function (cached) { return cached || Response.error(); });
    }));
    return;
  }
  if (ASSETS.indexOf(url.pathname) !== -1) {
    event.respondWith(fetch(request).catch(function () { return caches.match(url.pathname); }));
  }
});
