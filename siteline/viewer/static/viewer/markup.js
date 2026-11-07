/*
 * Mark-up drawing, shared by the drawing viewer (viewer.js) and revision compare (compare.js).
 *
 * Shapes are the stored form checked by viewer/markup.py: positions are fractions of the page
 * (0 to 1), line weight and text size are thousandths of the page width. Everything is drawn on
 * a canvas; text goes through fillText, so nothing in a mark-up is ever treated as HTML.
 */
export const COLOURS = {
  red: '#D7263D', blue: '#1F5FD1', green: '#1E8C45', orange: '#E8590C', purple: '#7A3CC2', black: '#15191F',
};

const TWO_POINT = ['line', 'arrow', 'rect', 'cloud', 'measure'];

export function isTwoPoint(kind) { return TWO_POINT.indexOf(kind) !== -1; }

/* Draw every shape on page `page` onto ctx, for a page drawn w by h CSS pixels (ctx already scaled). */
export function drawShapes(ctx, shapes, page, w, h) {
  ctx.save();
  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';
  for (const s of shapes) {
    if (s.p !== page) continue;
    drawShape(ctx, s, w, h);
  }
  ctx.restore();
}

export function drawShape(ctx, s, w, h) {
  const colour = COLOURS[s.c] || COLOURS.red;
  ctx.strokeStyle = colour;
  ctx.fillStyle = colour;
  ctx.lineWidth = Math.max(1, (s.w || 3) * w / 1000);
  const X = function (pt) { return pt[0] * w; }, Y = function (pt) { return pt[1] * h; };
  if (s.t === 'pen') {
    ctx.beginPath();
    s.pts.forEach(function (pt, i) { if (i) ctx.lineTo(X(pt), Y(pt)); else ctx.moveTo(X(pt), Y(pt)); });
    if (s.pts.length === 1) ctx.lineTo(X(s.pts[0]) + 0.1, Y(s.pts[0]));
    ctx.stroke();
  } else if (s.t === 'line' || s.t === 'arrow' || s.t === 'measure') {
    const ax = X(s.a), ay = Y(s.a), bx = X(s.b), by = Y(s.b);
    ctx.beginPath(); ctx.moveTo(ax, ay); ctx.lineTo(bx, by); ctx.stroke();
    if (s.t === 'arrow') arrowHead(ctx, ax, ay, bx, by);
    if (s.t === 'measure') {
      tick(ctx, ax, ay, bx, by, ax, ay);
      tick(ctx, ax, ay, bx, by, bx, by);
      if (s.text) label(ctx, s.text, (ax + bx) / 2, (ay + by) / 2, Math.max(11, 16 * w / 1000), colour);
    }
  } else if (s.t === 'rect') {
    const r = box(s, w, h);
    ctx.strokeRect(r.x, r.y, r.w, r.h);
  } else if (s.t === 'cloud') {
    cloud(ctx, box(s, w, h), Math.max(6, 14 * w / 1000));
  } else if (s.t === 'text') {
    const size = Math.max(8, (s.s || 18) * w / 1000);
    ctx.font = '600 ' + size + 'px system-ui, sans-serif';
    ctx.textBaseline = 'top';
    ctx.lineWidth = Math.max(2, size / 5);
    ctx.strokeStyle = 'rgba(255,255,255,.92)';
    ctx.strokeText(s.text, X(s.at), Y(s.at));
    ctx.fillText(s.text, X(s.at), Y(s.at));
  }
}

function box(s, w, h) {
  const x1 = Math.min(s.a[0], s.b[0]) * w, y1 = Math.min(s.a[1], s.b[1]) * h;
  return { x: x1, y: y1, w: Math.abs(s.a[0] - s.b[0]) * w, h: Math.abs(s.a[1] - s.b[1]) * h };
}

