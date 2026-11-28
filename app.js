/* Rewards Snapshot dashboard — vanilla JS, no build step.
 * Data contract: data.js sets window.SNAPSHOT = {meta:{ts,label,total,count},
 * markets:[{q,slug,c,sb,sp,vb,vn,ab,rb,cb,rw}]}.
 * Crossfilter convention: each chart recomputes against all active filters
 * EXCEPT its own dimension; KPIs + table use ALL filters. Bars are built once
 * and mutated in place so the CSS width/opacity transitions run
 * ("transitions, not redraws"). */
(function () {
  'use strict';

  var S = window.SNAPSHOT || { meta: { label: '—', total: 0, count: 0 }, markets: [] };
  var M = S.markets;

  var DIMS = [
    { key: 'c',  title: 'Category',           sub: 'click to filter',    chip: function (b) { return b; },
      buckets: ['Sports', 'Crypto', 'Politics', 'Pop-culture', 'Economy', 'Weather', 'Other'] },
    { key: 'sb', title: 'Current spread',     sub: 'hover for $ + %',    chip: function (b) { return 'spread ' + b; },
      buckets: ['0–5¢', '5–10¢', '10–20¢', '20–30¢', '30–50¢', '>50¢', 'no book'] },
    { key: 'mpb', title: 'Mid price',         sub: 'click to filter',    chip: function (b) { return 'mid ' + b; },
      buckets: ['<10¢', '10–30¢', '30–50¢', '50–70¢', '70–90¢', '>90¢', 'no book'] },
    { key: 'vb', title: '24h volume',         sub: 'click to filter',    chip: function (b) { return 'vol ' + b; },
      buckets: ['$0', '<1k', '1–10k', '10–100k', '>100k'] },
    { key: 'rwb', title: 'Rewards/day',       sub: 'per-market pool',    chip: function (b) { return 'reward ' + b; },
      buckets: ['<$10', '$10–25', '$25–50', '$50–100', '$100–500', '>$500'] },
    { key: 'msb', title: 'Min shares',        sub: 'reward min size',    chip: function (b) { return 'min ' + b; },
      buckets: ['≤20', '21–50', '51–100', '101–250', '>250'] },
    { key: 'ab', title: 'Market age',         sub: 'click to filter',    chip: function (b) { return 'age ' + b; },
      buckets: ['<1d', '1–7d', '7–30d', '>30d'] },
    { key: 'rb', title: 'Time to resolution', sub: 'click to filter',    chip: function (b) { return 'ends ' + b; },
      buckets: ['<1d', '1–7d', '7–30d', '>30d', 'no end'] },
    { key: 'cb', title: 'Competitiveness',    sub: 'farmers per market', chip: function (b) { return b; },
      buckets: ['no farmers', 'thin', 'contested'] },
    // metric:'lq' — measures in-band farming CAPITAL $, not pool $.
    // hidden: no chart card; rendered as the full-width breakdown table only
    // (renderYieldTable), which still cross-filters via this dimension.
    { key: 'yb', title: 'Reward per $100 liquidity', sub: 'bars = farming capital', hidden: true,
      chip: function (b) { return b === 'no farmers' ? b : 'yield ' + b; }, metric: 'lq',
      buckets: ['no farmers', '<$0.02', '$0.02–0.05', '$0.05–0.1', '$0.1–0.2', '$0.2–0.5',
                '$0.5–1', '$1–2', '$2–5', '$5–10', '$10–50', '>$50'] },
  ];
  var CB_ORDER = ['no farmers', 'thin', 'contested'];
  var PAGE_SIZE = 20;

  var COLS = [
    { key: 'q',  label: 'Question',        num: false, cls: 'q',
      val: function (m) { return m.q; } },
    { key: 'c',  label: 'Category',        num: false, cls: 'hm',
      val: function (m) { return m.c; } },
    { key: 'sp', label: 'Spread',          num: true,
      val: function (m) { return m.sp == null ? Infinity : m.sp; } },
    { key: 'vn', label: '24h vol',         num: true, cls: 'hm',
      val: function (m) { return m.vn; } },
    { key: 'rw', label: 'Reward/day',      num: true,
      val: function (m) { return m.rw; } },
    { key: 'y',  label: '$/100/d',         num: true, cls: 'hm',
      val: function (m) { return m.y == null ? -1 : m.y; } },
    { key: 'cb', label: 'Competitiveness', num: false, descDefault: true,
      val: function (m) { return CB_ORDER.indexOf(m.cb); } },
  ];

  var state = { filters: {}, sort: { key: 'rw', dir: 'desc' }, page: 1 };
  var barEls = {};    // dim key -> bucket -> {fill, val}
  var chartData = {}; // dim key -> {sums, counts, total}  (for tooltips)
  var lastChipSig = null;

  function $(id) { return document.getElementById(id); }
  function fmtInt(n) { return Math.round(n).toLocaleString('en-US'); }
  function fmtUsd(n) { return '$' + fmtInt(n); }
  function fmtVol(n) {
    if (n <= 0) return '$0';
    if (n < 1000) return '$' + Math.round(n);
    if (n < 1e6) return '$' + (n / 1e3).toFixed(1) + 'k';
    return '$' + (n / 1e6).toFixed(1) + 'M';
  }
  function fmtSpread(sp) {
    if (sp == null) return '—';
    return (sp < 10 ? sp.toFixed(1) : Math.round(sp)) + '¢';
  }
  function fmtReward(n) {
    return '$' + (n >= 100 ? fmtInt(n) : n.toFixed(2));
  }
  function fmtYield(y) {
    if (y == null) return '—';
    return '$' + (y < 1 ? y.toFixed(2) : y < 10 ? y.toFixed(1) : fmtInt(y));
  }
  function esc(s) {
    return String(s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }
  function pass(m, except) {
    for (var k in state.filters) {
      if (k === except) continue;
      if (m[k] !== state.filters[k]) return false;
    }
    return true;
  }

  // --- build (once) ----------------------------------------------------------

  function buildCharts() {
    var grid = $('grid');
    DIMS.forEach(function (d) {
      if (d.hidden) return; // computed + filterable, but no chart card
      var card = document.createElement('div');
      card.className = 'bd';
      card.innerHTML = '<div class="bdhd"><span class="t">' + esc(d.title) +
        '</span><span class="s">' + esc(d.sub) + '</span></div>';
      var bars = document.createElement('div');
      bars.className = 'bars';
      barEls[d.key] = {};
      d.buckets.forEach(function (b) {
        var row = document.createElement('div');
        row.className = 'bar';
        row.innerHTML = '<span class="lbl" title="' + esc(b) + '">' + esc(b) +
          '</span><span class="track"><span class="fill"></span></span><span class="val">—</span>';
        row.addEventListener('click', function () { toggleFilter(d.key, b); });
        row.addEventListener('mousemove', function (e) { showTip(e, d.key, b); });
        row.addEventListener('mouseleave', hideTip);
        bars.appendChild(row);
        barEls[d.key][b] = { fill: row.querySelector('.fill'), val: row.querySelector('.val') };
      });
      card.appendChild(bars);
      grid.appendChild(card);
    });
  }

  function buildTableHead() {
    var tr = document.createElement('tr');
    COLS.forEach(function (col) {
      var th = document.createElement('th');
      th.textContent = col.label;
      if (col.num) th.className = 'num';
      if (col.cls === 'hm') th.classList.add('hm');
      th.addEventListener('click', function () { setSort(col); });
      tr.appendChild(th);
    });
    $('tbl').tHead.appendChild(tr);
  }

  // --- state transitions -----------------------------------------------------

  function toggleFilter(key, bucket) {
    if (state.filters[key] === bucket) delete state.filters[key];
    else state.filters[key] = bucket;
    state.page = 1;
    render();
  }
  function setSort(col) {
    if (state.sort.key === col.key) {
      state.sort.dir = state.sort.dir === 'asc' ? 'desc' : 'asc';
    } else {
      state.sort = { key: col.key, dir: (col.num || col.descDefault) ? 'desc' : 'asc' };
    }
    state.page = 1;
    render();
  }

  // --- tooltip ---------------------------------------------------------------

  function showTip(e, key, bucket) {
    var cd = chartData[key];
    if (!cd) return;
    var sum = cd.sums[bucket] || 0;
    var pct = cd.total > 0 ? sum / cd.total * 100 : 0;
    var pctTxt = sum <= 0 ? '0' : (pct < 1 ? '<1' : String(Math.round(pct)));
    // capital-metric chart: line 1 = capital, line 2 shows the pool that
    // capital earns (for 'no farmers' that pool is the unclaimed slice)
    var l2 = cd.metric
      ? bucket + ' · pool ' + fmtUsd(cd.pools[bucket] || 0) + '/day · ' + fmtInt(cd.counts[bucket] || 0) + ' mkts'
      : bucket + ' · ' + pctTxt + '% of pool · ' + fmtInt(cd.counts[bucket] || 0) + ' mkts';
    var tip = $('tip');
    tip.innerHTML = '<div class="l1">' + esc(fmtUsd(sum)) + '</div><div class="l2">' + esc(l2) + '</div>';
    tip.style.left = (e.clientX + 14) + 'px';
    tip.style.top = (e.clientY - 12) + 'px';
    tip.hidden = false;
  }
  function hideTip() { $('tip').hidden = true; }

  // --- render ----------------------------------------------------------------

  function render() {
    // charts: context = all filters except own dimension
    DIMS.forEach(function (d) {
      var f = d.metric || 'rw'; // bar unit: pool $ by default, capital $ for yield
      var sums = {}, pools = {}, counts = {}, total = 0;
      d.buckets.forEach(function (b) { sums[b] = 0; pools[b] = 0; counts[b] = 0; });
      M.forEach(function (m) {
        if (!pass(m, d.key)) return;
        if (!(m[d.key] in sums)) return;
        sums[m[d.key]] += m[f];
        pools[m[d.key]] += m.rw;
        counts[m[d.key]]++;
        total += m[f];
      });
      chartData[d.key] = { sums: sums, pools: pools, counts: counts, total: total, metric: d.metric };
      if (d.hidden) return; // no bars to update (breakdown table renders this dim)
      var mx = 1;
      d.buckets.forEach(function (b) { if (sums[b] > mx) mx = sums[b]; });
      var sel = state.filters[d.key];
      d.buckets.forEach(function (b) {
        var el = barEls[d.key][b];
        el.fill.style.width = (sums[b] / mx * 100) + '%';
        el.fill.style.opacity = sel ? (b === sel ? 1 : 0.24) : 0.9;
        var pct = total > 0 ? sums[b] / total * 100 : 0;
        el.val.textContent = sums[b] <= 0 ? '—' : (pct < 1 ? '<1%' : Math.round(pct) + '%');
      });
    });

    // KPIs + table context: ALL filters
    var rows = M.filter(function (m) { return pass(m, null); });
    var ftot = rows.reduce(function (a, m) { return a + m.rw; }, 0);
    var nFilters = Object.keys(state.filters).length;

    $('kpiTotal').textContent = fmtUsd(ftot);
    $('kpiTotalCap').textContent = nFilters
      ? 'filtered · of ' + fmtUsd(S.meta.total) + ' total' : 'Total daily reward pool';
    $('kpiCount').textContent = fmtInt(rows.length);
    $('kpiCountCap').textContent = nFilters
      ? 'of ' + fmtInt(S.meta.count) + ' in snapshot' : 'Rewarded markets';

    renderChips(nFilters, rows.length, ftot);
    renderYieldTable();
    renderTable(rows);
  }

  // full-width breakdown of the yield dimension: capital vs pool per bucket.
  // Same crossfilter context as the yield chart (all filters except yb);
  // rows toggle the yb filter exactly like chart bars.
  function renderYieldTable() {
    var d = DIMS.filter(function (x) { return x.key === 'yb'; })[0];
    var cd = chartData[d.key];
    var sel = state.filters[d.key];
    var poolTotal = 0, mktTotal = 0;
    d.buckets.forEach(function (b) { poolTotal += cd.pools[b] || 0; mktTotal += cd.counts[b] || 0; });
    function pct(p, v) { return v <= 0 ? '—' : (p < 0.1 ? '<0.1%' : p.toFixed(1) + '%'); }
    var html = d.buckets.map(function (b) {
      var cap = cd.sums[b] || 0, pool = cd.pools[b] || 0, n = cd.counts[b] || 0;
      return '<tr data-b="' + esc(b) + '"' + (sel === b ? ' class="on"' : '') + '>' +
        '<td>' + esc(b) + '</td>' +
        '<td class="num">' + fmtUsd(cap) + '</td>' +
        '<td class="num">' + fmtUsd(pool) + '</td>' +
        '<td class="num">' + pct(poolTotal > 0 ? pool / poolTotal * 100 : 0, pool) + '</td>' +
        '<td class="num">' + fmtInt(n) + '</td></tr>';
    }).join('');
    html += '<tr class="tot"><td>total</td>' +
      '<td class="num">' + fmtUsd(cd.total) + '</td>' +
      '<td class="num">' + fmtUsd(poolTotal) + '</td><td class="num">100%</td>' +
      '<td class="num">' + fmtInt(mktTotal) + '</td></tr>';
    $('ybtbl').tBodies[0].innerHTML = html;
  }

  function activeChipList() {
    var out = [];
    DIMS.forEach(function (d) {
      if (state.filters[d.key] != null) out.push({ dim: d, bucket: state.filters[d.key] });
    });
    return out;
  }

  function renderChips(nFilters, fCount, fTotal) {
    var row = $('chipsRow');
    var chips = activeChipList();
    var sig = JSON.stringify(state.filters);
    if (sig !== lastChipSig) { // rebuild only on change so the pop anim fires once
      lastChipSig = sig;
      row.innerHTML = '';
      if (chips.length) {
        var lbl = document.createElement('span');
        lbl.className = 'flt';
        lbl.textContent = 'Filtering:';
        row.appendChild(lbl);
        chips.forEach(function (c) {
          var chip = document.createElement('span');
          chip.className = 'chip';
          chip.appendChild(document.createTextNode(c.dim.chip(c.bucket)));
          var x = document.createElement('button');
          x.textContent = '×';
          x.setAttribute('aria-label', 'remove filter');
          x.addEventListener('click', function () { toggleFilter(c.dim.key, c.bucket); });
          chip.appendChild(x);
          row.appendChild(chip);
        });
        var clear = document.createElement('button');
        clear.className = 'clearall';
        clear.textContent = 'clear all';
        clear.addEventListener('click', function () {
          state.filters = {};
          state.page = 1;
          render();
        });
        row.appendChild(clear);
      }
      var sum = document.createElement('span');
      sum.className = 'summary';
      sum.id = 'summary';
      row.appendChild(sum);
    }
    var summary = $('summary');
    if (chips.length) {
      summary.textContent = chips.map(function (c) { return c.bucket; }).join(' ∩ ') +
        ' · ' + fmtInt(fCount) + ' of ' + fmtInt(S.meta.count) + ' markets';
    } else {
      summary.textContent = fmtInt(S.meta.count) + ' rewarded markets · ' +
        fmtUsd(S.meta.total) + '/day';
    }
  }

  function renderTable(rows) {
    var col = COLS.filter(function (c) { return c.key === state.sort.key; })[0] || COLS[4];
    var dir = state.sort.dir === 'asc' ? 1 : -1;
    var sorted = rows.slice().sort(function (a, b) {
      var va = col.val(a), vb = col.val(b);
      if (typeof va === 'string') return va.localeCompare(vb) * dir;
      return (va === vb ? 0 : va < vb ? -1 : 1) * dir;
    });

    var last = Math.max(1, Math.ceil(sorted.length / PAGE_SIZE));
    if (state.page > last) state.page = last;
    var start = (state.page - 1) * PAGE_SIZE;
    var slice = sorted.slice(start, start + PAGE_SIZE);

    $('secttl').textContent = 'Markets — ' + fmtInt(sorted.length) + ' · click a header to sort';

    // header sort indicators
    var ths = $('tbl').tHead.rows[0].cells;
    COLS.forEach(function (c, i) {
      ths[i].classList.toggle('on', c.key === state.sort.key);
      ths[i].textContent = c.label +
        (c.key === state.sort.key ? (state.sort.dir === 'desc' ? ' ↓' : ' ↑') : '');
    });

    var pillCls = { 'no farmers': 'nf', 'thin': 'th', 'contested': 'ct' };
    $('tbl').tBodies[0].innerHTML = slice.map(function (m) {
      var q = m.slug
        ? '<a href="https://polymarket.com/market/' + esc(m.slug) + '" target="_blank" rel="noopener">' + esc(m.q) + '</a>'
        : esc(m.q);
      return '<tr>' +
        '<td class="q">' + q + '</td>' +
        '<td class="hm">' + esc(m.c) + '</td>' +
        '<td class="num">' + fmtSpread(m.sp) + '</td>' +
        '<td class="num hm">' + fmtVol(m.vn) + '</td>' +
        '<td class="num">' + fmtReward(m.rw) + '</td>' +
        '<td class="num hm">' + fmtYield(m.y) + '</td>' +
        '<td><span class="pill ' + pillCls[m.cb] + '">' + esc(m.cb) + '</span></td>' +
        '</tr>';
    }).join('');

    renderPager(sorted.length, last, start, slice.length);
  }

  function pageList(cur, last) {
    var want = { 1: true };
    want[last] = true;
    [cur - 1, cur, cur + 1].forEach(function (p) { want[p] = true; });
    if (cur <= 3) { want[2] = true; want[3] = true; }
    if (cur >= last - 2) { want[last - 1] = true; want[last - 2] = true; }
    var pages = Object.keys(want).map(Number)
      .filter(function (p) { return p >= 1 && p <= last; })
      .sort(function (a, b) { return a - b; });
    var out = [], prev = 0;
    pages.forEach(function (p) {
      if (p - prev > 1) out.push('gap');
      out.push(p);
      prev = p;
    });
    return out;
  }

  function renderPager(total, last, start, shown) {
    var pager = $('pager');
    pager.innerHTML = '';
    var info = document.createElement('span');
    info.className = 'pginfo';
    info.textContent = total === 0 ? '0 of 0'
      : fmtInt(start + 1) + '–' + fmtInt(start + shown) + ' of ' + fmtInt(total);
    pager.appendChild(info);

    function btn(label, page, opts) {
      var b = document.createElement('button');
      b.className = 'pgbtn' + ((opts && opts.on) ? ' on' : '') + ((opts && opts.dis) ? ' dis' : '');
      b.textContent = label;
      b.addEventListener('click', function () { state.page = page; render(); });
      pager.appendChild(b);
    }
    btn('‹', state.page - 1, { dis: state.page <= 1 });
    pageList(state.page, last).forEach(function (p) {
      if (p === 'gap') {
        var g = document.createElement('span');
        g.className = 'pggap';
        g.textContent = '…';
        pager.appendChild(g);
      } else {
        btn(String(p), p, { on: p === state.page });
      }
    });
    btn('›', state.page + 1, { dis: state.page >= last });
  }

  // --- boot ------------------------------------------------------------------

  // "updated · 2 Jul 10:54 UTC (43 min ago)". meta.ts is "YYYY-MM-DDTHH-MM-SSZ";
  // the relative age speaks for itself — no staleness badge or cadence label.
  (function () {
    var txt = 'updated · ' + S.meta.label;
    var iso = String(S.meta.ts || '').replace(/T(\d\d)-(\d\d)-(\d\d)Z$/, 'T$1:$2:$3Z');
    var ageMin = (Date.now() - Date.parse(iso)) / 60000;
    if (isFinite(ageMin) && ageMin >= 0) {
      var rel = ageMin < 1 ? 'just now'
        : ageMin < 60 ? Math.round(ageMin) + ' min ago'
        : ageMin < 2880 ? (ageMin / 60).toFixed(1) + ' h ago'
        : Math.round(ageMin / 1440) + ' d ago';
      txt += ' (' + rel + ')';
    }
    $('snapTs').textContent = txt;
  })();

  // "Copy data guide for your agent" — puts the #agentGuide markdown on the
  // clipboard so a visitor can paste it into any AI agent. The execCommand
  // fallback covers non-secure contexts (e.g. opening the page via file://).
  (function () {
    var btn = $('agentBtn');
    var guide = document.getElementById('agentGuide').textContent.trim() + '\n';
    var idle = btn.textContent;
    function done(ok) {
      btn.textContent = ok ? '✓ copied — paste it into your agent' : 'copy failed';
      btn.classList.toggle('ok', ok);
      setTimeout(function () { btn.textContent = idle; btn.classList.remove('ok'); }, 2500);
    }
    function fallback() {
      var ta = document.createElement('textarea');
      ta.value = guide;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      var ok = false;
      try { ok = document.execCommand('copy'); } catch (e) {}
      document.body.removeChild(ta);
      return ok;
    }
    btn.addEventListener('click', function () {
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(guide).then(function () { done(true); }, function () { done(fallback()); });
      } else {
        done(fallback());
      }
    });
  })();

  buildCharts();
  buildTableHead();
  $('ybtbl').tBodies[0].addEventListener('click', function (e) {
    var tr = e.target.closest('tr[data-b]');
    if (tr) toggleFilter('yb', tr.getAttribute('data-b'));
  });
  render();
})();
