// Diagrams and charts inside reports. After a report's Markdown is turned into HTML, enhanceMarkdown(element) draws
// ```mermaid blocks as Mermaid diagrams and ```chart blocks (JSON, see below) as charts made with Recharts. The
// libraries load only when a report has something to draw.
//
//   ```chart
//   {"type": "bar", "title": "Revenue by year", "unit": "$B", "x": "year", "series": ["Visa", "Mastercard"],
//    "data": [{"year": "2024", "Visa": 35.9, "Mastercard": 28.2}, {"year": "2025", "Visa": 39.4, "Mastercard": 31.4}]}
//   ```
// type is bar, line, area or pie (pie data is [{"name": ..., "value": ...}]); "stacked": true stacks bars or areas.
(function () {
  const LIBS = {
    mermaid: ['https://cdn.jsdelivr.net/npm/mermaid@10.9.1/dist/mermaid.min.js'],
    recharts: ['https://unpkg.com/react@18.3.1/umd/react.production.min.js', 'https://unpkg.com/react-dom@18.3.1/umd/react-dom.production.min.js',
      'https://unpkg.com/prop-types@15.8.1/prop-types.min.js', 'https://unpkg.com/recharts@2.12.7/umd/Recharts.js'],
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
    + '.md-fig.mmd{text-align:center}.md-fig.mmd svg{max-width:100%;height:auto}.md-fig .md-bad{font-size:12px;opacity:.7;text-align:left}';
  document.head.appendChild(css);

  const PALETTE = ['#8ab4f8', '#f28b82', '#81c995', '#fdd663', '#c58af9', '#78d9ec', '#fcad70', '#ff8bcb', '#a8dab5', '#d7aefb'];
  let mmdN = 0;

  async function drawMermaid(blocks) {
    await need('mermaid');
    if (!drawMermaid.ready) {
      drawMermaid.ready = true;
      window.mermaid.initialize({ startOnLoad: false, securityLevel: 'strict', theme: 'dark',
        themeVariables: { background: 'transparent', primaryColor: '#2b2e40', primaryTextColor: '#e9e9ed', primaryBorderColor: '#6b6f8a',
          secondaryColor: '#24273a', tertiaryColor: '#1e2030', lineColor: '#9397ab', edgeLabelBackground: '#1b1d2b', fontSize: '14px' },
        flowchart: { curve: 'basis', htmlLabels: true } });
    }
    for (const pre of blocks) {
      const code = pre.textContent, fig = document.createElement('figure'); fig.className = 'md-fig mmd';
      pre.replaceWith(fig);
      const id = 'mmd' + (++mmdN);
      try { const { svg } = await window.mermaid.render(id, code); fig.innerHTML = svg; }
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
    const kind = spec.type === 'line' ? 'Line' : spec.type === 'area' ? 'Area' : 'Bar';
    const marks = series.map((k, i) => h(R[kind], Object.assign({ key: k, dataKey: k, name: k, stroke: PALETTE[i % PALETTE.length], fill: PALETTE[i % PALETTE.length], isAnimationActive: false },
      kind === 'Bar' ? { radius: [3, 3, 0, 0], stackId: spec.stacked ? 's' : undefined } : kind === 'Area' ? { fillOpacity: .25, strokeWidth: 2, stackId: spec.stacked ? 's' : undefined } : { strokeWidth: 2, dot: data.length < 30 })));
    return h(R[kind + 'Chart'], { data, margin: { top: 6, right: 12, left: 0, bottom: 0 } }, grid,
      h(R.XAxis, { dataKey: x, tick, tickLine: false, axisLine: { stroke: '#3f424d' } }), h(R.YAxis, { tick, tickLine: false, axisLine: false, width: 56 }), tip, series.length > 1 ? legend : null, marks);
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
      window.ReactDOM.createRoot(box).render(window.React.createElement(window.Recharts.ResponsiveContainer, { width: '100%', height: '100%' }, chart(spec)));
    }
  }

  window.enhanceMarkdown = function (root) {
    const pick = lang => [...root.querySelectorAll('pre > code.language-' + lang)].map(c => c.parentElement);
    const jobs = [], m = pick('mermaid'), c = pick('chart');
    if (m.length) jobs.push(drawMermaid(m).catch(e => console.warn(e)));
    if (c.length) jobs.push(drawCharts(c).catch(e => console.warn(e)));
    return Promise.all(jobs);
  };
})();