function arrowHead(ctx, ax, ay, bx, by) {
  const ang = Math.atan2(by - ay, bx - ax), len = Math.max(10, ctx.lineWidth * 4);
  ctx.beginPath();
  ctx.moveTo(bx, by);
  ctx.lineTo(bx - len * Math.cos(ang - 0.45), by - len * Math.sin(ang - 0.45));
  ctx.lineTo(bx - len * Math.cos(ang + 0.45), by - len * Math.sin(ang + 0.45));
  ctx.closePath();
  ctx.fill();
}

function tick(ctx, ax, ay, bx, by, x, y) {
  const ang = Math.atan2(by - ay, bx - ax) + Math.PI / 2, len = Math.max(6, ctx.lineWidth * 3);
  ctx.beginPath();
  ctx.moveTo(x - len * Math.cos(ang), y - len * Math.sin(ang));
  ctx.lineTo(x + len * Math.cos(ang), y + len * Math.sin(ang));
  ctx.stroke();
}

function label(ctx, text, x, y, size, colour) {
  ctx.save();
  ctx.font = '600 ' + size + 'px system-ui, sans-serif';
  ctx.textBaseline = 'middle';
  ctx.textAlign = 'center';
  const tw = ctx.measureText(text).width, pad = size * 0.35;
  ctx.fillStyle = 'rgba(255,255,255,.95)';
  ctx.fillRect(x - tw / 2 - pad, y - size * 0.7, tw + pad * 2, size * 1.4);
  ctx.strokeStyle = colour;
  ctx.lineWidth = 1;
  ctx.strokeRect(x - tw / 2 - pad, y - size * 0.7, tw + pad * 2, size * 1.4);
  ctx.fillStyle = colour;
  ctx.fillText(text, x, y);
  ctx.restore();
}

/* A revision cloud: scallops along each edge of the box, bulging outwards. */
function cloud(ctx, r, scallop) {
  if (r.w < 2 || r.h < 2) { ctx.strokeRect(r.x, r.y, r.w, r.h); return; }
  const edges = [[r.x, r.y, r.x + r.w, r.y], [r.x + r.w, r.y, r.x + r.w, r.y + r.h],
                 [r.x + r.w, r.y + r.h, r.x, r.y + r.h], [r.x, r.y + r.h, r.x, r.y]];
  ctx.beginPath();
  edges.forEach(function (e) {
    const len = Math.hypot(e[2] - e[0], e[3] - e[1]);
    const n = Math.max(1, Math.round(len / (scallop * 2)));
    const ang = Math.atan2(e[3] - e[1], e[2] - e[0]), step = len / n, rad = step / 2;
    for (let i = 0; i < n; i++) {
      const cx = e[0] + Math.cos(ang) * (step * i + rad), cy = e[1] + Math.sin(ang) * (step * i + rad);
      ctx.moveTo(cx - Math.cos(ang) * rad, cy - Math.sin(ang) * rad);
      ctx.arc(cx, cy, rad, ang + Math.PI, ang, false);
    }
  });
  ctx.stroke();
}

/* Distance from a point (page fractions, scaled by w and h) to a shape, in CSS pixels: for the eraser. */
export function distanceTo(s, x, y, w, h) {
  const P = function (pt) { return [pt[0] * w, pt[1] * h]; };
  const px = x * w, py = y * h;
  if (s.t === 'pen') {
    let best = Infinity;
    for (let i = 0; i < s.pts.length; i++) {
      const a = P(s.pts[i]), b = P(s.pts[Math.min(i + 1, s.pts.length - 1)]);
      best = Math.min(best, segment(px, py, a, b));
    }
    return best;
  }
  if (s.t === 'text') {
    const at = P(s.at), size = (s.s || 18) * w / 1000;
    const tw = size * 0.6 * s.text.length;
    const dx = Math.max(at[0] - px, 0, px - (at[0] + tw)), dy = Math.max(at[1] - py, 0, py - (at[1] + size * 1.2));
    return Math.hypot(dx, dy);
  }
  const a = P(s.a), b = P(s.b);
  if (s.t === 'rect' || s.t === 'cloud') {
    const c = [[a[0], a[1]], [b[0], a[1]], [b[0], b[1]], [a[0], b[1]]];
    return Math.min(segment(px, py, c[0], c[1]), segment(px, py, c[1], c[2]), segment(px, py, c[2], c[3]),
                    segment(px, py, c[3], c[0]));
  }
  return segment(px, py, a, b);
}

