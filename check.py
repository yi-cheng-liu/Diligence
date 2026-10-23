#!/usr/bin/env python3
"""Quick health check for Diligence's database (sec.db): run `python3 check.py` after any change.

It reads a handful of companies straight from sec.db and checks figures that have gone wrong before,
plus a few rules every company should obey. If moomoo OpenD is running and logged in it also checks
the chart's price feed, reading a few days of prices from it.

It downloads nothing from the SEC and changes no company's figures. Opening sec.db does create it,
empty, when it is missing, and brings an older one up to date, as the server would.

Anything it could not check (a company not in the database, OpenD not running) is listed as skipped,
never as passed. It exits with 1 when a check failed or when no database check could run.
"""
import sys

import sec_db as s

PASSED, FAILED, SKIPPED = [], [], []


def check(name, ok, detail=""):
    (PASSED if ok else FAILED).append(name)
    print(("  ok    " if ok else "  FAIL  ") + name + (f"  ({detail})" if detail and not ok else ""))


def skip(name, why):
    SKIPPED.append(name)
    print(f"  skip  {name}  ({why})")


def near(a, b, tol=0.02):
    return a is not None and b is not None and abs(a - b) <= tol * abs(b)


def main():
    db = s.connect()
    have = lambda t: s.lookup_cik(db, t) is not None
    year = lambda t, y: s.desk_fundamentals(db, s.lookup_cik(db, t))["byYear"].get(str(y), {})
    more = lambda t: s.desk_statements(db, s.lookup_cik(db, t))
    quarters = lambda t: s.desk_quarters(db, s.lookup_cik(db, t))

    # in the reporting currency, straight from the database, so no exchange rates are needed
    metric = lambda t, y, m: (db.execute("SELECT value FROM metrics WHERE cik=? AND period_type='annual' AND fiscal_year=? AND metric=?",
                                         (s.lookup_cik(db, t), y, m)).fetchone() or [None])[0]
    mm = lambda t, y, m: (metric(t, y, m) or 0) / 1e6

    print("Figures that have been wrong before")
    for t in ("AAPL", "COST", "LLY", "CAT", "XOM", "NBIS", "NVDA", "MCD", "BRK-B", "NKE", "JPM", "ASTS", "TSM", "NEE"):
        if not have(t):
            skip(f"{t} figures", f"{t} is not in sec.db; run `python3 sec_db.py fetch {t}`")
    if have("AAPL"):
        check("Apple FY2025 revenue is $416.2B", near(year("AAPL", 2025).get("revenue"), 416161))
    if have("COST"):
        check("Costco FY2025 revenue is $275.2B", near(year("COST", 2025).get("revenue"), 275235))
    if have("LLY"):   # Lilly reports no operating income line; it is worked back from pre-tax income
        check("Lilly FY2025 operating income is worked out ($26.3B)", near(year("LLY", 2025).get("ebit"), 26302, 0.05), year("LLY", 2025).get("ebit"))
        check("Lilly FY2025 R&D is on file", (more("LLY")["annual"].get("2025", {}).get("rd") or 0) > 10000)
        q8 = quarters("LLY")[-8:]
        check("Lilly's last 8 quarters all have operating income", len(q8) == 8 and all("ebit" in q["row"] for q in q8), f"{len(q8)} quarters on file")
    if have("CAT"):   # Caterpillar reports debt only per business line
        check("Caterpillar FY2025 debt is $43.3B", near(year("CAT", 2025).get("debt"), 43330, 0.05), year("CAT", 2025).get("debt"))
        cq = quarters("CAT")
        check("Caterpillar's latest quarter has debt", bool(cq) and (cq[-1]["row"].get("debt") or 0) > 30000, "no quarters on file" if not cq else cq[-1]["row"].get("debt"))
    if have("XOM"):   # Exxon's history sits under its old registration
        st = more("XOM")
        check("Exxon has 15+ years of statements", len(st["annual"]) >= 15, len(st["annual"]))
        check("Exxon FY2025 has investing and financing cash flow", all(st["annual"].get("2025", {}).get(k) is not None for k in ("cfi", "cff")))
    if have("AAPL"):  # Apple's commercial paper is a balance-sheet line of its own
        check("Apple FY2025 debt includes $8.0B of commercial paper and $1.2B of finance leases ($99.9B)", near(mm("AAPL", 2025, "total_debt"), 99887, 0.01), mm("AAPL", 2025, "total_debt"))
    if have("NVDA"):  # two stock splits (4:1 in 2021, 10:1 in 2024) and investments under DebtSecuritiesCurrent
        check("NVIDIA FY2021 shares are on today's basis (about 25 billion)", 20000 < mm("NVDA", 2021, "shares_diluted") < 30000, mm("NVDA", 2021, "shares_diluted"))
        check("NVIDIA FY2021 EPS is split-adjusted ($0.17)", near(metric("NVDA", 2021, "eps_diluted"), 0.17, 0.1), metric("NVDA", 2021, "eps_diluted"))
        check("NVIDIA FY2025 short-term investments are $34.6B", near(mm("NVDA", 2025, "short_term_investments"), 34621, 0.02), mm("NVDA", 2025, "short_term_investments"))
        cs = mm("NVDA", 2026, "cash") + mm("NVDA", 2026, "short_term_investments")
        check("NVIDIA FY2026 cash and short-term investments are $62.6B", near(cs, 62556, 0.01), cs)
    if have("MCD"):   # McDonald's filed share counts in millions as plain shares
        check("McDonald's FY2023 diluted shares are about 730 million", 650 < mm("MCD", 2023, "shares_diluted") < 800, mm("MCD", 2023, "shares_diluted"))
    if have("BRK-B"): # debt and Treasury bills under business sections and its own tags; Class B per share
        check("Berkshire FY2025 debt is $129.1B", near(mm("BRK-B", 2025, "total_debt"), 129081, 0.01), mm("BRK-B", 2025, "total_debt"))
        check("Berkshire FY2025 short-term investments are $321.4B", near(mm("BRK-B", 2025, "short_term_investments"), 321434, 0.01), mm("BRK-B", 2025, "short_term_investments"))
        check("Berkshire FY2025 revenue is $371.4B", near(mm("BRK-B", 2025, "revenue"), 371444, 0.01), mm("BRK-B", 2025, "revenue"))
        check("Berkshire FY2025 EPS is per Class B share ($31.04)", near(metric("BRK-B", 2025, "eps_diluted"), 31.04, 0.01), metric("BRK-B", 2025, "eps_diluted"))
        check("Berkshire has no operating income worked out (an insurer)", year("BRK-B", 2025).get("ebit") is None, year("BRK-B", 2025).get("ebit"))
    if have("XOM"):   # sales only: income from affiliates is not revenue
        check("ExxonMobil FY2025 revenue is its sales ($323.9B)", near(mm("XOM", 2025, "revenue"), 323905, 0.01), mm("XOM", 2025, "revenue"))
    if have("MSFT"):  # finance leases are borrowing
        check("Microsoft FY2026 debt includes its finance leases ($106.9B)", near(mm("MSFT", 2026, "total_debt"), 106888, 0.01), mm("MSFT", 2026, "total_debt"))
    if have("JPM"):
        check("JPMorgan has no operating income worked out (a bank)", year("JPM", 2025).get("ebit") is None, year("JPM", 2025).get("ebit"))
    if have("NKE"):   # Nike reports no operating income line
        check("Nike FY2025 operating income is worked out ($3.7B)", near(year("NKE", 2025).get("ebit"), 3676, 0.05), year("NKE", 2025).get("ebit"))
    if have("LLY"):
        check("Lilly FY2025 capital spending is on file ($7.8B)", near(mm("LLY", 2025, "capex"), 7841, 0.02), mm("LLY", 2025, "capex"))
    if have("ASTS"):  # its 2025 10-K repeated the 2022 figure under one revenue tag for 2024
        check("AST SpaceMobile FY2024 revenue is $4.4M, not 2022's $13.8M", near(mm("ASTS", 2024, "revenue"), 4.42, 0.05), mm("ASTS", 2024, "revenue"))
    if have("TSM"):   # IFRS, in Taiwan dollars
        check("TSMC FY2024 revenue is NT$2,894B", near(mm("TSM", 2024, "revenue"), 2894308, 0.01), mm("TSM", 2024, "revenue"))
    if have("NEE"):   # the SEC's company facts lack NEE's newest 10-Qs; they come from the statements themselves
        nq = quarters("NEE")
        check("NEE has quarters up to its newest 10-Q", bool(nq) and str(nq[-1]["y"]) >= "2026" and nq[-1]["q"] >= 3, nq[-1] if nq else None)
        check("NEE FY2025 capital spending is on file", mm("NEE", 2025, "capex") > 10000, mm("NEE", 2025, "capex"))
    if have("NBIS"):  # Nebius used to report in roubles
        check("Nebius is read in US dollars", more("NBIS").get("currency") == "USD", more("NBIS").get("currency"))
        check("Nebius 2021 revenue is in dollars, not roubles", (year("NBIS", 2021).get("revenue") or 0) < 20000, year("NBIS", 2021).get("revenue"))

    print("Rules every company should obey (latest year on file)")
    looked = 0
    for r in db.execute("SELECT ticker, cik FROM companies WHERE ticker IS NOT NULL ORDER BY ticker"):
        by = s.desk_fundamentals(db, r["cik"])["byYear"]
        years = sorted(y for y in by if by[y].get("assets"))
        if not years:
            continue
        looked += 1
        v, x = by[years[-1]], s.desk_statements(db, r["cik"])["annual"].get(years[-1], {})
        problems = []
        # some companies report equity with the outside owners' share already inside it (AST SpaceMobile), others beside it
        if v.get("equity") is not None and x.get("liabTotal") is not None and not any(
                near(v["assets"], v["equity"] + x["liabTotal"] + extra, 0.03) for extra in (0, x.get("minority") or 0)):
            problems.append("assets do not equal liabilities plus equity")
        if v.get("curAssets") and v["curAssets"] > v["assets"] * 1.001:
            problems.append("current assets exceed total assets")
        if v.get("cash") and v["cash"] > v["assets"] * 1.001:
            problems.append("cash exceeds total assets")
        if v.get("revenue") is not None and v.get("cogs") is not None and v["cogs"] > v["revenue"] * 3:
            problems.append("cost of revenue is over three times revenue")
        if problems:
            check(f"{r['ticker']} FY{years[-1]} balance sheet adds up", False, "; ".join(problems))
    if not looked:
        skip("every company's latest balance sheet adds up", "no company in sec.db has a balance sheet yet")
    elif not any("balance sheet adds up" in f for f in FAILED):
        check(f"every company's latest balance sheet adds up ({looked} companies)", True)

    db_checked = len(PASSED) + len(FAILED)

    print("moomoo price feed")
    try:
        status = s.moomoo_status()
    except (s.OpenDError, OSError) as e:
        status = None
        skip("moomoo price feed", str(e) or "OpenD is not running")
    if status and not status.get("quotes"):
        skip("moomoo price feed", "OpenD is running but not logged in; log in to the moomoo OpenD app")
    elif status:
        try:
            bars = s.moomoo_candles("AAPL", "D", tail=True)["bars"]   # the last few candles; AAPL is almost always already counted this week
            check("daily candles arrive, oldest first", len(bars) > 3 and all(a[0] < b[0] for a, b in zip(bars, bars[1:])), f"{len(bars)} candles")
            check("each candle's high and low bracket its open and close", all(b[2] >= max(b[1], b[4]) - 1e-6 and b[3] <= min(b[1], b[4]) + 1e-6 for b in bars))
            pts = s.moomoo_valuation("AAPL")["points"]
            check("P/E history arrives", len(pts) > 1000, f"{len(pts)} days")
        except (s.OpenDError, OSError) as e:
            check("moomoo price feed answers", False, str(e))

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed, {len(SKIPPED)} skipped")
    if not db_checked:
        print("The database was not checked: sec.db has none of the companies above. Fetch some first,")
        print("for example `python3 sec_db.py fetch AAPL COST LLY`.")
        return 1
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
