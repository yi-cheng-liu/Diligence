"""Tests that need no internet, no moomoo OpenD and no sec.db: run `python3 -m unittest discover -s tests`.

Each test builds what it needs in a temporary folder. They cover bugs that have been fixed before, so they
stay fixed. check.py is the other half: it checks real figures in your own sec.db.
"""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sec_db as s  # noqa: E402


class TempDB(unittest.TestCase):
    """A fresh, empty sec.db in a temporary folder for each test."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self._db_path, s.DB_PATH = s.DB_PATH, os.path.join(self.dir.name, "sec.db")
        self.db = s.connect()

    def tearDown(self):
        self.db.close()
        s.DB_PATH = self._db_path
        self.dir.cleanup()


class Symbols(unittest.TestCase):
    def test_hong_kong_codes_round_trip(self):
        for code, sym in [("00700", "0700.HK"), ("09988", "9988.HK"), ("89988", "89988.HK"), ("00005", "0005.HK")]:
            self.assertEqual(s.moomoo_symbol(1, code), sym)
            self.assertEqual(s.moomoo_security(sym), ("HK", code))

    def test_class_shares(self):
        self.assertEqual(s.moomoo_symbol(11, "BRK.B"), "BRK-B")
        self.assertEqual(s.moomoo_security("BRK-B"), ("US", "BRK.B"))


class Tickers(TempDB):
    def test_every_spelling_after_a_refresh(self):
        def refresh(db, client, force=False):   # the SEC's list writes class shares with a hyphen
            db.execute("INSERT OR REPLACE INTO tickers VALUES ('ZZZQ-A', 999, 'Test Co')")
        real, s.refresh_tickers = s.refresh_tickers, refresh
        try:
            self.assertEqual(s.resolve(self.db, object(), "zzzq.a"), (999, "ZZZQ-A"))
            self.assertEqual(s.resolve(self.db, None, "ZZZQ/A"), (999, "ZZZQ-A"))
        finally:
            s.refresh_tickers = real


class Search(TempDB):
    def setUp(self):
        super().setUp()
        self.db.execute("INSERT INTO companies (cik, ticker, name) VALUES (1, 'TST', 'Test Co')")
        text = "Our R&D spending on the supply-chain rose. AT&T is a customer. See the Form 10-K."
        rows = [(1, "a1", "10-K", "2025-01-01", "2024-12-31", "u", "full", "Full document", text),
                (1, "a1", "10-K", "2025-01-01", "2024-12-31", "u", "1", "Business", text),
                (1, "a2", "10-K", "2024-01-01", "2023-12-31", "u", "full", "Full document", "Unsplit filing about tariffs.")]
        self.db.executemany("INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?)", rows)
        if s.has_fts(self.db):
            self.db.execute("INSERT INTO documents_fts VALUES (?,?,?,?)", (text, 1, "a1", "1"))
            self.db.execute("INSERT INTO documents_fts VALUES (?,?,?,?)", ("Unsplit filing about tariffs.", 1, "a2", "full"))
        self.db.commit()

    def test_queries_that_are_not_search_syntax(self):
        for q in ["R&D", "AT&T", "10-K", "supply-chain", 'say "hi', "*", "NEAR("]:
            s.search_docs(self.db, q, "TST")   # must not raise
        self.assertTrue(s.search_docs(self.db, "R&D", "TST"))
        self.assertEqual(s.search_docs(self.db, "   ", "TST"), [])

    def test_unknown_ticker_is_an_error(self):
        with self.assertRaises(LookupError):
            s.search_docs(self.db, "tariffs", "NOPE")

    def test_plain_search_reads_unsplit_filings_once(self):
        real, s.has_fts = s.has_fts, lambda db: False
        try:
            self.assertEqual([h["accession"] for h in s.search_docs(self.db, "tariffs", "TST")], ["a2"])
            self.assertEqual(len(s.search_docs(self.db, "AT&T", "TST")), 1)   # the section, not its whole document too
        finally:
            s.has_fts = real


class Database(TempDB):
    def test_wal_and_country_column(self):
        self.assertEqual(self.db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertIn("country", [r[1] for r in self.db.execute("PRAGMA table_info(companies)")])


class Forget(TempDB):
    """Deleting a stock that is in no group: its rows go; anything shared, unrelated, or a report, stays."""

    def setUp(self):
        super().setUp()
        self.reports = os.path.join(self.dir.name, "reports")
        os.makedirs(os.path.join(self.reports, "Company", "ZZZ"))
        with open(os.path.join(self.reports, "Company", "ZZZ", "a.md"), "w") as f:
            f.write("# ZZZ\n")
        self._saved = s.reports_dir
        s.reports_dir = lambda: self.reports
        for cik, t, all_ in ((1, "ZZZ", "ZZZ"), (2, "GOOGL", "GOOGL,GOOG"), (3, "KEEP", "KEEP")):   # one company, two tickers
            self.db.execute("INSERT INTO companies (cik, ticker, tickers, name) VALUES (?, ?, ?, ?)", (cik, t, all_, t))
            self.db.execute("INSERT INTO facts (cik, concept, period_end, value) VALUES (?, 'Revenues', '2025-12-31', 1)", (cik,))
        self.db.commit()

    def tearDown(self):
        s.reports_dir = self._saved
        super().tearDown()

    def test_deletes_only_its_own_data_and_never_reports(self):
        foot = {f["ticker"]: f for f in s.stock_footprint(self.db, ["ZZZ", "GOOG"], keep=["GOOGL", "KEEP"])}
        self.assertTrue(foot["GOOG"]["shared"])
        s.forget_stocks(self.db, ["ZZZ", "GOOG"], keep=["GOOGL", "KEEP"])
        facts = lambda cik: self.db.execute("SELECT COUNT(*) FROM facts WHERE cik=?", (cik,)).fetchone()[0]
        self.assertEqual(facts(1), 0)                     # ZZZ's rows are gone
        self.assertEqual(facts(2), 1)                     # GOOG shares its filer with GOOGL, still in a group
        self.assertEqual(facts(3), 1)                     # unrelated
        self.assertTrue(os.path.exists(os.path.join(self.reports, "Company", "ZZZ", "a.md")))   # its report is kept


class TimeZones(unittest.TestCase):
    def test_rules_match_the_time_zone_database(self):
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo("America/New_York")
        except Exception:
            self.skipTest("no time zone database on this computer")
        for name, (hours, rule) in s._RULE_ZONES.items():
            real, mine = ZoneInfo(name), s._RuleZone(name, hours, rule)
            t = datetime(2024, 1, 1, tzinfo=timezone.utc)
            while t.year < 2026:
                self.assertEqual(t.astimezone(real).replace(tzinfo=None), t.astimezone(mine).replace(tzinfo=None), (name, t))
                t += timedelta(hours=1)

    def test_works_without_a_time_zone_database(self):
        def missing(name):
            raise s.ZoneInfoNotFoundError(name)
        real, s.ZoneInfo = s.ZoneInfo, missing
        try:
            z = s.market_zone("America/New_York")
            summer = datetime(2025, 7, 1, 16, tzinfo=timezone.utc).astimezone(z)
            winter = datetime(2025, 1, 15, 17, tzinfo=timezone.utc).astimezone(z)
            self.assertEqual((summer.hour, winter.hour), (12, 12))
        finally:
            s.ZoneInfo = real


class Files(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.saved = (s.SETTINGS_PATH, s.REPORTS_DIR_DEFAULT, s.NOTES_PATH)
        s.SETTINGS_PATH = os.path.join(self.dir.name, "settings.json")
        s.REPORTS_DIR_DEFAULT = os.path.join(self.dir.name, "reports")
        s.NOTES_PATH = os.path.join(self.dir.name, "notes", "desk.json")

    def tearDown(self):
        s.SETTINGS_PATH, s.REPORTS_DIR_DEFAULT, s.NOTES_PATH = self.saved
        self.dir.cleanup()

    def test_reports_folder_defaults_inside_the_project(self):
        self.assertEqual(s.reports_dir(), s.REPORTS_DIR_DEFAULT)
        self.assertTrue(os.path.isdir(s.REPORTS_DIR_DEFAULT))   # made on a fresh copy
        with open(s.SETTINGS_PATH, "w") as f:
            json.dump({"reports_dir": "~/somewhere-else"}, f)
        self.assertEqual(s.reports_dir(), os.path.realpath(os.path.expanduser("~/somewhere-else")))

    def test_notes_keep_one_copy(self):
        self.assertEqual(s.notes_get(), {"store": None})
        s.notes_put({"store": {"data": {"A": {}}, "savedAt": 1}})
        s.notes_put({"store": {"data": {"B": {}}, "savedAt": 2}})
        self.assertEqual(list(s.notes_get()["store"]["data"]), ["B"])
        self.assertEqual(os.listdir(os.path.dirname(s.NOTES_PATH)), ["desk.json"])   # no older copy beside it
        with self.assertRaises(ValueError):
            s.notes_put({"store": "nonsense"})

    def test_research_instructions_build(self):
        text = s.RESEARCH_HOW.format(today="2026-01-01", language="English")
        self.assertIn('{"type": "bar"', text)


class Figures(unittest.TestCase):
    """How figures are put together, on the tag values companies really filed."""

    def test_debt_adds_every_family_once(self):
        apple = {"CommercialPaper": 7979, "LongTermDebt": 90700, "LongTermDebtCurrent": 12350, "LongTermDebtNoncurrent": 78328}
        self.assertEqual(s.debt_from_tags(apple)[2], 98679)            # commercial paper is not left out
        realty = {"CommercialPaper": 516.8, "LoansPayable": 1701.6, "NotesPayable": 25031.9, "SecuredDebt": 37.8}
        self.assertAlmostEqual(s.debt_from_tags(realty)[2], 27288.1)   # notes and loans count
        self.assertEqual(s.debt_from_tags({"DebtLongtermAndShorttermCombinedAmount": 6897, "LongTermDebtCurrent": 0})[2], 6897)
        self.assertEqual(s.debt_from_tags({"ConvertibleLongTermNotesPayable": 1491, "LongTermDebt": 1491, "CommercialPaper": 0})[2], 1491)
        xom = {"CommercialPaper": 3059, "DebtCurrent": 9296, "LongTermDebtAndCapitalLeaseObligations": 34241,
               "LongTermDebtAndCapitalLeaseObligationsCurrent": 6234, "LongTermDebtCurrent": 6200, "ShortTermBankLoansAndNotesPayable": 3}
        self.assertEqual(s.debt_from_tags(xom)[2], 9296 + 34241)
        self.assertIsNone(s.debt_from_tags({"Assets": 1}))

    def test_short_term_investments(self):
        self.assertEqual(s.sti_from_tags({"DebtSecuritiesCurrent": 39065}), 39065)
        self.assertEqual(s.sti_from_tags({"ShortTermInvestments": 10005, "AvailableForSaleSecuritiesDebtSecuritiesCurrent": 9000}), 10005)

    def test_statement_header_scales(self):
        def first(title):
            htm = (f'<table><tr><th class="tl"><div><strong>{title}</strong></div></th><th class="th"><div>Dec. 31, 2025</div></th></tr>'
                   '<tr class="ro"><td class="pl"><a onclick="Show.showAR( this, \'defref_us-gaap_Assets\', window );">Total assets</a></td>'
                   '<td class="nump">1,000</td></tr></table>')
            return s.parse_rendered(htm)["rows"][0][6][0]
        self.assertEqual(first("Balance Sheets - USD ($) shares in Thousands, $ in Millions"), 1e9)   # Apple
        self.assertEqual(first("Balance Sheet - USD ($) shares in Thousands"), 1000)                  # McDonald's: dollars as shown
        self.assertEqual(first("Statements - USD ($) In Millions, except Per Share data"), 1e9)

    def test_splits_are_found_from_restated_share_counts(self):
        dir_ = tempfile.TemporaryDirectory()
        saved, s.DB_PATH = s.DB_PATH, os.path.join(dir_.name, "sec.db")
        try:
            db = s.connect()
            rows = [(1, "us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding", "shares", "2021-01-27", "2022-01-30", v, a, None, None, "10-K", f, None)
                    for v, a, f in ((2535e6, "a", "2022-03-18"), (25350e6, "b", "2024-02-21"))]
            rows += [(1, "us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding", "shares", "2019-01-28", "2020-01-26", v, a, None, None, "10-K", f, None)
                     for v, a, f in ((625e6, "c", "2020-02-20"), (2500e6, "d", "2021-02-26"))]
            db.executemany("INSERT INTO facts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            self.assertEqual(sorted(s.split_events(db, 1)), [("2021-02-26", 4), ("2024-02-21", 10)])
            db.close()
        finally:
            s.DB_PATH = saved
            dir_.cleanup()

    def test_ttm_takes_the_fourth_quarter_from_the_year(self):
        from datetime import date as d
        q = lambda y, n, end, eps, ni: {"y": y, "q": n, "end": end, "row": {"eps": eps, "netIncome": ni, "revenue": 10}}
        quarters = [q(2025, 2, d(2025, 6, 30), 1.0, 100), q(2025, 3, d(2025, 9, 30), 1.1, 110), q(2025, 4, d(2025, 12, 31), None, 130),
                    q(2026, 1, d(2026, 3, 31), 1.4, 140), q(2025, 1, d(2025, 3, 31), 0.9, 90)]
        years = [{"y": 2025, "q": None, "end": d(2025, 12, 31), "row": {"eps": 4.3, "netIncome": 430}}]
        out = s._ttm(quarters, years)
        self.assertAlmostEqual(out["eps"], 1.0 + 1.1 + (4.3 - 0.9 - 1.0 - 1.1) + 1.4)
        self.assertEqual(out["netIncome"], 480)
        self.assertEqual(s._ttm([], years)["basis"], "year")

    def test_share_class_and_financials(self):
        self.assertEqual(s.ticker_class("BRK-B"), "B")
        self.assertIsNone(s.ticker_class("GOOGL"))
        self.assertTrue(s.is_financial("6021") and s.is_financial(6331))
        self.assertFalse(s.is_financial("2834") or s.is_financial("6798") or s.is_financial(None))


class Server(unittest.TestCase):
    """The web server, on a spare port, with no SEC client."""

    @classmethod
    def setUpClass(cls):
        handler = s.make_handler(None)
        handler.log_message = lambda *a: None   # keep the test output quiet
        cls.srv = s.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def ask(self, path, method="GET", headers=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, dict(r.headers)
        except urllib.error.HTTPError as e:
            with e:
                return e.code, dict(e.headers)

    def test_only_this_computer(self):
        self.assertEqual(self.ask("/api/health")[0], 200)
        self.assertEqual(self.ask("/api/health", headers={"Host": "evil.example"})[0], 403)
        status, headers = self.ask("/api/health", headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        status, headers = self.ask("/api/health", headers={"Origin": "http://localhost:3000"})
        self.assertEqual((status, headers.get("Access-Control-Allow-Origin")), (200, "http://localhost:3000"))
        self.assertEqual(self.ask("/desk/keys.local.js", headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors"})[0], 403)

    def test_other_methods_do_not_crash(self):
        for m in ("HEAD", "OPTIONS", "PUT", "DELETE", "PATCH"):
            self.assertEqual(self.ask("/api/health", method=m)[0], 501)
        self.assertEqual(self.ask("/api/health")[0], 200)   # still answering

    def test_old_page_name_is_sent_on(self):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        opener = urllib.request.build_opener(NoRedirect)
        try:
            opener.open(f"http://127.0.0.1:{self.port}/desk/Stock%20Research%20Desk%20v2.dc.html?open=NVDA", timeout=10)
            self.fail("expected a redirect")
        except urllib.error.HTTPError as e:
            e.close()
            self.assertEqual(e.code, 301)
            self.assertEqual(e.headers["Location"], "/desk/diligence.dc.html?open=NVDA")


if __name__ == "__main__":
    unittest.main()
