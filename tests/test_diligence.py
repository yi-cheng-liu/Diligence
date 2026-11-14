"""Tests that need no internet, no moomoo OpenD and no sec.db: run `python3 -m unittest discover -s tests`.

Each test builds what it needs in a temporary folder. They cover bugs that have been fixed before, so they
stay fixed. check.py is the other half: it checks real figures in your own sec.db.
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sec_db as s  # noqa: E402
import make_fixtures as fx  # noqa: E402


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
    def test_retired_tables_are_dropped(self):
        self.db.execute("CREATE TABLE transcripts (ticker TEXT)")
        self.db.execute("CREATE TABLE ir_exhibits (cik INTEGER)")
        self.db.commit()
        s.connect().close()
        names = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master")}
        self.assertFalse(names & set(s.RETIRED_TABLES))
        self.assertIn("companies", names)

    def test_wal_and_country_column(self):
        self.assertEqual(self.db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertIn("country", [r[1] for r in self.db.execute("PRAGMA table_info(companies)")])


class Holders13F(unittest.TestCase):
    """The 13F data set: values in thousands, other share classes, restatements, incomplete reports."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "01jun2026-31aug2026_form13f.zip")
        sub = ["ACCESSION_NUMBER\tFILING_DATE\tSUBMISSIONTYPE\tCIK\tPERIODOFREPORT"]
        cover = ["ACCESSION_NUMBER\tISAMENDMENT\tAMENDMENTTYPE\tFILINGMANAGER_NAME"]
        summ = ["ACCESSION_NUMBER\tTABLEENTRYTOTAL\tTABLEVALUETOTAL"]
        info = ["ACCESSION_NUMBER\tINFOTABLE_SK\tNAMEOFISSUER\tTITLEOFCLASS\tCUSIP\tVALUE\tSSHPRNAMT\tSSHPRNAMTTYPE\tPUTCALL"]
        def filing(accn, cik, name, rows, kind="13F-HR", amend="N", atype="", entries=None, period="30-JUN-2026"):
            sub.append(f"{accn}\t14-AUG-2026\t{kind}\t{cik}\t{period}")
            cover.append(f"{accn}\t{amend}\t{atype}\t{name}")
            summ.append(f"{accn}\t{entries if entries is not None else len(rows)}\t0")
            for i, (cusip, value, shares) in enumerate(rows):
                info.append(f"{accn}\t{i}\tBERKSHIRE HATHAWAY INC\tCL B\t{cusip}\t{value}\t{shares}\tSH\t")
        B, A = "084670702", "084670108"          # B at $500 a share, A at $750,000
        filing("a1", 1, "ONE", [(B, 500_000, 1000)])
        filing("a2", 2, "THOUSANDS", [(B, 500, 1000)])                      # value in thousands
        filing("a3", 3, "HOLDS A", [(A, 1_500_000, 2)])                      # 2 A shares = 3,000 B
        filing("a4", 4, "MISLABELED", [(A, 100_000, 200)])                   # B shares filed under the A CUSIP
        filing("a5", 5, "MISTAKE", [(B, 50_000_000, 100_000)])               # filed by mistake...
        filing("a6", 5, "MISTAKE", [], kind="13F-HR/A", amend="Y", atype="RESTATEMENT")   # ...restated without it
        filing("a7", 6, "PARTIAL", [(B, 500, 1)], entries=900)               # a report missing most of its table
        import zipfile
        with zipfile.ZipFile(self.path, "w") as z:
            for name, lines in (("SUBMISSION.tsv", sub), ("COVERPAGE.tsv", cover), ("SUMMARYPAGE.tsv", summ), ("INFOTABLE.tsv", info)):
                z.writestr(name, "\n".join(lines) + "\n")

    def tearDown(self):
        self.dir.cleanup()

    def test_scan(self):
        period, rows, restated = s.scan_13f(self.path, ["084670702", "084670108"], "Berkshire Hathaway Inc")
        self.assertEqual(period, "2026-06-30")
        got = {r[1]: (r[5], r[6]) for r in rows}
        self.assertEqual(got[1], (1000, 500_000))
        self.assertEqual(got[2], (1000, 500_000))                    # thousands put right
        self.assertAlmostEqual(got[3][0], 3000)                      # A shares counted as B
        self.assertAlmostEqual(got[4][0], 200)                       # by value, so a mislabeled class stays right
        self.assertIn(("2026-06-30", 5), restated)                   # the restatement is seen though it holds nothing
        filers = s.f13_complete_filers(self.path)["2026-06-30"]
        self.assertIn(1, filers)
        self.assertNotIn(6, filers)                                  # 1 row of 900: says nothing about what it held

    def test_family(self):
        self.assertEqual(s._family("VANGUARD CAPITAL MANAGEMENT LLC"), "Vanguard")
        self.assertEqual(s._family("Vanguard Fiduciary Trust Co"), "Vanguard")
        self.assertIsNone(s._family("Vanguard Capital Wealth Advisors"))   # an unrelated firm with the name
        self.assertEqual(s._family("PRICE T ROWE ASSOCIATES INC /MD/"), "T. Rowe Price")
        self.assertEqual(s._family("T. Rowe Price Investment Management, Inc."), "T. Rowe Price")
        self.assertEqual(s._family("Capital World Investors"), "Capital Group")
        self.assertIsNone(s._family("Capital Advisors Inc"))


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

    def backups(self, kind):
        return sorted(n for n in os.listdir(s.notes_backups()) if n.endswith(kind))

    def test_notes_are_backed_up(self):
        self.assertEqual(s.notes_get(), {"store": None})
        s.notes_put({"store": {"data": {"A": {"thesis": "x" * 500}}, "savedAt": 1}})
        s.notes_put({"store": {"data": {"A": {"thesis": "x" * 600}}, "savedAt": 2}, "base": 1})   # the page had seen copy 1
        self.assertEqual(s.notes_get()["saved_at"], 2)
        today = time.strftime("desk-%Y-%m-%d.json")
        self.assertEqual(os.listdir(s.notes_backups()), [today])               # one copy a day, and nothing set aside
        with open(os.path.join(s.notes_backups(), today)) as f:
            self.assertEqual(json.load(f)["saved_at"], 2)                      # the day's copy is the last save
        with self.assertRaises(ValueError):
            s.notes_put({"store": "nonsense"})

    def test_a_save_that_looks_wrong_sets_the_old_copy_aside(self):
        s.notes_put({"store": {"data": {"A": {"thesis": "x" * 500}}, "savedAt": 5}})
        # a tab that last saw copy 1 saves over copy 5, which another tab wrote in between
        s.notes_put({"store": {"data": {"A": {"thesis": "x" * 510}}, "savedAt": 6}, "base": 1})
        self.assertEqual(len(self.backups("-before.json")), 1)
        with open(os.path.join(s.notes_backups(), self.backups("-before.json")[0])) as f:
            self.assertEqual(json.load(f)["saved_at"], 5)                      # what the stale tab replaced
        # the same save sent twice is not a conflict with itself
        s.notes_put({"store": {"data": {"A": {"thesis": "x" * 510}}, "savedAt": 6}, "base": 1})
        self.assertEqual(len(self.backups("-before.json")), 1)

    def test_notes_that_shrink_a_lot_set_the_old_copy_aside(self):
        s.notes_put({"store": {"data": {"A": {"thesis": "x" * 5000}}, "savedAt": 1}})
        s.notes_put({"store": {"data": {"A": {"thesis": ""}}, "savedAt": 2}, "base": 1})
        self.assertEqual(len(self.backups("-before.json")), 1)

    def test_old_backups_are_cleared_out(self):
        os.makedirs(s.notes_backups())
        for i in range(40):
            day = (datetime(2026, 1, 1) + timedelta(days=i)).strftime("%Y-%m-%d")
            for name in (f"desk-{day}.json", f"desk-{day}-000000-before.json"):
                with open(os.path.join(s.notes_backups(), name), "w") as f:
                    f.write("{}")
        s.notes_put({"store": {"data": {}, "savedAt": 1}})
        self.assertEqual(len(self.backups("-before.json")), s.NOTES_BEFORE)
        daily = [n for n in self.backups(".json") if not n.endswith("-before.json")]
        self.assertEqual(len(daily), s.NOTES_DAYS)
        self.assertEqual(daily[-1], time.strftime("desk-%Y-%m-%d.json"))       # today's is the newest kept
        self.assertNotIn("desk-2026-01-01.json", daily)

    def test_fred_key_is_saved_with_the_other_settings(self):
        with open(s.SETTINGS_PATH, "w") as f:
            json.dump({"name": "A", "email": "a@b.co"}, f)
        saved, s.macro_status = s.macro_status, lambda: {}                     # no call to FRED
        try:
            s.macro_set_key("0123456789abcdef0123456789abcdef")
        finally:
            s.macro_status = saved
        with open(s.SETTINGS_PATH) as f:
            self.assertEqual(json.load(f), {"name": "A", "email": "a@b.co", "fred_api_key": "0123456789abcdef0123456789abcdef"})
        self.assertEqual(os.listdir(self.dir.name), ["settings.json"])         # written through a temporary file, none left

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

    def test_redeemable_interests(self):
        # Lunar: its own redeemable preferred stock and its minority holders' redeemable units, added
        self.assertAlmostEqual(s.redeemable_from_tags({"TemporaryEquityCarryingAmountAttributableToParent": 6.6,
                                                       "RedeemableNoncontrollingInterestEquityCarryingAmount": 951.5}), 958.1)
        # a total given by the company is taken as it is, not added to its parts
        self.assertEqual(s.redeemable_from_tags({s.REDEEMABLE_TOTAL: 58, "RedeemableNoncontrollingInterestEquityCarryingAmount": 58}), 58)
        self.assertEqual(s.redeemable_from_tags({"RedeemableNoncontrollingInterestEquityCommonCarryingAmount": 3,
                                                 "RedeemableNoncontrollingInterestEquityPreferredCarryingAmount": 4}), 7)
        self.assertIsNone(s.redeemable_from_tags({"Liabilities": 1}))

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


