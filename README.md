# SEC Database

A free replacement for paid stock-data APIs. Ask for a company and it downloads what
the SEC publishes about it and saves it in one file, `sec.db`, on your computer.

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

It does **not** have stock prices (the SEC doesn't publish them), so price-based numbers
like P/E still come from another site.

## Setup (once)
1. Install Python 3.9 or newer from python.org (nothing else to install).
2. Put this folder anywhere on your computer.
3. Copy `settings.example.json` to `settings.json` and put your name and email in it.
   The SEC requires every request to say who is asking; it is only sent to sec.gov.
   (If you skip this, the first run asks you and saves it.)
4. Optional: copy `desk/keys.example.js` to `desk/keys.local.js` and fill in the API keys you have
   (FMP, Finnhub, Alpha Vantage, Polygon, Twelve Data). The desk uses them unless you type a different
   key in Settings.

`settings.json`, `desk/keys.local.js`, `sec.db` and `13f_data/` are kept out of git (see `.gitignore`):
they hold your personal details and keys, or data the server rebuilds.

## Use
Open a terminal in this folder:

```
python sec_db.py fetch MSFT            # download a company (run again to refresh it)
python sec_db.py fetch AAPL NVDA COST  # several at once
python sec_db.py fetch MSFT --docs 3   # also keep the text of the last 3 annual reports
python sec_db.py show MSFT             # yearly financial table
python sec_db.py list                  # companies you've saved
python sec_db.py search "pricing power" --ticker MSFT
python sec_db.py export MSFT           # MSFT.json, for a website or spreadsheet
python sec_db.py serve                 # JSON API for your website
```

On Mac use `python3` instead of `python`. A company takes about 5 to 30 seconds.

## Your Stock Research Desk
The `desk` folder holds your Stock Research Desk page, connected to this database.

1. Run `python sec_db.py serve` and leave that window open.
2. Open **http://localhost:8765/** in your browser.

The page now gets financial statements (annual and quarterly, every year on file), revenue
by segment, the company profile, insider trades and ticker search from your own database. The first time you open a company
it downloads it from the SEC (about 10 to 30 seconds), and after that it loads instantly. It
re-checks the SEC for new filings at most every 12 hours.

Prices and charts, news, analyst estimates, peer lists and peer valuation numbers still
come from the API keys in Settings, because the SEC doesn't
publish them. In Settings, "Local SEC database" is listed first. Its box holds the
server address (`http://localhost:8765`), not a key.

### Workspaces
The strip down the left edge switches between **Macro** (the economy), **Themes** (deep research on a
subject), **Screener** (many stocks at once) and **Companies** (one company, the tabs below). The page
reopens on the one you used last.

### Macro
U.S. economic indicators: growth, jobs, inflation, rates and money, housing, markets, government debt.
It has three views:

- **Dashboard**: search the catalog (about 180 series, including payrolls by industry, CPI components
  and every state's unemployment and home prices), open one of the featured relationships (Okun's law,
  the Phillips curve, the yield curve, jobs by industry and others, each with a note on how to read
  it), or browse by group. Your saved charts are listed at the top.
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
show them. **Suggest topics** asks Claude for topics that would cover the theme. **Screen companies** sends the
tickers from the reports' "Listed companies" tables (or any table column headed Ticker or 代碼) to the
Screener; the ticker buttons on a report open that company.

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

### The Screener
Pick which stocks to look at on the left: one of your own lists, a moomoo watchlist, the largest
companies of the whole US or Hong Kong market (the top 300 above a market value you choose), every
company in your SEC database, or tickers you type. The table shows, for each: price, day's change,
market value, P/E and PEG of the last 12 months, P/B, dividend yield, growth of sales (last 12 months
and 3-year) and of EPS, gross, operating and net margin, ROE and the distance from the 52-week high.
Click a column to sort; add filters ("P/E at most 20"), or start from one of the screens on the left
and save your own. Click a row to open that company; tick rows to add them to one of your lists.

- Needs moomoo OpenD. Prices and valuation come from one moomoo snapshot for the whole list.
- Growth and margins come from each company's income statements at moomoo, which allows 30 of them
  every 30 seconds. They are read in the background (the header counts down), kept in `sec.db` for a
  day, and filled into the table as they arrive. A first look at 300 companies takes about 5 minutes.
- The figures cover the last 12 months against the 12 before. For a company that does not report every
  quarter it is the last full year instead; hover a figure to see which.
- The page is `desk/screener.html`; it also opens on its own at `http://localhost:8765/desk/screener.html`.

### The Chart tab and moomoo
The chart is drawn by the page itself (with KLineChart, kept in `desk/vendor`) from candles that
this server reads out of **moomoo OpenD**, the gateway app from moomoo. For the chart to work:

1. Open the moomoo OpenD app and log in with your moomoo account.
2. Leave it running. It listens on `127.0.0.1:11111`, which is where this server looks for it.

If OpenD is not running, the chart says so and offers the TradingView chart instead; the
"moomoo / TradingView" switch above the chart changes between the two at any time. Only prices
are read from OpenD: nothing here unlocks trading or can place an order.

### The Reports tab
The last tab, **Reports**, is a reading room for the research reports you generate (the markdown
files in `~/Desktop/ai-berkshire/reports`). Nothing is copied: the server reads that folder each
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
- To read reports from a different folder, add `"reports_dir": "/path/to/reports"` to
  `settings.json`.
- The page is `desk/reports.html`; it also opens on its own at
  `http://localhost:8765/desk/reports.html`.

## Using it from another website
The same server is a JSON API. If you ask for a company that isn't saved yet, it downloads it first.

| URL | Returns |
|---|---|
| `/api/companies` | every saved company |
| `/api/company/MSFT` | profile, yearly + quarterly financials, recent filings, document list |
| `/api/company/MSFT?refresh=1` | same, after re-downloading from the SEC |
| `/api/company/MSFT/section?name=1A` | Risk Factors text (`1` Business, `7` MD&A, `7A`, `8`) |
| `/api/company/MSFT/facts?concept=Revenues` | every raw value of one XBRL line item |
| `/api/search?q=competition&ticker=MSFT` | matching passages in saved reports |
| `/api/desk/fundamentals/MSFT`, `/quarters/MSFT`, `/segments/MSFT`, `/insiders/MSFT?years=5`, `/holders/MSFT`, `/statements/MSFT`, `/search?q=micro` | the feeds the Stock Research Desk uses (USD millions) |
| `/api/themes`, `/api/themes/one?where=Industry-theme&name=Payments` | the themes, and one theme with its topics and their progress. Changes are POSTed as JSON to `/api/themes/create`, `/api/themes/change` and `/api/themes/suggest`, from this server's own pages only |
| `/api/macro/series?id=UNRATE`, `/api/macro/batch?ids=UNRATE,GDPC1`, `/api/macro/search?q=mortgage`, `/api/macro/status` | an economic series as `[date, value]` points with where it came from; several at once; a search of FRED (needs the key); which sources are working. The key and the plain-words explanations are POSTed as JSON to `/api/macro/key` and `/api/macro/explain`, from this server's own pages only |
| `/api/screener?symbols=NVDA,0700.HK` | the screener's row for each symbol; `waiting` is how many companies' statements are still being read |
| `/api/screener/market?market=US&min_cap=1e10&limit=300` | the largest companies of the US or HK market by market value |
| `/api/reports?ticker=MSFT`, `/api/reports/file?path=...` | every saved research report (those about the ticker marked), and the text of one |
| `/api/moomoo/status` | whether moomoo OpenD is running and logged in, and how much of the price-history limit is used |
| `/api/moomoo/kline/MSFT?k=D` | candles from moomoo as `[time in ms, open, high, low, close, volume]`. `k` is `1 3 5 15 30 45 60 120 180 240 D W M 3M 6M 12M`; `ext=1` adds pre-market and after-hours; `before=<time in ms>` gives the stretch before that time; `all=1` gives everything moomoo keeps |
| `/api/moomoo/quotes?symbols=MSFT,AAPL` | live prices, with the pre-market, after-hours or overnight price when there is one |
| `/api/moomoo/insight/MSFT` | analysts' price targets and ratings, short interest, the last and next earnings dates |
| `/api/moomoo/events/MSFT` | earnings announcements of the last five years with the EPS and revenue reported, and every dividend, for the marks on the chart |
| `/api/moomoo/lists`, `/list?name=AI`, `/positions` | your moomoo watchlist groups, the stocks in one, and the stocks you hold (symbols only) |
| `/api/moomoo/valuation/MSFT` | P/E day by day for up to 30 years as `[date, value]` (price over the last twelve months' earnings; negative when those were a loss), with the current, average and forward figures. `kind=pb` or `kind=ps` for price-to-book or price-to-sales. `kind=peg` for the PEG ratio as `[date, PEG, growth]`: the P/E over the growth, in percent, of the last twelve months' EPS on the twelve months before, worked out from moomoo's income statements (PEG is null when earnings did not grow or were a loss) |

## Checking nothing broke
Run `python check.py` after any change to `sec_db.py`. It reads a few companies from `sec.db`
(no server or internet needed) and checks figures that have gone wrong before, that every
company's latest balance sheet adds up, and, if moomoo OpenD is running, the chart's price feed.
It ends with a count of what passed and failed.

## Looking inside the database
`sec.db` is a normal SQLite file. Open it with the free app "DB Browser for SQLite".
Handy tables and views: `annual_financials`, `quarterly_financials`, `companies`,
`filings`, `facts` (all raw numbers), `documents` (report text), `insider_trades`.

## Good to know
- Holders come from the SEC's quarterly Form 13F files. The two newest (about 100 MB each) are
  downloaded once into the `13f_data` folder and replaced when a new quarter is published.
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
