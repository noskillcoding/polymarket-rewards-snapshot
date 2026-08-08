/* Farmer leaderboard — renders window.FARMERS (built by compute_farmers.py).
   Per-window top-N; columns Trader / Farmed / Volume / Farmed÷Volume; sortable,
   paginated. Zero-build vanilla JS. */
(function () {
  var F = window.FARMERS;
  var $ = function (id) { return document.getElementById(id); };
  if (!F || !F.windows) { $('summary').textContent = 'no data'; return; }

  var WIN_LABEL = { '1d': ['Last 24h', ''], '7d': ['7 days', ''],
                    '30d': ['30 days', ''], 'all': ['All-time', ''] };
  var PAGE = 20;
  var state = { win: F.meta.windows.indexOf('all') >= 0 ? 'all' : F.meta.windows[0],
                sort: 'f', dir: -1, page: 0 };

  // ---- formatting ----------------------------------------------------------
  function usd(n) {
    if (n == null) return '—';
    var a = Math.abs(n);
    if (a >= 1e6) return '$' + (n / 1e6).toFixed(a >= 1e7 ? 1 : 2) + 'M';
    if (a >= 1e3) return '$' + (n / 1e3).toFixed(a >= 1e4 ? 0 : 1) + 'k';
    return '$' + n.toFixed(a < 10 ? 2 : 0);
  }
  function pct(f, v) {
    if (v == null) return null;
    if (v <= 0) return f > 0 ? Infinity : null;   // earned but never filled -> max efficiency
    return f / v * 100;
  }
  function pctStr(p) {
    if (p == null) return '—';
    if (p === Infinity) return '∞';
    if (p >= 1) return p.toFixed(2) + '%';
    if (p >= 0.01) return p.toFixed(3) + '%';
    return p.toPrecision(2) + '%';
  }
  function intcomma(n) { return n.toLocaleString('en-US'); }

  // ---- header --------------------------------------------------------------
  (function head() {
    if (F.meta.alltime_paid_usd != null) $('kpiFarmed').textContent = usd(F.meta.alltime_paid_usd);
    if (F.meta.lifetime_farmers != null) $('kpiFarmers').textContent = intcomma(F.meta.lifetime_farmers);
    var txt = 'updated · ' + (F.meta.label || '—');
    var iso = String(F.meta.ts || '').replace(/T(\d\d)-(\d\d)-(\d\d)Z$/, 'T$1:$2:$3Z');
    var age = (Date.now() - Date.parse(iso)) / 60000;
    if (isFinite(age) && age >= 0) {
      txt += ' (' + (age < 1 ? 'just now' : age < 60 ? Math.round(age) + ' min ago'
        : age < 2880 ? (age / 60).toFixed(1) + ' h ago' : Math.round(age / 1440) + ' d ago') + ')';
    }
    $('snapTs').textContent = txt;
  })();

  // ---- window tabs ---------------------------------------------------------
  (function tabs() {
    var wrap = $('winTabs');
    F.meta.windows.forEach(function (w) {
      var b = document.createElement('button');
      b.className = 'wintab' + (w === state.win ? ' on' : '');
      var lbl = WIN_LABEL[w] || [w, ''];
      b.innerHTML = lbl[0] + (lbl[1] ? '<small>' + lbl[1] + '</small>' : '');
      b.onclick = function () { state.win = w; state.page = 0; state.sort = 'f'; state.dir = -1; render(); };
      b.dataset.w = w;
      wrap.appendChild(b);
    });
  })();

  var COLS = [
    { k: 'rank', t: '#', cls: 'rank', sort: false },
    { k: 'trader', t: 'Trader', cls: 'trader', sort: false },
    { k: 'f', t: 'Farmed', cls: 'num', sort: true },
    { k: 'v', t: 'Volume', cls: 'num', sort: true },
    { k: 'r', t: 'Farmed / Volume', cls: 'num', sort: true }
  ];

  // ---- filters -------------------------------------------------------------
  function readFilters() {
    var num = function (id) { var v = parseFloat($(id).value); return isFinite(v) ? v : null; };
    return {
      q: ($('fSearch').value || '').trim().toLowerCase(),
      farmed: num('fFarmed'), vol: num('fVol'),
      ratMin: num('fRatMin'), ratMax: num('fRatMax')
    };
  }
  function matches(row, f) {
    if (f.q) {
      var short = row.a.slice(0, 6) + '…' + row.a.slice(-4);
      if (((row.n || '') + ' ' + row.a + ' ' + short).toLowerCase().indexOf(f.q) < 0) return false;
    }
    if (f.farmed != null && !(row.f >= f.farmed)) return false;
    if (f.vol != null && !((row.v || 0) >= f.vol)) return false;
    if (f.ratMin != null || f.ratMax != null) {
      var p = pct(row.f, row.v);
      if (p == null) return false;                          // no ratio -> can't satisfy a ratio filter
      if (f.ratMin != null && !(p >= f.ratMin)) return false;
      if (f.ratMax != null && !(p <= f.ratMax)) return false;   // Infinity fails any finite max -> excluded
    }
    return true;
  }

  function sorted() {
    var f = readFilters();
    var rows = F.windows[state.win].filter(function (r) { return matches(r, f); });
    var k = state.sort, d = state.dir;
    rows.sort(function (a, b) {
      var av, bv;
      if (k === 'r') { av = pct(a.f, a.v); bv = pct(b.f, b.v); }
      else { av = a[k]; bv = b[k]; }
      // nulls always last regardless of dir
      if (av == null && bv == null) return b.f - a.f;
      if (av == null) return 1;
      if (bv == null) return -1;
      return (av - bv) * d || (b.f - a.f);
    });
    return rows;
  }

  function render() {
    document.querySelectorAll('.wintab').forEach(function (b) {
      b.classList.toggle('on', b.dataset.w === state.win);
    });
    var rows = sorted();
    var n = rows.length;
    var pages = Math.max(1, Math.ceil(n / PAGE));
    if (state.page >= pages) state.page = 0;

    var lbl = WIN_LABEL[state.win][0];
    var total = F.windows[state.win].length;
    $('summary').textContent = (n === total ? intcomma(n) : intcomma(n) + ' of ' + intcomma(total))
      + ' farmers · ' + lbl
      + (F.meta.prev_day && state.win === '1d' ? ' (' + F.meta.prev_day + ')' : '')
      + ' · ranked by farmed, ≥ $' + F.meta.floor_usd;

    // head
    var thead = '<tr>' + COLS.map(function (c) {
      var on = c.sort && state.sort === c.k;
      var ar = on ? '<span class="ar">' + (state.dir < 0 ? '▼' : '▲') + '</span>' : '';
      return '<th class="' + (c.cls === 'num' ? 'num ' : '') + (on ? 'on' : '') + '" '
        + (c.sort ? 'data-k="' + c.k + '"' : '') + '>' + c.t + ' ' + ar + '</th>';
    }).join('') + '</tr>';
    $('tbl').querySelector('thead').innerHTML = thead;

    // body
    var start = state.page * PAGE;
    var slice = rows.slice(start, start + PAGE);
    var html = slice.map(function (row, i) {
      var p = pct(row.f, row.v);
      var url = 'https://polymarket.com/profile/' + row.a;
      var short = row.a.slice(0, 6) + '…' + row.a.slice(-4);
      var name = row.n && row.n !== short ? row.n : short;
      return '<tr>'
        + '<td class="rank">' + (start + i + 1) + '</td>'
        + '<td class="trader"><a href="' + url + '" target="_blank" rel="noopener">'
        + escapeHtml(name) + '</a> <span class="addr">' + short + '</span></td>'
        + '<td class="num">' + usd(row.f) + '</td>'
        + '<td class="num' + (row.v == null ? ' na' : '') + '">' + usd(row.v) + '</td>'
        + '<td class="num ratio' + (p == null ? ' na' : '') + '">' + pctStr(p) + '</td>'
        + '</tr>';
    }).join('');
    $('tbl').querySelector('tbody').innerHTML = html;

    // wire header sort
    $('tbl').querySelectorAll('th[data-k]').forEach(function (th) {
      th.onclick = function () {
        var k = th.dataset.k;
        if (state.sort === k) state.dir = -state.dir;
        else { state.sort = k; state.dir = -1; }
        state.page = 0; render();
      };
    });
    renderPager(pages, n);
  }

  function renderPager(pages, n) {
    var el = $('pager');
    if (pages <= 1) { el.innerHTML = '<span class="pginfo">' + intcomma(n) + ' rows</span>'; return; }
    var cur = state.page;
    var parts = ['<span class="pginfo">' + intcomma(n) + ' rows · page ' + (cur + 1) + '/' + pages + '</span>'];
    function btn(label, page, dis, on) {
      return '<button class="pgbtn' + (dis ? ' dis' : '') + (on ? ' on' : '') + '" data-p="' + page + '">' + label + '</button>';
    }
    parts.push(btn('‹', cur - 1, cur === 0));
    var wnd = [];
    for (var i = 0; i < pages; i++) {
      if (i < 1 || i >= pages - 1 || Math.abs(i - cur) <= 1) wnd.push(i);
      else if (wnd[wnd.length - 1] !== '…') wnd.push('…');
    }
    wnd.forEach(function (i) {
      parts.push(i === '…' ? '<span class="pggap">…</span>' : btn(i + 1, i, false, i === cur));
    });
    parts.push(btn('›', cur + 1, cur === pages - 1));
    el.innerHTML = parts.join('');
    el.querySelectorAll('.pgbtn[data-p]').forEach(function (b) {
      b.onclick = function () { var p = +b.dataset.p; if (p >= 0 && p < pages) { state.page = p; render(); window.scrollTo(0, 0); } };
    });
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  // ---- filter wiring -------------------------------------------------------
  (function filters() {
    var ids = ['fSearch', 'fFarmed', 'fVol', 'fRatMin', 'fRatMax'];
    ids.forEach(function (id) {
      $(id).addEventListener('input', function () { state.page = 0; render(); });
    });
    $('fReset').onclick = function () {
      ids.forEach(function (id) { $(id).value = ''; });
      state.page = 0; render();
    };
  })();

  // ---- agent guide copy ----------------------------------------------------
  (function agent() {
    var btn = $('agentBtn'), guide = $('agentGuide');
    if (!btn || !guide) return;
    btn.onclick = function () {
      var text = guide.textContent.trim();
      var done = function () { btn.classList.add('ok'); var t = btn.textContent; btn.textContent = 'Copied ✓';
        setTimeout(function () { btn.classList.remove('ok'); btn.textContent = t; }, 1600); };
      if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(done, done);
      else { var ta = document.createElement('textarea'); ta.value = text; document.body.appendChild(ta); ta.select();
        try { document.execCommand('copy'); } catch (e) {} document.body.removeChild(ta); done(); }
    };
  })();

  render();
})();
