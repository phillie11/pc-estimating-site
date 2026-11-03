// The Print button on a permit's printable page (no inline script: the CSP only allows files from this site)
document.addEventListener('click', function (e) {
  if (e.target.closest('[data-print]')) window.print();
});