function segment(px, py, a, b) {
  const dx = b[0] - a[0], dy = b[1] - a[1], len = dx * dx + dy * dy;
  let t = len ? ((px - a[0]) * dx + (py - a[1]) * dy) / len : 0;
  t = Math.max(0, Math.min(1, t));
  return Math.hypot(px - (a[0] + t * dx), py - (a[1] + t * dy));
}

/* "1:50" or "1:50 @ A1" -> 50; anything else -> 0 (no scale, so the measurement is typed in). */
export function parseScale(text) {
  const m = /1\s*:\s*(\d{1,5})/.exec(text || '');
  return m ? parseInt(m[1], 10) : 0;
}

/*
 * The real length of a measured line: page size in PDF points (1/72 inch) times the drawing's
 * scale. Only right when the sheet is printed at the size the scale was drawn for.
 */
export function measureLabel(s, pageWidthPt, pageHeightPt, scale) {
  if (!scale) return '';
  const dx = (s.b[0] - s.a[0]) * pageWidthPt, dy = (s.b[1] - s.a[1]) * pageHeightPt;
  const mm = Math.hypot(dx, dy) * 25.4 / 72 * scale;
  if (mm >= 1000) return (mm / 1000).toFixed(2) + ' m';
  return Math.round(mm) + ' mm';
}

/* ---- export --------------------------------------------------------------- */

/* A one-page PDF holding a JPEG of the page and its mark-ups, the page's own size in points. */
export async function pdfFromCanvas(canvas, widthPt, heightPt) {
  const blob = await new Promise(function (resolve) { canvas.toBlob(resolve, 'image/jpeg', 0.92); });
  const jpeg = new Uint8Array(await blob.arrayBuffer());
  const enc = new TextEncoder();
  const W = widthPt.toFixed(2), H = heightPt.toFixed(2);
  const content = 'q ' + W + ' 0 0 ' + H + ' 0 0 cm /Im0 Do Q';
  const objs = [
    '<< /Type /Catalog /Pages 2 0 R >>',
    '<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 ' + W + ' ' + H + '] /Resources << /XObject << /Im0 4 0 R >> >> /Contents 5 0 R >>',
    null,
    '<< /Length ' + content.length + ' >>\nstream\n' + content + '\nendstream',
  ];
  const parts = [], offsets = [];
  let size = 0;
  function push(bytes) { parts.push(bytes); size += bytes.length; }
  push(enc.encode('%PDF-1.4\n%âãÏÓ\n'));
  for (let i = 0; i < objs.length; i++) {
    offsets.push(size);
    if (i === 3) {
      push(enc.encode('4 0 obj\n<< /Type /XObject /Subtype /Image /Width ' + canvas.width + ' /Height ' + canvas.height +
                      ' /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length ' + jpeg.length +
                      ' >>\nstream\n'));
      push(jpeg);
      push(enc.encode('\nendstream\nendobj\n'));
    } else {
      push(enc.encode((i + 1) + ' 0 obj\n' + objs[i] + '\nendobj\n'));
    }
  }
  const xref = size;
  let table = 'xref\n0 ' + (objs.length + 1) + '\n0000000000 65535 f \n';
  offsets.forEach(function (o) { table += String(o).padStart(10, '0') + ' 00000 n \n'; });
  table += 'trailer\n<< /Size ' + (objs.length + 1) + ' /Root 1 0 R >>\nstartxref\n' + xref + '\n%%EOF\n';
  push(enc.encode(table));
  return new Blob(parts, { type: 'application/pdf' });
}

export function download(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.hidden = true;
  document.body.appendChild(a);
  a.click();
  setTimeout(function () { URL.revokeObjectURL(url); a.remove(); }, 2000);
}

export function safeName(text) {
  return (text || 'drawing').replace(/[^A-Za-z0-9._-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 80) || 'drawing';
}
