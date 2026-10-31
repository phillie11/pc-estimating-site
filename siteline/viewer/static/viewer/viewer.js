/*
 * Drawing viewer: draws one page of a PDF revision with pdf.js (self-hosted, static/vendor/pdfjs)
 * and the snag/RFI pins on it. Zoom with + and −, Ctrl + wheel or a pinch; move around by
 * scrolling, dragging with the mouse or with one finger. Tap and hold (or "Add pin", then tap)
 * to place a pin. Positions are fractions of the page (0 to 1), so they don't depend on zoom.
 *
 * No inline script and no eval: the page passes its settings in a JSON block (json_script), and
 * pdf.js is told isEvalSupported: false, so the strict Content-Security-Policy holds.
 */
const root = document.getElementById('dv');
if (root) {
  init().catch(function (err) {
    if (window.console) console.error(err);
    status('The drawing could not be shown here. Use Download to open it instead.', true);
  });
}

function $(sel, from) { return (from || document).querySelector(sel); }

function status(text, isError) {
  const el = $('[data-status]', root);
  if (!el) return;
  el.hidden = !text;
  el.textContent = text || '';
  el.classList.toggle('error', !!isError);
}

async function init() {
  const cfg = JSON.parse(document.getElementById('dv-config').textContent);
  const port = $('[data-port]', root);
  const stage = $('[data-stage]', root);
  const pinLayer = $('[data-pins]', root);
  let canvas = $('[data-canvas]', root);
  const dialog = $('[data-dialog]');
  const csrf = ($('[data-csrf] input[name=csrfmiddlewaretoken]') || {}).value || '';

  const MIN_ZOOM = 0.25, MAX_ZOOM = 8, LONG_PRESS_MS = 550, MOVE_SLOP = 10;
  const MAX_CANVAS_PIXELS = 4096 * 4096;  // phones run out of canvas memory above this
  let pdf = null, page = null, base = null, pageNum = 1, zoom = 1, renderTask = null, focusId = String(cfg.focus || '');
  let placing = false, tempPin = null, pendingPoint = null;

  // ---- load ---------------------------------------------------------------
  const pdfjs = await import(cfg.pdfjs);
  pdfjs.GlobalWorkerOptions.workerSrc = cfg.worker;
  const resp = await fetch(cfg.pdf_url, { credentials: 'same-origin', headers: { Accept: 'application/pdf' } });
  if (resp.status === 409) { status('This file is still being virus-checked. Try again in a minute.'); return; }
  if (resp.status === 403) { status('This file has been blocked and cannot be shown.', true); return; }
  if (!resp.ok) { status('The drawing could not be loaded. Use Download to open it instead.', true); return; }
  const data = new Uint8Array(await resp.arrayBuffer());
  pdf = await pdfjs.getDocument({ data: data, isEvalSupported: false, enableXfa: false,
                                  standardFontDataUrl: cfg.fonts }).promise;
  $('[data-pages]', root).textContent = pdf.numPages;
  const focusPin = cfg.pins.find(function (p) { return String(p.id) === focusId; });
  if (focusPin && focusPin.page <= pdf.numPages) pageNum = focusPin.page;
  await showPage(pageNum);
  status('');
  if (focusPin) centreOn(focusPin);

  // ---- drawing ------------------------------------------------------------
  async function showPage(n) {
    pageNum = Math.min(Math.max(1, n), pdf.numPages);
    page = await pdf.getPage(pageNum);
    base = page.getViewport({ scale: 1 });
    $('[data-page]', root).textContent = pageNum;
    $('[data-act=prev]', root).disabled = pageNum <= 1;
    $('[data-act=next]', root).disabled = pageNum >= pdf.numPages;
    await render();
    port.scrollTop = 0;
  }

  function fitScale() {
    const style = getComputedStyle(port);
    const inner = port.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
    return Math.max(0.05, inner / base.width);
  }

  async function render(keep) {
    const vp = page.getViewport({ scale: fitScale() * zoom });
    let px = Math.min(window.devicePixelRatio || 1, 3);
    if (vp.width * vp.height * px * px > MAX_CANVAS_PIXELS) px = Math.sqrt(MAX_CANVAS_PIXELS / (vp.width * vp.height));
    const c = document.createElement('canvas');
    c.width = Math.floor(vp.width * px);
    c.height = Math.floor(vp.height * px);
    c.style.width = vp.width + 'px';
    c.style.height = vp.height + 'px';
    c.setAttribute('data-canvas', '');
    c.setAttribute('role', 'img');
    c.setAttribute('aria-label', 'Page ' + pageNum + ' of the drawing');
    if (renderTask) renderTask.cancel();
    const task = page.render({ canvasContext: c.getContext('2d'), viewport: vp,
                               transform: px !== 1 ? [px, 0, 0, px, 0, 0] : null });
    renderTask = task;
    try {
      await task.promise;
    } catch (e) {
      if (e && e.name === 'RenderingCancelledException') return;
      throw e;
    }
    if (renderTask !== task) return;
    renderTask = null;
    stage.style.transform = '';
    stage.style.width = vp.width + 'px';
    stage.style.height = vp.height + 'px';
    canvas.replaceWith(c);
    canvas = c;
    $('[data-zoom]', root).textContent = Math.abs(zoom - 1) < 0.01 ? 'Fit' : Math.round(zoom * 100) + '%';
    drawPins();
    if (keep) {
      port.scrollLeft = keep.fx * vp.width + stage.offsetLeft - keep.cx;
      port.scrollTop = keep.fy * vp.height + stage.offsetTop - keep.cy;
    }
  }

  function drawPins() {
    pinLayer.textContent = '';
    cfg.pins.forEach(function (p) {
      if (p.page !== pageNum) return;
      const a = document.createElement('a');
      a.className = 'dv-pin' + (p.placed_on ? ' other' : '') + (String(p.id) === focusId ? ' focus' : '');
      a.href = p.url;
      a.style.left = (p.x * 100) + '%';
      a.style.top = (p.y * 100) + '%';
      a.dataset.pin = p.id;
      const label = p.ref + ' ' + p.title + ' · ' + p.status + (p.placed_on ? ' · ' + p.placed_on : '');
      a.title = label;
      a.setAttribute('aria-label', p.kind_label + ' ' + label);
      const dot = document.createElement('span');
      dot.className = 'dv-dot tone-' + p.tone;
      const tag = document.createElement('span');
      tag.className = 'dv-tag';
      tag.textContent = p.ref + (p.placed_on ? ' · ' + p.placed_on : '');
      a.appendChild(dot);
      a.appendChild(tag);
      pinLayer.appendChild(a);
    });
    if (tempPin) pinLayer.appendChild(tempPin);
  }

  function centreOn(p) {
    port.scrollLeft = p.x * stage.offsetWidth + stage.offsetLeft - port.clientWidth / 2;
    port.scrollTop = p.y * stage.offsetHeight + stage.offsetTop - port.clientHeight / 2;
  }

  // Zoom about a point in the port (client coordinates), keeping that spot of the drawing still
  function zoomTo(z, clientX, clientY) {
    z = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, z));
    if (Math.abs(z - zoom) < 0.001) return;
    const r = port.getBoundingClientRect();
    const cx = clientX === undefined ? port.clientWidth / 2 : clientX - r.left;
    const cy = clientY === undefined ? port.clientHeight / 2 : clientY - r.top;
    const keep = { cx: cx, cy: cy,
                   fx: (port.scrollLeft + cx - stage.offsetLeft) / stage.offsetWidth,
                   fy: (port.scrollTop + cy - stage.offsetTop) / stage.offsetHeight };
    zoom = z;
    render(keep);
  }

  // ---- toolbar ------------------------------------------------------------
  root.addEventListener('click', function (e) {
    const btn = e.target.closest('[data-act]');
    if (!btn || !root.contains(btn)) return;
    const act = btn.dataset.act;
    if (act === 'prev') showPage(pageNum - 1);
    else if (act === 'next') showPage(pageNum + 1);
    else if (act === 'in') zoomTo(zoom * 1.25);
    else if (act === 'out') zoomTo(zoom / 1.25);
    else if (act === 'fit') { zoom = 1; render(); }
    else if (act === 'pin') setPlacing(!placing);
  });

  function setPlacing(on) {
    placing = on;
    root.classList.toggle('placing', on);
    const b = $('[data-act=pin]', root);
    if (b) { b.setAttribute('aria-pressed', on ? 'true' : 'false'); b.textContent = on ? 'Tap the drawing…' : 'Add pin'; }
  }

  port.addEventListener('keydown', function (e) {
    if (e.key === '+' || e.key === '=') { zoomTo(zoom * 1.25); e.preventDefault(); }
    else if (e.key === '-' || e.key === '_') { zoomTo(zoom / 1.25); e.preventDefault(); }
    else if (e.key === 'PageDown') { showPage(pageNum + 1); e.preventDefault(); }
    else if (e.key === 'PageUp') { showPage(pageNum - 1); e.preventDefault(); }
  });

  port.addEventListener('wheel', function (e) {
    if (!e.ctrlKey && !e.metaKey) return;
    e.preventDefault();
    zoomTo(zoom * (e.deltaY < 0 ? 1.15 : 1 / 1.15), e.clientX, e.clientY);
  }, { passive: false });

  let resizeTimer = null, lastWidth = port.clientWidth;
  window.addEventListener('resize', function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () {
      if (port.clientWidth !== lastWidth) { lastWidth = port.clientWidth; render(); }
    }, 150);
  });

  // ---- pointer: drag to pan (mouse), tap and hold to pin (any) ------------
  let press = null, suppressClick = false;
  stage.addEventListener('contextmenu', function (e) { e.preventDefault(); });

  stage.addEventListener('pointerdown', function (e) {
    if (e.button !== 0 || e.target.closest('.dv-pin') || !e.isPrimary) return;
    press = { x: e.clientX, y: e.clientY, sl: port.scrollLeft, st: port.scrollTop, type: e.pointerType,
              moved: false, timer: null };
    if (cfg.can_pin) {
      press.timer = setTimeout(function () {
        if (!press || press.moved) return;
        suppressClick = true;
        placeAt(press.x, press.y);
        press = null;
      }, LONG_PRESS_MS);
    }
  });
  window.addEventListener('pointermove', function (e) {
    if (!press || !e.isPrimary) return;
    const dx = e.clientX - press.x, dy = e.clientY - press.y;
    if (!press.moved && Math.hypot(dx, dy) > MOVE_SLOP) {
      press.moved = true;
      clearTimeout(press.timer);
      if (press.type === 'mouse') root.classList.add('dragging');
    }
    if (press.moved && press.type === 'mouse') {
      port.scrollLeft = press.sl - dx;
      port.scrollTop = press.st - dy;
    }
  });
  function endPress() {
    if (press) {
      clearTimeout(press.timer);
      if (press.moved) suppressClick = true;
    }
    press = null;
    root.classList.remove('dragging');
  }
  window.addEventListener('pointerup', endPress);
  window.addEventListener('pointercancel', function () { if (press) clearTimeout(press.timer); press = null; root.classList.remove('dragging'); });

  stage.addEventListener('click', function (e) {
    if (suppressClick) { suppressClick = false; e.preventDefault(); return; }
    if (placing && !e.target.closest('.dv-pin')) {
      e.preventDefault();
      placeAt(e.clientX, e.clientY);
    }
  }, true);

  // ---- pinch to zoom (touch) ----------------------------------------------
  let pinch = null;
  function dist(t) { return Math.hypot(t[0].clientX - t[1].clientX, t[0].clientY - t[1].clientY); }
  port.addEventListener('touchstart', function (e) {
    if (e.touches.length !== 2) return;
    if (press) { clearTimeout(press.timer); press = null; }
    const r = port.getBoundingClientRect();
    const mx = (e.touches[0].clientX + e.touches[1].clientX) / 2, my = (e.touches[0].clientY + e.touches[1].clientY) / 2;
    pinch = { d0: dist(e.touches), ratio: 1, cx: mx - r.left, cy: my - r.top,
              ox: port.scrollLeft + mx - r.left - stage.offsetLeft, oy: port.scrollTop + my - r.top - stage.offsetTop };
    stage.style.transformOrigin = pinch.ox + 'px ' + pinch.oy + 'px';
  }, { passive: true });
  port.addEventListener('touchmove', function (e) {
    if (!pinch || e.touches.length !== 2) return;
    e.preventDefault();
    const z = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, zoom * dist(e.touches) / pinch.d0));
    pinch.ratio = z / zoom;
    stage.style.transform = 'scale(' + pinch.ratio + ')';
  }, { passive: false });
  port.addEventListener('touchend', function (e) {
    if (!pinch || e.touches.length >= 2) return;
    const p = pinch;
    pinch = null;
    suppressClick = true;
    setTimeout(function () { suppressClick = false; }, 400);
    stage.style.transformOrigin = '0 0';
    const keep = { cx: p.cx, cy: p.cy, fx: p.ox / stage.offsetWidth, fy: p.oy / stage.offsetHeight };
    zoom = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, zoom * p.ratio));
    render(keep);
  });

  // ---- placing a pin ------------------------------------------------------
  function placeAt(clientX, clientY) {
    if (!cfg.can_pin || !dialog) return;
    const r = canvas.getBoundingClientRect();
    const x = (clientX - r.left) / r.width, y = (clientY - r.top) / r.height;
    if (!(x >= 0 && x <= 1 && y >= 0 && y <= 1)) return;
    setPlacing(false);
    pendingPoint = { page: pageNum, x: x.toFixed(6), y: y.toFixed(6) };
    tempPin = document.createElement('span');
    tempPin.className = 'dv-pin temp';
    tempPin.style.left = (x * 100) + '%';
    tempPin.style.top = (y * 100) + '%';
    tempPin.innerHTML = '<span class="dv-dot"></span>';
    pinLayer.appendChild(tempPin);
    const q = new URLSearchParams({ pin_doc: cfg.doc, pin_rev: cfg.rev, pin_page: pageNum,
                                    pin_x: pendingPoint.x, pin_y: pendingPoint.y }).toString();
    dialog.querySelectorAll('[data-new]').forEach(function (a) { a.href = a.dataset.new + '?' + q; });
    $('[data-where]', dialog).textContent = 'Page ' + pageNum + (pdf.numPages > 1 ? ' of ' + pdf.numPages : '') +
      '. A new snag or RFI opens its usual form with this drawing filled in.';
    const err = $('[data-error]', dialog);
    err.hidden = true;
    if (typeof dialog.showModal === 'function') dialog.showModal(); else dialog.setAttribute('open', '');
  }

  if (dialog) {
    dialog.addEventListener('close', function () {
      if (tempPin) { tempPin.remove(); tempPin = null; }
    });
    const link = $('[data-act=link]', dialog);
    if (link) link.addEventListener('click', async function () {
      const err = $('[data-error]', dialog);
      const record = $('[data-existing]', dialog).value;
      if (!record) { err.textContent = 'Choose a snag or RFI from the list first.'; err.hidden = false; return; }
      link.disabled = true;
      try {
        const body = new URLSearchParams({ csrfmiddlewaretoken: csrf, rev: cfg.rev, record: record,
                                           page: pendingPoint.page, x: pendingPoint.x, y: pendingPoint.y });
        const r = await fetch(cfg.pin_url, { method: 'POST', body: body, credentials: 'same-origin',
                                             headers: { 'X-CSRFToken': csrf, Accept: 'application/json' } });
        const j = await r.json().catch(function () { return {}; });
        if (!r.ok) throw new Error(j.error || 'The pin could not be saved. Reload the page and try again.');
        window.location.assign(window.location.pathname + '?pin=' + encodeURIComponent(j.id));
      } catch (ex) {
        err.textContent = ex.message;
        err.hidden = false;
        link.disabled = false;
      }
    });
  }

  // ---- the list of pins under the drawing ----------------------------------
  document.addEventListener('click', async function (e) {
    const show = e.target.closest('[data-show]');
    if (show) {
      const p = cfg.pins.find(function (x) { return String(x.id) === show.dataset.show; });
      if (!p) return;
      focusId = String(p.id);
      if (p.page !== pageNum && p.page <= pdf.numPages) await showPage(p.page); else drawPins();
      root.scrollIntoView({ behavior: 'smooth', block: 'start' });
      centreOn(p);
      return;
    }
    const rm = e.target.closest('[data-remove]');
    if (rm) {
      if (!window.confirm('Remove this pin? The snag or RFI itself stays as it is.')) return;
      rm.disabled = true;
      const r = await fetch(rm.dataset.url, { method: 'POST', credentials: 'same-origin',
                                              body: new URLSearchParams({ csrfmiddlewaretoken: csrf }),
                                              headers: { 'X-CSRFToken': csrf, Accept: 'application/json' } });
      if (r.ok) window.location.assign(window.location.pathname);
      else { rm.disabled = false; window.alert('The pin could not be removed. Reload the page and try again.'); }
    }
  });
}
