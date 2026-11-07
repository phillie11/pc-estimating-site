/*
 * Revision compare: two revisions of a drawing, drawn in the browser by pdf.js (self-hosted).
 *
 * Side by side: both pages at the same zoom; scrolling or zooming one moves the other.
 * Overlay: both pages drawn at the same width and combined pixel by pixel: ink only on the older
 * revision shows red, ink only on the newer shows blue, ink on both stays black. The fade slider
 * weakens one side (left: older only, right: newer only).
 * "Cloud a change" (overlay) draws a revision cloud straight onto the person's own mark-up layer on
 * the newer revision, through the viewer's markup_add endpoint.
 *
 * As viewer.js: no inline script and no eval; settings arrive in a json_script block.
 */
const root = document.getElementById('dvc');
if (root) {
  init().catch(function (err) {
    if (window.console) console.error(err);
    status('The revisions could not be shown here. Open each one in the viewer instead.', true);
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

async function load(pdfjs, cfg, side) {
  const resp = await fetch(cfg[side].pdf, { credentials: 'same-origin', headers: { Accept: 'application/pdf' } });
  if (resp.status === 409) throw new Error('Rev ' + cfg[side].label + ' is still being virus-checked. Try again in a minute.');
  if (!resp.ok) throw new Error('Rev ' + cfg[side].label + ' could not be loaded.');
  const data = new Uint8Array(await resp.arrayBuffer());
  return pdfjs.getDocument({ data: data, isEvalSupported: false, enableXfa: false, standardFontDataUrl: cfg.fonts }).promise;
}

async function init() {
  const cfg = JSON.parse(document.getElementById('dvc-config').textContent);
  const csrf = ($('[data-csrf] input[name=csrfmiddlewaretoken]') || {}).value || '';
  const MIN_ZOOM = 0.25, MAX_ZOOM = 6, SIDE_PIXELS = 4096 * 4096, OVERLAY_PIXELS = 2400 * 2400;
  const ports = { a: $('[data-port=a]', root), b: $('[data-port=b]', root), o: $('[data-port=o]', root) };
  const stages = { a: $('[data-stage=a]', root), b: $('[data-stage=b]', root), o: $('[data-stage=o]', root) };
  const ink = $('[data-ink]', root);
  const fade = $('[data-fade]', root);
  let mode = 'side', pageNum = 1, zoom = 1, clouding = false, mineShapes = [];
  let syncing = false;
  let over = null;  // the overlay's darkness maps, kept so the fade slider needn't re-render

  const pdfjs = await import(cfg.pdfjs);
  pdfjs.GlobalWorkerOptions.workerSrc = cfg.worker;
  const mk = await import(cfg.markup_js);
  let docs;
  try {
    docs = { a: await load(pdfjs, cfg, 'a'), b: await load(pdfjs, cfg, 'b') };
  } catch (e) {
    status(e.message, true);
    return;
  }
  if (cfg.mine_url) {
    try {
      const r = await fetch(cfg.mine_url, { credentials: 'same-origin', headers: { Accept: 'application/json' } });
      if (r.ok) mineShapes = (await r.json()).shapes || [];
    } catch (e) { /* shown without them */ }
  }
  const pages = Math.max(docs.a.numPages, docs.b.numPages);
  $('[data-pages]', root).textContent = pages;
  status('');
  await draw();

  async function getPage(side) {
    return pageNum <= docs[side].numPages ? docs[side].getPage(pageNum) : null;
  }

  function inner(port) {
    const st = getComputedStyle(port);
    return port.clientWidth - parseFloat(st.paddingLeft) - parseFloat(st.paddingRight);
  }

  function canvasFor(vp, cap) {
    let px = Math.min(window.devicePixelRatio || 1, 3);
    if (vp.width * vp.height * px * px > cap) px = Math.sqrt(cap / (vp.width * vp.height));
    const c = document.createElement('canvas');
    c.width = Math.max(1, Math.floor(vp.width * px));
    c.height = Math.max(1, Math.floor(vp.height * px));
    c.style.width = vp.width + 'px';
    c.style.height = vp.height + 'px';
    return { c: c, px: px };
  }

  async function renderTo(page, vp, cap) {
    const out = canvasFor(vp, cap);
    const ctx = out.c.getContext('2d');
    ctx.fillStyle = '#fff';
    ctx.fillRect(0, 0, out.c.width, out.c.height);
    await page.render({ canvasContext: ctx, viewport: vp,
                        transform: out.px !== 1 ? [out.px, 0, 0, out.px, 0, 0] : null }).promise;
    return out.c;
  }

  function place(stage, c, label) {
    stage.style.transform = '';
    stage.textContent = '';
    stage.style.width = c.style.width;
    stage.style.height = c.style.height;
    c.setAttribute('role', 'img');
    c.setAttribute('aria-label', label);
    stage.appendChild(c);
  }

  async function draw(keep) {
    $('[data-page]', root).textContent = pageNum;
    $('[data-act=prev]', root).disabled = pageNum <= 1;
    $('[data-act=next]', root).disabled = pageNum >= pages;
    $('[data-zoom]', root).textContent = Math.abs(zoom - 1) < 0.01 ? 'Fit' : Math.round(zoom * 100) + '%';
    if (mode === 'side') await drawSide(keep); else await drawOverlay(keep);
  }

  async function drawSide(keep) {
    for (const side of ['a', 'b']) {
      const page = await getPage(side);
      if (!page) {
        stages[side].textContent = 'Rev ' + cfg[side].label + ' has no page ' + pageNum + '.';
        stages[side].style.width = '';
        stages[side].style.height = '';
        continue;
      }
      const base = page.getViewport({ scale: 1 });
      const vp = page.getViewport({ scale: Math.max(0.05, inner(ports[side]) / base.width) * zoom });
      place(stages[side], await renderTo(page, vp, SIDE_PIXELS), 'Rev ' + cfg[side].label + ', page ' + pageNum);
    }
    if (keep) { syncing = true; setFraction(ports.a, keep); setFraction(ports.b, keep); syncing = false; }
  }

  async function drawOverlay(keep) {
    const pa = await getPage('a'), pb = await getPage('b');
    const width = inner(ports.o) * zoom;
    const views = {};
    for (const [side, page] of [['a', pa], ['b', pb]]) {
      if (page) views[side] = page.getViewport({ scale: width / page.getViewport({ scale: 1 }).width });
    }
    const height = Math.max(views.a ? views.a.height : 0, views.b ? views.b.height : 0);
    const frame = canvasFor({ width: width, height: height }, OVERLAY_PIXELS);
    const W = frame.c.width, H = frame.c.height, px = frame.px;
    async function darkness(side, page) {
      const dark = new Uint8Array(W * H);
      if (!page) return dark;
      const c = document.createElement('canvas');
      c.width = W;
      c.height = H;
      const ctx = c.getContext('2d', { willReadFrequently: true });
      ctx.fillStyle = '#fff';
      ctx.fillRect(0, 0, W, H);
      await page.render({ canvasContext: ctx, viewport: views[side], transform: [px, 0, 0, px, 0, 0] }).promise;
      const d = ctx.getImageData(0, 0, W, H).data;
      for (let i = 0, j = 0; j < dark.length; i += 4, j++) {
        dark[j] = 255 - ((d[i] * 77 + d[i + 1] * 150 + d[i + 2] * 29) >> 8);
      }
      return dark;
    }
    over = { a: await darkness('a', pa), b: await darkness('b', pb), canvas: frame.c,
             w: width, hb: views.b ? views.b.height : height };
    composite();
    place(stages.o, frame.c, 'Rev ' + cfg.a.label + ' and rev ' + cfg.b.label + ' overlaid, page ' + pageNum);
    if (ink) {
      ink.width = Math.floor(over.w * px);
      ink.height = Math.floor(over.hb * px);
      ink.style.width = over.w + 'px';
      ink.style.height = over.hb + 'px';
      stages.o.appendChild(ink);
      ink.hidden = false;
      drawInk();
    }
    if (keep) setFraction(ports.o, keep);
  }

  function composite() {
    if (!over) return;
    const t = (fade ? parseInt(fade.value, 10) : 50) / 100;
    const fa = Math.min(1, 2 * (1 - t)), fb = Math.min(1, 2 * t);
    const ctx = over.canvas.getContext('2d');
    const img = ctx.createImageData(over.canvas.width, over.canvas.height);
    const d = img.data, A = over.a, B = over.b;
    for (let j = 0, i = 0; j < A.length; j++, i += 4) {
      const a = A[j] * fa, b = B[j] * fb;
      d[i] = 255 - b;
      d[i + 1] = 255 - (a > b ? a : b);
      d[i + 2] = 255 - a;
      d[i + 3] = 255;
    }
    ctx.putImageData(img, 0, 0);
  }

  function drawInk(extra) {
    if (!ink || !over) return;
    const ctx = ink.getContext('2d');
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, ink.width, ink.height);
    ctx.setTransform(ink.width / over.w, 0, 0, ink.height / over.hb, 0, 0);
    mk.drawShapes(ctx, mineShapes.concat(extra ? [extra] : []), pageNum, over.w, over.hb);
  }

  // ---- keep the two sides together ------------------------------------------
  function fraction(port) {
    const mx = port.scrollWidth - port.clientWidth, my = port.scrollHeight - port.clientHeight;
    return { x: mx > 0 ? port.scrollLeft / mx : 0, y: my > 0 ? port.scrollTop / my : 0 };
  }
  function setFraction(port, f) {
    port.scrollLeft = f.x * (port.scrollWidth - port.clientWidth);
    port.scrollTop = f.y * (port.scrollHeight - port.clientHeight);
  }
  ['a', 'b'].forEach(function (side) {
    const other = side === 'a' ? 'b' : 'a';
    ports[side].addEventListener('scroll', function () {
      if (syncing || mode !== 'side') return;
      syncing = true;
      setFraction(ports[other], fraction(ports[side]));
      requestAnimationFrame(function () { syncing = false; });
    });
  });

  function activePort() { return mode === 'side' ? ports.a : ports.o; }

  function zoomBy(factor) {
    const z = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, zoom * factor));
    if (Math.abs(z - zoom) < 0.001) return;
    const keep = fraction(activePort());
    zoom = z;
    draw(keep);
  }

  // ---- controls --------------------------------------------------------------
  root.addEventListener('click', function (e) {
    const btn = e.target.closest('[data-act], [data-mode]');
    if (!btn || !root.contains(btn)) return;
    if (btn.dataset.mode) { setMode(btn.dataset.mode); return; }
    const act = btn.dataset.act;
    if (act === 'prev' && pageNum > 1) { pageNum--; draw(); }
    else if (act === 'next' && pageNum < pages) { pageNum++; draw(); }
    else if (act === 'in') zoomBy(1.25);
    else if (act === 'out') zoomBy(1 / 1.25);
    else if (act === 'fit') { zoom = 1; draw(); }
    else if (act === 'cloud') setClouding(!clouding);
  });

  function setMode(m) {
    if (m === mode) return;
    const keep = fraction(activePort());
    mode = m;
    if (m === 'side' && clouding) setClouding(false);
    root.querySelectorAll('[data-mode]').forEach(function (b) {
      b.setAttribute('aria-pressed', b.dataset.mode === m ? 'true' : 'false');
    });
    $('[data-side]', root).hidden = m !== 'side';
    $('[data-over]', root).hidden = m !== 'overlay';
    root.querySelectorAll('[data-overlay-only]').forEach(function (el) { el.hidden = m !== 'overlay'; });
    draw(keep);
  }

  function setClouding(on) {
    if (!cfg.add_url) return;
    clouding = on;
    if (on && mode !== 'overlay') setMode('overlay');
    root.classList.toggle('marking', on);
    const b = $('[data-act=cloud]', root);
    if (b) {
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
      b.textContent = on ? 'Drag round a change…' : 'Cloud a change';
    }
  }

  if (fade) fade.addEventListener('input', function () {
    const v = parseInt(fade.value, 10);
    fade.setAttribute('aria-valuetext', v === 50 ? 'Both revisions at full strength'
      : v < 50 ? 'Rev ' + cfg.b.label + ' faded' : 'Rev ' + cfg.a.label + ' faded');
    requestAnimationFrame(composite);
  });

  root.addEventListener('keydown', function (e) {
    if (!e.target.closest('[data-port]')) return;
    if (e.key === '+' || e.key === '=') { zoomBy(1.25); e.preventDefault(); }
    else if (e.key === '-' || e.key === '_') { zoomBy(1 / 1.25); e.preventDefault(); }
  });

  Object.keys(ports).forEach(function (k) {
    const port = ports[k];
    port.addEventListener('wheel', function (e) {
      if (!e.ctrlKey && !e.metaKey) return;
      e.preventDefault();
      zoomBy(e.deltaY < 0 ? 1.15 : 1 / 1.15);
    }, { passive: false });
    // Pinch to zoom: the page is scaled while the fingers move, then drawn again sharp
    let pinch = null;
    const dist = function (t) { return Math.hypot(t[0].clientX - t[1].clientX, t[0].clientY - t[1].clientY); };
    port.addEventListener('touchstart', function (e) {
      if (e.touches.length !== 2) return;
      cloudStart = null;
      pinch = { d0: dist(e.touches), ratio: 1 };
      stages[k].style.transformOrigin = '0 0';
    }, { passive: true });
    port.addEventListener('touchmove', function (e) {
      if (!pinch || e.touches.length !== 2) return;
      e.preventDefault();
      const z = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, zoom * dist(e.touches) / pinch.d0));
      pinch.ratio = z / zoom;
      stages[k].style.transform = 'scale(' + pinch.ratio + ')';
    }, { passive: false });
    port.addEventListener('touchend', function (e) {
      if (!pinch || e.touches.length >= 2) return;
      const ratio = pinch.ratio;
      pinch = null;
      zoomBy(ratio);
    });
  });

  let resizeTimer = null, lastWidth = window.innerWidth;
  window.addEventListener('resize', function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () {
      if (window.innerWidth !== lastWidth) { lastWidth = window.innerWidth; draw(); }
    }, 150);
  });

  // ---- cloud a change ----------------------------------------------------------
  let cloudStart = null, cloudNow = null;
  function inkPoint(e) {
    const r = ink.getBoundingClientRect();
    return [Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)),
            Math.min(1, Math.max(0, (e.clientY - r.top) / r.height))];
  }
  if (stages.o) {
    stages.o.addEventListener('pointerdown', function (e) {
      if (!clouding || !e.isPrimary || !ink) return;
      e.preventDefault();
      try { stages.o.setPointerCapture(e.pointerId); } catch (ex) { /* older browsers */ }
      cloudStart = inkPoint(e);
      cloudNow = cloudStart;
    });
    stages.o.addEventListener('pointermove', function (e) {
      if (!clouding || !cloudStart || !e.isPrimary) return;
      cloudNow = inkPoint(e);
      drawInk({ t: 'cloud', p: pageNum, c: 'red', w: 3, a: cloudStart, b: cloudNow });
    });
    stages.o.addEventListener('pointerup', function (e) {
      if (!clouding || !cloudStart || !e.isPrimary) return;
      const a = cloudStart, b = inkPoint(e);
      cloudStart = null;
      if (Math.abs(a[0] - b[0]) * over.w < 8 || Math.abs(a[1] - b[1]) * over.hb < 8) { drawInk(); return; }
      const r5 = function (p) { return [Math.round(p[0] * 1e5) / 1e5, Math.round(p[1] * 1e5) / 1e5]; };
      saveCloud({ t: 'cloud', p: pageNum, c: 'red', w: 3, a: r5(a), b: r5(b) });
    });
  }

  async function saveCloud(shape) {
    const note = $('[data-cloud-state]', root);
    mineShapes.push(shape);
    drawInk();
    if (note) note.textContent = 'Saving…';
    try {
      const r = await fetch(cfg.add_url, { method: 'POST', credentials: 'same-origin',
                                           body: new URLSearchParams({ csrfmiddlewaretoken: csrf, shapes: JSON.stringify([shape]) }),
                                           headers: { 'X-CSRFToken': csrf, Accept: 'application/json' } });
      const j = await r.json().catch(function () { return {}; });
      if (!r.ok) throw new Error(j.error || 'The cloud could not be saved. Reload the page and try again.');
      mineShapes = j.shapes;
      drawInk();
      if (note) note.textContent = 'Cloud saved to your mark-up on rev ' + cfg.b.label + '.';
    } catch (ex) {
      mineShapes.pop();
      drawInk();
      if (note) note.textContent = ex.message;
    }
  }
}
