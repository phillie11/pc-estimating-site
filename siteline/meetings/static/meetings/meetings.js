// Print buttons on the look-ahead and the minutes (no inline script: the Content-Security-Policy forbids it)
(function () {
  document.addEventListener('click', function (e) {
    var b = e.target.closest('[data-print]');
    if (b) { e.preventDefault(); window.print(); }
  });
})();
