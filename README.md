# Diligence

A stock research desk that runs on your own computer. It keeps its own database of everything the SEC
publishes about the companies you look at, and puts it together with live prices, deep
research on themes and the economy's indicators, in one page in your browser.

**The pieces, by name**
- **Diligence** is the whole thing, and the name of the page you work in.
- **`sec_db.py`** is the one program you run. It downloads from the SEC, keeps the database, and serves
  the page: `python3 sec_db.py serve`.
- **`sec.db`** is the database file it builds next to itself.
- **`desk/`** holds the page and its parts. The main page is `desk/diligence.dc.html`, and
  http://localhost:8765/ opens it.

## What you need

| | Needed for | Without it |
|---|---|---|
| **Python 3.9+** (python.org) | everything: no other packages | nothing runs |
| **moomoo OpenD**, logged in to a moomoo account ([download](https://www.moomoo.com/download/OpenAPI)) | live prices, the price chart, P/E and PEG history, your moomoo watchlists and holdings | these show a "moomoo OpenD is not running" note; the chart can switch to TradingView |
| **Claude Code** (the `claude` command, signed in) ([install](https://docs.claude.com/en/docs/claude-code)) | **Themes** research and topic suggestions, **Explain** on Macro charts | those buttons say Claude Code was not found |
| A free **FRED API key** (optional) | every FRED series and FRED search on **Macro** | about 60 core series still load from other free sources |
| Data-provider keys (optional): FMP, Finnhub, Alpha Vantage, Polygon, Twelve Data, EODHD, Marketstack, Marketaux | news, analyst estimates, peers and their valuations on a company's page | those panels stay empty |
| Research reports in the reports folder (optional) | the **Reports** tab | the tab says the reports folder was not found |

About the Reports tab: Diligence reads research reports but writes only the Themes ones itself. The
company reports it lists (`Company/<TICKER>/*.md`, earnings notes, series) come from a separate
report-writing setup of your own that is **not part of this repository**. Without one, the Reports tab
shows your Themes reports and any markdown files you put in the folder yourself.

## What it stores per company
- **Profile**: name, industry (SIC), exchange, fiscal year end, address, website.
- **Every filing** in its history (10-K, 10-Q, 8-K, proxy, insider forms...) with links.
- **Every number** it has reported in XBRL financial statements (all years, all line items).
- **Clean yearly and quarterly financials** (quarters are true 3-month numbers, Q4 included): revenue, gross profit, operating income,
  net income, EPS, cash flow, capex, free cash flow, cash, debt, equity, shares, plus
  margins, ROE, ROA, current ratio and debt/equity.
- **Revenue by segment, product and region**, with operating income by segment, read from
  the data attached to each annual report.
- **Major holders**: every investment manager holding the company at the latest quarter end
  (Form 13F) with the change from the quarter before, plus owners of more than 5% who are not
  in that data, such as founders and private funds (Schedule 13D and 13G).
- **Insider trades** from Form 4: who bought or sold, their role, shares, price.
- **Annual report text** split into sections: Business, Risk Factors, MD&A, Market Risk,
  Financial Statements. Searchable, which helps with the SWOT and Strategy parts of your checklist.

The SEC doesn't publish stock prices; those come from moomoo OpenD or the data-provider keys.

## Setup (once)
1. Install Python 3.9 or newer from python.org (nothing else to install).
2. Put this folder anywhere on your computer.
3. Copy `settings.example.json` to `settings.json` and put your name and email in it.
   The SEC requires every request to say who is asking; it is only sent to sec.gov.
   (If you skip this, the first run asks you and saves it.)
4. For prices: install **moomoo OpenD**, open it, log in, and leave it running. It
   listens on `127.0.0.1:11111`, where Diligence looks for it. Diligence reads prices and keeps your watchlist groups
   in step; nothing here can trade or place an order.
5. For Themes and Macro's Explain: install **Claude Code** and sign in once by running `claude` in a
   terminal. Research runs use your Claude plan.
6. Optional: copy `desk/keys.example.js` to `desk/keys.local.js` and fill in the data-provider keys you
   have. The page uses them unless you type a different key in Settings.
7. Optional: add a FRED key with the key button on the Macro page.

`settings.json`, `desk/keys.local.js`, `sec.db`, `13f_data/`, `reports/`, `notes/` and `exports/` are kept out of git (see
`.gitignore`): they hold your personal details, keys and research, or data Diligence rebuilds.

## Use
Start Diligence and leave the window open:

```
python3 sec_db.py serve
```

Then open **http://localhost:8765/** in your browser. The first time you open a company it is
downloaded from the SEC (about 10 to 30 seconds); after that it loads instantly. A company saved more
than 12 hours ago opens straight away from what is saved and is brought up to date in the background.

The same program also works from the terminal:

```
python3 sec_db.py fetch MSFT            # download a company (run again to refresh it)
python3 sec_db.py fetch AAPL NVDA COST  # several at once
python3 sec_db.py fetch MSFT --docs 3   # also keep the text of the last 3 annual reports
python3 sec_db.py show MSFT             # yearly financial table
python3 sec_db.py list                  # companies you've saved
python3 sec_db.py search "pricing power" --ticker MSFT
python3 sec_db.py export MSFT           # exports/MSFT.json, for a website or spreadsheet
```

On Windows use `python` instead of `python3`. A company takes about 5 to 30 seconds.

## The Diligence page
**Groups and moomoo.** With moomoo OpenD running, every moomoo watchlist group is a group in Diligence too, and they
stay the same both ways:

- Adding or removing a stock in one of your own moomoo lists (in the group menu, marked moomoo) changes that list in
  moomoo as well. Removing takes it out of that list only; it stays in moomoo's All, as in the moomoo app.
- Changes made in moomoo appear here the next time the page opens.
- moomoo's built-in lists (Favorites, US, HK...) and your holdings are shown but can only be changed in moomoo: OpenD
  does not allow it.
- OpenD cannot create, rename, delete or reorder lists. Renaming or reordering a moomoo group here only changes it
  here; deleting one here removes it from Diligence only, and it is not brought back.
- Diligence's own groups that have no moomoo list stay in Diligence only.

Financial statements (annual and quarterly, every year on file), revenue by segment, the company
profile, insider trades, holders and ticker search come from your own database. Prices and the chart
come from moomoo OpenD. News, analyst estimates, peer lists and peer valuations come from
the data-provider keys in Settings.

**Settings** (the gear at the bottom of the left edge) lists every data source in one place, each with
a dot that shows whether it works: moomoo OpenD, SEC filings (the contact name and email the SEC asks
for, and the server address), the FRED key, each data-provider key, which source supplies what, the
reports folder and your research notes. Click one to see its details, test it or change it. The SEC
contact, FRED key and reports folder are saved to `settings.json`; data-provider keys stay in the browser.

Your theses, checklists, notes and lists are saved in the browser and, a few seconds after each change,
copied to `notes/desk.json` in this folder. A browser
that has none, or an older copy, picks them up from there. **Export notes** and **Import notes** in
Settings save them to a file and load them back, to keep a copy elsewhere or move to another computer.
API keys are never in these copies.

When a stock is in none of your groups any more (you removed it, or deleted its group), Diligence asks whether
to delete the data kept about it: your notes and the figures the page saved, and its SEC data in `sec.db`.
Reports are never deleted: its company reports and every report that mentions it stay. SEC data shared with a
ticker still in a group (GOOG and GOOGL) stays too.
Stocks that left with a moomoo list are not asked about one by one: Settings → Stocks in no group lists them.

### Workspaces
The strip down the left edge switches between **Macro** (the economy), **Themes** (deep research on a
subject), **Companies** (one company, the tabs below) and **Settings** (every data source). The page
reopens on the one you used last.

### Macro
U.S. economic indicators: growth, jobs, inflation, rates and money, housing, markets, government debt.
It has three views:

- **Dashboard**: search the catalog (about 240 series, including payrolls by industry, CPI components
  and every state's unemployment and home prices), open one of 52 featured charts, or browse by group.
  The featured charts are grouped by topic, after MacroMicro's U.S. page: classic relationships (Okun's
  law, the Phillips curve, the yield curve), business cycle (GDP by component, contributions to growth,
  the NBER indicators, GDPNow, the Chicago Fed activity index), recession signals (the Sahm rule,
  recession probability, the 10-year minus 3-month curve, jobless claims), housing (home prices, starts
  and permits, new and existing home sales and supply, mortgage rates and debt service, vacancy, bank
  lending standards), labor, inflation, credit and financial conditions, and households and government.
  Each has a note on how to read it, and a dashed line at any threshold the note names (the Sahm rule's
  0.5, the Fed's 2%). All are drawn from FRED. Charts MacroMicro builds from data that is not free
  (Zillow, NAHB, pending home sales, REIT prices, its own models) are left out. Your saved charts are
  listed at the top.
- **How it connects**: a map of how the indicators drive one another (money supply → bond prices →
  rates → investment → output → income → consumption and saving, the Fed reacting to inflation, and
  so on). Each arrow says whether the next indicator moves the same way (+) or the opposite way (−),
  how long that usually takes, and why. Click an indicator and push it up or down to follow what
  happens next; **Chart this chain** charts the indicators along it.
- **Chart**: overlay lines, change each line to a change from a year ago, a change from the previous
  period or an index, put it on the left or right axis, and add formula lines (`a - b`). Only the kinds
  of chart that suit the lines can be picked. Stacked and pie charts need parts of one total, such as
  jobs by industry. Bars are for changes and quarterly or yearly figures, and a scatter needs exactly
  two lines. Hover a greyed-out kind to see why. The sidebar searches as you type and lists the
  indicators related to what is on the chart. The panel on the right explains each line and what to
  watch for. **Explain** asks Claude to explain the chart in plain words, from its latest numbers. Charts
  are saved in this browser and can be exported as CSV.

Where the numbers come from:
- With a free **FRED API key** (from fred.stlouisfed.org; add it with the key button at the top of the
  page, and it is kept in `settings.json` as `fred_api_key`) every FRED series can be charted and
  searched.
- Without one, or while FRED is down, about 60 core series still load from other free sources:
  - Fed rates, money supply, industrial production and the dollar index come from the Federal Reserve
    via DBnomics.
  - GDP and its parts, spending, income, saving, PCE prices and tax receipts come from BEA via DBnomics.
  - Jobs, wages and CPI come from the BLS.
  - Consumer sentiment comes from the University of Michigan.
- Series that only FRED carries, such as mortgage rates, housing starts, the S&P 500, the VIX, oil and
  credit spreads, show "needs a FRED key".
- Figures are kept in `sec.db` for 6 hours. The page is `desk/macro.dc.html`.

### Themes
A theme is a subject you want to understand properly: Payments, Robotics, the Vision Pro. Give it a
brief (what you want to know) and topics, and each topic is researched and written up as its own long
report, several at once. Each topic has a kind, which sets what its report covers:

- **How it works**: how a technology, product or system works (how Zelle moves money, how Waymo's lidar sees)
- **Tech stack & progress**: the layers of a technology, the state of the art and what improves next
- **Supply chain**: the parts, who makes each, the bottlenecks, which listed suppliers depend on it
- **Industry & companies**: an industry, its economics and the companies in it
- **Open question**: anything else
- **Theme overview**: once two or more reports are done, ties them together

Reports draw their flows, mechanisms and supply chains as Mermaid diagrams, and their comparisons as
charts (a ` ```chart ` block of JSON, drawn with Recharts); both the Themes reader and the Reports tab
show them. Both also draw what a report has written as plain text: a table of figures gets a chart above
it, and a chain written as "A → B → C" becomes a row of numbered steps (see "The Reports tab").
**Suggest topics** asks Claude for topics that would cover the theme. The ticker buttons on a report
(from its "Listed companies" table, or any table column headed Ticker or 代碼) open that company.

How it runs:
- Each topic is researched by Claude Code (the `claude` command) on this computer, in the background,
  3 at a time; the rest wait their turn. Set `"research_parallel"` in `settings.json` to change that (1 to 8).
- A run may only search the web and read pages: no files, no shell, none of your connected services
  (Gmail, Notion and so on). Its last message is the report, which the server saves.
- A report takes 5 to 20 minutes and uses your Claude plan like any other Claude Code session.
- Runs keep going when the server restarts or the page is closed; the server picks them up again. A run
  is stopped after 90 minutes.
- A theme is a folder in `Industry-theme/` of the reports folder with a `_theme.json` (its brief and
  topics), so its reports also show on the Reports page. Report folders already in `Industry-theme/` and
  `Technical-research/` are listed too, to read; **Make it a theme** turns an `Industry-theme/` folder into
  one you can add topics to.

### The Chart tab and moomoo
The chart is drawn by the page itself (with KLineChart, kept in `desk/vendor`) from candles that
this server reads out of **moomoo OpenD**, the gateway app from moomoo. For the chart to work:

1. Open the moomoo OpenD app and log in with your moomoo account.
2. Leave it running. It listens on `127.0.0.1:11111`, which is where this server looks for it.

If OpenD is not running, the chart says so and offers the TradingView chart instead; the
"moomoo / TradingView" switch above the chart changes between the two at any time. Diligence reads
prices from OpenD and changes nothing but your own watchlist groups: nothing here unlocks trading or can place an order.

### The Reports tab
The last tab, **Reports**, is a reading room for research reports: your Themes reports and those from
your own report-writing setup, which is not part of this repository (see "What you need"). They are the
markdown files in the `reports` folder inside this project, or the folder set in `settings.json`). Nothing is copied: the server reads that folder each
time, so a new report shows up as soon as it is saved. Press the ↻ button, or just click back
into the window, to pick up new ones.

- With a stock open, the tab shows only that stock's own reports: the files in
  `Company/<TICKER>/` of the reports folder (for example `Company/OKLO/`). Nothing else is listed.
- Opened on its own (`/desk/reports.html`, no stock), the page shows the whole library by folder.
- The date shown for a report is the one it states itself (a line such as `報告日期：2026年6月24日`,
  `系列日期`, `最後更新` or `Report date:` near the top). Failing that it is the date in the file
  name, then the newest dated report in the same folder, then the day the file was last saved.
  The last two are guesses and are shown with a `~` in front.
- A stock's reports are sorted into sections: **Start here** (the newest series and the newest full
  research report), **Earnings**, **Topic studies**, **Reference**, **Older versions** and
  **Working notes**. The last two start closed; click the heading to open them. A series, a
  multi-part study or a results report with its article shows as one entry that opens to its parts.
  When a series exists in several versions the newest dated one is treated as current, and a
  version with no date of its own counts as older.
- With a stock open, each report in the list also shows what the stock closed at on the report's
  date (for example `Series  2026-07-14  NVDA  $172.41`), or on the last trading day before it.
  The prices come from moomoo OpenD, the same feed as the Chart tab, so they are adjusted for
  splits and dividends, and nothing is shown when OpenD is not running.
- Two rows of filters sit above the list. The first is the kind of work (deep research, earnings,
  industry and screening, portfolio, thinking tools). The second is what the report is about:
  company, industry or theme, screens and pools, people and method, multi-company comparison,
  portfolio, macro and index, technical research, working and audit files, running logs,
  community. That second label is the top-level folder a report sits in (`Company/`,
  `Industry-theme/`, `Screens-pools/` and so on); the names are listed under
  `"category_folders"` in the reports folder's `_index/config.json`.
- Charts and diagrams are drawn as each report opens, so every report gets them, old ones included.
  A table of figures gets a chart above it: years as a line or bars (years across the top work too),
  shares of a whole as a donut, and companies or segments as sorted bars. Totals are left out, and a
  table that mixes different measures (revenue next to a margin next to a P/E) gets no chart. A chain
  written as "A → B → C" becomes numbered steps, and changes like +12% or −3% are colored. The
  **Charts and diagrams** switch under a report's title turns this off.
- A diagram drawn with text (boxes and arrows in a code block) is redrawn as a real diagram: the server asks
  Claude Code to turn it into Mermaid (about 20 seconds, the first time only) and keeps the result in
  `diagrams/` (kept out of git), so it opens at once from then on. "Show the original text" under it shows the text.
- To keep reports somewhere else, set the folder in Settings → Reports folder, or add `"reports_dir": "/path/to/reports"` to `settings.json`
  (`~` and paths relative to this project folder work too). The `reports` folder is kept out of git.
- The page is `desk/reports.html`; it also opens on its own at
  `http://localhost:8765/desk/reports.html`.

## Using it from another page or program
The same server is a JSON API. If you ask for a company that isn't saved yet, it downloads it first.

Only this computer may use it. Programs on this computer (curl, Python scripts) can call it freely. In a
browser, only pages served from `localhost`, `127.0.0.1` or `[::1]` (any port) can read its answers, so a
page you build on `http://localhost:3000` works but other websites are refused. That keeps them from
reading your holdings, reports or API keys, or starting downloads. Requests addressed to any other host
name are refused too, which stops a site from pointing its own name at your computer. Pages opened from
a file (`file://`) are refused as well, so open the desk at `http://localhost:8765`.

| URL | Returns |
|---|---|
| `/api/companies` | every saved company |
| `/api/company/MSFT` | profile, yearly + quarterly financials, recent filings, document list |
| `/api/company/MSFT?refresh=1` | same, after re-downloading from the SEC |
| `/api/company/MSFT/section?name=1A` | Risk Factors text (`1` Business, `7` MD&A, `7A`, `8`) |
| `/api/company/MSFT/facts?concept=Revenues` | every raw value of one XBRL line item |
| `/api/search?q=competition&ticker=MSFT` | matching passages in saved reports |
| `/api/desk/fundamentals/MSFT`, `/quarters/MSFT`, `/segments/MSFT`, `/insiders/MSFT?years=5`, `/holders/MSFT`, `/statements/MSFT`, `/search?q=micro` | the feeds the Diligence page uses (USD millions) |
| `/api/themes`, `/api/themes/one?where=Industry-theme&name=Payments` | the themes, and one theme with its topics and their progress. Changes are POSTed as JSON to `/api/themes/create`, `/api/themes/change` and `/api/themes/suggest`, from this server's own pages only |
| `/api/macro/series?id=UNRATE`, `/api/macro/batch?ids=UNRATE,GDPC1`, `/api/macro/search?q=mortgage`, `/api/macro/status` | an economic series as `[date, value]` points with where it came from; several at once; a search of FRED (needs the key); which sources are working. The key and the plain-words explanations are POSTed as JSON to `/api/macro/key` and `/api/macro/explain`, from this server's own pages only |
| `/api/reports?ticker=MSFT`, `/api/reports/file?path=...` | every saved research report (those about the ticker marked), and the text of one |
| `/api/moomoo/status` | whether moomoo OpenD is running and logged in, and how much of the price-history limit is used |
| `/api/moomoo/kline/MSFT?k=D` | candles from moomoo as `[time in ms, open, high, low, close, volume]`. `k` is `1 3 5 15 30 45 60 120 180 240 D W M 3M 6M 12M`; `ext=1` adds pre-market and after-hours; `before=<time in ms>` gives the stretch before that time; `all=1` gives everything moomoo keeps |
| `/api/moomoo/quotes?symbols=MSFT,AAPL` | live prices, with the pre-market, after-hours or overnight price when there is one |
| `/api/moomoo/insight/MSFT` | analysts' price targets and ratings, short interest, the last and next earnings dates |
| `/api/moomoo/events/MSFT` | earnings announcements of the last five years with the EPS and revenue reported, and every dividend, for the marks on the chart |
| `/api/moomoo/lists`, `/list?name=AI`, `/positions` | your moomoo watchlist groups, the stocks in one, and the stocks you hold (symbols only). Changes to one of your own lists are POSTed as JSON to `/api/moomoo/list/change` (`{name, add, remove}`), from this server's own pages only |
| `/api/moomoo/valuation/MSFT` | P/E day by day for up to 30 years as `[date, value]` (price over the last twelve months' earnings; negative when those were a loss), with the current, average and forward figures. `kind=pb` or `kind=ps` for price-to-book or price-to-sales. `kind=peg` for the PEG ratio as `[date, PEG, growth]`: the P/E over the growth, in percent, of the last twelve months' EPS on the twelve months before, worked out from moomoo's income statements (PEG is null when earnings did not grow or were a loss) |

## Checking nothing broke
Two checks, both with nothing to install:

- `python3 -m unittest discover -s tests` runs the tests: no internet, OpenD or `sec.db` needed. They
  also run on GitHub for every push, on Linux, macOS and Windows (`.github/workflows/tests.yml`).
- `python3 check.py` checks real figures in your own `sec.db`.

Run both after any change to `sec_db.py`. `check.py` reads a few companies from `sec.db`
(no server or internet needed) and checks figures that have gone wrong before, that every
company's latest balance sheet adds up, and, if moomoo OpenD is running, the chart's price feed.
It ends with a count of what passed, failed and was skipped. A company missing from `sec.db`, or OpenD not
running or not logged in, shows as skipped, never as passed. It exits with an error when a check fails or
when no database check could run, for example with an empty `sec.db`.

## Looking inside the database
`sec.db` is a normal SQLite file. Open it with the free app "DB Browser for SQLite".
Handy tables and views: `annual_financials`, `quarterly_financials`, `companies`,
`filings`, `facts` (all raw numbers), `documents` (report text), `insider_trades`.

## How the figures are built
Figures come from what companies file with the SEC: the tagged numbers in the SEC's company facts, plus each
filing's statements as presented (the newest 12 annual and 8 quarterly reports, kept in `sec.db`). The presented
statements fill what the tags miss: lines under a company's own tags, figures split by business line or share class,
and filings the company facts leave out.

- **Valuation** uses today's shares (the newest filing's cover page, every class counted in the ticker's own class)
  and earnings per share for the last twelve months: the newest four quarters as reported, the fourth quarter being
  the year less its first three.
- **Earnings per share** is the diluted figure the company reports, so preferred dividends are netted out.
- **Debt** adds up every borrowing line on the balance sheet, short and long, notes, commercial paper and
  convertibles, each counted once. Operating leases are not included; finance leases count only where the company
  reports them on the same line as its debt (Delta).
- **Cash** for net debt, enterprise value, return on invested capital and the cash ratio includes short-term
  investments.
- **Stock splits** are applied to every earlier share count and per-share figure.
- **Operating income** is the company's own line. Without one it is worked out from operating figures (gross profit
  less operating expenses, or pre-tax income less non-operating items); banks, brokers and insurers get none.
- **Companies reporting under IFRS** (TSMC, Nokia) are read in their own vocabulary and shown in US dollars at FRED's
  monthly exchange rates (needs a FRED key), per depositary share where the stock trades as an ADS.

Where Diligence and sites like Stock Analysis or GuruFocus disagree, it is often a choice rather than an error:

- **Operating income**: some sites remove one-time charges (NVIDIA's $1.35B Arm write-off). Diligence shows the
  figure as reported.
- **Debt**: some sites count leases as debt (NVIDIA, Tesla, Amazon, Delta). Diligence does not.
- **Equity**: some sites include minority interest (Walmart, UnitedHealth, ExxonMobil, Tesla). Diligence shows the
  shareholders' own equity.
- **Gross profit and capital spending**: each site groups cost lines a little differently (AMD, Caterpillar, Deere,
  Walmart, IREN).
- **Buybacks**: some sites include shares withheld to pay employees' taxes. Diligence counts repurchases only.
- **Ratios**: GuruFocus uses the last twelve months, the latest quarter's balances and average capital; Diligence's
  ratio tables use each fiscal year with year-end balances. NVIDIA's return on equity is 76% one way and 120% the
  other; neither is wrong.
- **Timing**: some sites take the newest quarter from the earnings release (JPMorgan, Progressive, UnitedHealth).
  Diligence waits for the 10-Q to reach the SEC.

## Good to know
- Holders come from the SEC's quarterly Form 13F files. The two newest (about 100 MB each) are
  downloaded once into the `13f_data` folder and replaced when a new quarter is published. That first
  download runs in the background: the Major holders panel says so and fills in by itself.
- On Windows, time zones (for the chart's trading sessions) work without the `tzdata` package: Diligence
  knows the rules of the markets it shows.
- The 5% owners come from Schedule 13D/13G filings made since late 2024, when the SEC began
  taking them as data. A holder whose last filing is older than that is not picked up.
- Insider trades go back 5 years; the first read of a busy company can take a few minutes.
- Segment figures are annual by default (from 10-K reports). Choosing Quarterly on the page reads
  the 40 newest 10-Q reports as well, which takes up to a minute the first time; the fourth
  quarter is worked out as the year less the first nine months.
- Statements go back to about fiscal 2007. The SEC only has filings as machine-readable data
  from 2009 on (each carried two prior years); older annual reports exist only as documents.
- Segment figures are read the first time you open a company's Business tab (about 10 seconds),
  then kept. Detailed segment tagging starts around 2010 and varies by company.
- Companies report Q4 only inside the annual report, and cash flow as year-to-date, so the
  tool works out each true quarter by subtraction (for example Q4 = full year minus 9 months).
- Banks, insurers and REITs use different line items, so some columns can be blank.
- Foreign companies filing 20-F often report in IFRS; their numbers are in `facts`
  but the clean views may be mostly empty.
- The SEC allows up to 10 requests per second; the tool stays under that on its own.
- moomoo limits price history to a number of different symbols per seven days (300 on an
  account holding HK$10,000 or more, otherwise 100). Each symbol charted counts once, whatever
  the interval. The chart's information button shows how many are used.
- moomoo keeps 20 years of daily candles and 8 years of candles of an hour or less. Its prices
  are adjusted for splits and dividends, so long moving averages sit a little below
  TradingView's, which adjusts for splits only.
- moomoo has no US index or over-the-counter prices; for those use the TradingView chart.
- Under the candles the chart can show volume, the P/E ratio, the PEG ratio and the trend template score day
  by day ("Below chart" switches them). The score is worked out from moomoo's daily closes at
  each day's close, so it needs moomoo OpenD; rule 8 compares the stock with SPY.

## License
MIT, see `LICENSE`. The libraries in `desk/vendor/` keep their own licenses, listed in `desk/vendor/README.md`.