class NewReports(TempDB):
    """Whether a company should be downloaded again, from the SEC's list of its filings."""

    def setUp(self):
        super().setUp()
        self.now = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
        self.recent = {"accessionNumber": ["8k", "q3", "q2"], "form": ["8-K", "10-Q", "10-Q"], "isXBRL": [1, 1, 1],
                       "filingDate": ["2026-11-10", "2026-11-09", "2026-08-06"],
                       "acceptanceDateTime": ["2026-11-10T13:00:00.000Z", "2026-11-09T21:12:33.000Z", "2026-08-06T20:30:00.000Z"]}
        self.db.execute("INSERT INTO facts (cik, concept, period_end, value, accession) VALUES (1, 'Revenues', '2026-06-30', 1, 'q2')")

    def state(self, fetched_at, now=None):
        self.db.execute("DELETE FROM companies")
        self.db.execute("INSERT INTO companies (cik, ticker, fetched_at) VALUES (1, 'CEG', ?)", (fetched_at,))
        return s.report_state(self.db, 1, self.recent, now or self.now)

    def test_a_report_filed_since_the_last_download_is_new(self):
        self.assertEqual(self.state("2026-11-09T20:00:00+00:00"), "new")

    def test_a_report_the_sec_never_adds_to_its_figures_is_left_alone(self):
        # downloaded since the 10-Q was filed, and company facts still lack it four days on (Constellation, NextEra)
        self.assertIsNone(self.state("2026-11-13T10:00:00+00:00"))
        self.assertIsNone(self.state("2026-11-10T10:00:00+00:00"))

    def test_late_figures_are_looked_for_hourly_for_three_days(self):
        day_after = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(self.state("2026-11-09T22:00:00+00:00", day_after), "late")
        self.assertIsNone(self.state("2026-11-10T11:30:00+00:00", day_after))       # looked half an hour ago

    def test_saved_figures_need_nothing(self):
        self.db.execute("INSERT INTO facts (cik, concept, period_end, value, accession) VALUES (1, 'Revenues', '2026-09-30', 1, 'q3')")
        self.assertIsNone(self.state("2026-11-01T00:00:00+00:00"))

    def test_times_as_the_sec_writes_them(self):
        self.assertEqual(s._utc("2026-11-09T21:12:33.000Z"), datetime(2026, 11, 9, 21, 12, 33, tzinfo=timezone.utc))
        self.assertEqual(s._utc("2026-11-09"), datetime(2026, 11, 9, tzinfo=timezone.utc))
        self.assertIsNone(s._utc("") or s._utc(None) or s._utc("2026-13-45"))


