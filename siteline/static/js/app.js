(function () {
  'use strict';

  function csrf() {
    var m = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : '';
  }

  // Mobile nav
  document.addEventListener('click', function (e) {
    if (e.target.closest('[data-nav-toggle]')) { document.body.classList.toggle('nav-open'); return; }
    if (document.body.classList.contains('nav-open') && !e.target.closest('.side')) document.body.classList.remove('nav-open');
  });

  // Confirm before destructive actions
  document.addEventListener('submit', function (e) {
    var msg = e.target.getAttribute('data-confirm');
    if (msg && !window.confirm(msg)) e.preventDefault();
  });

  // Auto-submit selects
  document.addEventListener('change', function (e) {
    if (e.target.matches('[data-autosubmit]')) e.target.form.submit();
  });

  // Pipeline drag and drop (mouse). Each card also has a "Move to" form for touch and keyboard users.
  var board = document.querySelector('[data-board]');
  if (board) {
    var dragged = null;
    // Using the move form must not start a drag of its card
    board.addEventListener('pointerdown', function (e) {
      var card = e.target.closest('.bcard');
      if (card) card.draggable = !e.target.closest('[data-move]');
    });
    board.addEventListener('dragstart', function (e) {
      var card = e.target.closest('.bcard');
      if (!card) return;
      dragged = card;
      card.classList.add('dragging');
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', card.dataset.id);
    });
    board.addEventListener('dragend', function () {
      if (dragged) dragged.classList.remove('dragging');
      board.querySelectorAll('.col').forEach(function (c) { c.classList.remove('drop'); });
    });
    board.querySelectorAll('.col').forEach(function (col) {
      col.addEventListener('dragover', function (e) { e.preventDefault(); col.classList.add('drop'); });
      col.addEventListener('dragleave', function () { col.classList.remove('drop'); });
      col.addEventListener('drop', function (e) {
        e.preventDefault();
        col.classList.remove('drop');
        if (!dragged || dragged.parentElement === col.querySelector('.col-items')) return;
        var card = dragged, from = card.parentElement;
        col.querySelector('.col-items').prepend(card);
        fetch(card.dataset.stageUrl, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf() },
          body: JSON.stringify({ stage: col.dataset.stage }),
          credentials: 'same-origin'
        }).then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
          .then(function (res) {
            if (!res.ok) { from.prepend(card); alertBox(res.j.error || 'Could not move this project.'); }
            else if (res.j.redirect) { window.location = res.j.redirect; }
            else { recount(); }
          })
          .catch(function () { from.prepend(card); alertBox('Network error, project not moved.'); });
      });
    });
    function recount() {
      board.querySelectorAll('.col').forEach(function (col) {
        var n = col.querySelectorAll('.bcard').length, sum = 0;
        col.querySelectorAll('.bcard').forEach(function (c) { sum += parseFloat(c.dataset.value || '0'); });
        var cnt = col.querySelector('[data-count]'); if (cnt) cnt.textContent = n;
        var tot = col.querySelector('[data-total]');
        if (tot) tot.textContent = (board.dataset.currency || '£') + Math.round(sum).toLocaleString('en-GB');
      });
    }
    function alertBox(msg) {
      var el = document.createElement('div');
      el.className = 'flash error'; el.textContent = msg;
      board.parentElement.insertBefore(el, board);
      setTimeout(function () { el.remove(); }, 5000);
    }
  }

  // Drop-down menus (<details data-menu>): close on a click elsewhere, on Escape, and when another opens
  document.addEventListener('click', function (e) {
    document.querySelectorAll('details[data-menu][open]').forEach(function (d) {
      if (!d.contains(e.target)) d.removeAttribute('open');
    });
  });
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    document.querySelectorAll('details[data-menu][open]').forEach(function (d) {
      d.removeAttribute('open');
      var s = d.querySelector('summary'); if (s) s.focus();
    });
  });

  // Project tabs: show which edge has more tabs (fade + arrow), and bring the current tab into view
  document.querySelectorAll('[data-tabbar]').forEach(function (bar) {
    var row = bar.querySelector('.tabs');
    if (!row) return;
    var more = bar.querySelector('.tabmore');
    ['l', 'r'].forEach(function (side) {
      var b = document.createElement('button');
      b.type = 'button'; b.className = 'scroll-hint ' + side;
      b.setAttribute('aria-label', side === 'l' ? 'Scroll tabs left' : 'Scroll tabs right');
      b.textContent = side === 'l' ? '‹' : '›';
      b.addEventListener('click', function () { row.scrollBy({ left: (side === 'l' ? -1 : 1) * row.clientWidth * 0.7 }); });
      bar.appendChild(b);
    });
    function update() {
      if (more) bar.style.setProperty('--more-w', more.offsetWidth + 'px');
      bar.classList.toggle('can-left', row.scrollLeft > 2);
      bar.classList.toggle('can-right', row.scrollLeft + row.clientWidth < row.scrollWidth - 2);
    }
    var on = row.querySelector('a.on');
    if (on && row.scrollWidth > row.clientWidth) {
      row.style.scrollBehavior = 'auto';
      row.scrollLeft = Math.max(0, on.offsetLeft - row.clientWidth / 3);
      row.style.scrollBehavior = '';
    }
    row.addEventListener('scroll', update, { passive: true });
    window.addEventListener('resize', update);
    update();
  });

  // Gantt: when the chart is wider than the screen, open it with today in view
  document.querySelectorAll('.gantt-wrap').forEach(function (wrap) {
    var today = wrap.querySelector('.g-lines .today');
    if (!today || wrap.scrollWidth <= wrap.clientWidth) return;
    var label = wrap.querySelector('.g-label');
    var visible = wrap.clientWidth - (label ? label.offsetWidth : 0);
    wrap.scrollLeft = Math.max(0, today.offsetLeft - visible / 3);
  });
})();
