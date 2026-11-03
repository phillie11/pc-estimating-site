/* Safety pages: print buttons, and the fire roll call (ticks, tally, and keeping a copy for when there's no signal). */
(function () {
  'use strict';

  document.addEventListener('click', function (e) {
    if (e.target.closest && e.target.closest('[data-print]')) {
      e.preventDefault();
      window.print();
    }
  });

  var page = document.querySelector('[data-roll-call]');
  if (!page) return;

  var key = page.getAttribute('data-key');
  var boxes = Array.prototype.slice.call(page.querySelectorAll('[data-person]'));
  var tally = page.querySelector('[data-tally]');

  function load() {
    try { return JSON.parse(window.localStorage.getItem(key) || '[]'); } catch (err) { return []; }
  }
  function save(ids) {
    try { window.localStorage.setItem(key, JSON.stringify(ids)); } catch (err) { /* private browsing: ticks just aren't kept */ }
  }
  function update() {
    var done = boxes.filter(function (b) { return b.checked; });
    if (tally && boxes.length) {
      tally.textContent = 'Accounted for: ' + done.length + ' of ' + boxes.length + '.';
    }
    save(done.map(function (b) { return b.value; }));
  }

  var ticked = load();
  boxes.forEach(function (b) {
    b.checked = ticked.indexOf(b.value) !== -1;
    b.addEventListener('change', update);
  });
  update();

  var reset = page.querySelector('[data-reset]');
  if (reset) {
    reset.addEventListener('click', function () {
      boxes.forEach(function (b) { b.checked = false; });
      update();
    });
  }

  var offline = page.querySelector('[data-offline]');
  function showOffline() { if (offline) offline.hidden = navigator.onLine !== false; }
  window.addEventListener('online', showOffline);
  window.addEventListener('offline', showOffline);
  showOffline();

  // Keep a copy of this page on the phone, so it still opens at the assembly point with no signal
  if ('serviceWorker' in navigator && 'caches' in window && window.isSecureContext) {
    var swUrl = page.getAttribute('data-sw');
    var cacheName = page.getAttribute('data-cache');
    var scope = window.location.pathname;
    navigator.serviceWorker.register(swUrl, { scope: scope }).then(function () {
      return navigator.serviceWorker.ready;
    }).then(function () {
      if (navigator.onLine === false) return null;
      return caches.open(cacheName).then(function (cache) { return cache.add(scope); });
    }).then(function () {
      var saved = page.querySelector('[data-saved]');
      if (saved && navigator.onLine !== false) saved.hidden = false;
    }).catch(function () { /* no offline copy: the page still works while there's a signal */ });
  }
})();
