#!/usr/bin/env python3
"""Company fixtures for the tests: what the SEC filed for a company, as sec.db holds it, and the figures built from it.

    python3 tests/make_fixtures.py CAT NEE     # save (or replace) the fixtures of these companies, from your sec.db
    python3 tests/make_fixtures.py --expected  # after a change to how figures are built that is meant: work out
                                               # every fixture's figures again from its own filed data

A fixture is tests/fixtures/<TICKER>.json.gz: the company's row, its annual and quarterly filings, every number it
reported (facts), the statements as its reports present them and its debt by business line, plus "expected", the
metrics build_metrics makes of them. The tests load each one into an empty database, build the figures again and
compare, so a change that moves any figure of these companies shows up without sec.db or the internet.
"""
import glob
import gzip
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import sec_db as s  # noqa: E402

FIXTURES = os.path.join(HERE, "fixtures")
RAW_TABLES = ("facts", "statement_lines", "debt_split")   # read whole; filings are cut down to the reports themselves
REPORT_FORMS = s.ANNUAL_FORMS + s.QUARTER_FORMS


def paths():
    return sorted(glob.glob(os.path.join(FIXTURES, "*.json.gz")))


def read(path):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def write(path, data):
    os.makedirs(FIXTURES, exist_ok=True)
    # mtime=0 keeps the file the same, byte for byte, when its contents are
    with open(path, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as f:
        f.write(json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def load(db, data):
    """Put a fixture's filed data into a database and build its figures. Returns the company's CIK."""
    co = data["company"]
    cik = co["cik"]
    with db:
        db.execute("INSERT OR REPLACE INTO tickers VALUES (?,?,?)", (co["ticker"], cik, co.get("name")))
        db.execute(f"INSERT OR REPLACE INTO companies ({','.join(co)}) VALUES ({','.join('?' * len(co))})", list(co.values()))
        for table in ("filings",) + RAW_TABLES:
            rows = data.get(table) or []
            if rows:
                db.executemany(f"INSERT OR REPLACE INTO {table} VALUES ({','.join('?' * len(rows[0]))})", rows)
        s.build_metrics(db, cik)
    return cik


def figures(db, cik):
    """The metrics built for a company, in a fixed order: [period type, fiscal year, quarter, period end, metric, value]."""
    return [list(r) for r in db.execute(
        "SELECT period_type, fiscal_year, fiscal_quarter, period_end, metric, value FROM metrics WHERE cik=? "
        "ORDER BY period_type, period_end, metric", (cik,))]


def built(data):
    """A fixture's figures, built in a database of its own."""
    folder = tempfile.TemporaryDirectory()
    saved, s.DB_PATH = s.DB_PATH, os.path.join(folder.name, "sec.db")
    try:
        db = s.connect()
        try:
            return figures(db, load(db, data))
        finally:
            db.close()
    finally:
        s.DB_PATH = saved
        folder.cleanup()


def save(db, ticker):
    cik = s.lookup_cik(db, ticker)
    if cik is None:
        sys.exit(f"{ticker} is not in sec.db; run `python3 sec_db.py fetch {ticker}` first.")
    data = {"company": dict(db.execute("SELECT * FROM companies WHERE cik=?", (cik,)).fetchone())}
    marks = ",".join("?" * len(REPORT_FORMS))
    data["filings"] = [list(r) for r in db.execute(f"SELECT * FROM filings WHERE cik=? AND form IN ({marks}) ORDER BY filing_date, accession",
                                                    (cik, *REPORT_FORMS))]
    for table in RAW_TABLES:
        data[table] = [list(r) for r in db.execute(f"SELECT * FROM {table} WHERE cik=? ORDER BY rowid", (cik,))]
    data["expected"] = built(data)
    path = os.path.join(FIXTURES, data["company"]["ticker"].upper() + ".json.gz")
    write(path, data)
    live = figures(db, cik)
    same = "the same as" if live == data["expected"] else "DIFFERENT from"
    print(f"{ticker}: {len(data['facts'])} facts, {len(data['statement_lines'])} statement lines, {len(data['expected'])} figures "
          f"({same} sec.db's), {os.path.getsize(path) // 1000} KB")


def main(argv):
    if argv == ["--expected"]:
        for path in paths():
            data = read(path)
            new = built(data)
            changed = sum(a != b for a, b in zip(new, data["expected"])) + abs(len(new) - len(data["expected"]))
            data["expected"] = new
            write(path, data)
            print(f"{os.path.basename(path)}: {len(new)} figures, {changed} changed")
        return
    if not argv or argv[0].startswith("-"):
        sys.exit(__doc__)
    db = s.connect()
    try:
        for t in argv:
            save(db, t)
    finally:
        db.close()


if __name__ == "__main__":
    main(sys.argv[1:])