class Tidy(TempDB):
    """sec.db is kept to the stocks in the page's groups: the rest is deleted, with time to change your mind."""

    def setUp(self):
        super().setUp()
        self.saved = (s.SETTINGS_PATH, s.NOTES_PATH, dict(s._TIDY))
        s.SETTINGS_PATH = os.path.join(self.dir.name, "settings.json")
        s.NOTES_PATH = os.path.join(self.dir.name, "notes", "desk.json")
        s._TIDY.update(since={}, swept=0)
        self.now = time.time()
        stamp = lambda ago: (datetime.now(timezone.utc) - timedelta(seconds=ago)).isoformat(timespec="seconds")
        for cik, t, ago in ((1, "LISTED", 2 * 86400), (2, "OPEN", 2 * 86400), (3, "KEPT", 2 * 86400), (4, "GONE", 2 * 86400), (5, "LOOKEDUP", 60)):
            self.db.execute("INSERT INTO companies (cik, ticker, tickers, name, fetched_at) VALUES (?,?,?,?,?)", (cik, t, t, t, stamp(ago)))
            self.db.execute("INSERT INTO facts (cik, concept, period_end, value) VALUES (?, 'Revenues', '2025-12-31', 1)", (cik,))
        self.db.execute("INSERT INTO facts (cik, concept, period_end, value) VALUES (99, 'Revenues', '2025-12-31', 1)")   # its company is long gone
        self.db.execute("INSERT INTO meta VALUES ('13f:4', 'x')")
        self.db.execute("INSERT INTO macro_cache VALUES ('series:OLD', ?, '{}')", (self.now - 3 * 86400,))
        self.db.execute("INSERT INTO macro_cache VALUES ('series:NEW', ?, '{}')", (self.now,))
        self.db.commit()

    def tearDown(self):
        s.SETTINGS_PATH, s.NOTES_PATH = self.saved[:2]
        s._TIDY.clear()
        s._TIDY.update(self.saved[2])
        super().tearDown()

    def page(self, symbols, current="OPEN", **settings):
        s.notes_put({"store": {"data": {}, "groups": [{"id": "g", "symbols": symbols}], "current": current, "savedAt": 1}})
        with open(s.SETTINGS_PATH, "w") as f:
            json.dump(settings, f)

    def left(self):
        return [r[0] for r in self.db.execute("SELECT ticker FROM companies ORDER BY cik")]

    def test_nothing_goes_until_the_page_has_saved_its_groups(self):
        self.assertEqual(s.tidy(self.db, self.now) + s.tidy(self.db, self.now + 86400), [])
        self.assertEqual(len(self.left()), 5)
        self.page([], current="OPEN")                      # a copy with no stock in any group says nothing either
        self.assertEqual(s.tidy(self.db, self.now + 2 * 86400) + s.tidy(self.db, self.now + 3 * 86400), [])
        self.assertEqual(len(self.left()), 5)

    def test_a_company_in_no_group_goes_after_a_while(self):
        self.page(["LISTED"], keep=["KEPT"])
        self.assertEqual(s.tidy(self.db, self.now), [])                          # first seen in no group: not yet
        self.assertEqual(s.tidy(self.db, self.now + 60), [])
        self.assertEqual(s.tidy(self.db, self.now + s.TIDY_AFTER + 1), ["GONE"])
        self.assertEqual(self.left(), ["LISTED", "OPEN", "KEPT", "LOOKEDUP"])     # in a group, open, kept, just looked up
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM facts WHERE cik=4").fetchone()[0], 0)
        self.assertIsNone(self.db.execute("SELECT 1 FROM meta WHERE key='13f:4'").fetchone())
        self.assertEqual(s.tidy(self.db, self.now + s.TIDY_FRESH + 1), ["LOOKEDUP"])   # an hour after it was downloaded

    def test_a_stock_put_back_in_a_group_starts_over(self):
        self.page(["LISTED"])
        s.tidy(self.db, self.now)
        self.page(["LISTED", "GONE", "KEPT"])                                    # moved to another group meanwhile
        self.assertEqual(s.tidy(self.db, self.now + s.TIDY_AFTER + 1), [])
        self.page(["LISTED"])
        self.assertEqual(s.tidy(self.db, self.now + s.TIDY_AFTER + 2), [])        # the wait starts again
        self.assertEqual(s.tidy(self.db, self.now + 2 * s.TIDY_AFTER + 3), ["GONE", "KEPT"])

    def test_it_can_be_switched_off(self):
        self.page(["LISTED"], tidy=False)
        s.tidy(self.db, self.now)
        self.assertEqual(s.tidy(self.db, self.now + 86400), [])
        self.assertEqual(len(self.left()), 5)

    def test_leftovers_go_too(self):
        s.tidy(self.db, self.now)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM facts WHERE cik=99").fetchone()[0], 0)
        self.assertEqual([r[0] for r in self.db.execute("SELECT key FROM macro_cache")], ["series:NEW"])
        self.assertEqual(len(self.left()), 5)                                    # and no company with them

    def test_freed_space_goes_back_to_the_disk(self):
        self.db.execute("CREATE TABLE filler (cik INTEGER, body TEXT)")
        self.db.executemany("INSERT INTO filler VALUES (?, ?)", [(i % 2, "x" * 900) for i in range(3000)])
        self.db.commit()
        self.assertEqual(s.reclaim_space(self.db, least=100_000), 0)             # nothing freed yet
        self.db.execute("DELETE FROM filler")
        self.db.commit()
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()            # into the file itself, to measure it
        before = os.path.getsize(s.DB_PATH)
        self.assertGreater(s.reclaim_space(self.db, least=100_000), 2_000_000)
        self.assertLess(os.path.getsize(s.DB_PATH), before - 2_000_000)
        self.db.executemany("INSERT INTO filler VALUES (?, ?)", [(0, "x" * 900) for i in range(3000)])
        self.db.commit()
        self.db.execute("DELETE FROM filler")
        self.db.commit()
        self.assertGreater(s.reclaim_space(self.db, least=100_000), 2_000_000)   # the second time without rewriting the file
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM companies").fetchone()[0], 5)


