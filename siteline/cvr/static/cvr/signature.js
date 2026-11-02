/*
 * Daywork signature pad. Draws on a <canvas> with a finger, pen or mouse (pointer events), then on
 * submit turns the drawing into a PNG and puts it in the form's ordinary file input, so it goes up
 * through the normal upload path (type and size checks, private storage, malware scan) like any
 * other file. Without script, or without DataTransfer, the file input stays visible for a photo of
 * a signed paper sheet. No inline script: the Content-Security-Policy is unchanged.
 */
(function () {
  'use strict';

  function init(form) {
    var pad = form.querySelector('.sig-pad');
    var canvas = form.querySelector('.sig-canvas');
    var input = form.querySelector('input.sig-file');
    var upload = form.querySelector('.sig-upload');
    if (!pad || !canvas || !input || typeof DataTransfer === 'undefined' || !canvas.toBlob) {
      return;  // keep the plain file input
    }
    try { new DataTransfer(); } catch (e) { return; }
    pad.hidden = false;
    upload.hidden = true;
    input.required = false;

    var ctx = canvas.getContext('2d');
    var drawn = false, drawing = false, last = null;

    function reset() {
      ctx.fillStyle = '#ffffff';
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.strokeStyle = '#d0ccc2';
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(24, canvas.height - 40);
      ctx.lineTo(canvas.width - 24, canvas.height - 40);
      ctx.stroke();
      ctx.strokeStyle = '#15191F';
      ctx.lineWidth = 3;
      ctx.lineCap = 'round';
      ctx.lineJoin = 'round';
      drawn = false;
    }

    function point(e) {
      var r = canvas.getBoundingClientRect();
      return {x: (e.clientX - r.left) * canvas.width / r.width, y: (e.clientY - r.top) * canvas.height / r.height};
    }

    canvas.addEventListener('pointerdown', function (e) {
      e.preventDefault();
      drawing = true;
      last = point(e);
      if (canvas.setPointerCapture) { canvas.setPointerCapture(e.pointerId); }
    });
    canvas.addEventListener('pointermove', function (e) {
      if (!drawing) { return; }
      e.preventDefault();
      var p = point(e);
      ctx.beginPath();
      ctx.moveTo(last.x, last.y);
      ctx.lineTo(p.x, p.y);
      ctx.stroke();
      last = p;
      drawn = true;
    });
    ['pointerup', 'pointercancel', 'pointerleave'].forEach(function (name) {
      canvas.addEventListener(name, function () { drawing = false; });
    });
    form.querySelector('.sig-clear').addEventListener('click', reset);
    reset();

    var sending = false;
    form.addEventListener('submit', function (e) {
      if (sending) { return; }
      if (!drawn) { return; }  // the server says the box is empty
      e.preventDefault();
      canvas.toBlob(function (blob) {
        var dt = new DataTransfer();
        dt.items.add(new File([blob], 'signature.png', {type: 'image/png'}));
        input.files = dt.files;
        sending = true;
        form.submit();
      }, 'image/png');
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    Array.prototype.forEach.call(document.querySelectorAll('form.sig-form'), init);
  });
})();
