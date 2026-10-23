# Third-party files

Copies of open-source libraries the pages use, so Diligence works without reaching a CDN. Each is the
published release file, unchanged except where noted; the licenses are those of their projects.

| File | Project | License |
|---|---|---|
| `react-18.3.1.min.js`, `react-dom-18.3.1.min.js` | React 18.3.1 | MIT |
| `babel-standalone-7.29.0.min.js` | @babel/standalone 7.29.0 | MIT |
| `prop-types-15.8.1.min.js` | prop-types 15.8.1 | MIT |
| `recharts-2.12.7.js` | Recharts 2.12.7 | MIT |
| `mermaid-10.9.1.min.js` | Mermaid 10.9.1 | MIT |
| `marked-12.0.2.min.js` | marked 12.0.2 | MIT |
| `klinecharts-10.0.3.min.js` | KLineChart 10.0.3 | Apache 2.0, see `klinecharts-LICENSE.txt` |
| `phosphor-2.1.1/` | Phosphor Icons 2.1.1 (regular and fill), see `phosphor-2.1.1/LICENSE` | MIT. The stylesheets list only the woff2 font. |

React, ReactDOM and Babel match the integrity hashes the page runtime (`desk/support.js`) checks.