@unittest.skipIf(os.name == "nt", "research runs are followed by process number only on macOS and Linux")
class ResearchRuns(unittest.TestCase):
    """A run found in a theme's file after a restart is only this run if its process started when the run did."""

    def test_a_number_now_used_by_another_program_is_not_the_run(self):
        me = os.getpid()                                   # a live process that is no child of this one
        started = s._proc_stamp(me)
        self.assertTrue(started)
        self.assertTrue(s._alive(None, me, started))
        self.assertFalse(s._alive(None, me, "Thu Jan  1 00:00:00 1970"))
        self.assertFalse(s._alive(None, 0, started))       # no number saved at all
        self.assertFalse(s._alive(None, me, None))         # a run from before start times were saved must look like a run


class Companies(unittest.TestCase):
    """Whole companies, built from what they filed (tests/fixtures; see tests/make_fixtures.py). These are the ones
    whose figures took special handling, so a change that moves any of their figures fails here."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        cls.saved = (s.DB_PATH, s.SETTINGS_PATH)
        s.DB_PATH, s.SETTINGS_PATH = os.path.join(cls.dir.name, "sec.db"), os.path.join(cls.dir.name, "settings.json")
        cls.db = s.connect()
        cls.fixtures = {}
        for path in fx.paths():
            data = fx.read(path)
            cls.fixtures[data["company"]["ticker"]] = (fx.load(cls.db, data), data)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()
        s.DB_PATH, s.SETTINGS_PATH = cls.saved
        cls.dir.cleanup()

    def year(self, t, y):
        return s.desk_fundamentals(self.db, self.fixtures[t][0])["byYear"].get(str(y), {})

    def metric(self, t, y, m):
        row = self.db.execute("SELECT value FROM metrics WHERE cik=? AND period_type='annual' AND fiscal_year=? AND metric=?",
                              (self.fixtures[t][0], y, m)).fetchone()
        return row[0] if row else None

    def test_every_figure_is_what_it_was(self):
        self.assertGreaterEqual(len(self.fixtures), 5)
        for t, (cik, data) in self.fixtures.items():
            got, want = fx.figures(self.db, cik), data["expected"]
            hint = f"{t}: if this change is meant, run `python3 tests/make_fixtures.py --expected`"
            self.assertEqual([r[:5] for r in got], [r[:5] for r in want], hint)
            for a, b in zip(got, want):
                if a[5] != b[5]:
                    self.assertAlmostEqual(a[5], b[5], delta=abs(b[5]) * 1e-9, msg=f"{a[:5]} {hint}")

    def test_caterpillar_debt_is_added_up_from_its_business_lines(self):
        self.assertAlmostEqual(self.year("CAT", 2025)["debt"], 43330, delta=43330 * 0.05)
        quarters = s.desk_quarters(self.db, self.fixtures["CAT"][0])
        self.assertGreater(quarters[-1]["row"].get("debt") or 0, 30000)

    def test_nike_operating_income_is_worked_out(self):
        self.assertAlmostEqual(self.year("NKE", 2025)["ebit"], 3676, delta=3676 * 0.05)

    def test_mcdonalds_share_count_filed_in_millions(self):
        self.assertTrue(650e6 < self.metric("MCD", 2023, "shares_diluted") < 800e6)

    def test_nextera_quarters_come_from_the_statements(self):
        cik, data = self.fixtures["NEE"]
        last = s.desk_quarters(self.db, cik)[-1]
        self.assertTrue((last["y"], last["q"]) >= (2026, 3))                     # the SEC's company facts stop two 10-Qs earlier
        self.assertGreater(self.metric("NEE", 2025, "capex"), 10000e6)
        # and that missing 10-Q does not make the company look out of date for ever
        reports = sorted((f for f in data["filings"] if f[2] in ("10-K", "10-Q")), key=lambda f: f[3], reverse=True)
        recent = {"accessionNumber": [f[1] for f in reports], "form": [f[2] for f in reports], "isXBRL": [f[9] for f in reports],
                  "filingDate": [f[3] for f in reports]}
        self.assertIsNone(self.db.execute("SELECT 1 FROM facts WHERE cik=? AND accession=?", (cik, reports[0][1])).fetchone())
        filed = datetime.fromisoformat(reports[0][3]).replace(tzinfo=timezone.utc)
        self.db.execute("UPDATE companies SET fetched_at=? WHERE cik=?", ((filed + timedelta(days=5)).isoformat(), cik))
        self.assertIsNone(s.report_state(self.db, cik, recent, filed + timedelta(days=6)))

    def test_balance_sheets_add_up_with_redeemable_interests(self):
        # NextEra and Nebius report redeemable minority interests between liabilities and equity in some years
        for t in ("NEE", "NBIS"):
            cik = self.fixtures[t][0]
            # the years with the line, where the company also gives total liabilities (not every year's filing does)
            years = [(y, x, self.year(t, y)) for y, x in s.desk_statements(self.db, cik)["annual"].items()
                     if x.get("redeemable") and x.get("liabTotal") is not None]
            years = [(y, x, v) for y, x, v in years if v.get("equity") is not None and v.get("assets")]
            self.assertTrue(years, t)
            for y, x, v in years:
                told = v["equity"] + x["liabTotal"] + x["redeemable"] + (x.get("minority") or 0)
                self.assertAlmostEqual(told, v["assets"], delta=abs(v["assets"]) * 0.005, msg=f"{t} {y}")

    def test_nebius_is_read_in_dollars(self):
        self.assertEqual(s.desk_statements(self.db, self.fixtures["NBIS"][0]).get("currency"), "USD")
        self.assertLess(self.year("NBIS", 2021).get("revenue") or 0, 20000)       # not its rouble years


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
