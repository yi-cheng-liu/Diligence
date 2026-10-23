// Diagrams and charts inside reports. After a report's Markdown is turned into HTML, enhanceMarkdown(element) draws
// ```mermaid blocks as Mermaid diagrams and ```chart blocks (JSON, see below) as charts made with Recharts. The
// libraries load only when a report has something to draw.
//
//   ```chart
//   {"type": "bar", "title": "Revenue by year", "unit": "$B", "x": "year", "series": ["Visa", "Mastercard"],
//    "data": [{"year": "2024", "Visa": 35.9, "Mastercard": 28.2}, {"year": "2025", "Visa": 39.4, "Mastercard": 31.4}]}
//   ```
// type is bar, line, area or pie (pie data is [{"name": ..., "value": ...}]); "stacked": true stacks bars or areas.
//
// enhanceMarkdown(element, { auto: true }) also draws what a report has in plain text: a table of figures gets a chart
// above it (years as a line or bars, shares of a whole as a donut, a list of companies or segments as sorted bars),
// and a chain written as "A → B → C" becomes a row of steps. Signed changes like +12% or −3.4% are colored.
(function () {
  const LIBS = {
    mermaid: ['/desk/vendor/mermaid-10.9.1.min.js'],
    recharts: ['/desk/vendor/react-18.3.1.min.js', '/desk/vendor/react-dom-18.3.1.min.js',
      '/desk/vendor/prop-types-15.8.1.min.js', '/desk/vendor/recharts-2.12.7.js'],
  };
  const loading = {};
  const script = src => new Promise((ok, fail) => {
    if (document.querySelector('script[data-src="' + src + '"]')) return ok();
    const s = document.createElement('script'); s.src = src; s.dataset.src = src; s.onload = ok; s.onerror = () => fail(new Error('could not load ' + src));
    document.head.appendChild(s);
  });
  // one after another: Recharts needs React and PropTypes in place first
  const need = name => loading[name] || (loading[name] = LIBS[name].reduce((p, src) => p.then(() => {
    if (name === 'recharts' && /react\.production/.test(src) && window.React) return;
    if (name === 'recharts' && /react-dom/.test(src) && window.ReactDOM) return;
    return script(src);
  }), Promise.resolve()));

  const css = document.createElement('style');
  css.textContent = '.md-fig{margin:24px 0 30px;padding:16px 18px 12px;border:1px solid color-mix(in srgb,currentColor 14%,transparent);border-radius:10px;max-width:none!important;overflow-x:auto}'
    + '.md-fig figcaption{font-size:13px;font-weight:600;margin:0 0 10px;opacity:.9}.md-fig .md-unit{font-weight:400;opacity:.6;margin-left:6px}'
    + '.md-fig.mmd{text-align:center}.md-fig.mmd svg{max-width:100%;height:auto}.md-fig .md-bad{font-size:12px;opacity:.7;text-align:left}'
    + '.md-fig.auto{margin:22px 0 10px;padding:14px 16px 8px;background:color-mix(in srgb,currentColor 3%,transparent)}.md-fig.auto+.tw,.md-fig.auto+table{margin-top:8px}'
    + '.md-fig .md-from{font-weight:400;font-size:11.5px;opacity:.5;margin-left:10px}'
    + '.md-flow{margin:14px 0 22px;max-width:none!important}.md-flow figcaption{font-size:12.5px;font-weight:600;margin:0 0 8px;opacity:.85}'
    + '.md-flow ol{list-style:none;margin:0;padding:0;display:flex;flex-wrap:wrap;align-items:stretch;gap:8px 0;counter-reset:st}'
    + '.md-flow li{counter-increment:st;display:flex;align-items:center;margin:0!important}'
    + '.md-flow li>span{display:grid;gap:2px;padding:8px 12px;border-radius:9px;border:1px solid color-mix(in srgb,currentColor 16%,transparent);background:color-mix(in srgb,#b5abfc 9%,transparent);font-size:13.5px;line-height:1.45;max-width:17em}'
    + '.md-flow li>span::before{content:counter(st);font-size:10.5px;letter-spacing:.06em;color:#b5abfc;font-weight:600}'
    + '.md-flow li:not(:last-child)::after{content:"";width:26px;height:10px;flex:none;background:no-repeat center/26px 10px url("data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 viewBox=%270 0 26 10%27%3E%3Cpath d=%27M2 5h20m-4-4 4 4-4 4%27 stroke=%27%239397ab%27 stroke-width=%271.4%27 fill=%27none%27 stroke-linecap=%27round%27/%3E%3C/svg%3E")}'
    + '.md-flow li:last-child>span{border-color:color-mix(in srgb,#b5abfc 55%,transparent)}'
    + '.md-up{color:#81c995}.md-down{color:#f28b82}'
    + '.md-fig.txt{text-align:center}.md-fig.txt svg{max-width:100%;height:auto}.md-fig.txt .md-wait{font-size:12px;opacity:.6;text-align:left;margin:0 0 8px;display:flex;gap:8px;align-items:center}'
    + '.md-fig.txt .md-wait::before{content:"";width:10px;height:10px;border-radius:50%;border:2px solid currentColor;border-right-color:transparent;animation:mdspin .8s linear infinite}'
    + '@keyframes mdspin{to{transform:rotate(360deg)}}'
    + '.md-fig.txt details{text-align:left;margin-top:10px;font-size:12px}.md-fig.txt summary{cursor:pointer;opacity:.6;width:max-content}.md-fig.txt summary:hover{opacity:1}'
    + '.md-fig.txt pre{margin:8px 0 0!important;text-align:left}';
  document.head.appendChild(css);

  const PALETTE = ['#8ab4f8', '#f28b82', '#81c995', '#fdd663', '#c58af9', '#78d9ec', '#fcad70', '#ff8bcb', '#a8dab5', '#d7aefb'];
  let mmdN = 0;

  // a wide diagram is scrolled across rather than shrunk until its words cannot be read
  function readable(box) {
    const svg = box.querySelector('svg'); if (!svg) return;
    const vb = (svg.getAttribute('viewBox') || '').split(/[\s,]+/).map(Number), w = vb[2];
    if (!w || !box.clientWidth || w * 0.8 <= box.clientWidth) return;
    svg.style.maxWidth = 'none'; svg.style.width = Math.round(w * 0.8) + 'px'; svg.style.height = 'auto';
    box.style.overflowX = 'auto'; box.style.textAlign = 'left';
  }

  async function mermaidSvg(code) {
    await drawMermaid([]);
    const id = 'mmd' + (++mmdN);
    try { return (await window.mermaid.render(id, code)).svg; }
    catch (e) { const stray = document.getElementById('d' + id); if (stray) stray.remove(); throw e; }
  }

  async function drawMermaid(blocks) {
    await need('mermaid');
    if (!drawMermaid.ready) {
      drawMermaid.ready = true;
      window.mermaid.initialize({ startOnLoad: false, securityLevel: 'strict', theme: 'dark',
        themeVariables: { background: 'transparent', primaryColor: '#2b2e40', primaryTextColor: '#e9e9ed', primaryBorderColor: '#6b6f8a',
          secondaryColor: '#24273a', tertiaryColor: '#1e2030', lineColor: '#9397ab', edgeLabelBackground: '#1b1d2b', fontSize: '14px' },
        flowchart: { curve: 'basis', htmlLabels: true },
        // compact: seven parties still fit a reading column with words that can be read
        sequence: { width: 104, actorMargin: 18, boxMargin: 6, boxTextMargin: 4, noteMargin: 6, messageMargin: 30, mirrorActors: false, wrap: true, actorFontSize: 13, messageFontSize: 13, noteFontSize: 12 } });
    }
    for (const pre of blocks) {
      const code = pre.textContent, fig = document.createElement('figure'); fig.className = 'md-fig mmd';
      pre.replaceWith(fig);
      const id = 'mmd' + (++mmdN);
      try { const { svg } = await window.mermaid.render(id, code); const box = document.createElement('div'); box.innerHTML = svg; fig.appendChild(box); readable(box); }
      catch (e) {
        const stray = document.getElementById('d' + id); if (stray) stray.remove();
        fig.innerHTML = ''; const note = document.createElement('div'); note.className = 'md-bad'; note.textContent = 'This diagram could not be drawn, so here is its text:';
        const p = document.createElement('pre'); p.textContent = code; fig.append(note, p);
      }
    }
  }

  function chart(spec) {
    const R = window.Recharts, h = window.React.createElement;
    const tick = { fill: '#9397ab', fontSize: 12 }, grid = h(R.CartesianGrid, { stroke: 'rgba(255,255,255,.08)', vertical: false });
    const tip = h(R.Tooltip, { contentStyle: { background: '#232532', border: '1px solid #3f424d', borderRadius: 8, color: '#e9e9ed' }, labelStyle: { color: '#cfd3e5' },
      formatter: v => (typeof v === 'number' ? v.toLocaleString('en-US', { maximumFractionDigits: 2 }) : v) + (spec.unit ? ' ' + spec.unit : '') });
    const legend = h(R.Legend, { wrapperStyle: { fontSize: 12, color: '#cfd3e5' } });
    const data = Array.isArray(spec.data) ? spec.data : [];
    if (spec.type === 'pie') {
      return h(R.PieChart, null, tip, legend, h(R.Pie, { data, dataKey: 'value', nameKey: 'name', isAnimationActive: false, outerRadius: '78%', innerRadius: '42%', paddingAngle: 1,
        label: ({ percent }) => (percent * 100).toFixed(0) + '%' }, data.map((d, i) => h(R.Cell, { key: i, fill: PALETTE[i % PALETTE.length], stroke: 'none' }))));
    }
    const x = spec.x || Object.keys(data[0] || {})[0];
    const series = Array.isArray(spec.series) && spec.series.length ? spec.series : Object.keys(data[0] || {}).filter(k => k !== x && typeof data[0][k] === 'number');
    const fmt = v => typeof v === 'number' ? v.toLocaleString('en-US', { maximumFractionDigits: Math.abs(v) < 10 ? 2 : 1 }) : v;
    if (spec.horizontal) {   // a list of names: bars running across, the names readable down the side
      return h(R.BarChart, { data, layout: 'vertical', margin: { top: 2, right: 40, left: 4, bottom: 0 } },
        h(R.CartesianGrid, { stroke: 'rgba(255,255,255,.08)', horizontal: false }),
        h(R.XAxis, { type: 'number', tick, tickLine: false, axisLine: { stroke: '#3f424d' } }),
        h(R.YAxis, { type: 'category', dataKey: x, tick: Object.assign({}, tick, { fill: '#cfd3e5' }), tickLine: false, axisLine: false, width: spec.labelWidth || 120, interval: 0 }),
        tip, series.length > 1 ? legend : null,
        series.map((k, i) => h(R.Bar, { key: k, dataKey: k, name: k, fill: PALETTE[i % PALETTE.length], radius: [0, 3, 3, 0], isAnimationActive: false, maxBarSize: 18,
          label: series.length === 1 ? { position: 'right', fill: '#cfd3e5', fontSize: 11, formatter: fmt } : false })));
    }
    const kind = spec.type === 'line' ? 'Line' : spec.type === 'area' ? 'Area' : 'Bar';
    const marks = series.map((k, i) => h(R[kind], Object.assign({ key: k, dataKey: k, name: k, stroke: PALETTE[i % PALETTE.length], fill: PALETTE[i % PALETTE.length], isAnimationActive: false },
      kind === 'Bar' ? { radius: [3, 3, 0, 0], stackId: spec.stacked ? 's' : undefined } : kind === 'Area' ? { fillOpacity: .25, strokeWidth: 2, stackId: spec.stacked ? 's' : undefined } : { strokeWidth: 2, dot: data.length < 30 })));
    return h(R[kind + 'Chart'], { data, margin: { top: 6, right: 12, left: 0, bottom: 0 } }, grid,
      h(R.XAxis, { dataKey: x, tick, tickLine: false, axisLine: { stroke: '#3f424d' } }), h(R.YAxis, { tick, tickLine: false, axisLine: false, width: 56 }), tip, series.length > 1 ? legend : null, marks);
  }

  function renderChart(box, spec) {
    window.ReactDOM.createRoot(box).render(window.React.createElement(window.Recharts.ResponsiveContainer, { width: '100%', height: '100%' }, chart(spec)));
  }

  async function drawCharts(blocks) {
    await need('recharts');
    for (const pre of blocks) {
      const code = pre.textContent, fig = document.createElement('figure'); fig.className = 'md-fig';
      pre.replaceWith(fig);
      let spec; try { spec = JSON.parse(code); } catch (e) { spec = null; }
      if (!spec || (!Array.isArray(spec.data) || !spec.data.length)) {
        const note = document.createElement('div'); note.className = 'md-bad'; note.textContent = 'This chart could not be read, so here is its data:';
        const p = document.createElement('pre'); p.textContent = code; fig.append(note, p); continue;
      }
      if (spec.title) { const c = document.createElement('figcaption'); c.textContent = spec.title; if (spec.unit) { const u = document.createElement('span'); u.className = 'md-unit'; u.textContent = spec.unit; c.appendChild(u); } fig.appendChild(c); }
      const box = document.createElement('div'); box.style.height = (spec.type === 'pie' ? 300 : 320) + 'px'; fig.appendChild(box);
      renderChart(box, spec);
    }
  }

  // ---- figures written as text ----
  const SCALE = { bn: 1e9, b: 1e9, billion: 1e9, '十億': 1e9, mn: 1e6, m: 1e6, million: 1e6, k: 1e3, '千': 1e3, tn: 1e12, t: 1e12, trillion: 1e12, '兆': 1e12, '億': 1e8, '亿': 1e8, '萬': 1e4, '万': 1e4 };
  const NUM = /^(?:約|约|~|≈|c\.|ca\.)?\s*([+\-−–]?)\s*(\()?\s*(US\$|HK\$|NT\$|A\$|C\$|S\$|R\$|NTD|RMB|CNY|USD|HKD|EUR|JPY|TWD|\$|€|£|¥|₩|₹)?\s*(\d+(?:\.\d+)?)\s*(\))?\s*(%|pp|ppt|x|倍|bn|billion|b|mn|million|m|k|tn|trillion|t|十億|億|亿|萬|万|千|兆)?\s*(?:元|美元|港元|港幣|台幣|新台幣|日圓|日元|人民幣|人民币|歐元|欧元|euros?|dollars?|股)?$/i;
  // "US$1,234.5m" -> {v: 1234.5, scale: 1e6, kind: 'n', cur: 'US$'}; text that is not just a figure -> null
  function figure(text) {
    const t = text.replace(/[*_`]/g, '').replace(/(\d)[,，](?=\d{3}(?!\d))/g, '$1').replace(/[¹²³⁴⁵⁶⁷⁸⁹⁰†‡*]+$/, '').replace(/\s*[（(](?:估算?|預估|预估|est\.?|e|E|TTM)[）)]$/, '').replace(/(\d)E$/, '$1').trim();
    const m = t.match(NUM);
    if (!m) return null;
    let v = parseFloat(m[4]);
    if ((m[1] && m[1] !== '+') || (m[2] && m[5])) v = -v;
    const u = (m[6] || '').toLowerCase();
    return { v, scale: SCALE[u] || SCALE[m[6]] || 1, kind: /^(%|pp|ppt)$/.test(u) ? '%' : /^(x|倍)$/.test(u) ? 'x' : 'n', cur: (m[3] || '').toUpperCase() };
  }
  const TIME = /^(?:(?:FY|CY|財年|财年)\s*'?)?(?:19|20)?\d{2}(?:年度?|年報|A|E|e|F)?(?:\s*[\/\-.]?\s*(?:Q[1-4]|H[12]|[1-4]Q|\d{1,2}月?|第[一二三四1-4]季度?))?(?:\s*(?:TTM|LTM|預估|预估|估))?$|^(?:Q[1-4]|H[12]|[1-4]Q)\s*'?(?:19|20)?\d{2}$|^TTM$|^LTM$/i;
  const TOTAL = /^(?:total|sum|subtotal|合計|合计|總計|总计|小計|小计|總和|总和|全部|全體|all)(?=$|[\s:：(（,，])|^(?:總|总)(?:額|额|收入|營收|营收)?$/i;
  const KEY = /市值|營收|营收|收入|銷售|销售|revenue|sales|淨利|净利|利潤|利润|income|profit|earnings|EPS|毛利|margin|率|佔|占|比重|份額|份额|share|weight|權重|权重|cap|金額|金额|value|規模|规模|增速|增長|增长|growth|FCF|現金流|现金流|cash|估值|P\/?E|EV|ROE|ROIC|股價|股价|price|產能|产能|出貨|出货|capex|資本開支|资本开支/i;
  const clean = s => s.replace(/[*_`]/g, '').replace(/[¹²³⁴⁵⁶⁷⁸⁹⁰]+/g, '').replace(/\s+/g, ' ').trim();
  const UNITISH = /\$|€|£|¥|%|億|亿|萬|万|百萬|百万|千|bn|mn|\bm\b|\bB\b|\bM\b|\bK\b|元|股|倍|USD|HKD|RMB|TWD|NTD|EUR|JPY|CNY/;
  const headUnit = s => { const u = ((s.match(/[（(]([^（）()]{1,12})[）)]\s*$/) || [])[1] || '').trim(); return UNITISH.test(u) ? u : ''; };
  // a first column that names different measures ("Revenue", "Margin", "P/E"): its figures do not belong on one axis
  const MEASURES = /^(?:指標|指标|項目|项目|metrics?|items?|參數|参数|假設|假设|估值指標|估值指标|口徑|口径|parameters?|measures?|KPIs?|數據|数据|要素|科目|關鍵數據|关键数据|財務指標|财务指标|財務數據|财务数据|核心指標|核心指标)$/i;
  const unitOf = (k, shown) => k.unit || (k.kind === '%' ? '%' : k.kind === 'x' ? '×' : k.cur + ({ 1e3: 'K', 1e4: '萬', 1e6: 'M', 1e8: '億', 1e9: 'B', 1e12: 'T' }[shown] || ''));
  const yearOf = l => parseInt((l.match(/(?:19|20)\d{2}|\d{2}/) || [])[0], 10);
  // the figures in one row or column of a table, put on one scale
  function measure(cells) {
    const figs = cells.map(x => figure(x)), got = figs.filter(Boolean), count = {};
    if (!got.length) return null;
    got.forEach(f => { count[f.scale] = (count[f.scale] || 0) + 1; });
    const kind = got.filter(f => f.kind === '%').length > got.length / 2 ? '%' : got.filter(f => f.kind === 'x').length > got.length / 2 ? 'x' : 'n';
    const show = +Object.keys(count).sort((a, b) => count[b] - count[a])[0];
    const curs = new Set(got.filter(f => f.cur).map(f => f.cur));
    const nums = figs.map(f => f && f.kind === kind ? f.v * f.scale / show : null);
    const mags = nums.filter(v => v != null && v !== 0).map(Math.abs).sort((a, b) => a - b);
    return { kind, show, nums, cur: curs.size === 1 ? [...curs][0] : '', mixedCur: curs.size > 1,
      same: got.filter(f => f.kind === kind).length / got.length, mag: mags.length ? mags[mags.length >> 1] : 0,
      spread: mags.length > 1 && mags[0] ? mags[mags.length - 1] / mags[0] : 1 };
  }
  // years across the top, a measure on each row: one line or set of bars per measure, oldest year first
  function byYearAcross(head, rows) {
    const tcols = head.map((hd, c) => c > 0 && TIME.test(hd) ? c : -1).filter(c => c > 0);
    if (tcols.length < 3 || tcols.length < (head.length - 1) * 0.5) return null;
    const ms = rows.map(r => Object.assign({ name: r[0] || '' }, measure(tcols.map(c => r[c] || '')) || {}))
      .filter(m => m.nums && m.nums.filter(v => v != null).length >= Math.max(3, tcols.length * 0.75) && m.same >= 0.85 && !m.mixedCur && m.name);
    if (!ms.length) return null;
    const groups = [];
    ms.forEach(m => { const g = groups.find(g => g[0].kind === m.kind && g[0].cur === m.cur && g[0].show === m.show && Math.max(g[0].mag, m.mag) / Math.max(Math.min(g[0].mag, m.mag), 1e-9) <= 12); g ? g.push(m) : groups.push([m]); });
    const score = g => Math.min(g.length, 3) * 2 + (g.some(m => KEY.test(m.name)) ? 3 : 0);
    const best = groups.sort((a, b) => score(b) - score(a))[0].slice(0, 3);
    let data = tcols.map((c, i) => { const d = { label: head[c] }; best.forEach(m => { d[m.name] = m.nums[i]; }); return d; });
    if (yearOf(head[tcols[0]]) > yearOf(head[tcols[tcols.length - 1]])) data = data.reverse();
    const series = best.map(m => m.name);
    return { type: data.length >= 7 ? 'line' : 'bar', data, x: 'label', series, unit: unitOf({ kind: best[0].kind, cur: best[0].cur }, best[0].show), caption: series.join(' · '), height: 260 };
  }

  // what a table can show as a chart, or null
  function tableChart(table) {
    const head = [...table.querySelectorAll('thead th')].map(th => clean(th.textContent));
    const rows = [...table.querySelectorAll('tbody tr')].map(tr => [...tr.children].map(td => clean(td.textContent)));
    if (head.length < 2 || rows.length < 2 || rows.length > 40) return null;
    const across = byYearAcross(head, rows);
    if (across) return across;
    if (rows.length < 3) return null;
    const cols = head.map((hd, c) => {
      const cells = rows.map(r => r[c] || ''), filled = cells.filter(x => x && !/^[-–—]$|^n\/?a$/i.test(x));
      const figs = cells.map(x => figure(x)), got = figs.filter(Boolean);
      return { c, hd, unit: headUnit(hd), figs, numeric: filled.length >= 3 && got.length >= Math.max(3, filled.length * 0.75),
        years: got.length && got.every(f => f.kind === 'n' && f.scale === 1 && Number.isInteger(f.v) && f.v >= 1900 && f.v <= 2100) };
    });
    // the names: the first text column filled on every row (a column left blank under a merged heading is a grouping)
    const names = cols.filter(k => !k.numeric).map(k => { const f = rows.map(r => (r[k.c] || '').trim()).filter(Boolean), L = f.map(x => x.length).sort((a, b) => a - b);
      return Object.assign(k, { filled: f.length / rows.length, short: L.length && L[L.length >> 1] <= 28 }); }).filter(k => k.short);
    const label = names.find(k => k.filled === 1) || names.find(k => k.filled >= 0.8);
    if (!label) return null;
    const vals = cols.filter(k => k.numeric && !k.years && k.c !== label.c && !/^(#|no\.?|序號?|序号|排名|rank|名次|代碼|代码|ticker|年份?|year)$/i.test(k.hd));
    if (!vals.length) return null;
    const measures = MEASURES.test(label.hd);
    vals.forEach(k => { Object.assign(k, measure(rows.map(r => r[k.c] || ''))); });
    // keep a column only when its figures are one kind of thing: all percentages, all in one currency, ...
    const fit = vals.filter(k => k.same >= 0.85 && !k.mixedCur && (!measures || k.spread <= 20));
    if (!fit.length) return null;
    vals.length = 0; vals.push(...fit);
    // columns go on one chart only when they measure the same thing on the same scale
    const groups = [];
    vals.forEach(k => {
      const g = groups.find(g => { const a = g[0]; return a.kind === k.kind && a.unit === k.unit && a.cur === k.cur && a.show === k.show && (!a.mag || !k.mag || Math.max(a.mag, k.mag) / Math.min(a.mag, k.mag) <= 12); });
      g ? g.push(k) : groups.push([k]);
    });
    const score = g => Math.min(g.length, 4) * 2 + (g.some(k => KEY.test(k.hd)) ? 3 : 0) - g[0].c * 0.1;
    const best = groups.sort((a, b) => score(b) - score(a))[0].slice(0, 4);
    const labels = rows.map(r => r[label.c] || '');
    const timeish = labels.filter(l => TIME.test(l)).length >= labels.length * 0.7;
    let data = rows.map((r, i) => {
      const d = { label: labels[i].length > 22 ? labels[i].slice(0, 21) + '…' : labels[i] };
      best.forEach(k => { d[k.hd] = k.nums[i]; });
      return d;
    }).filter((d, i) => labels[i] && best.some(k => d[k.hd] != null) && (timeish || !TOTAL.test(labels[i])));
    if (data.length < 3) return null;
    const flat = data.flatMap(d => best.map(k => d[k.hd])).filter(v => v != null);
    if (new Set(flat).size < 2) return null;
    const series = best.map(k => k.hd);
    const unit = unitOf(best[0], best[0].show);
    const caption = series.join(' · ') + (label.hd ? ' — ' + label.hd : '');
    if (timeish) {
      if (yearOf(labels[0]) > yearOf(labels[labels.length - 1])) data = data.reverse();   // newest first in the table: oldest first on the chart
      return { type: data.length >= 7 ? 'line' : 'bar', data, x: 'label', series, unit, caption, height: 260 };
    }
    const one = series[0], sum = data.reduce((a, d) => a + (d[one] || 0), 0);
    if (series.length === 1 && best[0].kind === '%' && data.length <= 10 && data.every(d => d[one] > 0) && sum >= 90 && sum <= 101)
      return { type: 'pie', data: data.map(d => ({ name: d.label, value: d[one] })), unit, caption, height: 280 };
    if (series.length === 1) data = data.slice().sort((a, b) => (b[one] ?? -Infinity) - (a[one] ?? -Infinity));
    const longest = Math.max(...data.map(d => [...d.label].reduce((w, ch) => w + (/[\u2E80-\uFFFF]/.test(ch) ? 2 : 1), 0)));
    return { type: 'bar', horizontal: true, data, x: 'label', series, unit, caption, labelWidth: Math.min(200, Math.max(60, longest * 6.6 + 10)), height: 40 + data.length * (series.length > 1 ? 34 : 26) + (series.length > 1 ? 24 : 0) };
  }

  async function autoCharts(root) {
    const found = [];
    root.querySelectorAll('table').forEach(t => { if (!t.closest('.md-fig')) { const spec = tableChart(t); if (spec) found.push([t, spec]); } });
    if (!found.length) return;
    await need('recharts');
    found.slice(0, 24).forEach(([t, spec]) => {
      const fig = document.createElement('figure'); fig.className = 'md-fig auto';
      const cap = document.createElement('figcaption'); cap.textContent = spec.caption;
      if (spec.unit) { const u = document.createElement('span'); u.className = 'md-unit'; u.textContent = spec.unit; cap.appendChild(u); }
      const from = document.createElement('span'); from.className = 'md-from'; from.textContent = 'from the table below'; cap.appendChild(from);
      const box = document.createElement('div'); box.style.height = spec.height + 'px';
      fig.append(cap, box);
      const at = t.parentElement && t.parentElement.classList.contains('tw') ? t.parentElement : t;
      at.parentNode.insertBefore(fig, at);
      renderChart(box, spec);
    });
  }

  // "賣騰訊 → 回購自己 → 每股 NAV 增厚" as numbered steps
  const ARROW = /\s*(?:→|⟶|➜|➝|->|=>|⇒)\s*/;
  function autoFlows(root) {
    let n = 0;
    root.querySelectorAll('p, li').forEach(el => {
      if (n >= 30 || el.closest('table, pre, figure, .md-flow') || el.querySelector('p, li')) return;
      const text = el.textContent.replace(/\s+/g, ' ').trim();
      let best = null;
      text.split(/(?<=[。；;！？!?])\s*|(?<=\.)\s+(?=[A-Z])/).forEach(sent => {
        const s = sent.replace(/[。；;.]+$/, '').trim();
        if ((s.match(new RegExp(ARROW.source, 'g')) || []).length < 2) return;
        const lab = s.match(/^([^：:→]{1,24})[：:]\s*(.+)$/);
        const body = lab ? lab[2] : s;
        const steps = body.split(ARROW).map(x => { x = x.trim(); const o = (x.match(/[（(]/g) || []).length, c = (x.match(/[）)]/g) || []).length;
          return (c > o ? x.replace(/[）)]$/, '') : o > c ? x.replace(/^[（(]/, '') : x).trim(); }).filter(Boolean);
        if (steps.length < 3 || steps.length > 9 || steps.some(x => x.length > 48)) return;
        if (steps.filter(x => figure(x)).length >= steps.length / 2) return;   // "$487m → $913m": a change, not a chain
        if (!best || steps.length > best.steps.length) best = { steps, label: lab ? lab[1].trim() : '', whole: s.length >= text.replace(/[。；;.]+$/, '').length * 0.85 };
      });
      if (!best) return;
      n++;
      const fig = document.createElement('figure'); fig.className = 'md-flow';
      if (best.label) { const c = document.createElement('figcaption'); c.textContent = best.label; fig.appendChild(c); }
      const ol = document.createElement('ol');
      best.steps.forEach(x => { const li = document.createElement('li'), sp = document.createElement('span'); sp.textContent = x; li.appendChild(sp); ol.appendChild(li); });
      fig.appendChild(ol);
      if (el.tagName === 'LI') el.appendChild(fig);
      else if (best.whole) el.replaceWith(fig);
      else el.after(fig);
    });
  }

  // A diagram drawn with text (boxes and arrows in a plain code block): the server has Claude redraw it as Mermaid
  // once and keeps it, so it is drawn properly from then on. The text stays one click away.
  function isTextDiagram(code) {
    const lines = code.split('\n').filter(l => l.trim());
    if (lines.length < 3) return false;
    if (/^\s*(?:import|from|def|class|function|const|let|var|SELECT|#include|package)\b|[;{}]\s*$/m.test(code) && !/[┌└├─│]/.test(code)) return false;
    const boxChars = (code.match(/[┌┐└┘├┤┬┴┼─│═║╔╗╚╝▶▼◀▲►◄]/g) || []).length;
    const arrows = (code.match(/-{2,}>|<-{2,}|={2,}>|\.{3,}>|→|←|↓|↑|⟶|^\s*\|?\s*[v^]\s*$|\|\s*$/gm) || []).length;
    const boxes = (code.match(/\[[^\]\n]{2,40}\]|\+-{3,}\+/g) || []).length;
    return boxChars >= 6 || (arrows >= 3 && (boxes >= 2 || /^\s*\|/m.test(code)));
  }
  async function textDiagrams(root, url) {
    const blocks = [...root.querySelectorAll('pre > code')].filter(c => !/language-(?!text|plaintext|txt|ascii|diagram)/.test(c.className) && isTextDiagram(c.textContent)).map(c => c.parentElement);
    for (const pre of blocks.slice(0, 12)) {
      const code = pre.textContent, fig = document.createElement('figure'); fig.className = 'md-fig txt';
      const wait = document.createElement('div'); wait.className = 'md-wait'; wait.textContent = 'Drawing this diagram from its text…';
      pre.replaceWith(fig); fig.append(wait, pre);
      try {
        const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text: code }) });
        const j = await r.json(); if (!r.ok) throw new Error(j.error || 'the server answered ' + r.status);
        // in a sequence diagram ";" ends a line, so one inside a note or message would cut it in two (an escape like #quot; stays)
        const svg = await mermaidSvg(/^\s*sequenceDiagram/.test(j.mermaid) ? j.mermaid.replace(/#59;/g, ',').replace(/(#\w+;)|;/g, (m, ent) => ent || ',') : j.mermaid);
        const box = document.createElement('div'); box.innerHTML = svg;
        const det = document.createElement('details'), sum = document.createElement('summary'); sum.textContent = 'Show the original text';
        det.append(sum, pre); fig.replaceChildren(box, det); readable(box);
      } catch (e) {
        wait.className = 'md-bad'; wait.textContent = 'Shown as text: ' + (e.message || 'it could not be redrawn');
      }
    }
  }

  // +12% green, −3.4% red, in tables
  function signs(root) {
    root.querySelectorAll('td').forEach(td => {
      if (td.children.length > 1) return;
      const t = td.textContent.replace(/[*\s]/g, '');
      if (/^\+\d[\d,.]*(%|pp|ppt|bps)?$/i.test(t)) td.classList.add('md-up');
      else if (/^[-−–]\d[\d,.]*(%|pp|ppt|bps)$/i.test(t)) td.classList.add('md-down');
    });
  }

  window.enhanceMarkdown = function (root, opts) {
    const pick = lang => [...root.querySelectorAll('pre > code.language-' + lang)].map(c => c.parentElement);
    const jobs = [], m = pick('mermaid'), c = pick('chart');
    if (opts && opts.auto) {
      signs(root); autoFlows(root);
      if (opts.diagrams) jobs.push(textDiagrams(root, opts.diagrams).catch(e => console.warn(e)));
      // a report that draws its own charts has picked which tables matter
      if (!c.length) jobs.push(autoCharts(root).catch(e => console.warn(e)));
    }
    if (m.length) jobs.push(drawMermaid(m).catch(e => console.warn(e)));
    if (c.length) jobs.push(drawCharts(c).catch(e => console.warn(e)));
    return Promise.all(jobs);
  };
})();
