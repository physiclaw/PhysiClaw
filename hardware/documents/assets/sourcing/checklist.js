(function () {
  var KEY = document.body.dataset.storeKey;
  var PREFIX = document.body.dataset.storePrefix;
  var bought = {};
  try {
    Object.keys(localStorage).forEach(function (k) {
      if (k.indexOf(PREFIX) === 0 && k !== KEY)
        localStorage.removeItem(k);  // older guide revisions start clean
    });
    // Older guides stored only an array of bought part IDs.
    var stored = JSON.parse(localStorage.getItem(KEY) || '[]');
    (Array.isArray(stored) ? stored : stored.pids || []).forEach(function (p) {
      bought[p] = true;
    });
    if (stored.hl) document.body.classList.add('hl');
  } catch (e) {}

  // The row anatomy, resolved once: the tr, its part id, its checkbox.
  var items = Array.from(document.querySelectorAll('.sourcing > tbody > tr[data-pid]')).map(function (tr) {
    return {tr: tr, pid: tr.dataset.pid, box: tr.querySelector('td.get input')};
  });
  // A cell that spans several rows (class group, component group, Ditto
  // block) belongs to its anchor row, so row-level dimming would grey it
  // while spanned rows are still unbought. Map each spanning cell to the
  // part ids it covers; render() greys it only when all are ticked.
  var spans = [];
  items.forEach(function (it, i) {
    Array.from(it.tr.children).forEach(function (td) {
      if (td.rowSpan > 1) spans.push({
        td: td,
        pids: items.slice(i, i + td.rowSpan).map(function (x) { return x.pid; })
      });
    });
  });
  var tally = document.getElementById('tally');
  var reset = document.getElementById('reset');
  var hl = document.getElementById('hl');

  function save() {
    try {
      localStorage.setItem(KEY, JSON.stringify({
        pids: Object.keys(bought),
        hl: document.body.classList.contains('hl')
      }));
    } catch (e) {}
  }

  function render() {
    var n = 0;
    items.forEach(function (it) {
      var done = !!bought[it.pid];
      it.tr.classList.toggle('done', done);
      it.box.checked = done;
      if (done) n++;
    });
    spans.forEach(function (s) {
      s.td.classList.toggle('done', s.pids.every(function (p) {
        return bought[p];
      }));
    });
    tally.textContent = (n === items.length ? tally.dataset.done : tally.dataset.fmt)
      .replace('{n}', n).replace('{t}', items.length);
    reset.hidden = n === 0;
    // The highlight only means something once sourcing has started; an
    // empty list would just wash every row. body.hl is its only state.
    if (n === 0) document.body.classList.remove('hl');
    hl.hidden = n === 0;
  }

  // render() before save(): render may auto-clear the highlight (empty
  // list), and save() records the settled DOM state.
  items.forEach(function (it) {
    it.box.addEventListener('change', function (ev) {
      if (ev.target.checked) {
        bought[it.pid] = true;
        // Marks a hand-made tick: only these play the landing flash, so
        // restored checks on reload settle without animating.
        ev.target.classList.add('ticked');
      } else {
        delete bought[it.pid];
      }
      render();
      save();
    });
  });
  reset.addEventListener('click', function () {
    if (!confirm(reset.dataset.confirm)) return;
    bought = {};
    render();
    save();
  });
  hl.addEventListener('click', function () {
    document.body.classList.toggle('hl');
    render();
    save();
  });

  var lastOpened = null;
  document.querySelectorAll('.reportable a:not(.report-link)').forEach(function (link) {
    function markOpened(event) {
      if (event.type === 'auxclick' && event.button !== 1) return;
      if (lastOpened) {
        lastOpened.closest('.reportable').classList.remove('opened');
        lastOpened.removeAttribute('aria-description');
      }
      link.closest('.reportable').classList.add('opened');
      link.setAttribute('aria-description', document.body.dataset.openedLabel);
      lastOpened = link;
    }
    link.addEventListener('click', markOpened);
    link.addEventListener('auxclick', markOpened);
  });
  render();
})();

document.querySelectorAll('button.copy').forEach(function (b) {
  b.addEventListener('click', function () {
    navigator.clipboard.writeText(b.dataset.q).then(function () {
      b.classList.add('ok');
      setTimeout(function () { b.classList.remove('ok'); }, 1200);
    });
  });
});

document.querySelectorAll('[data-guide]').forEach(function (button) {
  button.addEventListener('click', function () {
    document.getElementById(button.dataset.guide).showModal();
  });
});

document.querySelectorAll('.supplier-guide').forEach(function (guide) {
  function outside(event) {
    var rect = guide.getBoundingClientRect();
    return event.clientX < rect.left || event.clientX > rect.right ||
      event.clientY < rect.top || event.clientY > rect.bottom;
  }
  // A drag that starts on a part number must not dismiss the dialog.
  var pressedOutside = false;
  guide.addEventListener('pointerdown', function (event) {
    pressedOutside = outside(event);
  });
  guide.addEventListener('click', function (event) {
    if (pressedOutside && outside(event)) guide.close();
    pressedOutside = false;
  });
});

