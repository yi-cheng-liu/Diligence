#!/usr/bin/env python3
"""Quick health check for the SEC database: run `python3 check.py` after any change.

It reads a handful of companies straight from sec.db (no server, no internet) and checks figures that
have gone wrong before, plus a few rules every company should obey. If moomoo OpenD is running and
logged in it also checks the chart's price feed. Nothing is written or downloaded.

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

    print("Figures that have been wrong before")
    for t in ("AAPL", "COST", "LLY", "CAT", "XOM", "NBIS"):
        if not have(t):
            skip(f"{t} figures", f"{t} is not in sec.db; run `python3 sec_db.py fetch {t}`")
    if have("AAPL"):
        check("Apple FY2025 revenue is $416.2B", near(year("AAPL", 2025).get("revenue"), 416161))
    if have("COST"):
        check("Costco FY2025 revenue is $275.2B", near(year("COST", 2025).get("revenue"), 275235))
    if have("LLY"):   # Lilly reports no operating income line; it is worked back from pre-tax income
        check("Lilly FY2025 operating income is worked out ($26.3B)", near(year("LLY", 2025).get("ebit"), 26302, 0.05), year("LLY", 2025).get("ebit"))
        check("Lilly FY2025 R&D is on file", (more("LLY")["annual"].get("2025", {}).get("rd") or 0) > 10000)
        check("Lilly's last 8 quarters all have operating income", all("ebit" in q["row"] for q in quarters("LLY")[-8:]))
    if have("CAT"):   # Caterpillar reports debt only per business line
        check("Caterpillar FY2025 debt is $43.3B", near(year("CAT", 2025).get("debt"), 43330, 0.05), year("CAT", 2025).get("debt"))
        check("Caterpillar's latest quarter has debt", (quarters("CAT")[-1]["row"].get("debt") or 0) > 30000)
    if have("XOM"):   # Exxon's history sits under its old registration
        st = more("XOM")
        check("Exxon has 15+ years of statements", len(st["annual"]) >= 15, len(st["annual"]))
        check("Exxon FY2025 has investing and financing cash flow", all(st["annual"].get("2025", {}).get(k) is not None for k in ("cfi", "cff")))
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
