#!/usr/bin/env python3
"""
sec_db.py: a free, local stock-research database built from SEC EDGAR.

Ask for a company and it downloads what the SEC publishes about it for free
(company profile, every filing in its history, every number it has reported in
its financial statements, recent insider trades from Form 4, and the text of its
latest annual report) and stores it in one SQLite file, sec.db, next to this script.

Usage:
    python sec_db.py fetch MSFT            # download / refresh a company
    python sec_db.py fetch MSFT --docs 3   # also store text of the last 3 10-Ks
    python sec_db.py show MSFT             # print the annual financial summary
    python sec_db.py list                  # companies already in the database
    python sec_db.py search "supply chain" # full-text search of stored reports
    python sec_db.py export MSFT           # write MSFT.json for a website
    python sec_db.py serve                 # Diligence desk + JSON API on http://localhost:8765

Only the Python standard library is used; nothing to install.
"""

import argparse
import codecs
import concurrent.futures
import errno
import gzip
import hashlib
import html
import json
import mimetypes
import os
import re
import shutil
import signal
import socket
import sqlite3
import statistics
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone, tzinfo
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("SEC_DB_PATH", os.path.join(HERE, "sec.db"))
SETTINGS_PATH = os.path.join(HERE, "settings.json")
DESK_DIR = os.path.join(HERE, "desk")
DESK_PAGE = "diligence.dc.html"
OLD_DESK_PAGES = ("Stock Research Desk v2.dc.html",)   # earlier names of the page, sent on to the new one

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SUBMISSIONS_PAGE_URL = "https://data.sec.gov/submissions/{name}"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accn}/{doc}"

# SEC fair-access rule: at most 10 requests per second.
MIN_SECONDS_BETWEEN_REQUESTS = 0.15


# --------------------------------------------------------------------------
# Settings (the SEC requires a contact name and email in every request)
# --------------------------------------------------------------------------

def load_settings(interactive=True):
    settings = {}
    if os.path.exists(SETTINGS_PATH):
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            settings = json.load(f)
    name = (settings.get("name") or "").strip()
    email = (settings.get("email") or "").strip()
    if not name or not email or "@" not in email or email == "you@example.com":
        if not interactive or not sys.stdin.isatty():
            sys.exit(
                "The SEC asks every request to include a contact name and email.\n"
                f"Open {SETTINGS_PATH} and fill in \"name\" and \"email\" "
                "(copy settings.example.json if the file does not exist)."
            )
        print("The SEC asks every request to include who is asking. "
              "This is saved in settings.json and only sent to sec.gov.")
        name = input("Your name: ").strip()
        email = input("Your email: ").strip()
        settings.update({"name": name, "email": email})
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
    return settings


class SecClient:
    def __init__(self, name, email):
        self.user_agent = f"{name} {email}"
        self._last = 0.0
        self._lock = threading.Lock()

    def _throttle(self):
        with self._lock:
            wait = MIN_SECONDS_BETWEEN_REQUESTS - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()

    def get(self, url, as_json=True, raw=False):
        for attempt in range(5):
            self._throttle()
            req = urllib.request.Request(url, headers={
                "User-Agent": self.user_agent,
                "Accept-Encoding": "gzip",
            })
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    body = resp.read()
                    if resp.headers.get("Content-Encoding") == "gzip":
                        body = gzip.decompress(body)
                if raw:
                    return body
                if as_json:
                    return json.loads(body)
                return body.decode("utf-8", errors="replace")
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
                if e.code in (429, 500, 502, 503, 504) and attempt < 4:
                    time.sleep(2 ** attempt)
                    continue
                if e.code == 403:
                    raise SystemExit(
                        "sec.gov refused the request (403). Check that settings.json "
                        "has a real name and email, then try again in a few minutes."
                    )
                raise
            except urllib.error.URLError:
                if attempt < 4:
                    time.sleep(2 ** attempt)
                    continue
                raise


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

# Standard metrics picked from the raw XBRL facts. For each metric the first
# concept in the list that the company reported for a period wins, because
# companies switch tags over the years (e.g. SalesRevenueNet -> Revenue...).
FLOW_METRICS = {
    "revenue": ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
                "SalesRevenueNet", "RevenueFromContractWithCustomerIncludingAssessedTax",
                "SalesRevenueGoodsNet", "RevenuesNetOfInterestExpense"],
    "cost_of_revenue": ["CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold"],
    "gross_profit": ["GrossProfit"],
    "research_development": ["ResearchAndDevelopmentExpense",
                             "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost"],
    "sga": ["SellingGeneralAndAdministrativeExpense"],
    # companies that report the two halves separately (Alphabet, Amazon, Microsoft); added together when read back
    "selling_marketing": ["SellingAndMarketingExpense", "MarketingExpense"],
    "general_admin": ["GeneralAndAdministrativeExpense"],
    "operating_income": ["OperatingIncomeLoss", "rendered:OperatingIncome"],
    # for companies with no operating income line: operating income is worked out from these (see _desk_row)
    "operating_expenses": ["OperatingExpenses"],
    "costs_and_expenses": ["CostsAndExpenses", "rendered:CostsAndExpenses"],
    "sales": ["rendered:Sales"],
    "interest_expense": ["InterestExpense", "InterestExpenseNonoperating", "InterestExpenseDebt", "InterestExpenseOperating"],
    "nonoperating_income": ["rendered:NonoperatingTotal", "NonoperatingIncomeExpense"],   # the statement's own run first   # everything between operating and pre-tax income
    "pretax_income": [
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"],
    "income_tax": ["IncomeTaxExpenseBenefit"],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    # after preferred dividends: what belongs to common shareholders, the basis of EPS and P/E (Boeing, AT&T)
    "net_income_common": ["NetIncomeLossAvailableToCommonStockholdersDiluted", "NetIncomeLossAvailableToCommonStockholdersBasic"],
    "eps_basic": ["EarningsPerShareBasic", "EarningsPerShareBasicAndDiluted"],
    "eps_diluted": ["EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted", "rendered:EPS"],
    "shares_diluted": ["WeightedAverageNumberOfDilutedSharesOutstanding", "rendered:Shares"],
    # Some companies (Alphabet, Microsoft, Tesla) put a line of their own on the cash flow statement and tag
    # only plain depreciation in the standard vocabulary, so that is the last resort: it leaves out the
    # amortization of intangibles, which is usually the smaller part.
    "depreciation_amortization": ["DepreciationDepletionAndAmortization",
                                  "DepreciationAndAmortization",
                                  "DepreciationAmortizationAndAccretionNet",
                                  "DepreciationAmortizationAndOther",
                                  "OtherDepreciationAndAmortization",
                                  "Depreciation"],
    "stock_compensation": ["ShareBasedCompensation"],
    "operating_cash_flow": ["NetCashProvidedByUsedInOperatingActivities",
                            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets",
              "PaymentsToAcquireOtherPropertyPlantAndEquipment", "PaymentsToAcquireOilAndGasPropertyAndEquipment",
              "PaymentsToAcquireOtherProductiveAssets", "rendered:Capex"],
    "dividends_paid": ["PaymentsOfDividends", "PaymentsOfDividendsCommonStock"],
    "buybacks": ["PaymentsForRepurchaseOfCommonStock"],
    "eps_basic_shares": ["WeightedAverageNumberOfSharesOutstandingBasic"],
    "investing_cash_flow": ["NetCashProvidedByUsedInInvestingActivities",
                            "NetCashProvidedByUsedInInvestingActivitiesContinuingOperations"],
    "financing_cash_flow": ["NetCashProvidedByUsedInFinancingActivities",
                            "NetCashProvidedByUsedInFinancingActivitiesContinuingOperations"],
    "acquisitions": ["PaymentsToAcquireBusinessesNetOfCashAcquired"],
    "debt_issued": ["ProceedsFromIssuanceOfLongTermDebt", "ProceedsFromIssuanceOfDebt",
                    "ProceedsFromIssuanceOfSeniorLongTermDebt"],
    "debt_repaid": ["RepaymentsOfLongTermDebt", "RepaymentsOfDebt", "RepaymentsOfSeniorDebt"],
}
METRICS_VERSION = "22"   # raise when the lists of metrics change, so stored companies are rebuilt
SEGMENTS_VERSION = "2"   # raise when the segment parser reads more, so each company's reports are read again when opened
# Per-share and share-count figures can't be added up across quarters.
NON_ADDITIVE = {"eps_basic", "eps_diluted", "shares_diluted", "eps_basic_shares"}
SHARE_COUNTS = {"shares_diluted", "eps_basic_shares", "shares_outstanding"}
PER_SHARE = {"eps_basic", "eps_diluted"}
INSTANT_METRICS = {
    "cash": ["CashAndCashEquivalentsAtCarryingValue",
             "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"],
    "short_term_investments": ["ShortTermInvestments", "MarketableSecuritiesCurrent",
                               "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
                               "DebtSecuritiesAvailableForSaleExcludingAccruedInterestCurrent",
                               "AvailableForSaleSecuritiesCurrent", "OtherShortTermInvestments"],
    "other_current_assets": ["PrepaidExpenseAndOtherAssetsCurrent", "OtherAssetsCurrent"],
    "other_receivables": ["NontradeReceivablesCurrent", "OtherReceivablesNetCurrent", "OtherReceivables"],
    "deferred_tax_assets": ["DeferredIncomeTaxAssetsNet", "DeferredTaxAssetsNetNoncurrent"],
    "other_noncurrent_assets": ["OtherAssetsNoncurrent"],
    "other_noncurrent_liabilities": ["OtherLiabilitiesNoncurrent"],
    "receivables": ["AccountsReceivableNetCurrent", "ReceivablesNetCurrent", "AccountsNotesAndLoansReceivableNetCurrent",
                    "AccountsAndOtherReceivablesNetCurrent", "AccountsReceivableNet"],
    "inventory": ["InventoryNet"],
    "current_assets": ["AssetsCurrent"],
    "total_assets": ["Assets"],
    "current_liabilities": ["LiabilitiesCurrent"],
    "short_term_debt": ["LongTermDebtCurrent", "DebtCurrent", "ShortTermBorrowings", "CommercialPaper"],
    "long_term_debt": ["LongTermDebtNoncurrent", "LongTermDebt", "LongTermDebtAndCapitalLeaseObligations"],
    # loans to customers (Deere's and Caterpillar's finance arms): when large, interest is a cost of that business
    "finance_receivables": ["rendered:FinanceReceivables"],
    "finance_leases": ["FinanceLeaseLiability"],
    "finance_leases_current": ["FinanceLeaseLiabilityCurrent"],
    "finance_leases_noncurrent": ["FinanceLeaseLiabilityNoncurrent"],
    "preferred_equity": ["PreferredStockValue", "PreferredStockValueOutstanding", "PreferredStockIncludingAdditionalPaidInCapital"],
    "trust_shares": ["CommonStockSharesHeldInEmployeeTrustShares"],
    # short_term_debt, long_term_debt, total_debt and short_term_investments are then worked out again from every
    # debt and investment line the company reports (see debt_from_tags and the presented balance sheet)
    "total_debt": [],
    "total_liabilities": ["Liabilities"],
    "equity": ["StockholdersEquity",
               "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "shares_outstanding": ["CommonStockSharesOutstanding"],
    "ppe": ["PropertyPlantAndEquipmentNet",
            "PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetAfterAccumulatedDepreciationAndAmortization"],
    "goodwill": ["Goodwill"],
    "intangibles": ["IntangibleAssetsNetExcludingGoodwill"],
    "long_term_investments": ["LongTermInvestments", "MarketableSecuritiesNoncurrent",
                              "AvailableForSaleSecuritiesDebtSecuritiesNoncurrent", "EquitySecuritiesFVNINoncurrent",
                              "OtherLongTermInvestments", "EquityMethodInvestments"],
    "accounts_payable": ["AccountsPayableCurrent"],
    "deferred_revenue": ["ContractWithCustomerLiabilityCurrent", "DeferredRevenueCurrent"],
    "retained_earnings": ["RetainedEarningsAccumulatedDeficit"],
    "paid_in_capital": ["CommonStocksIncludingAdditionalPaidInCapital", "AdditionalPaidInCapital",
                        "AdditionalPaidInCapitalCommonStock"],
    "aoci": ["AccumulatedOtherComprehensiveIncomeLossNetOfTax"],
    "treasury_stock": ["TreasuryStockValue", "TreasuryStockCommonValue"],
    "minority_interest": ["MinorityInterest"],
    "accrued_liabilities": ["AccruedLiabilitiesCurrent"],
    "lease_assets": ["OperatingLeaseRightOfUseAsset"],
    "lease_liabilities": ["OperatingLeaseLiabilityNoncurrent"],
    "lease_liabilities_current": ["OperatingLeaseLiabilityCurrent"],
    "taxes_payable": ["AccruedIncomeTaxesCurrent", "TaxesPayableCurrent"],
    "deferred_tax_liabilities": ["DeferredIncomeTaxLiabilitiesNet", "DeferredTaxLiabilitiesNoncurrent"],
    "deferred_revenue_noncurrent": ["ContractWithCustomerLiabilityNoncurrent", "DeferredRevenueNoncurrent"],
}
METRICS = list(FLOW_METRICS) + list(INSTANT_METRICS)

# Companies that report under IFRS (TSMC, Nokia) use the ifrs-full vocabulary instead.
IFRS_FLOW = {
    "revenue": ["Revenue", "RevenueFromContractsWithCustomers"],
    "cost_of_revenue": ["CostOfSales"],
    "gross_profit": ["GrossProfit"],
    "research_development": ["ResearchAndDevelopmentExpense"],
    "sga": ["SellingGeneralAndAdministrativeExpense"],
    "selling_marketing": ["SellingExpense", "DistributionCosts"],
    "general_admin": ["AdministrativeExpense", "GeneralAndAdministrativeExpense"],
    "operating_income": ["ProfitLossFromOperatingActivities"],
    "interest_expense": ["FinanceCosts", "InterestExpense"],
    "pretax_income": ["ProfitLossBeforeTax"],
    "income_tax": ["IncomeTaxExpenseContinuingOperations"],
    "net_income": ["ProfitLossAttributableToOwnersOfParent", "ProfitLoss"],
    "eps_basic": ["BasicEarningsLossPerShare", "BasicEarningsLossPerShareFromContinuingOperations"],
    "eps_diluted": ["DilutedEarningsLossPerShare", "DilutedEarningsLossPerShareFromContinuingOperations", "rendered:EPS"],
    "shares_diluted": ["AdjustedWeightedAverageShares", "rendered:Shares"],
    "eps_basic_shares": ["WeightedAverageShares"],
    "depreciation_amortization": ["DepreciationAndAmortisationExpense",
                                  "AdjustmentsForDepreciationAndAmortisationExpense",
                                  "DepreciationAmortisationAndImpairmentLossReversalOfImpairmentLossRecognisedInProfitOrLoss"],
    "stock_compensation": ["AdjustmentsForSharebasedPayments"],
    "operating_cash_flow": ["CashFlowsFromUsedInOperatingActivities"],
    "capex": ["PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities", "PurchaseOfPropertyPlantAndEquipment",
              "PurchaseOfPropertyPlantAndEquipmentIntangibleAssetsOtherThanGoodwillInvestmentPropertyAndOtherNoncurrentAssets",
              "rendered:Capex"],
    "dividends_paid": ["DividendsPaidClassifiedAsFinancingActivities", "DividendsPaid"],
    "buybacks": ["PaymentsToAcquireOrRedeemEntitysShares"],
    "investing_cash_flow": ["CashFlowsFromUsedInInvestingActivities"],
    "financing_cash_flow": ["CashFlowsFromUsedInFinancingActivities"],
    "acquisitions": ["CashFlowsUsedInObtainingControlOfSubsidiariesOrOtherBusinessesClassifiedAsInvestingActivities"],
    "debt_issued": ["ProceedsFromBorrowingsClassifiedAsFinancingActivities", "ProceedsFromIssueOfBondsNotesAndDebentures"],
    "debt_repaid": ["RepaymentsOfBorrowingsClassifiedAsFinancingActivities", "RepaymentsOfBondsNotesAndDebentures"],
}
IFRS_INSTANT = {
    "cash": ["CashAndCashEquivalents"],
    "receivables": ["TradeAndOtherCurrentReceivables", "CurrentTradeReceivables"],
    "inventory": ["Inventories"],
    "current_assets": ["CurrentAssets"],
    "total_assets": ["Assets"],
    "current_liabilities": ["CurrentLiabilities"],
    "total_liabilities": ["Liabilities"],
    "equity": ["EquityAttributableToOwnersOfParent", "Equity"],
    "ppe": ["PropertyPlantAndEquipment"],
    "goodwill": ["Goodwill"],
    "intangibles": ["IntangibleAssetsOtherThanGoodwill"],
    "long_term_investments": ["NoncurrentFinancialAssets", "InvestmentsAccountedForUsingEquityMethod"],
    "accounts_payable": ["TradeAndOtherCurrentPayablesToTradeSuppliers", "TradeAndOtherCurrentPayables"],
    "retained_earnings": ["RetainedEarnings"],
    "minority_interest": ["NoncontrollingInterests"],
    "lease_assets": ["RightofuseAssets"],
    "lease_liabilities": ["NoncurrentLeaseLiabilities"],
    "lease_liabilities_current": ["CurrentLeaseLiabilities"],
    "deferred_tax_assets": ["DeferredTaxAssets"],
    "deferred_tax_liabilities": ["DeferredTaxLiabilities"],
    "shares_outstanding": ["NumberOfSharesOutstanding"],
    "short_term_debt": [], "long_term_debt": [], "total_debt": [], "short_term_investments": [],
}

# Debt is added up from every tag that carries part of it. Tags in one family are different views of the same
# amount (the largest is taken); the families are added together, so nothing is counted twice.
DEBT_FAMILIES = {
    "us": {
        "borrowings": (["ShortTermBorrowings"], ["CommercialPaper", "OtherShortTermBorrowings", "ShortTermBankLoansAndNotesPayable",
                                                 "LinesOfCreditCurrent", "BankLoans"]),
        "current": (["LongTermDebtCurrent", "LongTermDebtAndCapitalLeaseObligationsCurrent"],
                    ["ConvertibleNotesPayableCurrent", "ConvertibleDebtCurrent", "SeniorNotesCurrent", "NotesPayableCurrent",
                     "LoansPayableCurrent", "SecuredDebtCurrent", "UnsecuredDebtCurrent", "OtherLongTermDebtCurrent"]),
        "noncurrent": (["LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations"],
                       ["ConvertibleDebtNoncurrent", "ConvertibleLongTermNotesPayable", "SeniorLongTermNotes", "SeniorNotesNoncurrent",
                        "LongTermNotesPayable", "UnsecuredLongTermDebt", "SecuredLongTermDebt", "LongTermLineOfCredit",
                        "LongTermLoansPayable", "OtherLongTermDebtNoncurrent", "LongTermFederalHomeLoanBankAdvancesNoncurrent"]),
        # totals of long-term debt with its current part
        "long_all": (["LongTermDebt", "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities"],
                     ["NotesPayable", "LoansPayable", "SecuredDebt", "UnsecuredDebt", "SeniorNotes", "ConvertibleNotesPayable",
                      "OtherLongTermDebt"]),
        "current_all": (["DebtCurrent"], []),
        "everything": (["DebtLongtermAndShorttermCombinedAmount", "DebtAndCapitalLeaseObligations"], []),
    },
    "ifrs": {
        "borrowings": (["ShorttermBorrowings"], []),
        "current": (["CurrentPortionOfLongtermBorrowings"], ["CurrentPortionOfNoncurrentBondsIssued", "CurrentPortionOfNoncurrentLoansReceived",
                                                             "CurrentBankLoansReceived", "CurrentLoansReceived"]),
        "noncurrent": (["LongtermBorrowings", "NoncurrentPortionOfNoncurrentBorrowings"],
                       ["NoncurrentPortionOfNoncurrentBondsIssued", "NoncurrentPortionOfNoncurrentLoansReceived", "NoncurrentLoansReceived"]),
        "long_all": ([], []),
        "current_all": (["CurrentBorrowingsAndCurrentPortionOfNoncurrentBorrowings", "CurrentBorrowings"], []),
        "everything": (["Borrowings"], []),
    },
}
STI_FAMILIES = {
    "us": (["ShortTermInvestments", "MarketableSecuritiesCurrent", "AvailableForSaleSecuritiesCurrent", "OtherShortTermInvestments"],
           [["AvailableForSaleSecuritiesDebtSecuritiesCurrent", "DebtSecuritiesAvailableForSaleExcludingAccruedInterestCurrent",
             "DebtSecuritiesCurrent"], ["HeldToMaturitySecuritiesCurrent"], ["EquitySecuritiesFvNiCurrent"], ["TradingSecuritiesCurrent"]]),
    "ifrs": (["CurrentInvestments", "ShorttermInvestments"],
             [["CurrentFinancialAssetsAtFairValueThroughProfitOrLoss"], ["CurrentFinancialAssetsAtFairValueThroughOtherComprehensiveIncome"],
              ["CurrentFinancialAssetsAtAmortisedCost"], ["ShorttermDepositsNotClassifiedAsCashEquivalents"]]),
}
FAMILY_TAGS = {"FinanceLeaseLiability", "FinanceLeaseLiabilityCurrent", "FinanceLeaseLiabilityNoncurrent"} | \
    {c for fam in DEBT_FAMILIES.values() for whole, parts in fam.values() for c in whole + parts} | \
    {c for whole, groups in STI_FAMILIES.values() for c in whole + [x for g in groups for x in g]}


def debt_from_tags(v, gaap="us"):
    """(short-term, long-term, total) debt from every debt tag reported for one date, or None."""
    fam = DEBT_FAMILIES[gaap]
    def part(name):
        whole, parts = fam[name]
        got = [v[c] for c in whole if v.get(c) is not None] + [sum(v[c] for c in parts if v.get(c) is not None)]
        return max(got) if any(c in v for c in whole + parts) else None
    b, cur, nc, la, ca, ev = (part(k) for k in ("borrowings", "current", "noncurrent", "long_all", "current_all", "everything"))
    if all(x is None for x in (b, cur, nc, la, ca, ev)):
        return None
    z = lambda x: x or 0
    short = max(z(b) + z(cur), z(ca))
    total = max(short + z(nc), z(b) + max(z(la), z(cur) + z(nc)), z(ev))
    return short, max(total - short, 0), total


def sti_from_tags(v, gaap="us"):
    whole, groups = STI_FAMILIES[gaap]
    got = [v[c] for c in whole if v.get(c) is not None]
    parts = [max(v[c] for c in g if v.get(c) is not None) for g in groups if any(v.get(c) is not None for c in g)]
    if parts:
        got.append(sum(parts))
    return max(got) if got else None


# ---- Lines of the presented statements that the tags alone miss ----
_DEBT_LINE = re.compile(r"Debt|Borrowing|NotesPayable|CommercialPaper|LoansPayable|SeniorNotes|ConvertibleNotes|LineOfCredit|"
                        r"Bonds|Debentures|FederalHomeLoanBankAdvances|\b(debt|borrowings?|notes payable|commercial paper|loans payable|"
                        r"senior notes|convertible (senior )?notes|term loans?|credit facilit\w*|line of credit|debentures|bonds)\b", re.I)
_NOT_DEBT = re.compile(r"Interest|Accrued|Securities|Repurchase|Deposit|Trading|FairValue|Receivable|Investment|Unamortized|Discount|"
                       r"IssuanceCost|Guarantee|Derivative|Asset|Beneficial|Collateral|"
                       r"\b(interest|accrued|securities|repurchase|deposits?|trading|receivables?|investments?|guarantee|beneficial)\b", re.I)
_LEASE_ONLY = re.compile(r"Lease|\bleases?\b", re.I)   # a lease line is left out unless it is debt and leases together
_CURRENT_LINE = re.compile(r"Current|ShortTerm|CommercialPaper|\b(current|short-term|commercial paper|due within one year)\b", re.I)
_STI_LINE = re.compile(r"ShortTermInvestments|MarketableSecuritiesCurrent|DebtSecurities\w*Current|AvailableForSale\w*Current|"
                       r"CurrentFinancialAssetsAt\w+|ShorttermDepositsNotClassifiedAsCashEquivalents|CurrentInvestments|"
                       r"HeldToMaturitySecuritiesCurrent|TradingSecuritiesCurrent|EquitySecuritiesFvNiCurrent|TreasuryBills|"
                       r"\b(short-term investments?|marketable securities|treasury bills|time deposits)\b", re.I)
_CAPEX_LINE = re.compile(r"PaymentsToAcquire(PropertyPlantAndEquipment|ProductiveAssets|OtherPropertyPlantAndEquipment|OilAndGasProperty)|"
                         r"CapitalExpenditure|\b(capital expenditures?|additions to (property|plant|utility plant)|purchases? of (property|plant|premises)|"
                         r"independent power and other investments|nuclear fuel purchases|expenditures for equipment leased to others|"
                         r"cost of equipment on operating leases acquired)\b", re.I)
_TOTAL_REVENUE = re.compile(r"^(Revenues|RevenuesNetOfInterestExpense|RegulatedAndUnregulatedOperatingRevenue|Revenue)$")
_ANCHORS = {"bs": {"Assets", "LiabilitiesAndStockholdersEquity"}, "is": {"NetIncomeLoss", "ProfitLoss"},
            "cf": {"NetCashProvidedByUsedInOperatingActivities", "CashFlowsFromUsedInOperatingActivities"}}


def ticker_class(ticker):
    """The share class a ticker trades: BRK-B -> B, GOOGL -> None (its class is not in the symbol)."""
    m = re.search(r"[-./]([A-Z])$", ticker or "")
    return m.group(1) if m else None


def rendered_inputs(db, cik, ticker, have_accessions, gaap="us"):
    """What the presented statements add, as extra facts: {"flows": {concept: {(start, end): row}}, "instants":
    {concept: {end: row}}, "class_ratio": number}. Synthetic concepts are named rendered:...; filings missing from the
    SEC's company facts (NEE's newest 10-Qs) are filled in with their own tags."""
    flows, instants = {}, {}
    lines = db.execute("SELECT * FROM statement_lines WHERE cik=? AND kind IN ('bs','is','cf') ORDER BY filed, accession, kind, seq",
                       (cik,)).fetchall()
    if not lines:
        return {"flows": flows, "instants": instants, "class_ratio": None}
    by = {}
    for r in lines:
        by.setdefault((r["accession"], r["kind"]), []).append(r)
    cls = ticker_class(ticker)
    class_eps = {}   # class letter -> [EPS values], to learn how many of one class equal one of another (BRK: 1,500)

    def add(store, concept, key, value, r, start=None):
        if store is flows and start is None:
            return   # a period figure needs its start (Rigetti's 2020 EPS line has none)
        row = {"concept": concept, "value": value, "accession": r["accession"], "filed": r["filed"], "form": r["form"],
               "period_start": start, "period_end": key if start is None else key[1]}
        cur = store.setdefault(concept, {}).get(key)
        if cur is None or row["filed"] >= cur["filed"]:
            store[concept][key] = row

    for (accn, kind), rows in by.items():
        blocks = {}
        for r in rows:
            blocks.setdefault(r["block"], []).append(r)
        main = next((b for b, rs in blocks.items() if any(x["concept"] in _ANCHORS[kind] for x in rs)), "")
        # Other sections with their own totals are either parts of the whole (Berkshire's insurance and railroad
        # businesses, whose totals add up to the company's) or a second registrant in the same filing (NEE's Florida
        # Power & Light, which is only part of it): the parts count, a second registrant is left out.
        # a share class's section (Visa's "Class A common stock", with the net income allocated to it) is not a company
        anchored = {b for b, rs in blocks.items() if b != main and any(x["concept"] in _ANCHORS[kind] for x in rs)
                    and not re.search(r"\bClass [A-Z]\b|common stock|ordinary shares|preferred stock|Series [A-Z]\b", b, re.I)}
        part_anchor = next((c for c in sorted(_ANCHORS[kind]) if anchored and all(any(x["concept"] == c for x in blocks[b]) for b in anchored)), None)
        def anchor_total(bs_, end):
            return sum(x["value"] for b in bs_ for x in blocks[b] if x["concept"] == part_anchor and x["period_end"] == end and x["value"])
        main_ends = {x["period_end"] for x in blocks.get(main, []) if x["concept"] == part_anchor}
        parts = anchored if part_anchor and main_ends and all(
            abs(anchor_total(anchored, e) - anchor_total([main], e)) <= 0.01 * abs(anchor_total([main], e) or 1) for e in main_ends) else set()
        other_entities = anchored - parts
        in_parts = {x["concept"] for b in parts for x in blocks[b]}
        # the whole's own lines that the parts also show are counted once, in the parts
        mine = [r for r in rows if r["block"] not in other_entities and not (parts and r["block"] == main and r["concept"] in in_parts
                                                                              and r["concept"] not in _ANCHORS[kind])]
        periods = {}
        for r in mine:
            periods.setdefault((r["period_start"], r["period_end"]), []).append(r)
        for (ps, pe), rs in periods.items():
            key = pe if ps is None else (ps, pe)
            store = instants if ps is None else flows
            if kind == "bs":
                # each section's lines after its own total assets are liabilities and equity
                assets_at = {}
                for x in rs:
                    if x["concept"] in ("Assets",):
                        assets_at.setdefault(x["block"], x["seq"])
                after_assets = lambda x: x["seq"] > assets_at.get(x["block"], assets_at.get(main, 10 ** 9))
                cur_at = {}
                for x in rs:
                    if x["concept"] in ("AssetsCurrent", "CurrentAssets"):
                        cur_at.setdefault(x["block"], x["seq"])
                text = lambda x: x["concept"] + " " + x["label"]
                debts = [x for x in rs if after_assets(x) and _DEBT_LINE.search(text(x)) and not _NOT_DEBT.search(text(x))
                         and not (_LEASE_ONLY.search(text(x)) and not re.search(r"Debt|Borrowing|\bdebt\b|borrowings?", text(x), re.I))]
                if rs:   # a balance sheet was read for this date: investments come from its lines only
                    add(instants, "rendered:BalanceSheet", key, 1, rs[0])
                # the consolidated lines, plus business-section lines for kinds of debt the consolidated ones do not show
                # (Caterpillar shows short-term borrowings for the whole and long-term debt only by business line;
                # Berkshire shows all of it by business line)
                whole = {x["concept"] for x in debts if x["block"] == main}
                debts = [x for x in debts if x["block"] == main or x["concept"] not in whole]
                if debts:
                    vals = [x["value"] for x in debts]
                    # a line that is the sum of the others is their total: count the total alone
                    tot = next((x for x in debts if len(debts) > 2 and abs(2 * x["value"] - sum(vals)) <= 0.005 * abs(sum(vals))), None)
                    use = [tot] if tot else debts
                    short = sum(x["value"] for x in use if _CURRENT_LINE.search(x["concept"] + " " + x["label"]))
                    total = sum(x["value"] for x in use)
                    add(instants, "rendered:TotalDebt", key, total, use[0])
                    if any(_LEASE_ONLY.search(text(x)) for x in use):
                        add(instants, "rendered:DebtHasLeases", key, 1, use[0])
                    add(instants, "rendered:ShortDebt", key, short, use[0])
                fin = [x for x in rs if x["value"] and
                       re.search(r"FinancingReceivable|NotesAndLoansReceivable|\bfinanc\w* receivables?\b|receivables?\s*[–-]\s*financ", x["concept"] + " " + x["label"], re.I)
                       and not re.search(r"Allowance|allowance", x["concept"] + " " + x["label"])]
                if fin:
                    add(instants, "rendered:FinanceReceivables", key, sum(x["value"] for x in fin), fin[0])
                stis = [x for x in rs if _STI_LINE.search(x["concept"] + " " + x["label"]) and not re.search(r"Noncurrent|LongTerm|long-term|non-current", x["concept"] + " " + x["label"], re.I)
                        and not re.match(r"total\b", x["label"], re.I) and not re.search(r"^Cash|CashAndCashEquivalents|cash", x["concept"] + " " + x["label"])
                        and not after_assets(x) and x["seq"] < cur_at.get(x["block"], cur_at.get(main, 10 ** 9))]
                # equity securities count only above a current-assets subtotal (NVIDIA's marketable equity securities);
                # on an unclassified balance sheet (Berkshire's) they are long-term holdings
                stis += [x for x in rs if re.search(r"EquitySecuritiesFvNi$|\bmarketable equity securities\b", x["concept"] + " " + x["label"], re.I)
                         and x["block"] in cur_at and x["seq"] < cur_at[x["block"]] and x not in stis]
                whole = {x["concept"] for x in stis if x["block"] == main}
                stis = [x for x in stis if x["block"] == main or x["concept"] not in whole]
                if stis:
                    add(instants, "rendered:ShortTermInvestments", key, sum(x["value"] for x in stis), stis[0])
            elif kind == "cf":
                # in the investing section only: Intel also lists equipment paid through vendor financing as "Additions to
                # property, plant and equipment" under financing activities, which is not capital spending in the year's terms
                inv_end = min((x["seq"] for x in rs if re.match(r"NetCashProvidedByUsedInInvestingActivities", x["concept"])), default=None)
                caps = [x for x in rs if _CAPEX_LINE.search(x["concept"] + " " + x["label"]) and x["value"] is not None
                        and not re.search(r"Proceeds|sale|disposal|incurred|accrued|unpaid|Financing", x["concept"] + " " + x["label"], re.I)
                        and (inv_end is None or x["seq"] < inv_end)]
                if caps:
                    add(flows, "rendered:Capex", key, abs(sum(x["value"] for x in caps)), caps[0], ps)
            else:
                totals = [x for x in rs if x["block"] == main and (_TOTAL_REVENUE.match(x["concept"]) or
                          re.match(r"total (net )?(revenues?|sales|revenues and other income|operating revenues?)$", x["label"], re.I))]
                if totals:
                    add(flows, "rendered:Revenues", key, max(x["value"] for x in totals), totals[0], ps)
                is_sales = lambda t: re.match(r"(net )?sales( and other operating revenues?)?$|sales and other operating revenue", t, re.I)
                # a sales line, or a revenue line inside a section named for sales (ExxonMobil's "Sales and other operating revenue")
                sales = [x for x in rs if (x["block"] == main and is_sales(x["label"]))
                         or (is_sales(re.sub(r"\s*\[Member\]$", "", x["block"])) and re.match(r"Revenues?$|RevenueFromContract", x["concept"]))]
                if sales:
                    add(flows, "rendered:Sales", key, sales[0]["value"], sales[0], ps)
                    # income from equity affiliates shown as revenue: it is a share of others' profit, not sales
                    if any(re.match(r"Revenues?$", x["concept"]) and re.search(r"equity (affiliates|method)|equity in (net )?(earnings|income)", x["block"], re.I) for x in rs):
                        add(flows, "rendered:OperatingRevenue", key, sales[0]["value"], sales[0], ps)
                costs = [x for x in rs if x["block"] == main and (x["concept"] == "CostsAndExpenses" or re.match(r"total costs and (other deductions|expenses)", x["label"], re.I))]
                if costs:
                    add(flows, "rendered:CostsAndExpenses", key, costs[0]["value"], costs[0], ps)
                ops = [x for x in rs if x["block"] == main and x["prefix"] not in ("us-gaap", "ifrs-full")
                       and re.match(r"(total )?(operating income|income from operations|operating profit)( \(loss\))?$", x["label"], re.I)]
                if ops:
                    add(flows, "rendered:OperatingIncome", key, ops[0]["value"], ops[0], ps)
                # no operating income line at all (Disney, Pfizer): a tagged "operating income" then comes from a note
                # (Disney's is its segments' total, before corporate costs and amortization) and is not the company's
                main_rows = sorted((x for x in rs if x["block"] == main), key=lambda x: x["seq"])
                if ps is not None and not ops and not any(x["concept"] == "OperatingIncomeLoss" or re.match(
                        r"(total )?(operating income|income from operations|operating profit|operating loss)", x["label"], re.I) for x in main_rows):
                    add(flows, "rendered:NoOperatingLine", key, 1, main_rows[0] if main_rows else rs[0], ps)
                    # the run of non-operating lines just above pretax income is everything between operating and pretax
                    # income: Pfizer's one "other (income)/deductions" line; Disney's other income, interest and investees.
                    # Signs come from the company's own facts (an interest expense tag is reported positive)
                    pre = next((i for i, x in enumerate(main_rows) if x["concept"] in FLOW_METRICS["pretax_income"]), None)
                    run, i = [], (pre or 0) - 1
                    while pre and i >= 0 and re.search(r"Nonoperating|InterestIncomeExpense|^InterestExpense|InvestmentIncome|"
                                                       r"IncomeLossFromEquityMethodInvestments|GainLossOnInvestments|ForeignCurrencyTransactionGainLoss",
                                                       main_rows[i]["concept"]):
                        run.append(main_rows[i])
                        i -= 1
                    total = 0.0
                    for x in run:
                        # the statement page can date a period a day off its facts (Disney: Sept 28 / Sept 29)
                        f = db.execute("SELECT value FROM facts WHERE cik=? AND concept=? AND period_end=? AND period_start "
                                       "BETWEEN date(?, '-7 day') AND date(?, '+7 day') ORDER BY filed DESC LIMIT 1",
                                       (cik, x["concept"], x["period_end"], x["period_start"], x["period_start"])).fetchone()
                        if not f or f[0] is None:
                            total = None
                            break
                        total += -f[0] if x["concept"].startswith("InterestExpense") else f[0]
                    if run and total is not None:
                        add(flows, "rendered:NonoperatingTotal", key, total, run[-1], ps)
                # earnings per share and the share count of the ticker's own class (Berkshire's Class B)
                eps = [x for x in rs if x["unit"].endswith("/shares") and re.search(r"EarningsPerShare|per (average )?(equivalent )?share", x["concept"] + " " + x["label"], re.I)]
                shs = [x for x in rs if x["unit"] == "shares" and re.search(r"WeightedAverage|average", x["concept"] + " " + x["label"], re.I)]
                for x in eps:
                    m = re.search(r"Class ([A-Z])\b", x["block"])
                    if m:
                        class_eps.setdefault(m.group(1), []).append(x["value"])
                pick = lambda xs: ([x for x in xs if cls and re.search(rf"Class {cls}\b", x["block"])] or
                                   [x for x in xs if not re.search(r"Class [A-Z]\b", x["block"])] or xs[:1])
                e = pick(sorted(eps, key=lambda x: "Diluted" not in x["concept"] and "dilution" not in x["label"].lower()))
                if e:
                    add(flows, "rendered:EPS", key, e[0]["value"], e[0], ps)
                sh = pick(sorted(shs, key=lambda x: "Diluted" not in x["concept"] and "dilut" not in x["label"].lower()))
                if sh:
                    add(flows, "rendered:Shares", key, sh[0]["value"], sh[0], ps)
            # a filing the SEC's company facts leave out: its lines under their own tags, where a tag is used once
            if accn not in have_accessions:
                seen = {}
                for x in rs:
                    if x["block"] == main and x["prefix"] in ("us-gaap", "ifrs-full"):
                        seen.setdefault(x["concept"], []).append(x)
                for concept, xs in seen.items():
                    if len(xs) == 1:
                        add(store, concept, key, xs[0]["value"], xs[0], ps)
    ratio = None
    if cls and cls in class_eps and len(class_eps) > 1:
        mine_v = statistics.median(class_eps[cls])
        other = [statistics.median(v) for k, v in class_eps.items() if k != cls]
        if mine_v and other and other[0]:
            ratio = max(other[0] / mine_v, mine_v / other[0])
    return {"flows": flows, "instants": instants, "class_ratio": ratio}


def split_events(db, cik):
    """Stock splits, seen where a later filing restates the same period's share count by a whole ratio:
    [(the first filing on the new basis, ratio)]. NVIDIA: 4 for 1 in 2021, 10 for 1 in 2024."""
    obs = {}
    for r in db.execute("SELECT concept, period_start, period_end, value, filed FROM facts WHERE cik=? AND concept IN "
                        "('WeightedAverageNumberOfDilutedSharesOutstanding','WeightedAverageNumberOfSharesOutstandingBasic',"
                        "'AdjustedWeightedAverageShares','WeightedAverageShares') AND value > 0 ORDER BY filed", (cik,)):
        obs.setdefault((r["concept"], r["period_start"], r["period_end"]), []).append((r["filed"], r["value"]))
    seen = []
    for vals in obs.values():
        for (f1, v1), (f2, v2) in zip(vals, vals[1:]):
            if f2 <= f1:
                continue
            r = v2 / v1
            for k in (2, 3, 4, 5, 6, 8, 10, 15, 20, 25, 30, 40, 50, 100, 1.5, 2.5):
                for ratio in (k, 1 / k):
                    if abs(r / ratio - 1) < 0.015:
                        seen.append((f1, f2, ratio))
    events = []
    for f1, f2, ratio in sorted(seen, key=lambda x: x[1]):
        same = next((e for e in events if abs(e[1] / ratio - 1) < 0.015 and e[2] <= f2 and f1 < e[0]), None)
        if same:   # the same split seen in another period: its new basis starts at the earliest such filing
            same[0] = min(same[0], f2)
            same[2] = max(same[2], f1)
        else:
            events.append([f2, ratio, f1])
    return [(e[0], e[1]) for e in events]
# Registration statements count too: a newly listed company's full-year figures are only in its S-1 until its first 10-K.
ANNUAL_FORMS = ("10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A", "10-KT", "10-KT/A",
                "S-1", "S-1/A", "F-1", "F-1/A", "424B4", "424B1", "10-12B", "10-12B/A", "S-4", "S-4/A")
QUARTER_FORMS = ("10-Q", "10-Q/A")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickers (
    ticker TEXT PRIMARY KEY, cik INTEGER NOT NULL, title TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS companies (
    cik INTEGER PRIMARY KEY,
    ticker TEXT, name TEXT, tickers TEXT, exchanges TEXT,
    sic TEXT, industry TEXT, entity_type TEXT, category TEXT,
    fiscal_year_end TEXT, state_of_incorporation TEXT,
    website TEXT, phone TEXT, business_address TEXT, description TEXT,
    fetched_at TEXT, country TEXT
);

CREATE TABLE IF NOT EXISTS filings (
    cik INTEGER, accession TEXT, form TEXT, filing_date TEXT, report_date TEXT,
    primary_document TEXT, description TEXT, items TEXT, size INTEGER,
    is_xbrl INTEGER, url TEXT,
    PRIMARY KEY (cik, accession)
);
CREATE INDEX IF NOT EXISTS filings_form ON filings (cik, form, filing_date);

-- Every number the company ever reported in XBRL (10-K, 10-Q, 8-K, ...).
CREATE TABLE IF NOT EXISTS facts (
    cik INTEGER, taxonomy TEXT, concept TEXT, unit TEXT,
    period_start TEXT, period_end TEXT, value REAL,
    accession TEXT, fiscal_year INTEGER, fiscal_period TEXT, form TEXT,
    filed TEXT, frame TEXT
);
CREATE INDEX IF NOT EXISTS facts_lookup ON facts (cik, concept, period_end);

CREATE TABLE IF NOT EXISTS concepts (
    taxonomy TEXT, concept TEXT, label TEXT, description TEXT,
    PRIMARY KEY (taxonomy, concept)
);

-- Clean, one-number-per-period metrics picked from facts. Quarterly flows are
-- true 3-month figures: year-to-date 10-Q numbers and the annual total are
-- subtracted to get Q2, Q3 and Q4 when the company didn't report them directly.
CREATE TABLE IF NOT EXISTS metrics (
    cik INTEGER, period_type TEXT,          -- 'annual' or 'quarterly'
    fiscal_year INTEGER, fiscal_quarter INTEGER, period_start TEXT, period_end TEXT,
    metric TEXT, value REAL, concept TEXT, accession TEXT, filed TEXT,
    PRIMARY KEY (cik, period_type, period_end, metric)
);

-- Text of annual reports, split into the sections a researcher reads.
CREATE TABLE IF NOT EXISTS documents (
    cik INTEGER, accession TEXT, form TEXT, filing_date TEXT, report_date TEXT,
    url TEXT, section TEXT, title TEXT, text TEXT,
    PRIMARY KEY (cik, accession, section)
);

-- Insider buying and selling from Form 4 (open-market and other stock transactions).
CREATE TABLE IF NOT EXISTS insider_trades (
    cik INTEGER, accession TEXT, filed TEXT, owner TEXT, role TEXT,
    transaction_date TEXT, code TEXT, type TEXT, shares REAL, price REAL,
    acquired_disposed TEXT, shares_after REAL, security TEXT, url TEXT,
    is_derivative INTEGER, ownership TEXT, expiration TEXT   -- options/RSUs; D(irect) or I(ndirect)
);
CREATE INDEX IF NOT EXISTS insider_lookup ON insider_trades (cik, transaction_date);
CREATE TABLE IF NOT EXISTS insider_filings_read (
    cik INTEGER, accession TEXT, PRIMARY KEY (cik, accession)
);

-- Revenue and operating income by business segment, product line and region, read from
-- the XBRL data attached to each annual report. axis 'total' holds the company-wide figure.
CREATE TABLE IF NOT EXISTS segment_facts (
    cik INTEGER, accession TEXT, filed TEXT, axis TEXT, member TEXT, label TEXT,
    metric TEXT, period_end TEXT, value REAL,
    PRIMARY KEY (cik, accession, axis, member, metric, period_end)
);
-- Institutional holders from the SEC's quarterly Form 13F data sets: one row per
-- investment manager per quarter end, for the companies you have opened.
CREATE TABLE IF NOT EXISTS holders_13f (
    cik INTEGER, period TEXT, manager_cik INTEGER, manager TEXT, shares REAL, value REAL, filed TEXT,
    PRIMARY KEY (cik, period, manager_cik)
);
-- The same figures for periods shorter than a year (a quarter, or the year to date), from 10-Qs.
CREATE TABLE IF NOT EXISTS segment_q_facts (
    cik INTEGER, accession TEXT, filed TEXT, axis TEXT, member TEXT, label TEXT,
    metric TEXT, period_start TEXT, period_end TEXT, value REAL,
    PRIMARY KEY (cik, accession, axis, member, metric, period_start, period_end)
);
-- What a company attached to its results announcements (8-K, item 2.02): press releases,
-- shareholder letters and slide decks, with the heading each one opens with.
CREATE TABLE IF NOT EXISTS ir_exhibits (
    cik INTEGER, accession TEXT, seq INTEGER, type TEXT, doc TEXT, size INTEGER, title TEXT, graphics INTEGER,
    PRIMARY KEY (cik, accession, doc)
);
-- Earnings call transcripts fetched for you (Alpha Vantage allows few requests a day, so each is kept).
CREATE TABLE IF NOT EXISTS transcripts (
    ticker TEXT, fiscal_year INTEGER, quarter INTEGER, source TEXT, fetched_at TEXT, body TEXT,
    PRIMARY KEY (ticker, fiscal_year, quarter)
);
CREATE TABLE IF NOT EXISTS ir_read (
    cik INTEGER, accession TEXT, PRIMARY KEY (cik, accession)
);
CREATE TABLE IF NOT EXISTS debt_split (      -- debt reported only per business line (Caterpillar, Deere)
    cik INTEGER, accession TEXT, filed TEXT, axis TEXT, member TEXT, concept TEXT, period_end TEXT, value REAL,
    PRIMARY KEY (cik, accession, axis, member, concept, period_end)
);
CREATE TABLE IF NOT EXISTS debt_split_read (cik INTEGER, accession TEXT, PRIMARY KEY (cik, accession));
-- The statements as each filing presents them (the SEC's rendered R pages): every line with its tag, label and the
-- section it sits in. They fill what the SEC's company facts lack: lines under a company's own tags, figures split by
-- business line or share class, and filings the company facts leave out. kind: bs, is, cf or cover.
CREATE TABLE IF NOT EXISTS statement_lines (
    cik INTEGER, accession TEXT, form TEXT, filed TEXT, kind TEXT, seq INTEGER, block TEXT,
    prefix TEXT, concept TEXT, label TEXT, unit TEXT, period_start TEXT, period_end TEXT, value REAL,
    PRIMARY KEY (cik, accession, kind, seq, period_start, period_end)
);
CREATE TABLE IF NOT EXISTS statement_read (cik INTEGER, accession TEXT, PRIMARY KEY (cik, accession));
CREATE TABLE IF NOT EXISTS statement_pages (    -- the pages themselves, compressed, so they can be read again
    cik INTEGER, accession TEXT, form TEXT, filed TEXT, kind TEXT, doc TEXT, body BLOB,
    PRIMARY KEY (cik, accession, kind)
);
CREATE TABLE IF NOT EXISTS segment_filings_read (
    cik INTEGER, accession TEXT, PRIMARY KEY (cik, accession)
);
-- Owners of more than 5% of a class of shares, from Schedule 13D and 13G filings:
-- one row per person named on a filing's cover pages.
CREATE TABLE IF NOT EXISTS holders_5pct (
    cik INTEGER, accession TEXT, filer_cik INTEGER, form TEXT, filed TEXT, event_date TEXT,
    class_title TEXT, person TEXT, person_type TEXT, shares REAL, pct REAL, url TEXT,
    issuer_cik INTEGER, issuer_cusip TEXT,      -- the company the filing is about: a company's own list also has
    PRIMARY KEY (cik, accession, person)        -- the 13D/13Gs it filed as an investor in others
);
-- Every manager that filed a 13F for a quarter, so a holder missing from the quarter before is called new only
-- when it filed that quarter (Norges Bank files twice a year).
CREATE TABLE IF NOT EXISTS holders_13f_filers (
    period TEXT, manager_cik INTEGER, PRIMARY KEY (period, manager_cik)
);
CREATE TABLE IF NOT EXISTS holders_5pct_read (
    cik INTEGER, accession TEXT, PRIMARY KEY (cik, accession)
);
CREATE TABLE IF NOT EXISTS macro_cache (        -- the Macro page's data series and lookups, kept a few hours
    key TEXT PRIMARY KEY, fetched_at REAL, data TEXT
);
"""

SECTION_TITLES_10K = {
    "1": "Business", "1A": "Risk Factors", "1B": "Unresolved Staff Comments",
    "1C": "Cybersecurity", "2": "Properties", "3": "Legal Proceedings",
    "5": "Market for Common Equity", "7": "Management's Discussion and Analysis",
    "7A": "Market Risk", "8": "Financial Statements",
}


def connect():
    db = sqlite3.connect(DB_PATH, timeout=60)
    db.row_factory = sqlite3.Row
    # WAL: the page keeps reading while a download writes, instead of waiting on a locked file
    db.execute("PRAGMA journal_mode=WAL")
    cols = [r[1] for r in db.execute("PRAGMA table_info(companies)")]
    if cols and "country" not in cols:
        db.execute("ALTER TABLE companies ADD COLUMN country TEXT")
    cols = [r[1] for r in db.execute("PRAGMA table_info(metrics)")]
    migrate = bool(cols) and "fiscal_quarter" not in cols
    if migrate:  # metrics are derived data: rebuild them in the new layout
        db.execute("DROP TABLE metrics")
    cols = [r[1] for r in db.execute("PRAGMA table_info(insider_trades)")]
    if cols and "is_derivative" not in cols:  # read the Form 4s again to fill the new columns
        db.execute("DROP TABLE insider_trades")
        db.execute("DROP TABLE IF EXISTS insider_filings_read")
    db.executescript(SCHEMA)
    try:
        db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5("
                   "text, cik UNINDEXED, accession UNINDEXED, section UNINDEXED)")
    except sqlite3.OperationalError:
        pass  # FTS5 not compiled in; search falls back to LIKE
    create_views(db)
    row = db.execute("SELECT value FROM meta WHERE key='metrics_version'").fetchone()
    if migrate or not row or row["value"] != METRICS_VERSION:
        with db:
            for r in db.execute("SELECT cik FROM companies").fetchall():
                build_metrics(db, r["cik"])
            db.execute("INSERT OR REPLACE INTO meta VALUES ('metrics_version', ?)", (METRICS_VERSION,))
    if "issuer_cik" not in [r[1] for r in db.execute("PRAGMA table_info(holders_5pct)")]:
        with db:   # older copies stored 13D/13Gs about other companies too: read them all again
            db.execute("ALTER TABLE holders_5pct ADD COLUMN issuer_cik INTEGER")
            db.execute("ALTER TABLE holders_5pct ADD COLUMN issuer_cusip TEXT")
            db.execute("DELETE FROM holders_5pct")
            db.execute("DELETE FROM holders_5pct_read")
            db.execute("DELETE FROM meta WHERE key LIKE '13f:%'")
    row = db.execute("SELECT value FROM meta WHERE key='segments_version'").fetchone()
    if not row or row["value"] != SEGMENTS_VERSION:
        with db:   # forget which reports were read; each company reads its own again the next time it is opened
            db.execute("DELETE FROM segment_filings_read")
            db.execute("INSERT OR REPLACE INTO meta VALUES ('segments_version', ?)", (SEGMENTS_VERSION,))
    return db


def has_fts(db):
    return db.execute("SELECT 1 FROM sqlite_master WHERE name='documents_fts'").fetchone() is not None


def create_views(db):
    names = METRICS
    have = {r[1] for r in db.execute("PRAGMA table_info(annual_financials)")}
    if all(m in have for m in names) and "fiscal_quarter" in have:
        return
    cols = ",\n  ".join(f"MAX(CASE WHEN metric='{m}' THEN value END) AS {m}" for m in names)
    for view, ptype in (("annual_financials", "annual"), ("quarterly_financials", "quarterly")):
        db.execute(f"DROP VIEW IF EXISTS {view}")
        db.execute(f"""
CREATE VIEW {view} AS
SELECT w.*,
  w.operating_cash_flow - w.capex AS free_cash_flow,
  ROUND(100.0 * w.gross_profit / NULLIF(w.revenue, 0), 2) AS gross_margin_pct,
  ROUND(100.0 * w.operating_income / NULLIF(w.revenue, 0), 2) AS operating_margin_pct,
  ROUND(100.0 * w.net_income / NULLIF(w.revenue, 0), 2) AS net_margin_pct,
  ROUND(100.0 * w.net_income / NULLIF(w.equity, 0), 2) AS roe_pct,
  ROUND(100.0 * w.net_income / NULLIF(w.total_assets, 0), 2) AS roa_pct,
  ROUND(1.0 * w.current_assets / NULLIF(w.current_liabilities, 0), 2) AS current_ratio,
  ROUND(1.0 * w.long_term_debt / NULLIF(w.equity, 0), 2) AS debt_to_equity
FROM (
  SELECT m.cik, c.ticker, m.fiscal_year, MAX(m.fiscal_quarter) AS fiscal_quarter,
  MIN(m.period_start) AS period_start, m.period_end,
  {cols}
  FROM metrics m JOIN companies c ON c.cik = m.cik
  WHERE m.period_type = '{ptype}'
  GROUP BY m.cik, m.period_end
) w""")
    db.commit()


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

def refresh_tickers(db, client, force=False):
    row = db.execute("SELECT value FROM meta WHERE key='tickers_at'").fetchone()
    if row and not force and time.time() - float(row["value"]) < 7 * 86400:
        return
    data = client.get(TICKERS_URL)
    rows = [(v["ticker"].upper(), int(v["cik_str"]), v["title"]) for v in data.values()]
    with db:
        db.execute("DELETE FROM tickers")
        db.executemany("INSERT OR REPLACE INTO tickers VALUES (?,?,?)", rows)
        db.execute("INSERT OR REPLACE INTO meta VALUES ('tickers_at', ?)", (str(time.time()),))


def resolve(db, client, ticker_or_cik):
    q = ticker_or_cik.strip().upper()
    if q.isdigit():
        return int(q), None
    # Accept BRK.B / BRK-B / BRK/B style: the SEC writes BRK-B
    spellings = list(dict.fromkeys((q, q.replace(".", "-"), q.replace("-", "."), q.replace("/", "-"), q.replace("/", "."))))
    find = lambda: next((row for c in spellings
                         for row in [db.execute("SELECT cik, ticker FROM tickers WHERE ticker=?", (c,)).fetchone()] if row), None)
    row = find()
    if row is None and client is not None:   # a new listing: fetch the SEC's list again and try every spelling
        refresh_tickers(db, client, force=True)
        row = find()
    if row:
        return row["cik"], row["ticker"]
    raise SystemExit(f"Could not find ticker {ticker_or_cik} in the SEC's company list.")


# A company that reorganises under a new holding company gets a new SEC number, and its history stays filed
# under the old one. Listed here (new number: old number) so the history is read as well. More can be added
# in settings.json as  "predecessors": {"2115436": 34088}.
PREDECESSORS = {2115436: 34088}   # ExxonMobil Holdings Corp <- Exxon Mobil Corp (July 2026)


def predecessor_of(cik):
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            extra = {int(k): int(v) for k, v in (json.load(f).get("predecessors") or {}).items()}
    except (OSError, ValueError):
        extra = {}
    return extra.get(cik) or PREDECESSORS.get(cik)


def fetch_company(db, client, ticker_or_cik, docs=1, insiders=40, log=print, statements=(12, 8)):
    refresh_tickers(db, client)
    cik, ticker = resolve(db, client, ticker_or_cik)
    log(f"Fetching CIK {cik} ({ticker or ticker_or_cik}) from SEC EDGAR...")

    sub = client.get(SUBMISSIONS_URL.format(cik=cik))
    if sub is None:
        raise SystemExit(f"SEC has no submissions for CIK {cik}.")
    filings = parse_filings(cik, sub["filings"]["recent"])
    for page in sub["filings"].get("files", []):
        older = client.get(SUBMISSIONS_PAGE_URL.format(name=page["name"]))
        if older:
            filings += parse_filings(cik, older)

    addr = (sub.get("addresses") or {}).get("business") or {}
    address = ", ".join(x for x in (addr.get("street1"), addr.get("street2"), addr.get("city"),
                                    addr.get("stateOrCountry"), addr.get("zipCode"), addr.get("country")) if x)
    # the SEC names the country for addresses abroad; at home it gives only the state (TX)
    country = addr.get("country") or ("United States" if addr.get("stateOrCountry") else None)
    tickers = sub.get("tickers") or []
    company = dict(
        cik=cik, ticker=ticker or (tickers[0] if tickers else None), name=sub.get("name"),
        tickers=",".join(tickers), exchanges=",".join(x or "" for x in sub.get("exchanges") or []),
        sic=sub.get("sic"), industry=sub.get("sicDescription"), entity_type=sub.get("entityType"),
        category=sub.get("category"), fiscal_year_end=sub.get("fiscalYearEnd"),
        state_of_incorporation=sub.get("stateOfIncorporation"), website=sub.get("website"),
        phone=sub.get("phone"), business_address=address, description=sub.get("description"), country=country,
        fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )

    facts_json = client.get(FACTS_URL.format(cik=cik))
    facts, concepts = parse_facts(cik, facts_json) if facts_json else ([], [])
    if not facts_json:
        log("  No XBRL financial data on file (common for funds and foreign filers).")
    old = predecessor_of(cik)
    if old:
        old_json = client.get(FACTS_URL.format(cik=old))
        if old_json:
            more, more_concepts = parse_facts(cik, old_json)   # stored under the current company
            facts, concepts = more + facts, more_concepts + concepts
            log(f"  Added {len(more)} reported numbers from its predecessor (CIK {old}).")

    with db:
        db.execute("DELETE FROM companies WHERE cik=?", (cik,))
        db.execute(f"INSERT INTO companies ({','.join(company)}) VALUES "
                   f"({','.join('?' * len(company))})", list(company.values()))
        db.execute("DELETE FROM filings WHERE cik=?", (cik,))
        db.executemany("INSERT OR REPLACE INTO filings VALUES (?,?,?,?,?,?,?,?,?,?,?)", filings)
        db.execute("DELETE FROM facts WHERE cik=?", (cik,))
        db.executemany("INSERT INTO facts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", facts)
        db.executemany("INSERT OR REPLACE INTO concepts VALUES (?,?,?,?)", concepts)
        build_metrics(db, cik)
    log(f"  {company['name']}: {len(filings)} filings, {len(facts)} reported numbers.")
    # the statements as presented (newest annual and quarterly reports): debt and investment lines under the company's
    # own tags, share classes, and figures the company facts leave out
    if statements and fetch_statement_lines(db, client, cik, annual=statements[0], quarterly=statements[1], log=log):
        with db:
            build_metrics(db, cik)

    if docs:
        fetch_documents(db, client, cik, docs, log)
    if insiders:
        fetch_insiders(db, client, cik, insiders, log)
    return cik


def parse_filings(cik, block):
    rows = []
    n = len(block.get("accessionNumber", []))

    def col(k, i, d=None):
        arr = block.get(k) or []
        return arr[i] if i < len(arr) else d

    for i in range(n):
        accn = block["accessionNumber"][i]
        doc = col("primaryDocument", i, "")
        url = ARCHIVE_URL.format(cik=cik, accn=accn.replace("-", ""), doc=doc) if doc else \
            f"https://www.sec.gov/Archives/edgar/data/{cik}/{accn.replace('-', '')}/"
        rows.append((cik, accn, col("form", i), col("filingDate", i), col("reportDate", i),
                     doc, col("primaryDocDescription", i), col("items", i), col("size", i),
                     col("isXBRL", i), url))
    return rows


def parse_facts(cik, data):
    facts, concepts = [], []
    for taxonomy, items in (data.get("facts") or {}).items():
        for concept, body in items.items():
            concepts.append((taxonomy, concept, body.get("label"), body.get("description")))
            for unit, values in (body.get("units") or {}).items():
                for v in values:
                    facts.append((cik, taxonomy, concept, unit, v.get("start"), v["end"],
                                  v.get("val"), v.get("accn"), v.get("fy"), v.get("fp"),
                                  v.get("form"), v.get("filed"), v.get("frame")))
    return facts, concepts


def fiscal_year_of(end):
    d = date.fromisoformat(end) if isinstance(end, str) else end
    # 52/53-week years that end in the first days of January belong to the prior year.
    return d.year - 1 if d.month == 1 and d.day <= 7 else d.year


def main_currency(db, cik):
    """The currency a company reports in now: the one most of its latest figures are in (USD, TWD, ...)."""
    row = db.execute(
        "SELECT unit, COUNT(*) n FROM facts WHERE cik=? AND length(unit)=3 AND unit=upper(unit) AND period_end="
        "(SELECT MAX(period_end) FROM facts WHERE cik=? AND length(unit)=3 AND unit=upper(unit)) "
        "GROUP BY unit ORDER BY n DESC LIMIT 1", (cik, cik)).fetchone()
    return row["unit"] if row else None


def uses_ifrs(db, cik):
    """True for a company that reports under IFRS now (TSMC, Nokia), judged by its newest annual report: IREN filed
    IFRS 20-Fs until 2024 and US GAAP 10-Ks since."""
    row = db.execute("SELECT taxonomy, COUNT(*) n FROM facts WHERE cik=? AND taxonomy IN ('us-gaap','ifrs-full') AND accession="
                     "(SELECT accession FROM facts WHERE cik=? AND taxonomy IN ('us-gaap','ifrs-full') AND form IN ('10-K','20-F','40-F') "
                     "ORDER BY filed DESC LIMIT 1) GROUP BY taxonomy ORDER BY n DESC LIMIT 1", (cik, cik)).fetchone()
    return bool(row) and row["taxonomy"] == "ifrs-full"


def cover_shares_dei(db, cik):
    """The newest share count on a filing's cover page (all classes added up), from the company facts."""
    row = db.execute("SELECT period_end, accession FROM facts WHERE cik=? AND concept='EntityCommonStockSharesOutstanding' "
                     "AND value > 0 ORDER BY period_end DESC, filed DESC LIMIT 1", (cik,)).fetchone()
    if not row:
        return None
    return db.execute("SELECT SUM(value) FROM (SELECT DISTINCT value FROM facts WHERE cik=? AND accession=? AND period_end=? "
                      "AND concept='EntityCommonStockSharesOutstanding')", (cik, row["accession"], row["period_end"])).fetchone()[0]


def build_metrics(db, cik):
    """Turn raw facts into one clean number per metric per fiscal year and quarter."""
    db.execute("DELETE FROM metrics WHERE cik=?", (cik,))
    D = date.fromisoformat
    ifrs = uses_ifrs(db, cik)
    gaap, taxonomy = ("ifrs", "ifrs-full") if ifrs else ("us", "us-gaap")
    flow_map, inst_map = (IFRS_FLOW, IFRS_INSTANT) if ifrs else (FLOW_METRICS, INSTANT_METRICS)
    wanted = {c for names in list(flow_map.values()) + list(inst_map.values()) for c in names} | FAMILY_TAGS

    # A company that changed its reporting currency (Yandex in roubles became Nebius in dollars) has both on file.
    # Only the currency it reports in now is used, so years in the old one are left out instead of being mixed in.
    main = main_currency(db, cik)
    # Latest-filed value for each (concept, start, end): later filings carry restatements.
    flows, instants = {}, {}
    for r in db.execute(
            f"SELECT * FROM facts WHERE cik=? AND taxonomy=? AND form IN "
            f"({','.join('?' * len(ANNUAL_FORMS + QUARTER_FORMS))})",
            (cik, taxonomy, *ANNUAL_FORMS, *QUARTER_FORMS)):
        if r["concept"] not in wanted or r["value"] is None:
            continue
        money = re.match(r"[A-Z]{3}(?=$|/)", r["unit"] or "")
        if main and money and money.group(0) != main:
            continue
        if r["period_start"]:
            key = (r["period_start"], r["period_end"])
            store = flows.setdefault(r["concept"], {})
        else:
            key = r["period_end"]
            store = instants.setdefault(r["concept"], {})
        cur = store.get(key)
        if cur is None or (r["filed"] or "") > (cur["filed"] or ""):
            store[key] = r

    # What the presented statements add: lines under the company's own tags, figures by business line or share class,
    # and filings the company facts leave out
    co = db.execute("SELECT ticker, sic, fiscal_year_end FROM companies WHERE cik=?", (cik,)).fetchone()
    have = {r[0] for r in db.execute("SELECT DISTINCT accession FROM facts WHERE cik=? AND taxonomy=?", (cik, taxonomy))}
    rend = rendered_inputs(db, cik, co["ticker"] if co else None, have, gaap)
    for target, extra in ((flows, rend["flows"]), (instants, rend["instants"])):
        for concept, periods in extra.items():
            mine = target.setdefault(concept, {})
            for k, r in periods.items():
                if k not in mine:
                    mine[k] = r

    # Fiscal years come from the 12-month periods reported in annual reports.
    fy_starts = {}
    for concept, periods in flows.items():
        for (s, e), r in periods.items():
            if r["form"] in ANNUAL_FORMS and 350 <= (D(e) - D(s)).days <= 380:
                fy_starts.setdefault(e, {}).setdefault(s, 0)
                fy_starts[e][s] += 1
    years = sorted((D(max(starts, key=starts.get)), D(e)) for e, starts in fy_starts.items())
    if years:  # the year in progress, which has 10-Qs but no 10-K yet
        last_end = years[-1][1]
        years.append((last_end + timedelta(days=1), last_end + timedelta(days=365)))
    else:
        # No full-year report at all (a new registrant, or a new holding company that has only filed 10-Qs):
        # lay the fiscal years out from the year end the company declares, so its quarters can still be placed.
        fye = (co["fiscal_year_end"] or "") if co else ""
        ends = [D(e) for periods in flows.values() for (_s, e) in periods]
        if re.fullmatch(r"\d{4}", fye) and ends:
            def year_end(y):
                m, d = int(fye[:2]), int(fye[2:])
                try:
                    return date(y, m, d)
                except ValueError:
                    return date(y, m, 28)
            for y in range(min(ends).year - 1, max(ends).year + 2):
                years.append((year_end(y - 1) + timedelta(days=1), year_end(y)))

    def locate(s, e):
        """Return (fiscal year index, quarter reached, quarters spanned, starts at FY start)."""
        for i, (fs, fe) in enumerate(years):
            if fs - timedelta(days=6) <= s and e <= fe + timedelta(days=6):
                q = round((e - fs).days / 91.3)
                n = round(((e - s).days + 1) / 91.3)
                if 1 <= q <= 4 and 1 <= n <= 4:
                    return i, q, n, abs((s - fs).days) <= 6
        return None

    annual, quarterly = {}, {}   # (period key, metric) -> (priority, value, row, start)
    q_ends = {}                  # (fy index, q) -> end date string
    cands = {}                   # every concept's value for a period, for the revenue rules below

    def put(store, key, metric, prio, value, row, start=None):
        cands.setdefault((store is annual, key, metric), {})[row["concept"]] = (prio, value, row, start)
        cur = store.get((key, metric))
        if cur is None or prio < cur[0]:
            store[(key, metric)] = (prio, value, row, start)

    flow_lists = dict(flow_map)
    flow_lists["revenue"] = list(flow_map["revenue"]) + (["Revenues"] if "Revenues" not in flow_map["revenue"] and not ifrs else []) + ["rendered:Revenues", "rendered:OperatingRevenue"]
    for metric, names in flow_lists.items():
        for prio, concept in enumerate(names):
            periods = flows.get(concept, {})
            disc, ytd, disc_start = {}, {}, {}   # (fy, q) -> row
            for (s, e), r in periods.items():
                ds, de = D(s), D(e)
                days = (de - ds).days
                if 350 <= days <= 380 and r["form"] in ANNUAL_FORMS:
                    put(annual, e, metric, prio, r["value"], r, s)
                loc = locate(ds, de)
                if not loc:
                    continue
                i, q, n, at_start = loc
                if n == 1:
                    disc[(i, q)] = r
                    disc_start[(i, q)] = ds
                if at_start and n == q:
                    ytd[(i, q)] = r
                q_ends.setdefault((i, q), e)
            for i in {k[0] for k in list(disc) + list(ytd)}:
                def back(q):
                    """The year to date at quarter q from the next quarter's: a company's first 10-Q (SpaceX's is for
                    June) gives the half year and the second quarter, but not the first quarter on its own."""
                    nxt = (i, q + 1)
                    return ytd[nxt]["value"] - disc[nxt]["value"] if nxt in ytd and nxt in disc else None

                def Y(q):
                    if q == 0:
                        return 0.0
                    if (i, q) in ytd:
                        return ytd[(i, q)]["value"]
                    if back(q) is not None:
                        return back(q)
                    prev = Y(q - 1)
                    return prev + disc[(i, q)]["value"] if prev is not None and (i, q) in disc else None
                for q in range(1, 5):
                    if (i, q) in disc:
                        v, src = disc[(i, q)]["value"], disc[(i, q)]
                    elif metric in NON_ADDITIVE:
                        if metric == "shares_diluted" and q == 4 and (i, 4) in ytd:
                            v, src = ytd[(i, 4)]["value"], ytd[(i, 4)]
                        else:
                            continue
                    else:
                        a, b = Y(q), Y(q - 1)
                        if a is None or b is None:
                            continue
                        if (i, q) in ytd:
                            src = ytd[(i, q)]
                        elif back(q) is not None:
                            src = ytd[(i, q + 1)]
                            q_ends.setdefault((i, q), (disc_start[(i, q + 1)] - timedelta(days=1)).isoformat())
                        else:
                            continue
                        v = a - b
                    put(quarterly, (i, q), metric, prio, v, src)

    # Revenue: a tag holding another year's figure (AST SpaceMobile's 2024 = its 2022) is set aside when the other
    # tags disagree, and the all-in total (Revenues, or the statement's total line) wins when it is larger, so a
    # partial tag (Berkshire's sales and service revenue only) is not taken for the whole.
    by_concept_years = {}
    for (is_annual, key, metric), cs in cands.items():
        if is_annual and metric == "revenue":
            for c, (_p, v, _r, _s) in cs.items():
                by_concept_years.setdefault(c, {})[key] = v
    for store in (annual, quarterly):
        for (key, metric), chosen in list(store.items()):
            if metric != "revenue":
                continue
            cs = dict(cands.get((store is annual, key, metric), {}))
            if store is annual:
                for c, (_p, v, _r, _s) in list(cs.items()):
                    copied = v and any(k2 != key and v2 == v for k2, v2 in by_concept_years.get(c, {}).items())
                    others = [x[1] for c2, x in cs.items() if c2 != c and x[1] and abs(x[1] / v - 1) > 0.01] if v else []
                    if copied and others:
                        del cs[c]
            if not cs:
                continue
            best = min(cs.values(), key=lambda x: x[0])
            for c in ("Revenues", "RevenuesNetOfInterestExpense", "rendered:Revenues"):   # SoFi: total net revenue
                if c in cs and cs[c][1] is not None and best[1] is not None and cs[c][1] > best[1] * 1.001:
                    best = cs[c]
            if "rendered:OperatingRevenue" in cs and cs["rendered:OperatingRevenue"][1]:
                best = cs["rendered:OperatingRevenue"]
            store[(key, metric)] = best

    # Capital spending: the cash flow statement's lines, when they add up to more than the tag (Caterpillar also spends on
    # equipment it leases to customers, on a line of its own)
    for store in (annual, quarterly):
        for (key, metric), chosen in list(store.items()):
            if metric == "capex":
                r = cands.get((store is annual, key, metric), {}).get("rendered:Capex")
                if r and r[1] is not None and chosen[1] is not None and abs(r[1]) > abs(chosen[1]) * 1.01:
                    store[(key, metric)] = r

    # Balance-sheet items: the value on each year-end and quarter-end date.
    q_keys = {k for (k, _m) in quarterly}
    annual_ends = {k for (k, _m) in annual}
    q_end_dates = {e: k for k, e in q_ends.items() if k in q_keys}
    for metric, names in inst_map.items():
        for prio, concept in enumerate(names):
            for e, r in instants.get(concept, {}).items():
                if e in annual_ends and r["form"] in ANNUAL_FORMS + QUARTER_FORMS:
                    put(annual, e, metric, prio, r["value"], r)
                if e in q_end_dates:
                    put(quarterly, q_end_dates[e], metric, prio, r["value"], r)

    # Shareholders' equity: when only the total including minority interest is filed (AT&T, AST SpaceMobile), the
    # shareholders' own part is that total less the minority interest
    for store in (annual, quarterly):
        for (key, metric), (p, val, row, st) in list(store.items()):
            if metric == "equity" and val is not None and str(row["concept"]).startswith("StockholdersEquityIncludingPortion"):
                mi = store.get((key, "minority_interest"))
                if mi and mi[1]:
                    store[(key, metric)] = (p, val - mi[1], dict(row, concept="equity less minority interest"), st)

    # Debt and short-term investments: every line that carries part of them, from the statement as presented when it
    # was read, otherwise from all the tags (largest of each family, families added up)
    for e in annual_ends | set(q_end_dates):
        v = {c: instants[c][e]["value"] for c in FAMILY_TAGS if e in instants.get(c, {}) and instants[c][e]["value"] is not None}
        newest = max((instants[c][e] for c in FAMILY_TAGS if e in instants.get(c, {})), key=lambda r: r["filed"] or "", default=None)
        got = {}
        rd, rs = instants.get("rendered:TotalDebt", {}).get(e), instants.get("rendered:ShortDebt", {}).get(e)
        if rd:
            short = rs["value"] if rs else 0
            got.update(total_debt=(rd["value"], rd), short_term_debt=(short, rd), long_term_debt=(rd["value"] - short, rd))
        else:
            t = debt_from_tags(v, gaap)
            if t and newest:
                row = dict(newest, concept="debt from all tags")
                got.update(short_term_debt=(t[0], row), long_term_debt=(t[1], row), total_debt=(t[2], row))
        # finance leases are borrowing in substance: added unless the debt lines already hold them (ExxonMobil's and
        # Delta's do); operating leases are not debt here
        if "total_debt" in got:
            fl = v.get("FinanceLeaseLiability")
            if fl is None and (v.get("FinanceLeaseLiabilityCurrent") is not None or v.get("FinanceLeaseLiabilityNoncurrent") is not None):
                fl = (v.get("FinanceLeaseLiabilityCurrent") or 0) + (v.get("FinanceLeaseLiabilityNoncurrent") or 0)
            already = (e in instants.get("rendered:DebtHasLeases", {}) if rd else
                       any(c in v for c in ("LongTermDebtAndCapitalLeaseObligations", "LongTermDebtAndCapitalLeaseObligationsCurrent",
                                            "DebtAndCapitalLeaseObligations", "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities")))
            if fl and not already:
                val, row = got["total_debt"]
                got["total_debt"] = (val + fl, dict(row, concept=str(row["concept"]) + " + finance leases"))
                cur = v.get("FinanceLeaseLiabilityCurrent") or 0
                got["short_term_debt"] = (got.get("short_term_debt", (0, row))[0] + cur, row)
                got["long_term_debt"] = (got.get("long_term_debt", (0, row))[0] + fl - cur, row)
        ri = instants.get("rendered:ShortTermInvestments", {}).get(e)
        if ri:
            got["short_term_investments"] = (ri["value"], ri)
        elif e in instants.get("rendered:BalanceSheet", {}):
            got["short_term_investments"] = (None, None)   # the balance sheet shows none: a tag from the notes is not counted
        else:
            sti = sti_from_tags(v, gaap)
            if sti is not None and newest:
                got["short_term_investments"] = (sti, dict(newest, concept="investments from all tags"))
        for metric, (val, row) in got.items():
            if val is None:
                annual.pop((e, metric), None)
                if e in q_end_dates:
                    quarterly.pop((q_end_dates[e], metric), None)
                continue
            if e in annual_ends:
                annual[(e, metric)] = (-1, val, row, None)
            if e in q_end_dates:
                quarterly[(q_end_dates[e], metric)] = (-1, val, row, None)

    # Stock splits: figures filed before a split are put on today's basis (NVIDIA's years before 2024 x 10)
    events = split_events(db, cik)
    def factor(filed):
        f = 1.0
        for after, ratio in events:
            if (filed or "") < after:
                f *= ratio
        return f
    for store in (annual, quarterly):
        for key, (p, val, row, st) in list(store.items()):
            if key[1] in SHARE_COUNTS | PER_SHARE and val is not None and events:
                k = factor(row["filed"])
                if k != 1:
                    store[key] = (p, val * k if key[1] in SHARE_COUNTS else val / k, row, st)

    # Share classes worth different amounts (Berkshire: one A = 1,500 B): older figures under the standard tags are
    # per A share; put them on the class the ticker trades
    ratio = rend["class_ratio"]
    if ratio and ratio >= 10:
        ref = {m: [abs(v) for (k, mm), (p, v, r, st) in annual.items() if mm == m and v and str(r["concept"]).startswith("rendered:")]
               for m in ("eps_diluted", "shares_diluted")}
        for store in (annual, quarterly):
            for key, (p, val, row, st) in list(store.items()):
                m = key[1]
                if not val or str(row["concept"]).startswith("rendered:"):
                    continue
                if m in PER_SHARE and ref["eps_diluted"] and abs(val) > statistics.median(ref["eps_diluted"]) * ratio / 20:
                    store[key] = (p, val / ratio, row, st)
                elif m in SHARE_COUNTS and ref["shares_diluted"] and val < statistics.median(ref["shares_diluted"]) / ratio * 20:
                    store[key] = (p, val * ratio, row, st)

    # Share counts filed in the wrong unit (McDonald's "715.4" shares for 715.4 million): checked against the cover page
    refs = [v for (k, m), (p, v, r, st) in annual.items() if m in SHARE_COUNTS and v and v > 0]
    ref = cover_shares_dei(db, cik) or (statistics.median(refs) if refs else None)
    if ref:
        for store in (annual, quarterly):
            for key, (p, val, row, st) in list(store.items()):
                if key[1] in SHARE_COUNTS and val and 0 < val < ref / 500:
                    for k in (1e3, 1e6, 1e9):
                        if 0.2 < val * k / ref < 5:
                            store[key] = (p, val * k, row, st)
                            break

    # an "operating income" tag for a period whose income statement has no such line comes from a note (Disney's
    # segment total): left out, so it is worked out from the statement instead (pretax less non-operating)
    no_line = {e for (_s, e) in rend["flows"].get("rendered:NoOperatingLine", {})}
    for key in [k for k in annual if k[1] == "operating_income" and annual[k][2]["concept"] == "OperatingIncomeLoss" and k[0] in no_line]:
        del annual[key]
    for key in [k for k in quarterly if k[1] == "operating_income" and quarterly[k][2]["concept"] == "OperatingIncomeLoss"
                and q_ends.get(k[0]) in no_line]:
        del quarterly[key]

    rows = []
    for (e, metric), (_p, v, r, s) in annual.items():
        rows.append((cik, "annual", fiscal_year_of(e), None, s, e, metric, v,
                     r["concept"], r["accession"], r["filed"]))
    for ((i, q), metric), (_p, v, r, _s) in quarterly.items():
        e = q_ends.get((i, q))
        if not e:
            continue
        rows.append((cik, "quarterly", fiscal_year_of(years[i][1]), q, None, e, metric, v,
                     r["concept"], r["accession"], r["filed"]))
    db.executemany("INSERT OR REPLACE INTO metrics VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)


# ---- Annual report text ---------------------------------------------------

class _TextExtractor(HTMLParser):
    BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section"}
    SKIP = {"script", "style", "ix:header", "head", "title"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")
        elif tag in ("td", "th"):
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(raw):
    p = _TextExtractor()
    p.feed(raw)
    text = "".join(p.parts).replace("\xa0", " ").replace("\u200b", "")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


ITEM_RE = re.compile(r"(?im)^\s*item\s*(\d{1,2}[a-c]?)\s*[\.:\-–—]?")


def split_10k_sections(text):
    """Find 'Item 1. Business', 'Item 1A. Risk Factors', ... skipping the table of contents
    by picking, for each item, the occurrence that yields the longest section."""
    heads = [(m.start(), m.group(1).upper()) for m in ITEM_RE.finditer(text)]
    best = {}
    for idx, (pos, item) in enumerate(heads):
        end = len(text)
        for npos, nitem in heads[idx + 1:]:
            if nitem != item:
                end = npos
                break
        if item not in best or end - pos > best[item][1] - best[item][0]:
            best[item] = (pos, end)
    return {item: text[s:e].strip() for item, (s, e) in best.items() if e - s > 200}


def fetch_documents(db, client, cik, count, log=print):
    rows = db.execute(
        "SELECT * FROM filings WHERE cik=? AND form IN ('10-K','20-F','40-F') "
        "AND primary_document != '' ORDER BY filing_date DESC LIMIT ?", (cik, count)).fetchall()
    if not rows:
        log("  No annual report documents found.")
        return
    stored = {r[0] for r in db.execute("SELECT DISTINCT accession FROM documents WHERE cik=?", (cik,))}
    fts = has_fts(db)
    for f in rows:
        if f["accession"] in stored:
            continue  # annual reports never change once filed
        log(f"  Reading {f['form']} filed {f['filing_date']}...")
        raw = client.get(f["url"], as_json=False)
        if not raw:
            continue
        text = html_to_text(raw)
        sections = split_10k_sections(text) if f["form"] == "10-K" else {}
        items = [("full", "Full document", text)] + [
            (k, SECTION_TITLES_10K.get(k, f"Item {k}"), v) for k, v in sections.items()
            if k in SECTION_TITLES_10K]
        with db:
            db.execute("DELETE FROM documents WHERE cik=? AND accession=?", (cik, f["accession"]))
            if fts:
                db.execute("DELETE FROM documents_fts WHERE cik=? AND accession=?", (cik, f["accession"]))
            for key, title, body in items:
                db.execute("INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?)",
                           (cik, f["accession"], f["form"], f["filing_date"], f["report_date"],
                            f["url"], key, title, body))
                if fts and key != "full":
                    db.execute("INSERT INTO documents_fts VALUES (?,?,?,?)",
                               (body, cik, f["accession"], key))
            if fts and not sections:
                db.execute("INSERT INTO documents_fts VALUES (?,?,?,?)",
                           (text, cik, f["accession"], "full"))
        found = ", ".join(t for k, t, _ in items if k != "full") or "full text only"
        log(f"    stored sections: {found}")


# ---- Insider trades (Form 4) ---------------------------------------------

TRANSACTION_TYPES = {
    "P": "P-Purchase", "S": "S-Sale", "A": "A-Award", "M": "M-Exempt", "F": "F-InKind",
    "G": "G-Gift", "D": "D-Return", "C": "C-Conversion", "X": "X-InTheMoney",
    "J": "J-Other", "W": "W-Will", "I": "I-Discretionary", "L": "L-Small",
}


def _x(node, path):
    el = node.find(path)
    if el is None:
        return None
    v = el.find("value")
    return ((v if v is not None else el).text or "").strip() or None


def _num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def parse_form4(xml_text):
    root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    owners, roles = [], []
    for o in root.findall("reportingOwner"):
        owners.append(_x(o, "reportingOwnerId/rptOwnerName") or "")
        rel = o.find("reportingOwnerRelationship")
        if rel is not None:
            r = []
            if (_x(rel, "isOfficer") or "").lower() in ("1", "true"):
                r.append(_x(rel, "officerTitle") or "Officer")
            if (_x(rel, "isDirector") or "").lower() in ("1", "true"):
                r.append("Director")
            if (_x(rel, "isTenPercentOwner") or "").lower() in ("1", "true"):
                r.append("10% Owner")
            if (_x(rel, "isOther") or "").lower() in ("1", "true"):
                r.append(_x(rel, "otherText") or "Other")
            roles.append(", ".join(r))
    out = []
    tables = (("nonDerivativeTable/nonDerivativeTransaction", 0), ("derivativeTable/derivativeTransaction", 1))
    for path, derivative in tables:
        for t in root.findall(path):
            code = _x(t, "transactionCoding/transactionCode") or ""
            price = _num(_x(t, "transactionAmounts/transactionPricePerShare"))
            if derivative and price is None:  # exercises carry the strike price instead
                price = _num(_x(t, "conversionOrExercisePrice"))
            out.append(dict(
                owner="; ".join(o for o in owners if o), role="; ".join(r for r in roles if r),
                transaction_date=(_x(t, "transactionDate") or "")[:10], code=code,
                type=TRANSACTION_TYPES.get(code, code), shares=_num(_x(t, "transactionAmounts/transactionShares")),
                price=price,
                acquired_disposed=_x(t, "transactionAmounts/transactionAcquiredDisposedCode"),
                shares_after=_num(_x(t, "postTransactionAmounts/sharesOwnedFollowingTransaction")),
                security=_x(t, "securityTitle"), is_derivative=derivative,
                ownership=_x(t, "ownershipNature/directOrIndirectOwnership"),
                expiration=(_x(t, "expirationDate") or "")[:10] or None,
            ))
    return out


def form4_xml_url(cik, accession, primary_document):
    # The listed document is the SEC's styled view ("xslF345X05/file.xml"); the raw XML
    # is the same file name at the top of the filing folder.
    doc = primary_document.split("/")[-1]
    return ARCHIVE_URL.format(cik=cik, accn=accession.replace("-", ""), doc=doc)


def fetch_insiders(db, client, cik, count=40, log=print, since=None):
    """Read the most recent `count` Form 4 filings, or every one filed on or after `since`."""
    rows = db.execute(
        "SELECT * FROM filings WHERE cik=? AND form IN ('4','4/A') AND primary_document LIKE '%.xml' "
        "AND filing_date >= ? ORDER BY filing_date DESC LIMIT ?",
        (cik, since or "", 3000 if since else count)).fetchall()
    done = {r[0] for r in db.execute("SELECT accession FROM insider_filings_read WHERE cik=?", (cik,))}
    todo = [f for f in rows if f["accession"] not in done]
    if not todo:
        return
    log(f"  Reading {len(todo)} insider filings (Form 4)...")
    n = 0
    for f in todo:
        url = form4_xml_url(cik, f["accession"], f["primary_document"])
        try:
            raw = client.get(url, as_json=False)
            trades = parse_form4(raw) if raw else []
        except ET.ParseError:
            trades = []
        with db:
            db.execute("DELETE FROM insider_trades WHERE cik=? AND accession=?", (cik, f["accession"]))
            for t in trades:
                db.execute("INSERT INTO insider_trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (cik, f["accession"], f["filing_date"], t["owner"], t["role"],
                            t["transaction_date"], t["code"], t["type"], t["shares"], t["price"],
                            t["acquired_disposed"], t["shares_after"], t["security"], f["url"],
                            t["is_derivative"], t["ownership"], t["expiration"]))
            db.execute("INSERT OR IGNORE INTO insider_filings_read VALUES (?,?)", (cik, f["accession"]))
        n += len(trades)
    log(f"    stored {n} insider transactions")


# ---- Segments (XBRL attached to annual reports) --------------------------

# The SEC's company-facts feed has company-wide totals only. The split by segment,
# product and region is in each annual report's own XBRL file, tagged with these axes.
SEGMENT_AXES = {
    "StatementBusinessSegmentsAxis": "segment", "SegmentsAxis": "segment",
    "ProductOrServiceAxis": "product", "ProductsAndServicesAxis": "product",
    "StatementGeographicalAxis": "geo", "GeographicalAreasAxis": "geo",
}
SEGMENT_CONCEPTS = {
    "revenue": FLOW_METRICS["revenue"] + ["Revenue"],
    "operating_income": ["OperatingIncomeLoss", "SegmentReportingInformationOperatingIncomeLoss"],
}
INDEX_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accn}/index.json"


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def member_name(member):
    """Readable fallback when a filing has no label for a member: aapl:IPhoneMember -> I Phone."""
    name = member.split(":")[-1]
    if member.startswith("country:"):
        return name
    name = re.sub(r"(Segment)?Member$", "", name) or name
    name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", name)
    return re.sub(r"\b(And|Of|The|In|For)\b", lambda m: m.group(1).lower(), name).strip()


def parse_segment_periods(xml_bytes):
    """Return [(axis, member, metric, period_start, period_end, value)] for a quarter, half, nine months or a year."""
    root = ET.fromstring(xml_bytes)
    contexts, paired = {}, {}
    for c in root:
        if _local(c.tag) != "context":
            continue
        dims, start, end = {}, None, None
        for el in c.iter():
            name = _local(el.tag)
            if name == "explicitMember":
                dims[(el.get("dimension") or "").split(":")[-1]] = (el.text or "").strip()
            elif name == "typedMember":
                dims["typed"] = ""
            elif name == "startDate":
                start = (el.text or "").strip()
            elif name == "endDate":
                end = (el.text or "").strip()
        if not start or not end:
            continue
        try:
            days = (date.fromisoformat(end) - date.fromisoformat(start)).days
        except ValueError:
            continue
        if not (350 <= days <= 380 or 80 <= days <= 100 or 170 <= days <= 190 or 260 <= days <= 285):
            continue
        # "Operating segments" on the consolidation axis is just a marker next to the real segment.
        if dims.get("ConsolidationItemsAxis", "").endswith(":OperatingSegmentsMember") and len(dims) == 2:
            del dims["ConsolidationItemsAxis"]
        if not dims:
            contexts[c.get("id")] = ("total", "", start, end)
        elif len(dims) == 1:
            (dim, member), = dims.items()
            if dim in SEGMENT_AXES and member:
                contexts[c.get("id")] = (SEGMENT_AXES[dim], member, start, end)
        elif len(dims) == 2:
            # A region tagged with a second axis too: Netflix writes each region's revenue as streaming in that
            # region (product Streaming x region UCAN). Kept as the region, if the other axis is the same member
            # for every region (checked below), so it is the whole company split by region, not a product's part.
            geo = [(d, m) for d, m in dims.items() if SEGMENT_AXES.get(d) == "geo"]
            other = [(d, m) for d, m in dims.items() if SEGMENT_AXES.get(d) in ("product", "segment")]
            if len(geo) == 1 and len(other) == 1 and geo[0][1] and other[0][1]:
                paired[c.get("id")] = (geo[0][1], other[0][1], start, end)
    priority = {c: (m, i) for m, names in SEGMENT_CONCEPTS.items() for i, c in enumerate(names)}
    # regions paired with one other member: usable only when that member is the same for all of a period's regions
    used = {}
    for f in root:
        p = paired.get(f.get("contextRef"))
        if p and _local(f.tag) in priority:
            used.setdefault((p[2], p[3]), set()).add(p[1])
    for cid, (geo, other, start, end) in paired.items():
        if len(used.get((start, end), ())) == 1:
            contexts[cid] = ("geo", geo, start, end)
    best = {}
    for f in root:
        concept = _local(f.tag)
        ctx = contexts.get(f.get("contextRef"))
        if concept not in priority or not ctx:
            continue
        value = _num((f.text or "").strip())
        if value is None:
            continue
        metric, prio = priority[concept]
        key = ctx + (metric,)
        if key not in best or prio < best[key][0]:
            best[key] = (prio, value)
    return [(axis, member, metric, start, end, v) for (axis, member, start, end, metric), (_p, v) in best.items()]


def parse_member_labels(xml_bytes):
    """Read a label linkbase (or a schema with one embedded): {'aapl:IPhoneMember': 'iPhone'}."""
    root = ET.fromstring(xml_bytes)
    href = "{http://www.w3.org/1999/xlink}"
    locs, arcs, labels = {}, {}, {}
    for el in root.iter():
        name = _local(el.tag)
        if name == "loc":
            locs[el.get(href + "label")] = (el.get(href + "href") or "").split("#")[-1]
        elif name == "labelArc":
            arcs.setdefault(el.get(href + "from"), []).append(el.get(href + "to"))
        elif name == "label" and (el.text or "").strip():
            role = (el.get(href + "role") or "").rsplit("/", 1)[-1]
            labels.setdefault(el.get(href + "label"), {})[role] = el.text.strip()
    out = {}
    for loc, element in locs.items():
        if not element.endswith("Member") and not element.startswith("country_"):
            continue
        for to in arcs.get(loc, []):
            roles = labels.get(to, {})
            text = roles.get("terseLabel") or roles.get("label") or roles.get("verboseLabel")
            if text:
                text = re.sub(r"(?i)\s*\[member\]\s*$", "", text)
                out[element.replace("_", ":", 1)] = text.title() if text.isupper() and len(text) > 3 else text
                break
    return out


def fetch_segments(db, client, cik, log=print, quarterly=False, limit=40):
    """Read segment, product and region figures from every annual report not read yet.

    With quarterly=True, read the quarterly reports instead (the newest `limit` of them).
    """
    forms = "('10-Q')" if quarterly else "('10-K','20-F','40-F')"
    rows = db.execute(
        f"SELECT * FROM filings WHERE cik=? AND form IN {forms} AND is_xbrl=1 "
        "ORDER BY filing_date DESC" + (f" LIMIT {int(limit)}" if quarterly else ""), (cik,)).fetchall()
    done = {r[0] for r in db.execute("SELECT accession FROM segment_filings_read WHERE cik=?", (cik,))}
    todo = [f for f in rows if f["accession"] not in done]
    if not todo:
        return
    log(f"  Reading segment data from {len(todo)} {'quarterly' if quarterly else 'annual'} reports...")
    labels = {r["member"]: r["label"] for r in db.execute(
        "SELECT member, label FROM segment_facts WHERE cik=? ORDER BY filed", (cik,))}
    n = 0
    for f in todo:
        accn = f["accession"].replace("-", "")
        facts = []
        try:
            index = client.get(INDEX_URL.format(cik=cik, accn=accn)) or {}
            names = [i["name"] for i in (index.get("directory") or {}).get("item", [])]
            skip = re.compile(r"(_(cal|def|lab|pre)|FilingSummary|defnref|^R\d+)\.xml$", re.I)
            instances = [x for x in names if x.lower().endswith(".xml") and not skip.search(x)]
            instances.sort(key=lambda x: not x.endswith("_htm.xml"))
            if instances:
                raw = client.get(ARCHIVE_URL.format(cik=cik, accn=accn, doc=instances[0]), as_json=False)
                facts = parse_segment_periods(raw.encode("utf-8")) if raw else []
            if any(m and m not in labels for _a, m, *_ in facts):
                lab = [x for x in names if x.lower().endswith("_lab.xml")] or \
                      [x for x in names if x.lower().endswith(".xsd")]
                raw = client.get(ARCHIVE_URL.format(cik=cik, accn=accn, doc=lab[0]), as_json=False) if lab else None
                if raw:
                    for member, label in parse_member_labels(raw.encode("utf-8")).items():
                        labels.setdefault(member, label)
        except ET.ParseError:
            pass
        with db:
            db.execute("DELETE FROM segment_facts WHERE cik=? AND accession=?", (cik, f["accession"]))
            db.execute("DELETE FROM segment_q_facts WHERE cik=? AND accession=?", (cik, f["accession"]))
            for axis, member, metric, start, end, value in facts:
                label = labels.get(member) or member_name(member)
                if (date.fromisoformat(end) - date.fromisoformat(start)).days >= 350:
                    db.execute("INSERT OR REPLACE INTO segment_facts VALUES (?,?,?,?,?,?,?,?,?)",
                               (cik, f["accession"], f["filing_date"], axis, member, label, metric, end, value))
                else:
                    db.execute("INSERT OR REPLACE INTO segment_q_facts VALUES (?,?,?,?,?,?,?,?,?,?)",
                               (cik, f["accession"], f["filing_date"], axis, member, label, metric, start, end, value))
            db.execute("INSERT OR IGNORE INTO segment_filings_read VALUES (?,?)", (cik, f["accession"]))
        n += len(facts)
    log(f"    stored {n} segment figures")


# ---- Debt split by business line ------------------------------------------

# Companies with a finance arm (Caterpillar, Deere) show debt on the balance sheet only per business
# line. The company-facts feed drops figures tagged that way, so they are read from the report itself.
# ---- Statements as presented (the SEC's rendered R pages) --------------------------------------------------------
#
# Every XBRL filing comes with FilingSummary.xml and one small HTML page per statement (R2.htm, R4.htm, ...), which
# show each line as the company presents it: its tag (its own or the standard one), its label, the section it sits
# in (a business line, a share class, a second registrant) and its value for each period, with the scale.

_MONTHS = {m: i + 1 for i, m in enumerate(("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"))}
STATEMENT_NAMES = {   # kind -> what the statement is called
    "bs": re.compile(r"balance sheets?|financial condition|financial position|statements? of condition", re.I),
    "is": re.compile(r"income|operations|earnings|profit or loss", re.I),
    "cf": re.compile(r"cash flows?", re.I),
}


def statement_reports(summary_xml):
    """{kind: R file} for the cover page and the three main statements, from a filing's FilingSummary.xml."""
    root = ET.fromstring(summary_xml)
    reps = [(r.findtext("HtmlFileName") or "", r.findtext("ShortName") or "", r.findtext("MenuCategory") or "")
            for r in root.iter("Report")]
    out = {}
    cover = next((f for f, n, m in reps if f and (m == "Cover" or re.search(r"cover|document and entity", n, re.I))), None)
    if cover:
        out["cover"] = cover
    for kind, name in STATEMENT_NAMES.items():
        cands = [(f, n) for f, n, m in reps if f and m == "Statements" and name.search(n)
                 and not re.search(r"parenthetical|changes in|stockholders|shareholders|equity\b|comprehensive income \(?loss\)?$", n, re.I)
                 and not (kind == "is" and re.search(r"cash flow|balance|financial (condition|position)", n, re.I))]
        if kind == "is":   # a statement of operations rather than one that is only comprehensive income
            cands.sort(key=lambda c: bool(re.search(r"comprehensive", c[1], re.I)))
        if cands:
            out[kind] = cands[0][0]
    return out


def _r_date(text):
    m = re.search(r"([A-Z][a-z]{2})\.? (\d{1,2}), (\d{4})", text)
    return date(int(m.group(3)), _MONTHS[m.group(1)], int(m.group(2))) if m and m.group(1) in _MONTHS else None


def _r_value(text):
    t = re.sub(r"<[^>]+>|&#160;|\s|\$|€|£|¥", "", html.unescape(text))
    t = re.sub(r"\[\d+\]$", "", t)
    neg = t.startswith("(") and t.endswith(")")
    t = t.strip("()").replace(",", "")
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


def parse_rendered(htm):
    """One rendered statement: {"currency", "columns": [(start, end)], "rows": [(seq, block, prefix, concept, label,
    unit, [values])]}. Money and share counts are scaled to units; per-share figures are as shown."""
    heads = re.findall(r"<tr>\s*(<th.*?)</tr>", htm, re.S)
    if not heads:
        return None
    first = re.findall(r"<th(.*?)>(.*?)</th>", heads[0], re.S)
    title = " ".join(re.sub(r"<[^>]+>", " ", html.unescape(first[0][1])).split()) if first else ""
    scale_of = lambda word: {"thousands": 1e3, "millions": 1e6, "billions": 1e9}.get((word or "").lower(), 1)
    # "$ in Millions", "In Millions, except Per Share data"; "shares in Thousands" is about share counts only
    m = re.search(r"(?:[$€£¥]|\b[A-Z]{3}\b|\))\s*in (Thousands|Millions|Billions)", title) or \
        re.search(r"(?<!shares )(?<!Shares )\bIn (Thousands|Millions|Billions)", title)
    money = scale_of(m.group(1)) if m else 1
    m = re.search(r"shares in (Thousands|Millions|Billions)", title, re.I)
    generic = re.search(r"(?<!shares )(?<!Shares )\bIn (Thousands|Millions|Billions)", title)   # one scale for every unit
    shares = scale_of(m.group(1)) if m else (money if generic and not re.search(r"except (share|shares)\b", title, re.I) else 1)
    cur = re.search(r"-\s*([A-Z]{3})\b", title)
    currency = cur.group(1) if cur else "USD"
    # columns: a second header row gives the dates under "12 Months Ended" style groups; a balance sheet has one row
    span = lambda attrs: int(re.search(r'colspan="(\d+)"', attrs).group(1)) if 'colspan="' in attrs else 1
    if len(heads) > 1:
        # "12 Months Ended" groups span sub-columns; each date below spans one or more of them (Caterpillar's span two)
        units = []
        for attrs, text in first[1:]:
            mo = re.search(r"(\d+)\s+Months? Ended", text, re.I)
            units += [int(mo.group(1)) if mo else 0] * span(attrs)
        groups, pos, cells = [], 0, []
        for attrs, t in re.findall(r"<th(.*?)>(.*?)</th>", heads[1], re.S):
            groups.append(units[pos] if pos < len(units) else 0)
            cells.append(t)
            pos += span(attrs)
    else:
        cells = [t for _a, t in first[1:]]
        groups = [0] * len(cells)
    ends = [_r_date(t) for t in cells]
    # a column may name its own currency: TSMC shows each year in NT$ and again in US$ (a convenience translation)
    col_cur = []
    for t in cells:
        m = re.search(r"\b([A-Z]{3})\s*\(", re.sub(r"<[^>]+>", " ", t))
        col_cur.append(m.group(1) if m else None)
    columns = []
    for i, e in enumerate(ends):
        months = groups[i] if i < len(groups) else 0
        if e is None:
            columns.append(None)
        elif months:
            y, mth = e.year, e.month - months
            while mth <= 0:
                mth, y = mth + 12, y - 1
            start = date(y, mth, min(e.day, 28)) + timedelta(days=1)
            if e.day >= 28:   # a month end: the period starts on the first of the next month
                start = date(y, mth, 1) + timedelta(days=32)
                start = start.replace(day=1)
            columns.append((start.isoformat(), e.isoformat()))
        else:
            columns.append((None, e.isoformat()))
    rows, block, seq = [], "", 0
    for cls, body in re.findall(r'<tr class="(r[a-z]*)">(.*?)</tr>', htm, re.S):
        if cls == "rh":
            block = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", body)).split())
            continue
        ref = re.search(r"defref_([A-Za-z0-9-]+)_([A-Za-z0-9_]+)'", body)
        if not ref:
            continue
        label = re.search(r"<a [^>]*>(.*?)</a>", body, re.S)
        label = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", label.group(1))).split()) if label else ""
        cells = [_r_value(x) for x in re.findall(r'<td class="(?:nump|num|text)"[^>]*>(.*?)</td>', body, re.S)]
        concept = ref.group(2)
        per_share = "PerShare" in concept or re.search(r"per (common |average |equivalent )*share", label, re.I)
        is_shares = not per_share and (re.search(r"Shares|NumberOf", concept) and not re.search(r"Value|Amount|Cost|Payment|Proceeds|Repurchase", concept))
        unit = (currency + "/shares") if per_share else "shares" if is_shares else currency
        scale = 1 if per_share else shares if is_shares else money
        seq += 1
        rows.append((seq, block, ref.group(1), concept, label, unit,
                     [None if v is None else v * scale for v in cells]))
    return {"currency": currency, "columns": columns, "rows": rows, "column_currency": col_cur}


def fetch_statement_lines(db, client, cik, annual=12, quarterly=8, log=print):
    """Read the presented statements of the newest annual and quarterly reports not read yet."""
    done = {r[0] for r in db.execute("SELECT accession FROM statement_read WHERE cik=?", (cik,))}
    todo = []
    old = predecessor_of(cik)
    for forms, n in ((("10-K", "20-F", "40-F", "10-KT"), annual), (("10-Q",), quarterly)):
        marks = ",".join("?" * len(forms))
        mine = [dict(f, filer=cik) for f in db.execute(f"SELECT * FROM filings WHERE cik=? AND form IN ({marks}) AND is_xbrl=1 "
                                                       "ORDER BY filing_date DESC LIMIT ?", (cik, *forms, n))]
        if old and len(mine) < n:   # reports filed under the company's previous registration (ExxonMobil before 2026)
            have = {f["accession"] for f in mine}
            mine += [{"accession": r["accession"], "form": r["form"], "filing_date": r["filed"], "filer": old}
                     for r in db.execute(f"SELECT accession, form, MAX(filed) filed FROM facts WHERE cik=? AND form IN ({marks}) "
                                         "GROUP BY accession ORDER BY filed DESC LIMIT ?", (cik, *forms, n))
                     if r["accession"] not in have and r["accession"].startswith(f"{old:010d}")][:n - len(mine)]
        todo += [f for f in mine if f["accession"] not in done]
    for f in todo:
        accn, pages, filer = f["accession"].replace("-", ""), [], f.get("filer", cik)
        try:
            summary = client.get(ARCHIVE_URL.format(cik=filer, accn=accn, doc="FilingSummary.xml"), as_json=False)
            reports = statement_reports(summary.encode("utf-8")) if summary else {}
            for kind, doc in reports.items():
                page = client.get(ARCHIVE_URL.format(cik=filer, accn=accn, doc=doc), as_json=False)
                if page:
                    pages.append((cik, f["accession"], f["form"], f["filing_date"], kind, doc, gzip.compress(page.encode("utf-8"))))
        except (ET.ParseError, ValueError) as e:
            log(f"  could not read the statements of {f['accession']}: {e}")
        with db:
            db.executemany("INSERT OR REPLACE INTO statement_pages VALUES (?,?,?,?,?,?,?)", pages)
            db.execute("INSERT OR IGNORE INTO statement_read VALUES (?,?)", (cik, f["accession"]))
            _lines_from_pages(db, cik, f["accession"])
    return len(todo)


def _lines_from_pages(db, cik, accession=None):
    """(Re)build statement_lines from the stored pages: after a fetch, or when the reading rules change."""
    where, args = ("cik=? AND accession=?", (cik, accession)) if accession else ("cik=?", (cik,))
    db.execute(f"DELETE FROM statement_lines WHERE {where}", args)
    main = None
    for pg in db.execute(f"SELECT * FROM statement_pages WHERE {where}", args).fetchall():
        try:
            got = parse_rendered(gzip.decompress(pg["body"]).decode("utf-8"))
        except (ValueError, OSError):
            got = None
        if not got:
            continue
        if main is None:
            main = main_currency(db, cik)
        curs = got.get("column_currency") or []
        keep = [not (i < len(curs) and curs[i] and main and curs[i] != main) for i in range(len(got["columns"]))]
        lines = [(cik, pg["accession"], pg["form"], pg["filed"], pg["kind"], seq, block, prefix, concept, label,
                  (unit.replace(got["currency"], curs[i]) if i < len(curs) and curs[i] else unit), col[0], col[1], v)
                 for seq, block, prefix, concept, label, unit, values in got["rows"]
                 for i, (col, v) in enumerate(zip(got["columns"], values)) if col and v is not None and keep[i]]
        db.executemany("INSERT OR REPLACE INTO statement_lines VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", lines)


DEBT_PARTS = {   # part of total debt -> the concepts that carry it, best first
    "borrowings": ["ShortTermBorrowings", "CommercialPaper"],
    "current": ["LongTermDebtAndCapitalLeaseObligationsCurrent", "LongTermDebtCurrent"],
    "noncurrent": ["LongTermDebtAndCapitalLeaseObligations", "LongTermDebtNoncurrent"],
}
OWN_LONG_TERM_DEBT = "own:LongTermDebtNoncurrent"
DEBT_CONCEPTS = {c for names in DEBT_PARTS.values() for c in names} | {OWN_LONG_TERM_DEBT}


def parse_debt_split(xml_bytes):
    """Return [(axis, member, concept, date, value)] for debt figures tagged with one business line."""
    root = ET.fromstring(xml_bytes)
    contexts = {}
    for c in root:
        if _local(c.tag) != "context":
            continue
        dims, when = {}, None
        for el in c.iter():
            name = _local(el.tag)
            if name == "explicitMember":
                dims[(el.get("dimension") or "").split(":")[-1]] = (el.text or "").strip()
            elif name == "typedMember":
                dims["typed"] = ""
            elif name == "instant":
                when = (el.text or "").strip()
        if when and len(dims) == 1:
            (dim, member), = dims.items()
            if SEGMENT_AXES.get(dim) in ("segment", "product") and member:
                contexts[c.get("id")] = (SEGMENT_AXES[dim], member, when)
        elif when and not dims:
            contexts[c.get("id")] = ("whole", "", when)
    out = {}
    for f in root:
        concept, ctx = _local(f.tag), contexts.get(f.get("contextRef"))
        if ctx and ctx[0] == "whole":
            # a company's own label for long-term debt on the balance sheet (Deere), which the feed also drops
            own = "fasb.org" not in f.tag and "sec.gov" not in f.tag
            if not (own and re.match(r"LongTerm(Debt|Borrowings)", concept) and not concept.endswith("Current")):
                continue
            concept = OWN_LONG_TERM_DEBT
        if concept in DEBT_CONCEPTS and ctx:
            value = _num((f.text or "").strip())
            if value is not None:
                out[ctx[:2] + (concept, ctx[2])] = value
    return [k + (v,) for k, v in out.items()]


def fetch_debt_split(db, client, cik, quarters=9, years=10):
    """For a company whose quarterly balance sheets carry no debt total, read debt per business line."""
    last = db.execute("SELECT MAX(period_end) FROM metrics WHERE cik=? AND period_type='quarterly' "
                      "AND metric='total_assets'", (cik,)).fetchone()[0]
    if not last or db.execute("SELECT 1 FROM metrics WHERE cik=? AND period_type='quarterly' AND period_end=? "
                              "AND metric='long_term_debt'", (cik, last)).fetchone():
        return
    done = {r[0] for r in db.execute("SELECT accession FROM debt_split_read WHERE cik=?", (cik,))}
    todo = []
    for form, n in (("10-Q", quarters), ("10-K", years)):
        todo += [f for f in db.execute("SELECT * FROM filings WHERE cik=? AND form=? AND is_xbrl=1 "
                                       "ORDER BY filing_date DESC LIMIT ?", (cik, form, n)) if f["accession"] not in done]
    found = bool(db.execute("SELECT 1 FROM debt_split WHERE cik=? LIMIT 1", (cik,)).fetchone())
    for i, f in enumerate(todo):
        if i >= 3 and not found:   # three reports with nothing split this way: the company simply has no such debt
            with db:
                db.executemany("INSERT OR IGNORE INTO debt_split_read VALUES (?,?)", [(cik, x["accession"]) for x in todo[i:]])
            break
        accn, facts = f["accession"].replace("-", ""), []
        try:
            index = client.get(INDEX_URL.format(cik=cik, accn=accn)) or {}
            names = [i["name"] for i in (index.get("directory") or {}).get("item", [])]
            skip = re.compile(r"(_(cal|def|lab|pre)|FilingSummary|defnref|^R\d+)\.xml$", re.I)
            instances = [x for x in names if x.lower().endswith(".xml") and not skip.search(x)]
            instances.sort(key=lambda x: not x.endswith("_htm.xml"))
            if instances:
                raw = client.get(ARCHIVE_URL.format(cik=cik, accn=accn, doc=instances[0]), as_json=False)
                facts = parse_debt_split(raw.encode("utf-8")) if raw else []
        except ET.ParseError:
            pass
        with db:
            for axis, member, concept, when, value in facts:
                db.execute("INSERT OR REPLACE INTO debt_split VALUES (?,?,?,?,?,?,?,?)",
                           (cik, f["accession"], f["filing_date"], axis, member, concept, when, value))
            db.execute("INSERT OR IGNORE INTO debt_split_read VALUES (?,?)", (cik, f["accession"]))
        found = found or bool(facts)


def debt_from_split(db, cik):
    """{date: (short-term debt, long-term debt)} rebuilt from the per-business-line figures."""
    split, newest = {}, {}   # date -> axis -> member -> concept -> value, from the newest filing that covers the date
    for r in db.execute("SELECT * FROM debt_split WHERE cik=? ORDER BY filed, accession", (cik,)):
        if newest.get(r["period_end"]) != r["accession"]:
            newest[r["period_end"]], split[r["period_end"]] = r["accession"], {}
        split[r["period_end"]].setdefault(r["axis"], {}).setdefault(r["member"], {})[r["concept"]] = r["value"]
    if not split:
        return {}
    whole = {}   # the same concepts where the company also gives a company-wide figure
    marks = ",".join("?" * len(DEBT_CONCEPTS))
    for r in db.execute(f"SELECT concept, period_end, value FROM facts WHERE cik=? AND concept IN ({marks}) "
                        "AND period_start IS NULL ORDER BY filed", (cik, *DEBT_CONCEPTS)):
        whole.setdefault(r["period_end"], {})[r["concept"]] = r["value"]
    first = lambda d, names: next((d[c] for c in names if c in d), None)
    out = {}
    for when, axes in split.items():
        members = axes.get("product") or axes.get("segment") or {}
        part = {}
        for key, names in DEBT_PARTS.items():
            found = [v for v in (first(m, names) for m in members.values()) if v is not None]
            # annual reports repeat a business line under a second name (the same figure twice): count it once
            part[key] = max(sum(set(found)), first(whole.get(when, {}), names) or 0)
        part["noncurrent"] = max(part["noncurrent"], axes.get("whole", {}).get("", {}).get(OWN_LONG_TERM_DEBT) or 0)
        if part["noncurrent"] or part["current"]:
            out[when] = (part["borrowings"] + part["current"], part["noncurrent"])
    return out


# ---- Institutional holders (Form 13F) ------------------------------------

# Investment managers report their holdings on Form 13F, filed under the manager, not the
# company. The SEC publishes every manager's holdings as one file per quarter (about 100 MB);
# the two newest are kept in the 13f_data folder and searched for each company you open.
F13_PAGE = "https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets"
F13_DIR = os.path.join(os.path.dirname(DB_PATH), "13f_data")
F13_KEEP = 2
_f13_lock = threading.Lock()


def _f13_date(text):
    try:
        return datetime.strptime(text.strip().title(), "%d-%b-%Y").date().isoformat()
    except ValueError:
        return None


def ensure_13f_files(db, client, log=print):
    """Download the newest quarterly 13F data sets if they aren't here yet. Returns their paths."""
    with _f13_lock:
        row = db.execute("SELECT value FROM meta WHERE key='13f_list'").fetchone()
        cached = json.loads(row["value"]) if row else None
        if not cached or time.time() - cached["at"] > 86400:
            html = client.get(F13_PAGE, as_json=False) or ""
            urls = re.findall(r'href="([^"]+form13f[^"]*\.zip)"', html, re.I)[:F13_KEEP]
            if urls:
                cached = {"at": time.time(), "urls": ["https://www.sec.gov" + u if u.startswith("/") else u for u in urls]}
                with db:
                    db.execute("INSERT OR REPLACE INTO meta VALUES ('13f_list', ?)", (json.dumps(cached),))
        if not cached:
            return []
        os.makedirs(F13_DIR, exist_ok=True)
        paths = []
        for url in cached["urls"]:
            path = os.path.join(F13_DIR, url.rsplit("/", 1)[-1])
            if not os.path.exists(path):
                log(f"  Downloading 13F holdings data set {os.path.basename(path)} (about 100 MB)...")
                body = client.get(url, raw=True)
                if not body:
                    continue
                with open(path + ".part", "wb") as f:
                    f.write(body)
                os.replace(path + ".part", path)
            paths.append(path)
        for name in os.listdir(F13_DIR):  # older quarters are no longer needed
            full = os.path.join(F13_DIR, name)
            if name.endswith(".zip") and full not in paths:
                os.remove(full)
        return paths


def company_cusips(db, client, cik):
    """CUSIP numbers of the company's shares, from the Schedule 13G/13D filings about it (read first)."""
    return [r[0] for r in db.execute(
        "SELECT issuer_cusip FROM holders_5pct WHERE cik=? AND issuer_cik=? AND issuer_cusip IS NOT NULL "
        "GROUP BY issuer_cusip ORDER BY MAX(filed) DESC", (cik, cik)) if re.fullmatch(r"[0-9A-Z]{9}", r[0] or "")]


def _issuer_key(name):
    name = re.sub(r"/[A-Z]{2,3}/?$|[.,'&/\\-]", " ", (name or "").upper())
    words = [w for w in name.split() if w not in (
        "INC", "CORP", "CORPORATION", "CO", "COMPANY", "LTD", "LIMITED", "PLC", "NEW", "THE", "COM", "HOLDINGS", "HLDGS")]
    return " ".join(words)


def f13_complete_filers(path):
    """{quarter end: managers whose 13F for it is complete in this data set}. A filing whose table has far fewer rows
    than its own cover page counts (Norges Bank's March 2026 report has 1 of 1,507) says nothing about what it held."""
    with zipfile.ZipFile(path) as z:
        name = lambda suffix: next(n for n in z.namelist() if n.upper().endswith(suffix))

        def table(suffix):
            with z.open(name(suffix)) as f:
                head = f.readline().decode("utf-8", "replace").rstrip("\r\n").split("\t")
                for line in f:
                    yield dict(zip(head, line.decode("utf-8", "replace").rstrip("\r\n").split("\t")))
        subs = {r["ACCESSION_NUMBER"]: r for r in table("SUBMISSION.TSV") if r["SUBMISSIONTYPE"].startswith("13F-HR")}
        want = {r["ACCESSION_NUMBER"]: _num(r.get("TABLEENTRYTOTAL") or "") for r in table("SUMMARYPAGE.TSV")}
        have = {}
        with z.open(name("INFOTABLE.TSV")) as f:
            f.readline()
            for line in f:
                a = line[:line.find(b"\t")].decode()
                have[a] = have.get(a, 0) + 1
    out = {}
    for a, r in subs.items():
        if have.get(a, 0) >= 0.9 * (want.get(a) or 0):
            out.setdefault(_f13_date(r["PERIODOFREPORT"]), set()).add(int(r["CIK"]))
    return out


def scan_13f(path, cusips, company_name):
    """Find every manager holding the company in one quarterly data set.

    Returns (the quarter end most filings in it report, [(quarter end, manager cik, filed,
    kind of filing, manager name, shares, value)], {quarter end: every manager cik that filed for it}).
    A data set also carries late filings and amendments for earlier quarters, so every quarter in it is returned.

    Values are in dollars; a filer still reporting thousands (as before 2023) is put right by its price. Holdings of
    another share class are counted in the main class by price: one Berkshire A share as about 1,500 B shares.
    """
    with zipfile.ZipFile(path) as z:
        def member(suffix):
            return next(n for n in z.namelist() if n.upper().endswith(suffix))

        def table(suffix):
            with z.open(member(suffix)) as f:
                head = f.readline().decode("utf-8", "replace").rstrip("\r\n").split("\t")
                for line in f:
                    yield dict(zip(head, line.decode("utf-8", "replace").rstrip("\r\n").split("\t")))

        subs = {r["ACCESSION_NUMBER"]: r for r in table("SUBMISSION.TSV") if r["SUBMISSIONTYPE"].startswith("13F-HR")}
        counts = {}
        for r in subs.values():
            counts[r["PERIODOFREPORT"]] = counts.get(r["PERIODOFREPORT"], 0) + 1
        if not counts:
            return None, [], {}
        period = max(counts, key=counts.get)
        covers = {r["ACCESSION_NUMBER"]: r for r in table("COVERPAGE.TSV") if r["ACCESSION_NUMBER"] in subs}
        # every restatement in the set, holding the company or not: it replaces the manager's report for that quarter
        # (Sixth Street's August 2026 report listed 4,441 holdings by mistake; the restatement the same day has 9)
        restated = {}
        for a, r in subs.items():
            if covers.get(a, {}).get("AMENDMENTTYPE", "").upper().startswith("RESTATE"):
                key = (_f13_date(r["PERIODOFREPORT"]), int(r["CIK"]))
                restated[key] = max(restated.get(key, ("", "")), (_f13_date(r["FILING_DATE"]) or "", a))

        def rows(match):
            with z.open(member("INFOTABLE.TSV")) as f:
                head = f.readline().decode().rstrip("\r\n").split("\t")
                col = {h: i for i, h in enumerate(head)}
                for line in f:
                    if not match(line):
                        continue
                    v = line.decode("utf-8", "replace").rstrip("\r\n").split("\t")
                    if len(v) < len(head) or v[col["PUTCALL"]].strip() or v[col["SSHPRNAMTTYPE"]].strip().upper() != "SH":
                        continue   # options and bond principal amounts aren't share holdings
                    yield (v[0], v[col["CUSIP"]].upper(), v[col["NAMEOFISSUER"]],
                           _num(v[col["SSHPRNAMT"]]) or 0.0, _num(v[col["VALUE"]]) or 0.0)

        def by_name():   # the CUSIP holders put the most money in under the company's name
            key, by_value = _issuer_key(company_name), {}
            first = key.split()[0].encode() if key else b""
            if first:
                for _a, cusip, issuer, _s, value in rows(lambda line: first in line.upper()):
                    if _issuer_key(issuer) == key:
                        by_value[cusip] = by_value.get(cusip, 0.0) + value
            return max(by_value, key=by_value.get) if by_value else None

        if cusips:   # a 13G's CUSIP can be a class funds hardly hold (TSMC's ordinary shares; they hold the ADR)
            prefixes = {b"\t" + c[:6].encode() for c in cusips}
            if len({a for a, *_r in rows(lambda line: any(p in line for p in prefixes))}) < 50:
                alt = by_name()
                if alt and alt not in cusips:
                    cusips = list(cusips) + [alt]
        else:   # no 13G on file: find the shares by the company's name
            alt = by_name()
            cusips = [alt] if alt else []
        if not cusips:
            return _f13_date(period), [], restated
        # The first six characters identify the issuer; the rest tell its share classes apart.
        prefixes = {b"\t" + c[:6].encode() for c in cusips}
        held, by_cusip, prices = {}, {}, {}
        for accn, cusip, _i, shares, value in rows(lambda line: any(p in line for p in prefixes)):
            if accn in subs and cusip[:6] in {c[:6] for c in cusips}:
                held.setdefault(accn, {}).setdefault(cusip, [0.0, 0.0])
                held[accn][cusip][0] += shares
                held[accn][cusip][1] += value
                if shares > 0 and value > 0:
                    prices.setdefault(cusip, []).append(value / shares)
        # each class's price: the middle of what its holders report (a filer in thousands is far below it)
        price = {c: sorted(v)[len(v) // 2] for c, v in prices.items()}
        for per in held.values():
            for c, v in per.items():
                # reported in thousands of dollars: times 1,000 it lands on the price everyone else reports. Anything
                # else far off is another mistake (Vinva filed Berkshire B shares under the A CUSIP), left as it is
                if c in price and v[0] > 0 and abs(v[1] * 1000 / v[0] / price[c] - 1) < 0.15:
                    v[1] *= 1000
                by_cusip[c] = by_cusip.get(c, 0.0) + v[1]
        top = max(by_cusip.values(), default=0.0)
        keep = {c for c, v in by_cusip.items() if c in cusips or v >= 0.05 * top}   # skip stray preferreds and units
        # the main class is the one the 13D/13Gs name first (the newest); others are converted to it by price
        held_sh = {}
        for per in held.values():
            for c, v in per.items():
                held_sh[c] = held_sh.get(c, 0.0) + v[0]
        main = max(keep & set(price), key=lambda c: held_sh.get(c, 0.0), default=None)
        weight = {c: (price[c] / price[main] if main and c in price and price[main] else 1.0) for c in keep}

        out = []
        for accn, per_cusip in held.items():
            sub, cover = subs[accn], covers.get(accn, {})
            # another class counted in the main one by what it is worth, which stays right when a filer puts one
            # class's shares under the other's CUSIP
            shares = sum(v[0] if c == main or not main else (v[1] / price[main] if v[1] > 0 and price.get(main) else v[0] * weight[c])
                         for c, v in per_cusip.items() if c in keep)
            value = sum(v[1] for c, v in per_cusip.items() if c in keep)
            if shares <= 0:
                continue
            kind = "restate" if cover.get("AMENDMENTTYPE", "").upper().startswith("RESTATE") else \
                   "add" if cover.get("ISAMENDMENT", "").upper() == "Y" else "original"
            out.append((_f13_date(sub["PERIODOFREPORT"]), int(sub["CIK"]), _f13_date(sub["FILING_DATE"]) or "",
                        kind, cover.get("FILINGMANAGER_NAME") or sub["CIK"], shares, value, accn))
        return _f13_date(period), out, restated


def fetch_holders(db, client, cik, log=print):
    """Store who holds the company, from the two newest 13F data sets."""
    paths = ensure_13f_files(db, client, log)
    if not paths:
        return
    stamp = "v5:" + ",".join(os.path.basename(p) for p in paths)   # v5: units, share classes by value, restatements
    row = db.execute("SELECT value FROM meta WHERE key=?", (f"13f:{cik}",)).fetchone()
    if row and row["value"] == stamp:
        return  # already searched these quarters
    company = db.execute("SELECT name FROM companies WHERE cik=?", (cik,)).fetchone()
    cusips = company_cusips(db, client, cik)
    log(f"  Searching {len(paths)} quarters of 13F holdings...")
    periods, filings = [], {}
    restated = {}
    for path in paths:
        mark = "f13filers:" + os.path.basename(path)
        if not db.execute("SELECT 1 FROM meta WHERE key=?", (mark,)).fetchone():
            with db:   # once per data set: which managers filed a complete report for each quarter
                for per, ciks in f13_complete_filers(path).items():
                    db.executemany("INSERT OR IGNORE INTO holders_13f_filers VALUES (?, ?)", [(per, m) for m in ciks])
                db.execute("INSERT OR REPLACE INTO meta VALUES (?, '1')", (mark,))
        period, rows, rs = scan_13f(path, cusips, company["name"] if company else "")
        if period:
            periods.append(period)
        for k, v in rs.items():
            restated[k] = max(restated.get(k, ("", "")), v)
        for row in rows:
            filings.setdefault((row[0], row[1]), []).append(row[2:])
    found = []
    for period in periods:
        managers = {}
        for (per, mcik), forms in filings.items():
            if per != period:
                continue
            # A restatement replaces the original report; other amendments add holdings to it,
            # often ones first filed confidentially.
            forms.sort()
            last = restated.get((per, mcik))
            if last:   # a restatement replaces everything filed before it, even when it does not hold the company
                forms = [f for f in forms if f[5] == last[1] or f[0] > last[0]]
            restates = [f for f in forms if f[1] == "restate"]
            use = ([restates[-1]] if restates else [f for f in forms if f[1] == "original"]) + \
                  [f for f in forms if f[1] == "add"]
            if use:
                managers[mcik] = [use[-1][2], sum(f[3] for f in use), sum(f[4] for f in use), use[-1][0]]
        found.append((period, managers))
    with db:
        db.execute("DELETE FROM holders_13f WHERE cik=?", (cik,))
        for period, managers in found:
            db.executemany("INSERT OR REPLACE INTO holders_13f VALUES (?,?,?,?,?,?,?)",
                           [(cik, period, m, v[0], v[1], v[2], v[3]) for m, v in managers.items()])
        db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (f"13f:{cik}", stamp))
    log(f"    stored {sum(len(m) for _p, m in found)} holder positions")


def parse_schedule_13(xml_text):
    """Read a structured Schedule 13D or 13G: who reports, how many shares, what percent."""
    def tag(block, *names):
        for name in names:
            m = re.search(rf"<(?:\w+:)?{name}(?:\s[^>]*)?>([^<]*)<", block)
            if m and m.group(1).strip():
                return html.unescape(m.group(1)).strip()
        return None

    def day(text):
        m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", text or "")
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}" if m else None

    filer = tag(xml_text, "cik")
    issuer = tag(xml_text, "issuerCik", "issuerCIK")
    out = {"filer_cik": int(filer) if filer and filer.isdigit() else None,
           "issuer_cik": int(issuer) if issuer and issuer.isdigit() else None,
           "issuer_cusip": (tag(xml_text, "issuerCusip", "issuerCUSIP", "issuerCusipNumber") or "").upper() or None,
           "event_date": day(tag(xml_text, "eventDateRequiresFilingThisStatement", "dateOfEvent")),
           "class_title": tag(xml_text, "securitiesClassTitle"), "persons": []}
    for _t, block in re.findall(
            r"<(coverPageHeaderReportingPersonDetails|reportingPersonInfo)>(.*?)</\1>", xml_text, re.S):
        name = tag(block, "reportingPersonName")
        shares = _num(tag(block, "reportingPersonBeneficiallyOwnedAggregateNumberOfShares", "aggregateAmountOwned"))
        if name and shares is not None:
            out["persons"].append((name, tag(block, "typeOfReportingPerson"), shares,
                                   _num(tag(block, "classPercent", "percentOfClass"))))
    return out


def fetch_major_holders(db, client, cik, log=print, limit=80):
    """Store the Schedule 13D/13G filings about the company (the structured ones, filed since late 2024)."""
    read = {r[0] for r in db.execute("SELECT accession FROM holders_5pct_read WHERE cik=?", (cik,))}
    todo = [f for f in db.execute(
        "SELECT * FROM filings WHERE cik=? AND (form LIKE 'SCHEDULE 13G%' OR form LIKE 'SCHEDULE 13D%') "
        "AND primary_document LIKE '%.xml' ORDER BY filing_date DESC LIMIT ?", (cik, limit)).fetchall()
        if f["accession"] not in read]
    if todo:
        log(f"  Reading {len(todo)} Schedule 13D/13G filings...")
    for f in todo:
        url = ARCHIVE_URL.format(cik=cik, accn=f["accession"].replace("-", ""),
                                 doc=f["primary_document"].split("/")[-1])
        try:
            d = parse_schedule_13(client.get(url, as_json=False) or "")
        except Exception:
            continue  # try again next time
        page = ARCHIVE_URL.format(cik=cik, accn=f["accession"].replace("-", ""), doc=f["primary_document"])
        with db:
            # the company's own 13D/13Gs as an investor in others are in its filing list too: only the ones about it
            about = d["issuer_cik"] is None or d["issuer_cik"] == cik
            for name, kind, shares, pct in (d["persons"] if about else []):
                db.execute("INSERT OR REPLACE INTO holders_5pct VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (cik, f["accession"], d["filer_cik"], f["form"], f["filing_date"], d["event_date"],
                            d["class_title"], name, kind, shares, pct, page, d["issuer_cik"], d["issuer_cusip"]))
            db.execute("INSERT OR REPLACE INTO holders_5pct_read VALUES (?, ?)", (cik, f["accession"]))


# --------------------------------------------------------------------------
# Reading back
# --------------------------------------------------------------------------

def company_payload(db, cik, filings_limit=50):
    c = db.execute("SELECT * FROM companies WHERE cik=?", (cik,)).fetchone()
    if not c:
        return None
    annual = [dict(r) for r in db.execute(
        "SELECT * FROM annual_financials WHERE cik=? ORDER BY period_end", (cik,))]
    quarterly = [dict(r) for r in db.execute(
        "SELECT * FROM quarterly_financials WHERE cik=? ORDER BY period_end", (cik,))]
    filings = [dict(r) for r in db.execute(
        "SELECT form, filing_date, report_date, description, items, url FROM filings "
        "WHERE cik=? ORDER BY filing_date DESC LIMIT ?", (cik, filings_limit))]
    docs = [dict(r) for r in db.execute(
        "SELECT accession, form, filing_date, section, title, url, length(text) AS chars "
        "FROM documents WHERE cik=? ORDER BY filing_date DESC, section", (cik,))]
    insiders = [dict(r) for r in db.execute(
        "SELECT transaction_date, owner, role, type, shares, price, acquired_disposed, shares_after, url "
        "FROM insider_trades WHERE cik=? ORDER BY transaction_date DESC LIMIT 100", (cik,))]
    return {"company": dict(c), "annual": annual, "quarterly": quarterly,
            "recent_filings": filings, "documents": docs, "insider_trades": insiders}


def lookup_cik(db, ticker_or_cik):
    q = ticker_or_cik.strip().upper()
    if q.isdigit():
        return int(q)
    for cand in dict.fromkeys((q, q.replace(".", "-"), q.replace("-", "."))):
        known = db.execute("SELECT cik FROM tickers WHERE ticker=?", (cand,)).fetchone()
        if known:  # the SEC's ticker list is authoritative
            row = db.execute("SELECT cik FROM companies WHERE cik=?", (known["cik"],)).fetchone()
            if row:
                return row["cik"]
            continue
        row = db.execute("SELECT cik FROM companies WHERE upper(ticker)=? OR ','||tickers||',' LIKE ? "
                       "ORDER BY upper(ticker)=? DESC", (cand, f"%,{cand},%", cand)).fetchone()
        if row:
            return row["cik"]
    return None


def fmt_num(v, metric):
    if v is None:
        return "-"
    if metric.startswith("eps") or metric.endswith("_pct") or metric in ("current_ratio", "debt_to_equity"):
        return f"{v:,.2f}"
    if metric.startswith("shares"):
        return f"{v / 1e6:,.0f}M"
    return f"{v / 1e6:,.0f}"


def cmd_show(db, args):
    cik = lookup_cik(db, args.ticker)
    if cik is None:
        sys.exit(f"{args.ticker} is not in the database yet. Run: python sec_db.py fetch {args.ticker}")
    p = company_payload(db, cik)
    c = p["company"]
    print(f"{c['name']} ({c['ticker']})  CIK {c['cik']}  {c['industry'] or ''}")
    print(f"Fiscal year ends {c['fiscal_year_end']}, data fetched {c['fetched_at']}")
    rows = p["annual"][-args.years:]
    if not rows:
        print("No annual financial data.")
        return
    show = ["revenue", "gross_profit", "operating_income", "net_income", "eps_diluted",
            "operating_cash_flow", "capex", "free_cash_flow", "cash", "long_term_debt", "equity",
            "shares_diluted", "gross_margin_pct", "operating_margin_pct", "net_margin_pct",
            "roe_pct", "current_ratio", "debt_to_equity"]
    print("\nAnnual (USD millions unless noted)")
    print(f"{'':24}" + "".join(f"{('FY' + str(r['fiscal_year'])):>12}" for r in rows))
    for m in show:
        print(f"{m:24}" + "".join(f"{fmt_num(r.get(m), m):>12}" for r in rows))


def cmd_list(db, args):
    rows = db.execute("SELECT c.ticker, c.name, c.industry, c.fetched_at, "
                        "(SELECT COUNT(*) FROM filings f WHERE f.cik=c.cik) AS n "
                        "FROM companies c ORDER BY c.ticker").fetchall()
    if not rows:
        print("No companies saved yet. Add one with: python3 sec_db.py fetch AAPL")
    for r in rows:
        print(f"{r['ticker'] or '':8} {(r['name'] or '')[:40]:40} {r['n']:>6} filings  updated {r['fetched_at'] or '-'}")


def _fts_phrases(query):
    """Each word as a quoted phrase, so R&D, AT&T, 10-K and supply-chain are searched as written rather than
    read as search syntax. Words must all appear, as in a plain search."""
    return " ".join('"' + w.replace('"', '""') + '"' for w in query.split())


def search_docs(db, query, ticker=None, limit=20):
    query = (query or "").strip()
    if not query:
        return []
    cik = lookup_cik(db, ticker) if ticker else None
    if ticker and cik is None:
        raise LookupError(f"{ticker} is not in the database yet. Run: python sec_db.py fetch {ticker}")
    rows = None
    if has_fts(db):
        sql = ("SELECT f.cik, c.ticker, f.accession, f.section, "
               "snippet(documents_fts, 0, '[', ']', ' ... ', 24) AS snippet "
               "FROM documents_fts f JOIN companies c ON c.cik=f.cik WHERE documents_fts MATCH ?"
               + (" AND f.cik=?" if cik else "") + " ORDER BY rank LIMIT ?")
        # the text as typed first, so OR, NEAR and "quoted phrases" still work; if it is not valid search
        # syntax, the same words quoted; failing that, the plain search below
        for q in dict.fromkeys((query, _fts_phrases(query))):
            try:
                rows = db.execute(sql, [q] + ([cik] if cik else []) + [limit]).fetchall()
                break
            except sqlite3.OperationalError:
                continue
    if rows is None:
        sql = ("SELECT d.cik, c.ticker, d.accession, d.section, substr(d.text, "
               "max(1, instr(lower(d.text), lower(?)) - 120), 300) AS snippet "
               "FROM documents d JOIN companies c ON c.cik=d.cik "
               "WHERE (d.section != 'full' OR NOT EXISTS (SELECT 1 FROM documents s WHERE s.cik=d.cik AND "
               "s.accession=d.accession AND s.section != 'full')) "
               "AND lower(d.text) LIKE lower(?)" + (" AND d.cik=?" if cik else "") + " LIMIT ?")
        rows = db.execute(sql, [query, f"%{query}%"] + ([cik] if cik else []) + [limit]).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["section_title"] = SECTION_TITLES_10K.get(d["section"], d["section"])
        d["snippet"] = " ".join((d["snippet"] or "").split())
        out.append(d)
    return out


def cmd_search(db, args):
    try:
        found = search_docs(db, args.query, args.ticker)
    except LookupError as e:
        sys.exit(str(e))
    if not found:
        print("No matches.")
    for r in found:
        print(f"{r['ticker']} {r['accession']} {r['section_title']}:\n  {r['snippet']}\n")


def cmd_export(db, args):
    cik = lookup_cik(db, args.ticker)
    if cik is None:
        sys.exit(f"{args.ticker} is not in the database yet. Run: python sec_db.py fetch {args.ticker}")
    p = company_payload(db, cik)
    if args.out:
        path = args.out
    else:
        os.makedirs(os.path.join(HERE, "exports"), exist_ok=True)
        path = os.path.join(HERE, "exports", f"{p['company']['ticker'] or cik}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(p, f, indent=1)
    print(f"Wrote {path}")


# --------------------------------------------------------------------------
# Diligence desk feed: the same shapes the page gets from paid APIs
# (amounts in USD millions, like the page's other sources)
# --------------------------------------------------------------------------

def _period_rows(db, cik, ptype):
    out = {}
    for r in db.execute("SELECT * FROM metrics WHERE cik=? AND period_type=? ORDER BY period_end",
                        (cik, ptype)):
        p = out.setdefault(r["period_end"], {"y": r["fiscal_year"], "q": r["fiscal_quarter"], "m": {}, "start": None})
        p["m"][r["metric"]] = (r["value"], r["concept"])
        p["start"] = p["start"] or r["period_start"]
    for end, (st, lt) in debt_from_split(db, cik).items():
        m = out.get(end, {}).get("m")
        if m is None:
            continue
        if m.get("total_debt") and str(m["total_debt"][1]).startswith("rendered:"):
            continue   # the balance sheet as presented already has every borrowing line
        have = m["total_debt"][0] if m.get("total_debt") and m["total_debt"][0] is not None else \
            sum((m[k][0] or 0) for k in ("short_term_debt", "long_term_debt") if k in m)
        st = max(st, m["short_term_debt"][0] or 0) if "short_term_debt" in m else st
        if st + lt > have:   # the company-wide tags miss part of the debt (or all of it)
            m["short_term_debt"], m["long_term_debt"] = (st, "by business line"), (lt, "by business line")
            m["total_debt"] = (st + lt, "by business line")
    return out


def is_financial(sic):
    """Banks, brokers and insurers (SIC 6000-6499): operating income means nothing for them, so none is worked out."""
    try:
        return 6000 <= int(sic or 0) < 6500
    except ValueError:
        return False


def _desk_row(m, financial=False):
    g = lambda k: m[k][0] if k in m and m[k][0] is not None else None
    debt = g("total_debt")
    if debt is None:
        lt, st = m.get("long_term_debt"), m.get("short_term_debt")
        if lt or st:
            debt = (lt[0] if lt else 0) or 0
            if st and not (lt and lt[1] == "LongTermDebt"):  # LongTermDebt already includes the current part
                debt += st[0] or 0
    cogs = g("cost_of_revenue")
    if cogs is None and g("revenue") is not None and g("gross_profit") is not None:
        cogs = g("revenue") - g("gross_profit")
    capex = g("capex")
    ebit = g("operating_income")
    if ebit is None and not financial:
        # No operating income line. Worked out only from operating figures: never from pre-tax income plus interest,
        # which would count investment and other non-operating income as operating.
        gp = g("gross_profit")
        if gp is None and g("revenue") is not None and cogs is not None:
            gp = g("revenue") - cogs
        sga = g("sga")
        if sga is None and (g("selling_marketing") is not None or g("general_admin") is not None):
            sga = (g("selling_marketing") or 0) + (g("general_admin") or 0)
        if g("pretax_income") is not None and g("nonoperating_income") is not None:   # Eli Lilly
            ebit = g("pretax_income") - g("nonoperating_income")
        elif gp is not None and g("operating_expenses") is not None:
            ebit = gp - g("operating_expenses")
        elif gp is not None and sga is not None:                                       # Nike
            ebit = gp - sga - (g("research_development") or 0)
        elif g("sales") is not None and g("costs_and_expenses") is not None and g("revenue") and g("sales") >= 0.9 * g("revenue"):
            # ExxonMobil: its revenue includes income from affiliates and other income, so start from sales
            ebit = g("sales") - (g("costs_and_expenses") - (g("interest_expense") or 0))
        elif g("revenue") is not None and g("costs_and_expenses") is not None:
            # revenue less costs, before interest (Realty Income); a lender's interest (Deere's finance arm, with loans to
            # customers over a fifth of its assets) is a cost of its business and stays in
            lender = (g("finance_receivables") or 0) > 0.2 * (g("total_assets") or float("inf"))
            ebit = g("revenue") - g("costs_and_expenses") + (0 if lender else (g("interest_expense") or 0))
    shares = g("shares_diluted") if g("shares_diluted") is not None else g("eps_basic_shares")
    eps = g("eps_diluted") if g("eps_diluted") is not None else g("eps_basic")
    v = {"revenue": g("revenue"), "cogs": cogs, "ebit": ebit,
         "interest": g("interest_expense"), "netIncome": g("net_income"),
         "da": g("depreciation_amortization"), "shares": shares,
         "assets": g("total_assets"), "curAssets": g("current_assets"), "cash": g("cash"),
         "sti": g("short_term_investments"),
         "curLiab": g("current_liabilities"), "debt": debt, "equity": g("equity"),
         "cfo": g("operating_cash_flow"), "capex": abs(capex) if capex is not None else None}
    v["netIncomeCommon"] = g("net_income_common")
    v["minority"] = g("minority_interest")
    v["preferred"] = g("preferred_equity")
    out = {k: round(x / 1e6, 3) for k, x in v.items() if x is not None}
    if eps is not None:   # as reported (diluted), so preferred dividends and share classes are right; not in millions
        out["eps"] = round(eps, 4)
    return out


# ---- Figures in US dollars for companies that report in another currency (TSMC in TWD, Nokia in EUR) ----
FX_SERIES = {   # currency -> (FRED series, quoted as units per dollar)
    "TWD": ("DEXTAUS", True), "EUR": ("DEXUSEU", False), "GBP": ("DEXUSUK", False), "JPY": ("DEXJPUS", True),
    "CAD": ("DEXCAUS", True), "CNY": ("DEXCHUS", True), "KRW": ("DEXKOUS", True), "CHF": ("DEXSZUS", True),
    "INR": ("DEXINUS", True), "BRL": ("DEXBZUS", True), "MXN": ("DEXMXUS", True), "HKD": ("DEXHKUS", True),
    "SEK": ("DEXSDUS", True), "DKK": ("DEXDNUS", True), "NOK": ("DEXNOUS", True), "AUD": ("DEXUSAL", False),
    "SGD": ("DEXSIUS", True), "ZAR": ("DEXSFUS", True), "NZD": ("DEXUSNZ", False),
}
_FX = {}
FLOW_KEYS = {"revenue", "cogs", "ebit", "interest", "netIncome", "netIncomeCommon", "da", "cfo", "capex"}
INSTANT_KEYS = {"assets", "curAssets", "cash", "sti", "curLiab", "debt", "equity", "minority", "preferred"}


def fx_to_usd(db, currency):
    """{"YYYY-MM": US dollars per unit}, monthly averages from FRED, or None."""
    if currency in (None, "USD") or currency not in FX_SERIES:
        return None
    hit = _FX.get(currency)
    if hit and time.time() - hit[0] < 6 * 3600:
        return hit[1]
    sid, inverted = FX_SERIES[currency]
    try:
        pts = (macro_series(db, sid) or {}).get("points") or []
    except Exception:
        pts = []
    rates = {d[:7]: (1 / v if inverted else v) for d, v in pts if v}
    _FX[currency] = (time.time(), rates or None)
    return rates or None


def _fx_rate(rates, start, end):
    """The average rate over a period's months, or for one date the rate of its month (nearest month on file)."""
    months, (y, mo) = [], ((int(start[:4]), int(start[5:7])) if start else (int(end[:4]), int(end[5:7])))
    while (y, mo) <= (int(end[:4]), int(end[5:7])):
        months.append(f"{y}-{mo:02d}")
        y, mo = (y + 1, 1) if mo == 12 else (y, mo + 1)
    vals = [rates[k] for k in months if k in rates]
    if vals:
        return sum(vals) / len(vals)
    near = min(rates, key=lambda k: abs((int(k[:4]) * 12 + int(k[5:])) - (int(end[:4]) * 12 + int(end[5:7]))), default=None)
    return rates[near] if near else None


_ADS = {}


def ads_ratio(db, cik):
    """How many shares one American depositary share stands for (TSMC: 5), read from the newest annual report; 1 if none."""
    row = db.execute("SELECT accession, text FROM documents WHERE cik=? AND form IN ('20-F','40-F') "
                     "ORDER BY filing_date DESC, section='full' DESC LIMIT 1", (cik,)).fetchone()
    if not row:
        return 1
    if row["accession"] in _ADS:
        return _ADS[row["accession"]]
    words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
             "twelve": 12, "fifteen": 15, "twenty": 20, "twenty-five": 25, "thirty": 30, "forty": 40, "fifty": 50,
             "one hundred": 100, "hundred": 100, "one-half": 0.5, "one-tenth": 0.1, "one-fifth": 0.2, "one-quarter": 0.25}
    pats = (r"(?:each|one)\s+(?:American\s+Depositary\s+Shares?|ADSs?)\b[^.]{0,80}?\b(?:represent(?:s|ing)?|equal(?:s)? to|equivalent to)\s+"
            r"(?:the right to receive\s+)?(\d+(?:\.\d+)?|[a-z]+(?:[- ][a-z]+)?)\b(?:\s*\(\d+\))?\s+(?:of\s+(?:our|the)\s+)?"
            r"(?:ordinary|common|class\s+[a-z]\s+(?:ordinary|common))?\s*shares?",
            r"ADSs?,?\s+each\s+(?:of\s+which\s+)?represent(?:s|ing)\s+(\d+|[a-z]+(?:-[a-z]+)?)\s+(?:ordinary|common)?\s*shares?")
    found = None
    for p in pats:
        for m in re.finditer(p, row["text"] or "", re.I):
            w = m.group(1).lower()
            found = float(w) if re.fullmatch(r"\d+(\.\d+)?", w) else words.get(w)
            if found:
                break
        if found:
            break
    _ADS[row["accession"]] = found or 1
    return found or 1


def _class_eps(db, cik):
    """{share class: typical EPS} from the presented income statements, for companies with classes worth different
    amounts (Berkshire A and B)."""
    out = {}
    for r in db.execute("SELECT block, value FROM statement_lines WHERE cik=? AND kind='is' AND unit LIKE '%/shares' "
                        "AND block LIKE '%Class %' AND value > 0", (cik,)):
        m = re.search(r"Class ([A-Z])\b", r["block"])
        if m:
            out.setdefault(m.group(1), []).append(r["value"])
    return {k: statistics.median(v) for k, v in out.items()}


def cover_shares(db, cik, ticker):
    """{"value", "date"}: the share count on the newest filing's cover page, all classes counted in the ticker's own
    class (one Berkshire A share counts as 1,500 B shares). None when no filing has one."""
    newest = db.execute("SELECT accession FROM statement_lines WHERE cik=? AND kind='cover' AND concept='EntityCommonStockSharesOutstanding' "
                        "AND value > 0 ORDER BY filed DESC LIMIT 1", (cik,)).fetchone()
    if newest:
        rows = db.execute("SELECT block, value, period_end FROM statement_lines WHERE cik=? AND accession=? AND kind='cover' "
                          "AND concept='EntityCommonStockSharesOutstanding' AND value > 0", (cik, newest["accession"])).fetchall()
        by, when = {}, max(r["period_end"] for r in rows)
        for r in rows:
            m = re.search(r"Class ([A-Z])\b", r["block"])
            k = m.group(1) if m else ""
            by[k] = by.get(k, 0) + r["value"]
        cls, eps = ticker_class(ticker), _class_eps(db, cik)
        if cls in eps and len(by) > 1:
            total = sum(v * (eps[k] / eps[cls] if k in eps else 1) for k, v in by.items())
        else:
            total = sum(by.values())
        return {"value": total - _trust_shares(db, cik, when), "date": when}
    v = cover_shares_dei(db, cik)
    if v:
        when = db.execute("SELECT MAX(period_end) FROM facts WHERE cik=? AND concept='EntityCommonStockSharesOutstanding'", (cik,)).fetchone()[0]
        return {"value": v - _trust_shares(db, cik, when), "date": when}
    return None


def _trust_shares(db, cik, when):
    """Shares a company's own employee benefit trust holds: issued and on the cover page, but not outstanding for
    earnings per share or market value (Lilly: 50 million). The newest figure within a year of the cover date."""
    row = db.execute("SELECT value, period_end FROM facts WHERE cik=? AND concept='CommonStockSharesHeldInEmployeeTrustShares' "
                     "AND period_end <= ? ORDER BY period_end DESC, filed DESC LIMIT 1", (cik, when)).fetchone()
    if not row or not row["value"] or (date.fromisoformat(when) - date.fromisoformat(row["period_end"])).days > 400:
        return 0
    return row["value"]


def _ttm(qrows, arows):
    """The last twelve months from the newest four quarters (Q4 = the year less its first three quarters), or the
    newest year when there are no quarters (companies filing 20-F)."""
    keys = ("revenue", "netIncome", "ebit", "cfo", "capex", "da")
    qs = sorted(qrows, key=lambda x: x["end"])[-4:]
    newest_year = max(arows, key=lambda x: x["end"]) if arows else None
    if len(qs) == 4 and (qs[-1]["end"] - qs[0]["end"]).days <= 300 and (not newest_year or qs[-1]["end"] >= newest_year["end"]):
        out = {"basis": "quarters", "asOf": qs[-1]["end"].isoformat(), "from": f"Q{qs[0]['q']} FY{qs[0]['y']} to Q{qs[-1]['q']} FY{qs[-1]['y']}"}
        for k in keys:
            vals = [q["row"].get(k) for q in qs]
            if all(v is not None for v in vals):
                out[k] = round(sum(vals), 3)
        common = [q["row"].get("netIncomeCommon") for q in qs]
        if all(v is not None for v in common):   # after preferred dividends, as EPS is
            out["netIncome"] = round(sum(common), 3)
        eps = []
        for q in qs:
            e = q["row"].get("eps")
            if e is None and q["q"] == 4:   # the 10-K gives the year: the fourth quarter is what the first three leave
                year = next((a for a in arows if a["y"] == q["y"]), None)
                firsts = [x["row"].get("eps") for x in qrows if x["y"] == q["y"] and x["q"] in (1, 2, 3)]
                if year and year["row"].get("eps") is not None and len(firsts) == 3 and None not in firsts:
                    e = year["row"]["eps"] - sum(firsts)
            if e is None and q["row"].get("netIncome") is not None and q["row"].get("shares"):
                e = q["row"]["netIncome"] / q["row"]["shares"]
            eps.append(e)
        if None not in eps:
            out["eps"] = round(sum(eps), 4)
        return out
    if newest_year:
        out = {"basis": "year", "asOf": newest_year["end"].isoformat(), "from": f"FY{newest_year['y']}"}
        out.update({k: newest_year["row"][k] for k in keys + ("eps",) if newest_year["row"].get(k) is not None})
        if newest_year["row"].get("netIncomeCommon") is not None:
            out["netIncome"] = newest_year["row"]["netIncomeCommon"]
        return out
    return None


def _desk_periods(db, cik, ptype):
    """[{"y", "q", "end", "row"}] in US dollars (millions; EPS per share or per ADS), with the currency they came in."""
    co = db.execute("SELECT sic FROM companies WHERE cik=?", (cik,)).fetchone()
    financial = is_financial(co["sic"] if co else None)
    currency = main_currency(db, cik) or "USD"
    rates = fx_to_usd(db, currency) if currency != "USD" else None
    ads = ads_ratio(db, cik) if currency != "USD" or uses_ifrs(db, cik) else 1
    out = []
    for end, p in _period_rows(db, cik, ptype).items():
        row = _desk_row(p["m"], financial)
        if not row:
            continue
        if rates:
            start = p["start"] or (date.fromisoformat(end) - timedelta(days=88)).isoformat()
            flow, inst = _fx_rate(rates, start, end), _fx_rate(rates, None, end)
            if flow is None or inst is None:
                continue
            row = {k: round(v * (flow if k in FLOW_KEYS else inst if k in INSTANT_KEYS else 1), 3) for k, v in row.items()}
            if "eps" in row:
                row["eps"] = round(row["eps"] * flow, 4)
        if ads != 1:   # per depositary share, as the ADS trades
            if "eps" in row:
                row["eps"] = round(row["eps"] * ads, 4)
            if "shares" in row:
                row["shares"] = round(row["shares"] / ads, 3)
        out.append({"y": p["y"], "q": p["q"], "end": date.fromisoformat(end), "row": row})
    return out, {"currency": currency, "converted": bool(rates), "adsRatio": ads, "financial": financial,
                 "convertFailed": currency != "USD" and not rates}


def desk_fundamentals(db, cik):
    c = db.execute("SELECT * FROM companies WHERE cik=?", (cik,)).fetchone()
    t = db.execute("SELECT title FROM tickers WHERE cik=? LIMIT 1", (cik,)).fetchone()
    years, info = _desk_periods(db, cik, "annual")
    quarters, _ = _desk_periods(db, cik, "quarterly")
    by = {str(p["y"]): p["row"] for p in years}
    errs = [f"Figures are reported in {info['currency']} and could not be converted to US dollars: add a FRED key on the Macro page."] if info["convertFailed"] else []
    shares = cover_shares(db, cik, c["ticker"])
    if shares:
        shares = {"value": round(shares["value"] / 1e6 / info["adsRatio"], 3), "date": shares["date"]}
    ttm = _ttm([q for q in quarters if q["q"]], years)
    desc = c["description"] or ""
    if not desc:
        d = db.execute("SELECT text FROM documents WHERE cik=? AND section='1' "
                       "ORDER BY filing_date DESC LIMIT 1", (cik,)).fetchone()
        if d:
            body = re.sub(r"(?is)^item\s*1\.?\s*business\.?\s*", "", d["text"]).strip()
            paras = [x.strip() for x in body.split("\n") if len(x.strip()) > 120]
            desc = " ".join(paras[:2])[:900]
    addr = (c["business_address"] or "").split(", ")
    # saved before the country was kept: a U.S. address ends in a state code and a ZIP code ("TX, 75039")
    country = c["country"] or ("United States" if len(addr) >= 2 and re.fullmatch(r"[A-Z]{2}", addr[-2])
                                                  and re.fullmatch(r"\d{5}(-\d{0,4})?", addr[-1]) else None)
    return {
        "name": (t["title"] if t else None) or c["name"], "byYear": by, "peers": [], "errs": errs,
        "ttm": ttm, "sharesOutstanding": shares, "currency": info["currency"], "converted": info["converted"],
        "adsRatio": info["adsRatio"], "financial": info["financial"],
        "profile": {"desc": desc, "sector": c["industry"], "industry": c["industry"], "sic": c["sic"], "employees": None,
                    "country": country, "website": c["website"]},
        "fetchedAt": c["fetched_at"],
    }


def desk_quarters(db, cik):
    out = []
    for p in _desk_periods(db, cik, "quarterly")[0]:
        if p["q"]:
            ms = int(datetime(p["end"].year, p["end"].month, p["end"].day, tzinfo=timezone.utc).timestamp() * 1000)
            out.append({"y": p["y"], "q": p["q"], "row": p["row"], "end": ms})
    return out


DETAIL_KEYS = {   # the page's name for each extra statement line -> metric
    "rd": "research_development", "sga": "sga", "pretax": "pretax_income", "tax": "income_tax",
    "epsBasic": "eps_basic", "epsDiluted": "eps_diluted", "sharesBasic": "eps_basic_shares",
    "sbc": "stock_compensation", "dividends": "dividends_paid", "buybacks": "buybacks",
    "cfi": "investing_cash_flow", "cff": "financing_cash_flow", "acquisitions": "acquisitions",
    "debtIssued": "debt_issued", "debtRepaid": "debt_repaid",
    "sti": "short_term_investments", "receivables": "receivables", "inventory": "inventory",
    "ppe": "ppe", "goodwill": "goodwill", "intangibles": "intangibles", "ltInvest": "long_term_investments",
    "ap": "accounts_payable", "deferredRev": "deferred_revenue", "stDebt": "short_term_debt",
    "ltDebt": "long_term_debt", "liabTotal": "total_liabilities", "retained": "retained_earnings",
    "paidIn": "paid_in_capital", "aoci": "aoci", "treasury": "treasury_stock", "minority": "minority_interest",
    "accrued": "accrued_liabilities", "leaseAssets": "lease_assets", "leaseLiab": "lease_liabilities",
    "deferredTax": "deferred_tax_liabilities", "deferredRevNC": "deferred_revenue_noncurrent",
    "sharesOut": "shares_outstanding", "leaseLiabCur": "lease_liabilities_current", "taxPayable": "taxes_payable",
    "otherCAr": "other_current_assets", "otherRec": "other_receivables", "deferredTaxAssets": "deferred_tax_assets", "otherNCAr": "other_noncurrent_assets",
    "otherNCLr": "other_noncurrent_liabilities",
}


def desk_statements(db, cik):
    """The statement lines beyond the page's core fifteen, for every year and quarter (USD millions)."""
    def row(m):
        out = {}
        for key, metric in DETAIL_KEYS.items():
            v = m[metric][0] if metric in m else None
            if v is not None:
                out[key] = round(v, 4) if key.startswith("eps") else round(abs(v) / 1e6 if key in (
                    "dividends", "buybacks", "acquisitions", "debtRepaid") else v / 1e6, 3)
        if "sga" not in out and "selling_marketing" in m and "general_admin" in m:
            parts = [m["selling_marketing"][0], m["general_admin"][0]]
            if all(p is not None for p in parts):
                out["sga"] = round(sum(parts) / 1e6, 3)
        return out
    currency = main_currency(db, cik) or "USD"
    rates = fx_to_usd(db, currency) if currency != "USD" else None
    ads = ads_ratio(db, cik) if currency != "USD" or uses_ifrs(db, cik) else 1
    flows = {"rd", "sga", "pretax", "tax", "sbc", "dividends", "buybacks", "cfi", "cff", "acquisitions", "debtIssued", "debtRepaid"}

    def usd(end, p, out):
        """In US dollars, as the core figures are; per depositary share for an ADS."""
        if rates:
            start = p["start"] or (date.fromisoformat(end) - timedelta(days=88)).isoformat()
            flow, inst = _fx_rate(rates, start, end), _fx_rate(rates, None, end)
            if flow is None or inst is None:
                return {}
            out = {k: round(v * (flow if k in flows or k.startswith("eps") else 1 if k in ("sharesBasic", "sharesOut") else inst), 4 if k.startswith("eps") else 3)
                   for k, v in out.items()}
        if ads != 1:
            out = {k: (round(v * ads, 4) if k.startswith("eps") else round(v / ads, 3) if k in ("sharesBasic", "sharesOut") else v) for k, v in out.items()}
        return out
    annual = {str(p["y"]): usd(e, p, row(p["m"])) for e, p in _period_rows(db, cik, "annual").items()}
    quarterly = [{"y": p["y"], "q": p["q"], "row": usd(e, p, row(p["m"]))}
                 for e, p in _period_rows(db, cik, "quarterly").items() if p["q"]]
    return {"annual": annual, "quarterly": quarterly, "currency": "USD" if rates or currency == "USD" else currency,
            "reportedIn": currency}


def _segment_sources(db, cik, table):
    """Which filings the stored segment figures were read from: how many, and the newest one."""
    rows = db.execute(
        f"SELECT f.form, f.filing_date, f.report_date, f.url FROM filings f WHERE f.cik=? AND f.accession IN "
        f"(SELECT DISTINCT accession FROM {table} WHERE cik=?) ORDER BY f.filing_date", (cik, cik)).fetchall()
    if not rows:
        return None
    forms = sorted({r["form"] for r in rows})
    return {"count": len(rows), "forms": forms, "first": rows[0]["filing_date"],
            "latest": {"form": rows[-1]["form"], "filed": rows[-1]["filing_date"],
                       "period": rows[-1]["report_date"], "url": rows[-1]["url"]}}


def _pick_members(revenue, total):
    """Choose the members that make up revenue once, without double counting.

    Filings tag overlapping breakdowns on one axis (Products and Services next to iPhone,
    Mac, iPad...). Keep the most detailed set that adds up to the company total.
    """
    names = [m for m, v in sorted(revenue.items(), key=lambda x: -x[1]) if v > 0]
    if len(names) < 2 or len(names) > 15:
        return names

    def sums(items):
        out = [0.0] * (1 << len(items))
        for mask in range(1, len(out)):
            low = mask & -mask
            out[mask] = out[mask ^ low] + revenue[items[low.bit_length() - 1]]
        return out

    if total:
        tol, best = max(abs(total) * 0.002, 1.0), 0
        all_sums = sums(names)
        for mask, v in enumerate(all_sums):
            if abs(v - total) <= tol and bin(mask).count("1") > bin(best).count("1"):
                best = mask
        if best:
            return [m for i, m in enumerate(names) if best >> i & 1]
        # Nothing adds up exactly: the parts can leave out a small business (Netflix's regions are streaming only,
        # without DVD). Take the most parts within 2% of the total, the closest if several tie.
        near = [(bin(mask).count("1"), -abs(v - total), mask) for mask, v in enumerate(all_sums)
                if bin(mask).count("1") >= 2 and abs(v - total) <= abs(total) * 0.02]
        if near:
            best = max(near)[2]
            return [m for i, m in enumerate(names) if best >> i & 1]
    # No total to check against: drop any member that is the sum of several others.
    for m in list(names):
        rest = [x for x in names if x != m]
        tol = max(revenue[m] * 0.0005, 1.0)
        if any(abs(v - revenue[m]) <= tol and bin(mask).count("1") >= 2
               for mask, v in enumerate(sums(rest))):
            names = rest
    return names


def desk_segments(db, cik):
    """Revenue by segment, product and region for every fiscal year on file (USD millions)."""
    facts = db.execute("SELECT * FROM segment_facts WHERE cik=? ORDER BY filed", (cik,)).fetchall()
    totals = {(r["accession"], r["period_end"]): r["value"] for r in facts
              if r["axis"] == "total" and r["metric"] == "revenue"}
    reported = {r["period_end"]: r["value"] for r in db.execute(
        "SELECT period_end, value FROM metrics WHERE cik=? AND period_type='annual' AND metric='revenue'",
        (cik,))}
    out = {"source": "SEC filings", "filings": _segment_sources(db, cik, "segment_facts")}
    for axis in ("segment", "product", "geo"):
        # Each year comes from the newest report that covers it, so restated figures win.
        latest = {}
        for r in facts:
            if r["axis"] == axis and r["metric"] == "revenue":
                latest[r["period_end"]] = r["accession"]
        by_year = {}
        for end, accn in latest.items():
            rev, op, label = {}, {}, {}
            for r in facts:
                if r["axis"] == axis and r["accession"] == accn and r["period_end"] == end:
                    (rev if r["metric"] == "revenue" else op)[r["member"]] = r["value"]
                    label[r["member"]] = r["label"]
            keep = _pick_members(rev, totals.get((accn, end)) or reported.get(end))
            by_year[fiscal_year_of(end)] = {label[m]: (rev[m], op.get(m)) for m in keep}
        years = sorted(by_year)
        if not years or len(by_year[years[-1]]) < 2:
            out[axis] = None
            continue
        names = dict.fromkeys(n for y in reversed(years) for n in by_year[y])
        rows = []
        for name in names:
            cell = [by_year[y].get(name) for y in years]
            row = {"name": name, "vals": [round(c[0] / 1e6, 3) if c else None for c in cell]}
            if any(c and c[1] is not None for c in cell):
                row["op"] = [round(c[1] / 1e6, 3) if c and c[1] is not None else None for c in cell]
            rows.append(row)
        rows.sort(key=lambda r: -(r["vals"][-1] or 0))
        out[axis] = {"years": years, "rows": rows}
    return out


def desk_segments_quarterly(db, cik, max_quarters=40):
    """Revenue by segment, product and region for each fiscal quarter (USD millions).

    Quarterly reports give the quarter itself and the year to date; the fourth quarter is never
    reported, so it is the year from the annual report less the first nine months.
    """
    D = date.fromisoformat
    quarters = {}   # period_end -> (fiscal year, quarter)
    for r in db.execute("SELECT DISTINCT period_end, fiscal_year, fiscal_quarter FROM metrics "
                        "WHERE cik=? AND period_type='quarterly' AND fiscal_quarter IS NOT NULL", (cik,)):
        quarters[r["period_end"]] = (r["fiscal_year"], r["fiscal_quarter"])
    reported = {r["period_end"]: r["value"] for r in db.execute(
        "SELECT period_end, value FROM metrics WHERE cik=? AND period_type='quarterly' AND metric='revenue'", (cik,))}
    ends = sorted(quarters)

    def nearest(day):   # filings can date a period a few days off the fiscal calendar
        for e in ends:
            if abs((D(e) - D(day)).days) <= 6:
                return e
        return None

    out = {"source": "SEC filings", "filings": _segment_sources(db, cik, "segment_q_facts")}
    for axis in ("segment", "product", "geo"):
        # the latest-filed figure for each line and period
        short, year, labels = {}, {}, {}
        for r in db.execute("SELECT * FROM segment_q_facts WHERE cik=? AND axis=? ORDER BY filed", (cik, axis)):
            e = nearest(r["period_end"])
            if e:
                span = round((D(r["period_end"]) - D(r["period_start"])).days / 91.3)
                short[(r["member"], r["metric"], e, span)] = r["value"]
                labels[r["member"]] = r["label"]
        for r in db.execute("SELECT * FROM segment_facts WHERE cik=? AND axis=? ORDER BY filed", (cik, axis)):
            e = nearest(r["period_end"])
            if e:
                year[(r["member"], r["metric"], e)] = r["value"]
                labels.setdefault(r["member"], r["label"])
        by_fy = {}
        for e, (fy, q) in quarters.items():
            by_fy.setdefault(fy, {})[q] = e
        cells = {}   # (period_end, member) -> {metric: value}
        for fy, qs in by_fy.items():
            for member in labels:
                for metric in ("revenue", "operating_income"):
                    def upto(q):   # the year's total through quarter q
                        if q == 0:
                            return 0.0
                        e = qs.get(q)
                        if e is None:
                            return None
                        if q == 4 and (member, metric, e) in year:
                            return year[(member, metric, e)]
                        if (member, metric, e, q) in short:
                            return short[(member, metric, e, q)]
                        before = upto(q - 1)
                        alone = short.get((member, metric, e, 1))
                        return before + alone if before is not None and alone is not None else None
                    for q, e in qs.items():
                        v = short.get((member, metric, e, 1))
                        if v is None:
                            a, b = upto(q), upto(q - 1)
                            v = a - b if a is not None and b is not None else None
                        if v is not None:
                            cells.setdefault((e, member), {})[metric] = v
        by_q = {}
        for e in ends:
            rev = {m: c["revenue"] for (pe, m), c in cells.items() if pe == e and "revenue" in c}
            if not rev:
                continue
            keep = _pick_members(rev, reported.get(e))
            by_q[e] = {labels[m]: (rev[m], cells[(e, m)].get("operating_income")) for m in keep}
        shown = sorted(by_q)[-max_quarters:]
        if not shown or len(by_q[shown[-1]]) < 2:
            out[axis] = None
            continue
        names = dict.fromkeys(n for e in reversed(shown) for n in by_q[e])
        rows = []
        for name in names:
            cell = [by_q[e].get(name) for e in shown]
            row = {"name": name, "vals": [round(c[0] / 1e6, 3) if c else None for c in cell]}
            if any(c and c[1] is not None for c in cell):
                row["op"] = [round(c[1] / 1e6, 3) if c and c[1] is not None else None for c in cell]
            rows.append(row)
        rows.sort(key=lambda r: -(r["vals"][-1] or 0))
        out[axis] = {"years": [f"Q{quarters[e][1]} FY{str(quarters[e][0])[2:]}" for e in shown], "rows": rows}
    return out


def desk_insiders(db, cik, limit=6000):
    """Insider trades, one row per insider, day and kind of transaction.

    A Form 4 lists a sale as one line per price it executed at, and an insider can file
    more than one form for the same day. Those are combined here into the shares traded,
    the average price and the holding left afterwards.
    """
    groups = {}
    for r in db.execute("SELECT rowid, * FROM insider_trades WHERE cik=? ORDER BY filed, rowid", (cik,)):
        key = ((r["owner"] or "").upper(), r["transaction_date"], r["code"], r["is_derivative"] or 0, r["security"])
        g = groups.setdefault(key, {
            "date": r["transaction_date"], "who": r["owner"],
            "role": "; ".join(dict.fromkeys(x.strip() for x in (r["role"] or "").split(";") if x.strip())),
            "type": r["type"], "code": r["code"], "security": r["security"],
            "derivative": bool(r["is_derivative"]), "expiration": r["expiration"],
            "side": r["acquired_disposed"], "shares": 0.0, "_paid": 0.0, "_priced": 0.0, "after": None})
        shares = r["shares"] or 0.0
        g["shares"] += shares
        if r["price"]:
            g["_paid"] += shares * r["price"]
            g["_priced"] += shares
        g["filed"], g["url"] = r["filed"], r["url"]   # the latest form filed for that day
        after = r["shares_after"]
        if after is not None:
            # The balance once the day's trades are done: the lowest after selling, the highest after buying.
            done = min if r["acquired_disposed"] == "D" else max
            if g["after"] is None or done(after, g["after"]) == after:
                g["after"], g["own"] = after, r["ownership"]
    out = []
    for g in groups.values():
        paid, priced = g.pop("_paid"), g.pop("_priced")
        g["price"] = round(paid / priced, 4) if priced else None
        g["value"] = round(paid, 2) if priced else None
        g.setdefault("own", None)
        out.append(g)
    out.sort(key=lambda g: (g["date"] or "", g["filed"] or ""), reverse=True)
    return out[:limit]


def shares_outstanding(db, cik):
    """Latest share count the company reported on a filing's cover page (all classes added up)."""
    row = db.execute("SELECT period_end, accession FROM facts WHERE cik=? AND concept='EntityCommonStockSharesOutstanding' "
                     "AND value > 0 ORDER BY period_end DESC, filed DESC LIMIT 1", (cik,)).fetchone()
    if row:
        return db.execute("SELECT SUM(value) FROM (SELECT DISTINCT value FROM facts WHERE cik=? AND accession=? AND period_end=? "
                          "AND concept='EntityCommonStockSharesOutstanding')", (cik, row["accession"], row["period_end"])).fetchone()[0]
    for metric in ("shares_outstanding", "shares_diluted"):
        row = db.execute("SELECT value FROM metrics WHERE cik=? AND metric=? AND value > 0 ORDER BY period_end DESC LIMIT 1",
                         (cik, metric)).fetchone()
        if row:
            return row["value"]
    return None


def _tidy_name(name):
    """Manager names are often filed in capitals: BLACKROCK, INC. -> Blackrock, Inc."""
    if not name.isupper():
        return name
    keep = {"LLC", "LP", "LLP", "FMR", "UBS", "AG", "SA", "NV", "USA", "BNY", "NA", "PLC", "LTD", "II", "III", "IV", "US"}
    return " ".join(w if w.strip(".,") in keep else w.capitalize() for w in name.split())


def _holder_key(name):
    words = re.sub(r"[^A-Z0-9 ]", "", (name or "").upper().replace("&", " AND ")).split()
    return " ".join(w for w in words if w not in (
        "INC", "LLC", "LP", "LLP", "CORP", "CORPORATION", "CO", "COMPANY", "LTD", "LIMITED", "PLC", "THE", "NA", "SA", "AG"))


# What kind of holder a name is. The SEC's 13F data does not say, so this goes by well-known
# names and by words in the name; Schedule 13D/13G filers declare a type code, used when present.
_TYPE_RULES = [
    ("Sovereign wealth and central banks", r"NORGES BANK|SWISS NATIONAL BANK|GIC PRIVATE|TEMASEK|INVESTMENT AUTHORITY|PUBLIC INVESTMENT FUND|"
     r"SOVEREIGN|KOREA INVESTMENT CORP|CHINA INVESTMENT CORP|MUBADALA|QATAR INVESTMENT|BANK OF JAPAN|NATIONAL BANK OF|"
     r"NATIONAL DEVELOPMENT FUND|EXECUTIVE YUAN|MINISTRY OF FINANCE|GOVERNMENT OF|STATE COUNCIL|NATIONAL PENSION SERVICE"),
    ("Pension funds", r"RETIREMENT|PENSION|TEACHERS|\bPERS\b|\bSTRS\b|EMPLOYEES|SUPERANNUATION|CPP INVESTMENT|CAISSE DE DEPOT|"
     r"AUSTRALIANSUPER|AP-FONDEN|PUBLIC SECTOR|STATE BOARD OF ADMINISTRATION|TREASURER|\bSTATE OF\b|\bPGGM\b|\bAPG\b|PROVIDENT FUND"),
    ("Insurance companies", r"INSURANCE|ASSURANCE|\bLIFE\b|MUTUAL OF|ALLIANZ|\bAXA\b|METLIFE|PRUDENTIAL|AEGON|MANULIFE|"
     r"AVIVA|NATIONWIDE|ALLSTATE|AFLAC|ZURICH|GENERALI|REINSURANCE|\bRE\b GROUP"),
    ("Fund managers", r"VANGUARD|BLACKROCK|STATE STREET|\bFMR\b|FIDELITY|GEODE|T\.? ?ROWE|INVESCO|CAPITAL RESEARCH|CAPITAL WORLD|"
     r"CAPITAL INTERNATIONAL|DIMENSIONAL|FRANKLIN|WELLINGTON|NUVEEN|\bTIAA\b|JANUS|\bMFS\b|MASSACHUSETTS FINANCIAL|AMUNDI|"
     r"LEGAL & GENERAL|ALLIANCEBERNSTEIN|PUTNAM|AMERICAN CENTURY|DODGE & COX|NEUBERGER|PIMCO|PACIFIC INVESTMENT|SCHRODER|ABERDEEN|"
     r"\bABRDN\b|COLUMBIA THREADNEEDLE|EATON VANCE|LORD,? ABBETT|VAN ECK|MIRAE|ARK INVEST|PRIMECAP|HARRIS ASSOCIATES|LOOMIS|"
     r"BAILLIE GIFFORD|JENNISON|VICTORY CAPITAL|FEDERATED|FIRST TRUST|PROSHARE|WISDOMTREE|GLOBAL X|NORTHERN TRUST|"
     r"\bFIL LTD|FONDER|FORVALTNING|GRANTHAM, MAYO|\bFUNDS?\b|\bETFS?\b|INVESTMENT COMPANY|ASSET MANAGEMENT|INVESTMENT MANAGEMENT|INVESTMENT MANAGERS|GLOBAL INVESTORS"),
    ("Hedge funds and trading firms", r"CITADEL|RENAISSANCE TECH|TWO SIGMA|MILLENNIUM|D\.? ?E\.? ?SHAW|BRIDGEWATER|\bAQR\b|POINT72|"
     r"SUSQUEHANNA|JANE STREET|TIGER GLOBAL|COATUE|VIKING GLOBAL|LONE PINE|THIRD POINT|PERSHING SQUARE|ELLIOTT|BALYASNY|"
     r"MARSHALL WACE|MAN GROUP|WINTON|SCULPTOR|FARALLON|BAUPOST|GREENLIGHT|APPALOOSA|SOROS|DUQUESNE|HUDSON BAY|SCHONFELD|"
     r"EXODUSPOINT|WOLVERINE|OPTIVER|VIRTU|\bIMC\b|\bDRW\b|HUDSON RIVER|SQUAREPOINT|\bQUBE\b|CAPULA|BREVAN|HEDGE|MASTER FUND|"
     r"OFFSHORE|ARBITRAGE|QUANT|TRADING"),
    ("Private equity and venture", r"SILVER LAKE|\bKKR\b|BLACKSTONE|CARLYLE|APOLLO|\bTPG\b|WARBURG|SEQUOIA|ANDREESSEN|VENTURE|"
     r"PRIVATE EQUITY|GROWTH EQUITY|BUYOUT|\bSPV\b"),
    ("Banks and brokers", r"\bBANK\b|\bBANC|BANCORP|BANQUE|MORGAN STANLEY|GOLDMAN SACHS|JPMORGAN|CITIGROUP|WELLS FARGO|\bUBS\b|"
     r"BARCLAYS|\bHSBC\b|DEUTSCHE|CREDIT SUISSE|\bBNP\b|SOCIETE GENERALE|NOMURA|MIZUHO|SUMITOMO|MITSUBISHI UFJ|TORONTO DOMINION|"
     r"SWEDBANK|HANDELSBANKEN|NYKREDIT|\bKBC\b|KEYBANK|NATIONAL ASSOCIATION|SCOTIA|SECURITIES|BROKER|RAYMOND JAMES|SCHWAB|STIFEL|AMERIPRISE|\bLPL\b|TRUST CO\b|TRUST COMPANY|\bBNY\b|CREDIT AGRICOLE"),
    ("Companies", r"BERKSHIRE HATHAWAY"),
    ("Family offices, trusts and endowments", r"FAMILY|FOUNDATION|ENDOWMENT|UNIVERSITY|COLLEGE|REVOCABLE|\bTRUST\b|ESTATE OF"),
    ("Hedge funds and trading firms", r"(CAPITAL|PARTNERS|ADVISORS|MANAGEMENT),? L\.?P\.?$|\bL\.?P\.?$"),
    ("Investment advisers", r"WEALTH|ADVISOR|ADVISER|ADVISORY|FINANCIAL|PLANNING|PRIVATE CLIENT|COUNSEL|CAPITAL MANAGEMENT|"
     r"INVESTMENT|CAPITAL|PARTNERS|MANAGEMENT|ASSOCIATES"),
]
_TYPE_RULES = [(label, re.compile(pattern)) for label, pattern in _TYPE_RULES]
_TYPE_CODES = {"IN": "Individuals and insiders", "IC": "Insurance companies", "EP": "Pension funds", "BK": "Banks and brokers",
               "BD": "Banks and brokers", "SA": "Banks and brokers", "IV": "Fund managers"}


def holder_type(name, code=None):
    code = (code or "").upper().strip()
    if code == "IN":
        return _TYPE_CODES[code]
    text = (name or "").upper()
    for label, pattern in _TYPE_RULES:
        if pattern.search(text):
            return label
    if code in _TYPE_CODES:
        return _TYPE_CODES[code]
    return "Companies" if code in ("CO", "HC") else "Other"


def major_holders(db, cik):
    """The latest Schedule 13D/13G position of each filer, with the change from its filing before."""
    by_filer = {}
    for r in db.execute("SELECT * FROM holders_5pct WHERE cik=? ORDER BY filed, accession", (cik,)):
        # one filer's filings over time, by the share class's CUSIP: the class's name is written differently from one
        # filing to the next ("Common Stock", "CLASS A SHARES"), which made one holder look like several
        key = (r["filer_cik"] or r["person"], r["issuer_cusip"] or "")
        by_filer.setdefault(key, {}).setdefault(r["accession"], []).append(r)
    out = []
    for filings in by_filer.values():
        order = list(filings)
        top = max(filings[order[-1]], key=lambda r: r["shares"] or 0)   # a group reports its total on one member
        if not top["shares"] or top["shares"] <= 0:
            continue  # sold out
        before = max((r["shares"] or 0) for r in filings[order[-2]]) if len(order) > 1 else None
        out.append({"filer_cik": top["filer_cik"], "name": top["person"],
                    "names": {_holder_key(r["person"]) for r in filings[order[-1]]},
                    "group": len(filings[order[-1]]), "shares": top["shares"], "pct": top["pct"],
                    "change": top["shares"] - before if before is not None else None,
                    "type": holder_type(top["person"], top["person_type"]),
                    "form": "13D" if "13D" in top["form"] else "13G", "filed": top["filed"],
                    "date": top["event_date"] or top["filed"], "class": top["class_title"], "url": top["url"]})
    return out


# Vanguard reports through several 13F filers since its 2025 reorganisation (and some smaller arms), T. Rowe Price through
# two since 2022; each is one holder. Named one by one, so a firm that only shares the name (Vanguard Capital Wealth Advisors) is not swept in.
HOLDER_FAMILIES = [("Vanguard", re.compile(
    r"^vanguard (group|capital management|portfolio management|fiduciary trust|global advisers|asset management|"
    r"investments (australia|canada|hong kong|uk|switzerland)|national trust|personalized indexing|advisers|marketing)\b", re.I)),
    # Capital Group's investment divisions file separately; other sites show them as Capital Research
    ("Capital Group", re.compile(r"^capital (research (global investors|and management|management)|world investors|"
                                 r"international (investors|inc|ltd|limited|sarl|k\.?k)|group)\b", re.I)),
    # T. Rowe Price has filed through two managers since 2022
    ("T. Rowe Price", re.compile(r"^(price t rowe associates|t\.? rowe price (associates|investment management))\b", re.I))]


def _family(name):
    return next((f for f, pat in HOLDER_FAMILIES if pat.search(name or "")), None)


def annual_report_holders(db, cik):
    """A foreign company's own list of its major shareholders, from Item 7.A of its newest 20-F: everyone with 5% or
    more of all its shares, wherever they trade (TSMC: Taiwan's National Development Fund, 6.38%), the as-of date, and
    how many of its shares sit in the ADR program. None when there is no 20-F text on file."""
    row = db.execute("SELECT text FROM documents WHERE cik=? AND form IN ('20-F','20-F/A') ORDER BY filing_date DESC LIMIT 1",
                     (cik,)).fetchone()
    if not row:
        return None
    text = row[0]
    start = next((m.start() for m in re.finditer(r"Major Shareholders\s*\n", text)
                  if re.search(r"(?i)beneficially owned|owned by|five percent|5%", text[m.start():m.start() + 1500])), None)
    if start is None:
        return None
    block = text[start:start + 6000]
    end = re.search(r"\n\s*Related Party Transactions\s*\n", block)
    block = block[:end.start()] if end else block
    lines = [l.strip() for l in block.split("\n") if l.strip()]
    shares_re, pct_re = re.compile(r"^\(?([\d,]{5,})\)?$"), re.compile(r"^(\d{1,2}(?:\.\d+)?)\s*%$")
    holders = []
    for i, line in enumerate(lines):
        both = re.match(r"^(.*?[A-Za-z].*?)\s+([\d,]{5,})\s+(\d{1,2}(?:\.\d+)?)\s*%$", line)   # name, shares and % on one line
        if both:
            name, sh, pc = both.groups()
        elif i + 2 < len(lines) and shares_re.match(lines[i + 1]) and pct_re.match(lines[i + 2]) and re.search(r"[A-Za-z]{3}", line):
            name, sh, pc = line, shares_re.match(lines[i + 1]).group(1), pct_re.match(lines[i + 2]).group(1)
        else:
            continue
        name = re.sub(r"\(\d\)$", "", name).strip()
        if re.match(r"(?i)names? of|total", name):
            continue
        holders.append({"name": name, "shares": float(sh.replace(",", "")), "pct": float(pc),
                        "insiders": bool(re.search(r"(?i)directors|officers|management", name))})
    if not holders:
        return None
    asof = re.search(r"as of ([A-Z][a-z]+ \d{1,2}, \d{4})", block)
    try:
        asof = datetime.strptime(asof.group(1), "%B %d, %Y").date().isoformat() if asof else None
    except ValueError:
        asof = None
    adr = re.search(r"([\d,]{7,}) (?:common|ordinary) shares,? represented by ([\d,]{5,}) (?:ADSs|ADRs|American Depositary)", text[start:start + 8000])
    total = re.search(r"a total of ([\d,]{7,}) (?:common|ordinary) shares were outstanding", text[start:start + 8000])
    return {"holders": holders, "date": asof,
            "adrShares": float(adr.group(1).replace(",", "")) if adr else None,
            "totalShares": float(total.group(1).replace(",", "")) if total else None}


def desk_holders(db, cik, limit=400):
    """Major holders: 13F managers at the latest quarter end plus the owners on Schedule 13D/13G."""
    periods = [r[0] for r in db.execute("SELECT DISTINCT period FROM holders_13f WHERE cik=? ORDER BY period DESC", (cik,))]
    latest, prior = (periods[0] if periods else None), (periods[1] if len(periods) > 1 else None)
    before = {r["manager_cik"]: r["shares"] for r in db.execute(
        "SELECT manager_cik, shares FROM holders_13f WHERE cik=? AND period=?", (cik, prior))} if prior else {}
    filed_before = {r[0] for r in db.execute("SELECT manager_cik FROM holders_13f_filers WHERE period=?", (prior,))} if prior else set()
    co = db.execute("SELECT ticker FROM companies WHERE cik=?", (cik,)).fetchone()
    cover = cover_shares(db, cik, co["ticker"] if co else "")
    out_shares = (cover or {}).get("value") or shares_outstanding(db, cik)   # all classes, in the ticker's class
    if out_shares:
        out_shares /= ads_ratio(db, cik) or 1   # funds hold the ADRs (TSMC: 1 ADR = 5 shares)
    rows = [dict(r) for r in db.execute("SELECT * FROM holders_13f WHERE cik=? AND period=? ORDER BY shares DESC", (cik, latest))] if latest else []
    # one row per family (Vanguard's filers), its change the sum of theirs
    fam_rows, merged = {}, []
    for r in rows:
        f = _family(r["manager"])
        if not f:
            merged.append(r)
            continue
        g = fam_rows.get(f)
        if not g:
            g = fam_rows[f] = dict(r, manager=f, members=[], family=True)
            g["shares"] = g["value"] = 0.0
            merged.append(g)
        g["members"].append(r)
        g["shares"] += r["shares"]
        g["value"] += r["value"] or 0
        g["filed"] = max(g["filed"] or "", r["filed"] or "")
    rows = sorted(merged, key=lambda r: -r["shares"])
    majors = major_holders(db, cik)
    by_cik = {m["filer_cik"]: m for m in majors if m["filer_cik"]}
    by_name = {n: m for m in majors for n in m["names"] if n}
    holders, matched, types = [], set(), {}

    def count(kind, shares):
        t = types.setdefault(kind, [0.0, 0])
        t[0] += shares or 0
        t[1] += 1

    def was(r):   # (shares the quarter before, or None when it did not file then; whether it is new)
        if r.get("family"):
            prev = [before.get(x["manager_cik"]) for x in r["members"]]
            return (sum(p or 0 for p in prev), False) if any(p is not None for p in prev) else (None, False)
        if r["manager_cik"] in before:
            return before[r["manager_cik"]], False
        return None, r["manager_cik"] in filed_before   # new only if it filed that quarter without holding any

    for r in rows:
        name = r["manager"] or ""
        prev, new = was(r)
        change = r["shares"] - prev if prev is not None else None
        m = by_cik.get(r["manager_cik"]) or by_name.get(_holder_key(name))
        shares, value, src_note = r["shares"], r["value"], ""
        if r.get("family"):   # every Vanguard 13G (The Vanguard Group, Vanguard Capital Management) is the family's
            fam = [x for x in majors if _family(x["name"]) == r["manager"] or _holder_key(x["name"]).startswith(_holder_key(name))]
            matched.update(id(x) for x in fam)
            m = m or (fam[0] if fam else None)
        exact = bool(m)
        if not m and not r.get("family"):   # the same firm under a slightly different name (Orbis ... Ltd / (U.S.), L.P.)
            two = " ".join(_holder_key(name).split()[:2])
            m = next((x for x in majors if len(two.split()) == 2 and id(x) not in matched and not _family(x["name"])
                      and " ".join(_holder_key(x["name"]).split()[:2]) == two), None)
        if m:
            # all of that filer's 13D/13Gs, whatever class or CUSIP they name (Baillie Gifford filed under two)
            matched.update(id(x) for x in majors if x is m or (m["filer_cik"] and x["filer_cik"] == m["filer_cik"]))
            # a 13D/13G as of this quarter's end with far more shares: it counts all of the firm's funds worldwide,
            # where its 13F can list only some (BlackRock's Shopify 13G: 61.6M shares; its 13F: 5.8M)
            if exact and (m["date"] or "") >= (latest or "") and m["shares"] > shares * 1.5:
                value = value * m["shares"] / shares if value and shares else value
                shares, src_note, change = m["shares"], " (13F lists fewer)", m["change"]
        r["shares"] = shares
        kind = holder_type(name)
        count(kind, r["shares"])
        holders.append({
            "type": kind,
            "name": _tidy_name(name) + (f" ({len(r['members'])} filers)" if r.get("family") and len(r["members"]) > 1 else ""),
            "shares": shares, "value": value,
            "pct": r["shares"] / out_shares if out_shares else None,
            "change": change, "isNew": new,
            "members": len(r["members"]) if r.get("family") else None,
            "changePct": change / out_shares if change is not None and out_shares else None,
            "filed": r["filed"], "date": latest, "src": ("13F · " + m["form"] + src_note) if m else "13F"})
    total = sum(r["shares"] for r in rows)
    extra = 0.0
    for m in majors:
        if id(m) in matched:
            continue  # the same holder is already listed from its 13F
        pct = m["pct"] if m["pct"] is not None else (m["shares"] / out_shares * 100 if out_shares else None)
        if pct is not None and pct < 5:
            continue  # a filing saying it has gone below 5%: no longer a major holder
        extra += m["shares"]
        count(m["type"], m["shares"])
        holders.append({
            "type": m["type"],
            "name": _tidy_name(m["name"]) + (f" and {m['group'] - 1} related" if m["group"] > 1 else ""),
            "shares": m["shares"], "value": None,
            "pct": m["shares"] / out_shares if out_shares else (m["pct"] / 100 if m["pct"] is not None else None),
            "change": m["change"], "isNew": False,
            "changePct": m["change"] / out_shares if m["change"] is not None and out_shares else None,
            "filed": m["filed"], "date": m["date"], "src": m["form"], "url": m["url"], "class": m["class"]})
    # a foreign company's own list (20-F): owners of its home-market shares the SEC filings never show
    home = annual_report_holders(db, cik)
    ratio = ads_ratio(db, cik) or 1
    if home:
        for h in home["holders"]:
            if any(_holder_key(h["name"]) == _holder_key(x["name"]) for x in holders):
                continue
            sh = h["shares"] / ratio   # in ADRs, like the rest of the list
            extra += sh
            kind = "Insiders" if h["insiders"] else holder_type(h["name"])
            count(kind, sh)
            holders.append({"type": kind, "name": h["name"], "shares": sh, "value": None, "pct": h["pct"] / 100,
                            "change": None, "isNew": False, "changePct": None, "filed": None, "date": home["date"],
                            "src": "20-F", "home": True})
    holders.sort(key=lambda h: -(h["shares"] or 0))
    adr_share = home["adrShares"] / home["totalShares"] if home and home["adrShares"] and home["totalShares"] else None
    return {"period": latest, "prior": prior, "holders": holders[:limit], "count": len(rows), "totalShares": total,
            "adrRatio": ratio if ratio != 1 else None, "adrShare": adr_share,
            "otherCount": sum(1 for h in holders if "13F" not in h["src"]), "otherShares": extra,
            "types": sorted(({"name": k, "shares": v[0], "count": v[1]} for k, v in types.items()),
                            key=lambda t: -t["shares"]),
            "sharesOutstanding": out_shares, "pctHeld": total / out_shares if out_shares else None,
            "source": "SEC Form 13F and Schedule 13D/13G"}


def desk_search(db, q, limit=10):
    q = q.strip().upper()
    if not q:
        return []
    rows = db.execute(
        "SELECT ticker, title FROM tickers WHERE ticker LIKE ? OR upper(title) LIKE ? "
        "ORDER BY (ticker = ?) DESC, (ticker LIKE ?) DESC, length(ticker) LIMIT ?",
        (q + "%", "%" + q + "%", q, q + "%", limit)).fetchall()
    return [{"symbol": r["ticker"], "name": r["title"], "meta": "SEC EDGAR"} for r in rows]


# --------------------------------------------------------------------------
# moomoo OpenD: price candles from the user's own moomoo account
# --------------------------------------------------------------------------
# OpenD is moomoo's gateway program. It runs on this computer, logged in to a moomoo account, and
# answers on a local socket. Each message is a 44-byte header and a body, and the body may be JSON,
# so the standard library is all this needs. Only market data is asked for: nothing here unlocks
# trading or can place an order.

OPEND_ADDR = ("127.0.0.1", 11111)
OPEND_HEAD = struct.Struct("<2sIBBII20s8s")   # "FT", protocol number, 1 = JSON body, version, serial, body length, SHA-1 of body, reserved
OPEND_OFFLINE = "moomoo OpenD is not running on this computer. Open the moomoo OpenD app, log in, and try again."
# market prefix -> (moomoo's market number, the exchange's time zone, minute of the day at which each session starts)
class _RuleZone(tzinfo):
    """A market's time zone worked out from its rules, for computers without a time zone database (Windows without
    the tzdata package). Exact for the zones below from 2007 on, which is all the chart needs."""
    def __init__(self, name, hours, rule):
        self.name, self.std, self.rule = name, timedelta(hours=hours), rule

    @staticmethod
    def _sunday(year, month, n):          # the n-th Sunday of a month
        d = date(year, month, 1)
        return d + timedelta(days=(6 - d.weekday()) % 7 + 7 * (n - 1))

    def _summer(self, utc):               # is daylight time in force at this UTC moment?
        y = utc.year
        if self.rule == "us":             # 2:00 local on the second Sunday of March to the first Sunday of November
            start = datetime.combine(self._sunday(y, 3, 2), datetime.min.time()) + timedelta(hours=2) - self.std
            end = datetime.combine(self._sunday(y, 11, 1), datetime.min.time()) + timedelta(hours=1) - self.std
            return start <= utc < end
        if self.rule == "au":             # 2:00 local on the first Sunday of October to 3:00 on the first Sunday of April
            end = datetime.combine(self._sunday(y, 4, 1), datetime.min.time()) + timedelta(hours=2) - self.std
            start = datetime.combine(self._sunday(y, 10, 1), datetime.min.time()) + timedelta(hours=2) - self.std
            return not (end <= utc < start)
        return False

    def fromutc(self, dt):
        u = dt.replace(tzinfo=None)
        return (u + self.std + (timedelta(hours=1) if self._summer(u) else timedelta(0))).replace(tzinfo=self)

    def utcoffset(self, dt):
        return self.std + self.dst(dt)

    def dst(self, dt):
        return timedelta(hours=1) if dt is not None and self._summer(dt.replace(tzinfo=None) - self.std) else timedelta(0)

    def tzname(self, dt):
        return self.name


_RULE_ZONES = {"America/New_York": (-5, "us"), "America/Toronto": (-5, "us"), "Australia/Sydney": (10, "au"),
               "Asia/Hong_Kong": (8, None), "Asia/Shanghai": (8, None), "Asia/Singapore": (8, None),
               "Asia/Kuala_Lumpur": (8, None), "Asia/Tokyo": (9, None)}


def market_zone(name):
    """The time zone of a market: from the computer's database, or worked out from its rules when there is none."""
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        hours, rule = _RULE_ZONES[name]
        return _RuleZone(name, hours, rule)


MOOMOO_MARKETS = {
    "US": (11, "America/New_York", (240, 570, 960, 1200)),   # pre-market 04:00, regular 09:30, after-hours 16:00, overnight 20:00
    "HK": (1, "Asia/Hong_Kong", (570, 780)),
    "SH": (21, "Asia/Shanghai", (570, 780)), "SZ": (22, "Asia/Shanghai", (570, 780)),
    "SG": (31, "Asia/Singapore", ()), "JP": (41, "Asia/Tokyo", ()), "AU": (51, "Australia/Sydney", ()),
    "MY": (61, "Asia/Kuala_Lumpur", ()), "CA": (71, "America/Toronto", ()),
}
TV_EXCHANGES = {"NASDAQ": "US", "NYSE": "US", "AMEX": "US", "ARCA": "US", "NYSEARCA": "US", "BATS": "US", "CBOE": "US",
                "HKEX": "HK", "SEHK": "HK", "SSE": "SH", "SZSE": "SZ", "SGX": "SG", "TSE": "JP", "ASX": "AU",
                "MYX": "MY", "TSX": "CA"}
# the page's interval -> (moomoo's candle type, minutes per candle, how many of them make one candle on the chart)
MOOMOO_KL = {
    "1": (1, 1, 1), "3": (10, 3, 1), "5": (6, 5, 1), "10": (12, 10, 1), "15": (7, 15, 1), "30": (8, 30, 1),
    "45": (7, 15, 3),      # moomoo has no 45-minute candle: three 15-minute ones
    "60": (9, 60, 1), "120": (13, 120, 1), "180": (14, 180, 1), "240": (15, 240, 1),
    "D": (2, 0, 1), "W": (3, 0, 1), "M": (4, 0, 1), "3M": (11, 0, 1),
    "6M": (11, 0, 2),      # nor a half-year one: two quarters
    "12M": (5, 0, 1),
}
MOOMOO_BARS = 1500         # about how many candles one request brings back
MOOMOO_TAIL_DAYS = {"D": 12, "W": 40, "M": 100, "3M": 200, "6M": 400, "12M": 800}   # how far back "the last few candles" reaches


class OpenDError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


class OpenD:
    """One short-lived connection to OpenD: connect, ask, close."""

    def __init__(self, addr=None, timeout=20):
        try:
            self.sock = socket.create_connection(addr or OPEND_ADDR, timeout=timeout)
        except OSError:
            raise OpenDError(OPEND_OFFLINE, 503)
        self.serial = 0
        self.call(1001, {"clientVer": 300, "clientID": "diligence", "recvNotify": False,
                         "packetEncAlgo": -1, "pushProtoFmt": 1, "programmingLanguage": "Python"})

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        try:
            self.sock.close()
        except OSError:
            pass

    def _read(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise OpenDError("moomoo OpenD closed the connection.")
            buf += chunk
        return buf

    def call(self, proto, c2s):
        """Send one request (moomoo's protocol number and its parameters) and return the reply's data."""
        _opend_turn(proto)
        self.serial += 1
        body = json.dumps({"c2s": c2s}).encode("utf-8")
        try:
            self.sock.sendall(OPEND_HEAD.pack(b"FT", proto, 1, 0, self.serial, len(body),
                                              hashlib.sha1(body).digest(), b"\0" * 8) + body)
            while True:
                _flag, pid, fmt, _ver, serial, n, _sha, _ = OPEND_HEAD.unpack(self._read(OPEND_HEAD.size))
                raw = self._read(n)
                if pid == proto and serial == self.serial:
                    break              # anything else is a notice OpenD pushes on its own
        except socket.timeout:
            raise OpenDError("moomoo OpenD did not answer in time.", 504)
        except OSError as e:
            raise OpenDError(f"The connection to moomoo OpenD failed: {e}")
        try:
            rsp = json.loads(raw.decode("utf-8")) if fmt == 1 else None
        except ValueError:
            rsp = None
        if rsp is None:
            raise OpenDError("moomoo OpenD sent a reply this server cannot read. If an encryption key is set in "
                             "OpenD's settings, remove it.")
        if rsp.get("retType") != 0:
            raise OpenDError(rsp.get("retMsg") or f"moomoo OpenD refused the request (code {rsp.get('retType')}).")
        return rsp.get("s2c") or {}


# moomoo refuses some kinds of request when there are too many in 30 seconds: wait for a turn instead
OPEND_LIMITS = {3227: 28, 3203: 55, 3213: 9, 3214: 9, 3222: 9}   # statements 30, snapshots 60, watchlists 10 per 30 seconds
_OPEND_SENT, _OPEND_SENT_LOCK = {}, threading.Lock()


def _opend_turn(proto):
    limit = OPEND_LIMITS.get(proto)
    while limit:
        with _OPEND_SENT_LOCK:
            now = time.time()
            sent = _OPEND_SENT[proto] = [t for t in _OPEND_SENT.get(proto, []) if now - t < 30]
            if len(sent) < limit:
                sent.append(now)
                return
            wait = 30.05 - (now - sent[0])
        time.sleep(wait)


def moomoo_security(symbol):
    """The market and code moomoo uses for a symbol typed the page's way, TradingView's way or moomoo's way.

    COST -> US COST,  BRK-B -> US BRK.B,  0700.HK -> HK 00700,  NYSE:TSM -> US TSM,  HK.00700 -> HK 00700
    """
    s = re.sub(r"\s", "", symbol or "").upper()
    m = re.fullmatch(r"(%s)\.(.+)" % "|".join(MOOMOO_MARKETS), s)
    if m:
        market, code = m.groups()
    elif ":" in s:
        exchange, code = s.split(":", 1)
        market = TV_EXCHANGES.get(exchange)
        if market is None:
            raise OpenDError(f"moomoo has no prices for the exchange {exchange}.", 404)
    elif re.search(r"\.(HK|SS|SH|SZ)$", s):
        code, suffix = s.rsplit(".", 1)
        market = {"SS": "SH"}.get(suffix, suffix)
    else:
        market, code = "US", s
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9._-]{0,19}", code):
        raise OpenDError(f"{symbol!r} is not a symbol moomoo can look up.", 404)
    if market == "HK" and code.isdigit():
        code = code.zfill(5)
    if market == "US":
        code = code.replace("-", ".")      # Berkshire's B shares are BRK-B here and BRK.B at moomoo
    return market, code


_MOOMOO_CACHE, _MOOMOO_LOCK = {}, threading.Lock()


def _moomoo_history(number, code, kl, begin, end, ext, live):
    """Candles as moomoo sends them, oldest first, for the dates given. Remembered briefly."""
    key = (number, code, kl, begin, end, ext)
    now = time.time()
    with _MOOMOO_LOCK:
        hit = _MOOMOO_CACHE.get(key)
        if hit and hit[0] > now:
            return hit[1], hit[2]
    req = {"rehabType": 1, "klType": kl, "security": {"market": number, "code": code},   # 1 = prices adjusted for splits and dividends
           "beginTime": begin.isoformat(), "endTime": end.isoformat(), "maxAckKLNum": 1000}
    if ext:
        req["extendedTime"] = True
    rows, name = [], ""
    with OpenD() as od:
        for _ in range(60):                # a thousand candles a page
            r = od.call(3103, req)
            rows += r.get("klList") or []
            name = r.get("name") or name
            if not r.get("nextReqKey"):
                break
            req["nextReqKey"] = r["nextReqKey"]
    with _MOOMOO_LOCK:
        if len(_MOOMOO_CACHE) > 200:
            for k in [k for k, v in _MOOMOO_CACHE.items() if v[0] <= now]:
                del _MOOMOO_CACHE[k]
            if len(_MOOMOO_CACHE) > 200:
                _MOOMOO_CACHE.clear()
        _MOOMOO_CACHE[key] = (now + (3 if live else 900), rows, name)   # a range that includes today is still changing
    return rows, name


def moomoo_candles(symbol, interval="D", ext=False, before=None, tail=False, everything=False):
    """Candles for the page's chart: {"bars": [[opening time in ms, open, high, low, close, volume], ...], "more": ...}.

    Without `before` the newest candles come back. With it (a time in ms), the stretch just before that time,
    which is how the chart asks for older history as it is scrolled back. `tail` is only the last few candles,
    for keeping a chart that is already drawn up to date. `everything` is all the history moomoo keeps.
    """
    market, code = moomoo_security(symbol)
    if interval not in MOOMOO_KL:
        raise OpenDError(f"{interval!r} is not an interval the chart uses.", 400)
    kl, minutes, fold = MOOMOO_KL[interval]
    number, tz, sessions = MOOMOO_MARKETS[market]
    zone = market_zone(tz)
    today = datetime.now(zone).date()
    ext = bool(ext) and minutes > 0 and market == "US"
    if minutes:        # moomoo keeps 8 years of candles of an hour or less
        days = int(MOOMOO_BARS * minutes * fold / (960 if ext else 390) * 7 / 5) + 4
        oldest = today - timedelta(days=8 * 365)
    elif interval == "D":   # and 20 years of daily ones
        days, oldest = int(MOOMOO_BARS * 7 / 5), today - timedelta(days=20 * 365)
    else:              # weeks and longer: everything at once
        days, oldest = 60000, date(1970, 1, 1)
    if tail:
        days, before = MOOMOO_TAIL_DAYS.get(interval, 4), None
    elif everything:
        days, before = 60000, None
    end = today if before is None else datetime.fromtimestamp(before / 1000, zone).date()
    begin = max(end - timedelta(days=days), oldest)
    rows, name = _moomoo_history(number, code, kl, begin, end, ext, end >= today)

    bars, keys = [], []
    prev_close = prev_day = None
    for k in rows:
        ts, when = k.get("timestamp"), k.get("time") or ""
        if ts is None:
            continue
        start = ts
        if minutes:
            # moomoo stamps a candle with the time it closes; charts show the time it opens. The last candle
            # of a session can be shorter than the rest (15:30 to 16:00 on an hourly chart).
            start = ts - minutes * 60
            if prev_close is not None and prev_day == when[:10] and prev_close > start:
                start = prev_close
            prev_close, prev_day = ts, when[:10]
        if k.get("isBlank") or k.get("closePrice") is None:
            continue
        bars.append([int(start * 1000), round(k["openPrice"], 4), round(k["highPrice"], 4), round(k["lowPrice"], 4),
                     round(k["closePrice"], 4), float(k.get("volume") or 0)])
        if fold > 1 and minutes:
            opened = datetime.strptime(when, "%Y-%m-%d %H:%M:%S") - timedelta(seconds=ts - start)
            minute = opened.hour * 60 + opened.minute
            session = max((b for b in sessions if b <= minute), default=0)
            keys.append((opened.date(), session, (minute - session) // (minutes * fold)))
        elif fold > 1:
            keys.append((when[:4], (int(when[5:7]) - 1) // (3 * fold)))
    if fold > 1:
        folded, last = [], None
        for b, key in zip(bars, keys):
            if key != last:
                folded.append(b)
                last = key
            else:
                a = folded[-1]
                a[2], a[3], a[4], a[5] = max(a[2], b[2]), min(a[3], b[3]), b[4], a[5] + b[5]
        bars = folded
    if before is not None:
        bars = [b for b in bars if b[0] < before]
    if tail:
        bars = bars[-40:]
    return {"symbol": re.sub(r"\s", "", symbol).upper(), "code": f"{market}.{code}", "name": name, "interval": interval,
            "tz": tz, "ext": ext, "bars": bars, "more": bool(bars) and not tail and begin > oldest, "source": "moomoo"}


def moomoo_valuation(symbol, kind="pe"):
    """A valuation ratio day by day, as far back as moomoo has it (up to 30 years): {"points": [["2026-10-09", 45.61], ...]}.

    For "pe" this is price over the earnings per share of the twelve months reported by that day, so it steps
    when results come out. A negative figure means the twelve months were a loss. "peg" is worked out here from the
    P/E and moomoo's income statements: see moomoo_peg.
    """
    if kind == "peg":
        return moomoo_peg(symbol)
    market, code = moomoo_security(symbol)
    number, tz, _sessions = MOOMOO_MARKETS[market]
    kinds = {"pe": 1, "pb": 2, "ps": 3}
    if kind not in kinds:
        raise OpenDError(f"{kind!r} is not a valuation ratio moomoo has.", 400)
    key, now = ("valuation", number, code, kind), time.time()
    with _MOOMOO_LOCK:
        hit = _MOOMOO_CACHE.get(key)
        if hit and hit[0] > now:
            return hit[1]
    with OpenD() as od:
        r = od.call(3232, {"security": {"market": number, "code": code}, "valuationType": kinds[kind], "intervalType": 10})
    trend = r.get("trend") or {}
    points = [[x["timeStr"], round(x["value"], 3)] for x in trend.get("historicalItems") or []
              if x.get("timeStr") and x.get("value") is not None] if r.get("valuationType") == kinds[kind] else []
    out = {"symbol": re.sub(r"\s", "", symbol).upper(), "code": f"{market}.{code}", "kind": kind, "tz": tz, "points": points,
           "current": trend.get("currentValue"), "average": trend.get("averageValue"), "forward": trend.get("forwardValue"),
           "percentile": trend.get("valuationPercentile"), "updated": r.get("lastUpdateTimeStr"), "source": "moomoo"}
    with _MOOMOO_LOCK:
        _MOOMOO_CACHE[key] = (now + 300, out)
    return out


# moomoo's kinds of income statement that run from the start of the fiscal year: first quarter, half year, nine months, full year
MOOMOO_CUMULATIVE = (1, 5, 6, 7)


def _moomoo_eps_growth(od, sec):
    """How fast earnings per share were growing after each set of results: [[first day it applies, growth in %], ...], oldest first.

    The growth is the EPS of the twelve months to the end of the period over the twelve months before those. It is
    None when it cannot be worked out: a figure is missing, or the earlier twelve months were not a profit.
    """
    cum, ends = {}, {}                 # (fiscal year, kind) -> EPS from the start of that year, and the day the period ended
    for kind in MOOMOO_CUMULATIVE:
        try:
            income = od.call(3227, {"security": sec, "statementType": 1, "financialType": kind, "num": 50})
        except OpenDError:
            continue
        names = {x.get("fieldId"): x.get("displayName") or "" for x in income.get("structureList") or []}
        for rep in income.get("reportList") or []:
            vals = {}
            for it in rep.get("itemList") or []:
                vals.setdefault(names.get(it.get("fieldId")) or it.get("displayName") or "", it.get("data"))
            eps = vals.get("Diluted EPS") if vals.get("Diluted EPS") is not None else vals.get("Basic EPS")
            key = (rep.get("fiscalYear"), kind)
            if key[0] and re.fullmatch(r"\d{4}-\d\d-\d\d", rep.get("dateTimeStr") or ""):
                ends[key] = date.fromisoformat(rep["dateTimeStr"])
                if eps is not None:
                    cum[key] = eps

    def ttm(year, kind):               # the twelve months to the end of a period: last full year, plus this year so far, less the same stretch of last year
        if kind == 7:
            return cum.get((year, 7))
        parts = cum.get((year - 1, 7)), cum.get((year, kind)), cum.get((year - 1, kind))
        return None if None in parts else parts[0] + parts[1] - parts[2]

    # the day each set of results came out. moomoo knows it for the last five years; before that, the usual wait
    # after the period ended stands in, so an old step can be a few days off.
    try:
        known = od.call(3226, {"security": sec}).get("detailList") or []
    except OpenDError:
        known = []
    out_on, waits = {}, {}
    for x in known:
        key = (x.get("fiscalYear"), {1: 1, 2: 5, 3: 6, 4: 7}.get(x.get("financialType")))
        if key in ends and re.fullmatch(r"\d{4}-\d\d-\d\d", x.get("pubTradingDayStr") or ""):
            day = date.fromisoformat(x["pubTradingDayStr"])
            # results that came out before the open count from that day, any others from the next
            out_on[key] = day if x.get("pubType") == 1 else day + timedelta(days=1)
            waits.setdefault(key[1], []).append((out_on[key] - ends[key]).days)
    steps = []
    for key in sorted(ends, key=lambda k: ends[k]):
        year, kind = key
        wait = sorted(waits[kind])[len(waits[kind]) // 2] if waits.get(kind) else (76 if kind == 7 else 46)
        now, before = ttm(year, kind), ttm(year - 1, kind)
        growth = round((now / before - 1) * 100, 2) if now is not None and before is not None and before > 0 else None
        steps.append([(out_on.get(key) or ends[key] + timedelta(days=wait)).isoformat(), growth])
    steps.sort(key=lambda x: x[0])
    return steps


def moomoo_peg(symbol):
    """The PEG ratio day by day: {"points": [["2026-10-09", 0.95, 31.6], ...]}, each the day, the PEG and the growth behind it.

    PEG is the P/E of the last twelve months over the growth, in percent, of those twelve months' EPS on the twelve
    months before. It is None on the days it means nothing: the twelve months were a loss, or earnings did not grow
    (the growth is then nought or less), or the growth is not known (None as well).
    """
    pe = moomoo_valuation(symbol, "pe")
    market, code = moomoo_security(symbol)
    number = MOOMOO_MARKETS[market][0]
    key, now = ("peg", number, code), time.time()
    with _MOOMOO_LOCK:
        hit = _MOOMOO_CACHE.get(key)
        if hit and hit[0] > now:
            return hit[1]
        steps = _MOOMOO_CACHE.get(("epsgrowth", number, code))
    if not steps or steps[0] <= now:
        with OpenD() as od:
            od.call(1002, {"userID": 0})
            steps = (now + 1800, _moomoo_eps_growth(od, {"market": number, "code": code}))
        with _MOOMOO_LOCK:
            _MOOMOO_CACHE[("epsgrowth", number, code)] = steps
    steps, points, i = steps[1], [], 0
    for day, ratio in pe["points"]:
        while i < len(steps) and steps[i][0] <= day:
            i += 1
        if i:
            growth = steps[i - 1][1]
            points.append([day, round(ratio / growth, 2) if ratio > 0 and growth and growth > 0 else None, growth])
    out = {"symbol": pe["symbol"], "code": pe["code"], "kind": "peg", "tz": pe["tz"], "points": points,
           "current": points[-1][1] if points else None, "growth": points[-1][2] if points else None,
           "updated": pe.get("updated"), "source": "moomoo"}
    with _MOOMOO_LOCK:
        _MOOMOO_CACHE[key] = (now + 300, out)
    return out


def moomoo_symbol(market_no, code):
    """moomoo's market number and code back to the page's symbol: (11, 'BRK.B') -> BRK-B, (1, '00700') -> 0700.HK,
    (1, '09988') -> 9988.HK, (1, '89988') -> 89988.HK."""
    if market_no == 11 and re.fullmatch(r"[A-Z]{1,6}(\.[A-Z]{1,2})?", code or ""):
        return code.replace(".", "-")
    if market_no == 1 and re.fullmatch(r"\d{5}", code or ""):
        return (code.lstrip("0") or "0").zfill(4) + ".HK"
    return None        # options, futures, funds and other markets are left out


def moomoo_quotes(symbols):
    """Live prices for a list of symbols: {"COST": {"price", "prev", "chg", "pct", "name", "ext": {"label", "price", "pct"}}}.

    `ext` is the latest trade outside regular hours (pre-market, after hours or overnight), when there is one.
    """
    wanted = {}
    for sym in symbols[:200]:
        try:
            market, code = moomoo_security(sym)
        except OpenDError:
            continue
        wanted[(MOOMOO_MARKETS[market][0], code)] = sym
    if not wanted:
        return {"quotes": {}, "session": None}
    with OpenD() as od:
        state = od.call(1002, {"userID": 0}).get("marketUS")
        snaps = od.call(3203, {"securityList": [{"market": m, "code": c} for m, c in wanted]}).get("snapshotList") or []
    # which out-of-hours figure is the current one for the US market right now
    which = {8: ("preMarket", "Pre-market"), 9: ("preMarket", "Pre-market"), 10: ("afterMarket", "After hours"),
             11: ("afterMarket", "After hours"), 13: ("overnight", "Overnight"), 14: ("overnight", "Overnight")}.get(state)
    out = {}
    for x in snaps:
        b = x.get("basic") or {}
        sec = b.get("security") or {}
        sym = wanted.get((sec.get("market"), sec.get("code")))
        price, prev = b.get("curPrice"), b.get("lastClosePrice")
        if sym is None or not price:
            continue
        q = {"price": price, "prev": prev, "chg": round(price - prev, 4) if prev else None,
             "pct": round((price / prev - 1) * 100, 3) if prev else None, "name": b.get("name"), "time": b.get("updateTime")}
        ext = b.get(which[0]) if which and sec.get("market") == 11 else None
        if ext and ext.get("price") and abs(ext["price"] - price) > 1e-9:
            q["ext"] = {"label": which[1], "price": ext["price"], "pct": round((ext["price"] / price - 1) * 100, 3)}
        out[sym] = q
    return {"quotes": out, "session": {5: "open", 8: "pre", 9: "pre", 10: "after", 11: "after", 13: "overnight"}.get(state, "closed")}


_EARNINGS = {"at": 0, "by": {}}


def _moomoo_earnings(od):
    """Announced US earnings dates for the next 14 weeks, by code. moomoo gives a week of the market at a time."""
    now = time.time()
    with _MOOMOO_LOCK:
        if _EARNINGS["by"] and now - _EARNINGS["at"] < 6 * 3600:
            return _EARNINGS["by"]
    by, today = {}, datetime.now(market_zone("America/New_York")).date()
    for w in range(14):
        b = today + timedelta(days=7 * w)
        r = od.call(3401, {"market": 11, "sortType": 2, "beginDate": b.isoformat(), "endDate": (b + timedelta(days=6)).isoformat()})
        for x in r.get("itemList") or []:
            code = (x.get("security") or {}).get("code")
            if code and code not in by and x.get("earningsDate"):
                est = {e.get("estimateType"): e.get("predictValue") for e in x.get("estimateList") or []}
                by[code] = {"date": x["earningsDate"], "when": {1: "before the open", 2: "after the close"}.get(x.get("pubType"), ""),
                            "period": x.get("periodText"), "eps": est.get(1), "revenue": est.get(2)}
    with _MOOMOO_LOCK:
        _EARNINGS["at"], _EARNINGS["by"] = now, by
    return by


def moomoo_insight(symbol):
    """What the market thinks of a stock, from moomoo: analysts' price targets, short interest, and the earnings dates."""
    market, code = moomoo_security(symbol)
    number = MOOMOO_MARKETS[market][0]
    key, now = ("insight", number, code), time.time()
    with _MOOMOO_LOCK:
        hit = _MOOMOO_CACHE.get(key)
        if hit and hit[0] > now:
            return hit[1]
    sec, out = {"market": number, "code": code}, {"symbol": re.sub(r"\s", "", symbol).upper(), "source": "moomoo"}

    def ask(proto, c2s):
        try:
            return od.call(proto, c2s)
        except OpenDError:
            return {}
    with OpenD() as od:
        od.call(1002, {"userID": 0})                 # fails here, loudly, if OpenD is not logged in
        a = ask(3229, {"security": sec})
        if a.get("total"):
            out["analysts"] = {"count": a.get("total"), "average": a.get("average"), "high": a.get("highest"), "low": a.get("lowest"),
                               "buy": a.get("buy"), "hold": a.get("hold"), "sell": a.get("sell"), "updated": a.get("updateTimeStr")}
        rows = ask(3249, {"security": sec, "num": 2}).get("usItemList") or []
        if rows:
            f = lambda r: {"date": r.get("timestampStr"), "shares": float(r.get("sharesShort") or 0), "percent": r.get("shortPercent"),
                           "daysToCover": r.get("daysToCover")}
            out["short"] = dict(f(rows[0]), previous=f(rows[1]) if len(rows) > 1 else None)
        past = ask(3225, {"security": sec, "periodCount": 1}).get("detailList") or []
        earnings = {}
        if past and past[0].get("pubTradingDayStr"):
            p0, i = past[0], past[0].get("priceInfoIndex", -1)
            days = p0.get("itemList") or []
            move = None
            if 0 <= i < len(days) and days[i].get("closePrice") and days[i].get("lastClosePrice"):
                # results after the close move the price the next day
                d = days[i + 1] if p0.get("pubType") == 2 and i + 1 < len(days) else days[i]
                if d.get("closePrice") and d.get("lastClosePrice"):
                    move = round((d["closePrice"] / d["lastClosePrice"] - 1) * 100, 2)
            earnings["last"] = {"date": p0["pubTradingDayStr"], "period": p0.get("periodText"), "move": move}
        if market == "US":
            nxt = _moomoo_earnings(od).get(code)
            if nxt:
                earnings["next"] = nxt
        if earnings:
            out["earnings"] = earnings
    with _MOOMOO_LOCK:
        _MOOMOO_CACHE[key] = (now + 1800, out)
    return out


def moomoo_events(symbol):
    """Earnings announcements (with the EPS and revenue reported) and dividends, to pin on the chart."""
    market, code = moomoo_security(symbol)
    number = MOOMOO_MARKETS[market][0]
    key, now = ("events", number, code), time.time()
    with _MOOMOO_LOCK:
        hit = _MOOMOO_CACHE.get(key)
        if hit and hit[0] > now:
            return hit[1]
    sec = {"market": number, "code": code}

    def ask(od, proto, c2s):
        try:
            return od.call(proto, c2s)
        except OpenDError:
            return {}
    with OpenD() as od:
        od.call(1002, {"userID": 0})
        periods = ask(od, 3226, {"security": sec}).get("detailList") or []
        income = ask(od, 3227, {"security": sec, "statementType": 1, "financialType": 10, "num": 50})
        payouts = ask(od, 3234, {"security": sec}).get("dividendList") or []
    names = {x.get("fieldId"): x.get("displayName") or "" for x in income.get("structureList") or []}
    figures = {}                       # "2026/Q4" -> {"eps", "revenue", "currency"}
    for rep in income.get("reportList") or []:
        vals = {}
        for it in rep.get("itemList") or []:
            vals.setdefault(names.get(it.get("fieldId")) or it.get("displayName") or "", it.get("data"))
        pick = lambda *wanted: next((vals[n] for n in wanted if vals.get(n) is not None), None)
        figures[rep.get("periodText")] = {
            "eps": pick("Diluted EPS", "Basic EPS"),
            "revenue": pick("Total Operating Revenue", "Operating Revenue", "Total Revenue", "Total Revenue as Reported"),
            "currency": rep.get("currencyCode")}
    earnings = []
    for x in periods:
        if not x.get("pubTradingDayStr"):
            continue
        label = x.get("periodText") or ""
        parts = [dict(figures.get(label, {}), period=label)]
        if label.endswith("/Q4") and label.replace("/Q4", "/FY") in figures:     # the fourth quarter comes out with the full year
            parts.append(dict(figures[label.replace("/Q4", "/FY")], period=label.replace("/Q4", "/FY")))
        earnings.append({"date": x["pubTradingDayStr"], "time": (x.get("pubTimeStr") or "")[11:16],
                         "when": {1: "before the open", 2: "after the close"}.get(x.get("pubType"), ""), "parts": parts})
    iso = lambda d: "-".join((d[6:10], d[0:2], d[3:5])) if re.fullmatch(r"\d\d/\d\d/\d{4}", d or "") else None   # 08/27/2026
    dividends = [{"date": iso(d.get("exDate")), "text": d.get("statement"), "pay": iso(d.get("dividendPayableDate")),
                  "record": iso(d.get("recordDate")), "announced": iso(d.get("pubDate"))} for d in payouts if iso(d.get("exDate"))]
    out = {"symbol": re.sub(r"\s", "", symbol).upper(), "earnings": earnings, "dividends": dividends, "source": "moomoo"}
    with _MOOMOO_LOCK:
        _MOOMOO_CACHE[key] = (now + 1800, out)
    return out


def moomoo_lists():
    """The user's moomoo watchlist groups, and whether positions can be read."""
    with OpenD() as od:
        groups = od.call(3222, {"groupType": 3}).get("groupList") or []
        try:
            accounts = [a for a in od.call(2001, {"userID": 0, "needGeneralSecAccount": True}).get("accList") or [] if a.get("trdEnv") == 1]
        except OpenDError:
            accounts = []
    # your own lists, and Favorites (moomoo's list that you fill yourself); moomoo's other built-in groups (All, US, HK,
    # Options...) are automatic filters of All by market or kind, not lists
    return {"groups": [{"name": g["groupName"], "own": g.get("groupType") == 1} for g in groups
                       if g.get("groupType") == 1 or g.get("groupName") == "Favorites"],
            "positions": bool(accounts)}


def moomoo_list(name):
    """The stocks in one moomoo watchlist group, as the page's symbols, in moomoo's order."""
    with OpenD() as od:
        rows = od.call(3213, {"groupName": name}).get("staticInfoList") or []
    out = []
    for r in rows:
        b = r.get("basic") or {}
        sec = b.get("security") or {}
        sym = moomoo_symbol(sec.get("market"), sec.get("code")) if b.get("secType") in (3, 4) else None   # shares and funds that trade like shares
        if sym and sym not in out:
            out.append(sym)
    return {"name": name, "symbols": out}


def moomoo_list_change(name, add=(), remove=()):
    """Add stocks to, or take them out of, one of the user's own moomoo watchlist groups, as the moomoo app does:
    taking a stock out of a group leaves it in All. moomoo's built-in groups (All, US, Favorites...) can only be changed
    in the moomoo app. Returns the group as it is afterwards. Only the watchlist is touched: nothing is traded."""
    def secs(symbols):
        out = []
        for sym in symbols:
            market, code = moomoo_security(sym)
            out.append({"market": MOOMOO_MARKETS[market][0], "code": code})
        return out
    with OpenD() as od:
        own = {g["groupName"] for g in od.call(3222, {"groupType": 1}).get("groupList") or []}
        if name not in own:
            raise OpenDError(f"\u201c{name}\u201d is one of moomoo's built-in lists, which only the moomoo app can change.", 400)
        if add:
            od.call(3214, {"groupName": name, "op": 1, "securityList": secs(add)})
        if remove:
            od.call(3214, {"groupName": name, "op": 3, "securityList": secs(remove)})   # 3: out of this group only
    return moomoo_list(name)


def moomoo_positions():
    """The stocks held in the user's real moomoo accounts: symbols only. Nothing is unlocked and nothing can be traded."""
    out = []
    with OpenD() as od:
        for a in od.call(2001, {"userID": 0, "needGeneralSecAccount": True}).get("accList") or []:
            if a.get("trdEnv") != 1:           # 1 is a real account; the others are paper trading
                continue
            for mkt in a.get("trdMarketAuthList") or []:
                try:
                    rows = od.call(2102, {"header": {"trdEnv": 1, "accID": a["accID"], "trdMarket": mkt}}).get("positionList") or []
                except OpenDError:
                    continue               # an account that needs something this does not send (crypto asks for a currency)
                for p in rows:
                    sym = moomoo_symbol({1: 1, 2: 11}.get(p.get("secMarket")), p.get("code"))
                    if sym and (p.get("qty") or 0) > 0 and sym not in out:
                        out.append(sym)
    return {"name": "Positions", "symbols": out}


def moomoo_status():
    """Whether OpenD is running and logged in, and how much of the weekly candle-history allowance is used."""
    with OpenD() as od:
        g = od.call(1002, {"userID": 0})
        try:
            q = od.call(3104, {"bGetDetail": False})
        except OpenDError:
            q = {}
    return {"ok": True, "version": g.get("serverVer"), "quotes": bool(g.get("qotLogined")),
            "used": q.get("usedQuota"), "remaining": q.get("remainQuota")}


# --------------------------------------------------------------------------
# Local web server: the Diligence desk page plus a JSON API
# --------------------------------------------------------------------------

LOCAL_NAMES = {"localhost", "127.0.0.1", "::1"}


def _is_local(value, schemes=None):
    """True when a Host header ("localhost:8765") or an address ("http://127.0.0.1:3000") names this computer."""
    try:
        u = urlparse(value if "//" in value else "//" + value)
        return (u.hostname or "") in LOCAL_NAMES and (schemes is None or u.scheme in schemes)
    except ValueError:
        return False


def make_handler(client, max_age_hours=12):
    locks, locks_guard = {}, threading.Lock()

    jobs, jobs_guard = {}, threading.Lock()

    def background(key, work):
        """Run work(db) on its own thread and database connection, once per key at a time. Returns the thread."""
        with jobs_guard:
            t = jobs.get(key)
            if t and t.is_alive():
                return t
            def run():
                bdb = connect()
                try:
                    work(bdb)
                except Exception as e:    # a failed refresh leaves the saved copy in place
                    sys.stderr.write(f"  background {key}: {e!r}\n")
                finally:
                    bdb.close()
            t = jobs[key] = threading.Thread(target=run, daemon=True)
            t.start()
            return t

    def refresh(bdb, key):
        """Download a company again, while the page keeps showing the saved copy."""
        with locks_guard:
            lock = locks.setdefault(key, threading.Lock())
        with lock:
            cik = fetch_company(bdb, client, key, docs=1, insiders=40, log=lambda *_: None)
            fetch_debt_split(bdb, client, cik)

    def ensure(db, ticker, force=False, segments=False, insiders=0):
        """Return the CIK, fetching from the SEC if the company is missing. A saved company that is out of
        date is answered from what is saved and downloaded again in the background."""
        key = ticker.strip().upper()
        quiet = lambda *_: None
        with locks_guard:
            lock = locks.setdefault(key, threading.Lock())
        with lock:
            cik = lookup_cik(db, key)
            fresh = False
            if cik is not None and not force:
                row = db.execute("SELECT fetched_at FROM companies WHERE cik=?", (cik,)).fetchone()
                age = datetime.now(timezone.utc) - datetime.fromisoformat(row["fetched_at"])
                fresh = client is None or age < timedelta(hours=max_age_hours)
                if not fresh:
                    background("refresh:" + key, lambda bdb: refresh(bdb, key))
                    return cik
            if not fresh:
                if client is None:
                    raise SystemExit(f"{ticker} is not in the database.")
                cik = fetch_company(db, client, key, docs=1, insiders=40, log=quiet, statements=(2, 4))
                def more_statements(bdb, cik=cik):
                    if fetch_statement_lines(bdb, client, cik, log=quiet):
                        with bdb:
                            build_metrics(bdb, cik)
                background(f"statements:{cik}", more_statements)
            if client is not None:
                fetch_debt_split(db, client, cik)  # only for companies with no debt total in quarterly reports
            if insiders and client is not None:
                fetch_insiders(db, client, cik, insiders, log=quiet)  # only reads filings it hasn't seen
            if segments and client is not None:
                fetch_segments(db, client, cik, log=quiet)  # only reads reports it hasn't seen
            return cik

    class Handler(BaseHTTPRequestHandler):
        def handle(self):
            try:
                super().handle()
            except ConnectionError:  # browser closed the connection mid-response
                pass

        cors = None   # the localhost page allowed to read this reply, set by _refused()

        def _refused(self, path):
            """Only this computer may use the server. Another website open in the browser must not read your
            holdings, reports or keys, or start downloads, and a site that points its own name at 127.0.0.1
            (DNS rebinding) is turned away by the Host check. Returns why a request is refused, or None."""
            self.cors = None
            if not _is_local(self.headers.get("Host") or ""):
                return "This server only answers requests addressed to localhost."
            origin = self.headers.get("Origin")
            if origin:   # sent by pages making fetch() calls to another address, and with every POST
                if not _is_local(origin, ("http", "https")):
                    return "Only pages on this computer (localhost) may use this server."
                self.cors = origin
                return None
            # No Origin: a script, image or link. Browsers say where it came from in Sec-Fetch-Site; older ones
            # only in Referer.
            site, referer = self.headers.get("Sec-Fetch-Site"), self.headers.get("Referer")
            cross = site == "cross-site" if site else bool(referer) and not _is_local(referer)
            if cross and not (self.headers.get("Sec-Fetch-Mode") == "navigate" and not path.startswith("/api/")):
                return "Only pages on this computer (localhost) may use this server."
            return None

        def _send(self, status, payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            if self.cors:
                self.send_header("Access-Control-Allow-Origin", self.cors)
            self.send_header("Vary", "Origin")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _eodhd(self, path, query):
            """Pass a request on to EODHD. Its replies lack the header browsers need, so the page asks through here."""
            if not re.fullmatch(r"(real-time|eod)/[A-Za-z0-9._-]+|news", path):
                return self._send(404, {"error": "not an EODHD address the page uses"})
            req = urllib.request.Request(f"https://eodhd.com/api/{path}?{query}", headers={"User-Agent": "Diligence"})
            try:
                with urllib.request.urlopen(req, timeout=25) as r:
                    status, body = r.status, r.read()
            except urllib.error.HTTPError as e:
                status, body = e.code, e.read()
            except (urllib.error.URLError, OSError) as e:
                return self._send(502, {"error": f"EODHD could not be reached: {e}"})
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                if self.cors:
                    self.send_header("Access-Control-Allow-Origin", self.cors)
                self.send_header("Vary", "Origin")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except ConnectionError:
                pass

        def _moomoo(self, parts, qs):
            """Price candles from the moomoo OpenD gateway running on this computer."""
            arg = lambda k, d="": (qs.get(k) or [d])[0]
            try:
                if parts == ["status"]:
                    return self._send(200, moomoo_status())
                if len(parts) == 2 and parts[0] == "kline":
                    before = arg("before")
                    return self._send(200, moomoo_candles(unquote(parts[1]), arg("k", "D"), arg("ext") == "1",
                                                          int(before) if before.isdigit() else None, arg("tail") == "1",
                                                          arg("all") == "1"))
                if len(parts) == 2 and parts[0] == "valuation":
                    return self._send(200, moomoo_valuation(unquote(parts[1]), arg("kind", "pe")))
                if parts == ["quotes"]:
                    return self._send(200, moomoo_quotes([x for x in arg("symbols").split(",") if x]))
                if len(parts) == 2 and parts[0] == "insight":
                    return self._send(200, moomoo_insight(unquote(parts[1])))
                if len(parts) == 2 and parts[0] == "events":
                    return self._send(200, moomoo_events(unquote(parts[1])))
                if parts == ["lists"]:
                    return self._send(200, moomoo_lists())
                if parts == ["list"]:
                    return self._send(200, moomoo_list(arg("name")))
                if parts == ["positions"]:
                    return self._send(200, moomoo_positions())
                return self._send(404, {"error": "unknown path", "try": ["/api/moomoo/status", "/api/moomoo/kline/MSFT?k=D",
                                                                         "/api/moomoo/valuation/MSFT"]})
            except OpenDError as e:
                return self._send(e.status, {"error": str(e)})
            except ConnectionError:    # browser gave up waiting
                pass
            except Exception as e:     # keep the server alive
                return self._send(500, {"error": repr(e)})

        def _static(self, rel):
            if unquote(rel) in OLD_DESK_PAGES:   # an old bookmark
                self.send_response(301)
                self.send_header("Location", "/desk/" + quote(DESK_PAGE) + ("?" + urlparse(self.path).query if urlparse(self.path).query else ""))
                self.end_headers()
                return
            base = os.path.realpath(DESK_DIR)
            path = os.path.realpath(os.path.join(base, unquote(rel)))
            if not path.startswith(base + os.sep) or not os.path.isfile(path):
                return self._send(404, {"error": "not found"})
            with open(path, "rb") as f:
                body = f.read()
            ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
            if path.endswith(".html"):
                ctype = "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urlparse(self.path)
            why = self._refused(url.path)
            if why:
                return self._send(403, {"error": why})
            qs = parse_qs(url.query)
            parts = [p for p in url.path.split("/") if p]
            if not parts:
                self.send_response(302)
                self.send_header("Location", "/desk/" + quote(DESK_PAGE))
                self.end_headers()
                return
            if parts[0] == "desk":
                return self._static("/".join(parts[1:]))
            if parts in (["favicon.ico"], ["apple-touch-icon.png"], ["apple-touch-icon-precomposed.png"]):
                return self._static("favicon.png" if parts[0] == "favicon.ico" else "favicon-180.png")
            if parts[:3] == ["api", "proxy", "eodhd"]:
                return self._eodhd("/".join(parts[3:]), url.query)
            if parts[:2] == ["api", "moomoo"]:
                return self._moomoo(parts[2:], qs)
            db = connect()
            try:
                force = bool(qs.get("refresh"))
                if parts == ["api", "health"]:
                    n = db.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
                    return self._send(200, {"ok": True, "companies": n, "sec": client is not None})
                if parts == ["api", "companies"]:
                    rows = db.execute("SELECT cik, ticker, name, industry, fetched_at FROM companies "
                                      "ORDER BY ticker").fetchall()
                    return self._send(200, [dict(r) for r in rows])
                if len(parts) == 3 and parts[:2] == ["api", "desk"] and parts[2] == "search":
                    if client is not None:
                        refresh_tickers(db, client)
                    return self._send(200, desk_search(db, (qs.get("q") or [""])[0]))
                if len(parts) == 4 and parts[:2] == ["api", "desk"]:
                    kind, ticker = parts[2], parts[3]
                    fn = {"fundamentals": desk_fundamentals, "quarters": desk_quarters}.get(kind)
                    if fn:
                        return self._send(200, fn(db, ensure(db, ticker, force)))
                    if kind == "insiders":
                        cik = ensure(db, ticker, force, insiders=40)
                        years = min(float((qs.get("years") or ["0"])[0] or 0), 20)
                        if years and client is not None:
                            # A long history is hundreds of filings; read it outside the company lock
                            # so the statements stay available meanwhile.
                            since = (date.today() - timedelta(days=round(years * 365.25))).isoformat()
                            with locks_guard:
                                lock = locks.setdefault("insiders:" + ticker.strip().upper(), threading.Lock())
                            with lock:
                                fetch_insiders(db, client, cik, log=lambda *_: None, since=since)
                        return self._send(200, desk_insiders(db, cik))
                    if kind == "holders":
                        cik = ensure(db, ticker, force)
                        out_of_date = False
                        if client is not None:
                            # the first time this downloads the SEC's 13F data sets (about 200 MB): never inside
                            # the request. Wait a few seconds, then answer with what is known and let the page ask again.
                            def holders_work(bdb, cik=cik):
                                fetch_major_holders(bdb, client, cik, log=lambda *_: None)   # first: its CUSIPs
                                fetch_holders(bdb, client, cik, log=lambda *_: None)
                            job = background(f"holders:{cik}", holders_work)
                            job.join(8)
                            out_of_date = job.is_alive()
                        out = desk_holders(db, cik)
                        out["updating"] = out_of_date
                        have = [f for f in os.listdir(F13_DIR) if f.endswith(".zip")] if os.path.isdir(F13_DIR) else []
                        out["downloading13f"] = out_of_date and len(have) < F13_KEEP
                        return self._send(200, out)
                    if kind == "statements":
                        return self._send(200, desk_statements(db, ensure(db, ticker, force)))
                    if kind == "segments":
                        cik = ensure(db, ticker, force, segments=True)
                        if (qs.get("period") or [""])[0] != "q":
                            return self._send(200, desk_segments(db, cik))
                        if client is not None:
                            # dozens of quarterly reports: read them outside the company lock
                            with locks_guard:
                                lock = locks.setdefault("segq:" + ticker.strip().upper(), threading.Lock())
                            with lock:
                                fetch_segments(db, client, cik, log=lambda *_: None, quarterly=True)
                        return self._send(200, desk_segments_quarterly(db, cik))
                if len(parts) == 3 and parts[:2] == ["api", "company"]:
                    return self._send(200, company_payload(db, ensure(db, parts[2], force)))
                if len(parts) == 4 and parts[:2] == ["api", "company"] and parts[3] == "facts":
                    cik = lookup_cik(db, parts[2])
                    concept = (qs.get("concept") or [""])[0]
                    rows = db.execute("SELECT * FROM facts WHERE cik=? AND concept=? ORDER BY period_end",
                                      (cik, concept)).fetchall()
                    return self._send(200, [dict(r) for r in rows])
                if len(parts) == 4 and parts[:2] == ["api", "company"] and parts[3] == "section":
                    cik = lookup_cik(db, parts[2])
                    sec = (qs.get("name") or ["1A"])[0].upper()
                    row = db.execute("SELECT * FROM documents WHERE cik=? AND upper(section)=? "
                                     "ORDER BY filing_date DESC LIMIT 1", (cik, sec)).fetchone()
                    return self._send(200 if row else 404, dict(row) if row else {"error": "not found"})
                if parts == ["api", "macro", "status"]:
                    return self._send(200, macro_status())
                if parts == ["api", "macro", "series"]:
                    return self._send(200, macro_series(db, (qs.get("id") or [""])[0]))
                if parts == ["api", "macro", "batch"]:
                    return self._send(200, macro_batch((qs.get("ids") or [""])[0].split(",")))
                if parts == ["api", "macro", "search"]:
                    return self._send(200, macro_search((qs.get("q") or [""])[0]))
                if parts == ["api", "themes"]:
                    return self._send(200, themes_list())
                if parts == ["api", "themes", "one"]:
                    try:
                        return self._send(200, theme_get((qs.get("where") or [""])[0], (qs.get("name") or [""])[0]))
                    except ValueError as e:
                        return self._send(404, {"error": str(e)})
                if parts == ["api", "reports"]:
                    return self._send(200, reports_for(db, (qs.get("ticker") or [""])[0]))
                if parts == ["api", "reports", "file"]:
                    doc = report_text((qs.get("path") or [""])[0])
                    return self._send(200 if doc else 404, doc or {"error": "report not found"})
                if parts == ["api", "desk", "notes"]:
                    return self._send(200, notes_get())
                if parts == ["api", "desk", "footprint"]:
                    arg = lambda k: [x for x in (qs.get(k) or [""])[0].split(",") if x]
                    return self._send(200, stock_footprint(db, arg("tickers")[:200], arg("keep")))
                if parts == ["api", "settings", "sources"]:
                    return self._send(200, data_sources(client))
                if parts == ["api", "search"]:
                    q = (qs.get("q") or [""])[0]
                    t = (qs.get("ticker") or [None])[0]
                    try:
                        return self._send(200, search_docs(db, q, t))
                    except LookupError as e:
                        return self._send(404, {"error": str(e)})
                self._send(404, {"error": "unknown path", "try": [
                    "/", "/api/health", "/api/companies", "/api/company/MSFT",
                    "/api/company/MSFT?refresh=1", "/api/desk/fundamentals/MSFT",
                    "/api/desk/quarters/MSFT", "/api/desk/segments/MSFT", "/api/desk/insiders/MSFT",
                    "/api/desk/holders/MSFT", "/api/desk/statements/MSFT",
                    "/api/desk/search?q=micro",
                    "/api/company/MSFT/facts?concept=Revenues", "/api/company/MSFT/section?name=1A",
                    "/api/search?q=competition&ticker=MSFT", "/api/reports?ticker=MSFT", "/api/moomoo/status", "/api/moomoo/kline/MSFT?k=D"]})
            except ConnectionError:  # browser gave up waiting; nobody to answer
                pass
            except SystemExit as e:
                self._send(404, {"error": str(e)})
            except OpenDError as e:  # moomoo OpenD is not running
                self._send(e.status, {"error": str(e)})
            except Exception as e:  # keep the server alive
                self._send(500, {"error": repr(e)})
            finally:
                db.close()

        def do_POST(self):
            """Changes to themes. Only JSON from this server's own pages: another site open in the browser could
            otherwise start research runs."""
            url = urlparse(self.path)
            parts = [p for p in url.path.split("/") if p]
            why = self._refused(url.path)
            if why:
                return self._send(403, {"error": why})
            origin = self.headers.get("Origin")
            host = self.headers.get("Host") or ""
            if origin and origin not in ("http://" + host, "http://localhost:" + host.rsplit(":", 1)[-1], "http://127.0.0.1:" + host.rsplit(":", 1)[-1]):
                return self._send(403, {"error": "not from this server's pages"})
            if "application/json" not in (self.headers.get("Content-Type") or ""):
                return self._send(415, {"error": "send JSON"})
            try:
                cap = 50_000_000 if parts == ["api", "desk", "notes"] else 2_000_000   # notes carry cached figures too
                body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), cap)) or b"{}")
                if parts == ["api", "desk", "notes"]:
                    return self._send(200, notes_put(body))
                if parts == ["api", "themes", "create"]:
                    return self._send(200, theme_create(body.get("name"), body.get("brief", ""), body.get("language", "en")))
                if parts == ["api", "themes", "change"]:
                    return self._send(200, theme_change(body.get("where"), body.get("name"), body))
                if parts == ["api", "themes", "suggest"]:
                    return self._send(200, theme_suggest(body.get("where"), body.get("name")))
                if parts == ["api", "macro", "key"]:
                    return self._send(200, macro_set_key(body.get("key")))
                if parts == ["api", "desk", "forget"]:
                    db = connect()
                    try:
                        return self._send(200, forget_stocks(db, [str(x) for x in body.get("tickers") or []][:200], [str(x) for x in body.get("keep") or []]))
                    finally:
                        db.close()
                if parts == ["api", "settings", "sec"]:
                    return self._send(200, set_sec_identity(client, body.get("name"), body.get("email")))
                if parts == ["api", "settings", "reports"]:
                    return self._send(200, set_reports_dir(body.get("dir")))
                if parts == ["api", "moomoo", "list", "change"]:
                    try:
                        return self._send(200, moomoo_list_change(str(body.get("name") or ""), [str(x) for x in body.get("add") or []][:50],
                                                                  [str(x) for x in body.get("remove") or []][:50]))
                    except OpenDError as e:
                        return self._send(e.status, {"error": str(e)})
                if parts == ["api", "macro", "explain"]:
                    return self._send(200, macro_explain(body))
                if parts == ["api", "reports", "diagram"]:
                    return self._send(200, report_diagram(body.get("text")))
                return self._send(404, {"error": "unknown path"})
            except (ValueError, subprocess.TimeoutExpired) as e:
                return self._send(400, {"error": str(e) or "Claude took too long to answer."})
            except ConnectionError:
                pass
            except Exception as e:     # keep the server alive
                return self._send(500, {"error": repr(e)})

        def log_message(self, fmt, *a):
            if "/desk/" not in (str(a[0]) if a else ""):
                sys.stderr.write("  " + fmt % a + "\n")
    return Handler


# ---- Research reports: the markdown files written by the report skills ----
# The Reports tab reads them straight from their folder, so a report shows up as soon as it is saved.
REPORTS_DIR_DEFAULT = os.path.join(HERE, "reports")
NOTES_PATH = os.path.join(HERE, "notes", "desk.json")   # a copy of the page's research notes (git-ignored)


def notes_get():
    """The copy of the page's notes kept on disk: {"store": ..., "saved_at": ms} or {"store": None}."""
    try:
        with open(NOTES_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"store": None}


def _write_settings(**changes):
    """Change some keys in settings.json, keeping the rest."""
    with _MACRO_LOCK:
        try:
            with open(SETTINGS_PATH, encoding="utf-8") as f:
                settings = json.load(f)
        except (OSError, ValueError):
            settings = {}
        settings.update(changes)
        tmp = SETTINGS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
        os.replace(tmp, SETTINGS_PATH)


def data_sources(client):
    """Every source the desk reads, and whether it works: the Settings page's list."""
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            settings = json.load(f)
    except (OSError, ValueError):
        settings = {}
    try:
        mm = moomoo_status()
    except Exception as e:     # not running, or not logged in
        mm = {"ok": False, "error": str(e) or "OpenD is not running"}
    mm["address"] = "%s:%d" % OPEND_ADDR
    d = reports_dir()
    count = sum(1 for _r, _d, fs in os.walk(d) for f in fs if f.endswith((".md", ".html"))) if os.path.isdir(d) else 0
    db = connect()
    try:
        companies = db.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
    finally:
        db.close()
    return {"sec": {"name": settings.get("name") or "", "email": settings.get("email") or "", "ok": client is not None,
                    "companies": companies},
            "moomoo": mm, "fred": macro_status(),
            "reports": {"dir": d, "setting": settings.get("reports_dir") or "", "exists": os.path.isdir(d), "count": count},
            "notes": {"path": NOTES_PATH, "saved_at": (notes_get() or {}).get("saved_at")}}


def set_sec_identity(client, name, email):
    """The contact the SEC asks every request to carry. Takes effect at once."""
    name, email = (name or "").strip(), (email or "").strip()
    if not name or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or email == "you@example.com":
        raise ValueError("Give your name and a real email address: the SEC asks every request to say who is asking.")
    _write_settings(name=name, email=email)
    if client is not None:
        client.user_agent = f"{name} {email}"
    return {"ok": True}


def set_reports_dir(path):
    """Where the Reports tab reads from. Empty goes back to the reports folder next to this file."""
    path = (path or "").strip()
    if path and not os.path.isdir(os.path.join(HERE, os.path.expanduser(path))):
        raise ValueError("That folder does not exist: " + path)
    _write_settings(reports_dir=path)
    return {"ok": True, "dir": reports_dir()}


def notes_put(body):
    """Keep the page's notes on disk, so a cleared browser loses nothing. Written to a temporary file first, so a
    half-written copy never replaces a good one."""
    store = body.get("store")
    if not isinstance(store, dict) or not isinstance(store.get("data"), dict):
        raise ValueError("not a Diligence notes file")
    os.makedirs(os.path.dirname(NOTES_PATH), exist_ok=True)
    tmp = NOTES_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"store": store, "saved_at": store.get("savedAt") or int(time.time() * 1000)}, f)
    os.replace(tmp, NOTES_PATH)
    return {"ok": True, "saved_at": store.get("savedAt")}
REPORT_KINDS = [("《看懂", "Series"), ("bottleneck-map", "Bottleneck scan"), ("-how-it-works-", "How it works"),
                ("-tech-stack-", "Tech stack"), ("-supply-chain-", "Supply chain"), ("-overview-", "Theme overview"), ("-industry-", "Industry"), ("-funnel-", "Funnel"),
                ("-earnings-", "Earnings"), ("財報", "Earnings"), ("-checklist", "Checklist"), ("-management-", "Management"),
                ("-thesis", "Thesis"), ("-private-", "Private company"), ("income-investment", "Income"), ("portfolio", "Portfolio"),
                ("最終報告", "Team report"), ("最终报告", "Team report"), ("視角", "Team view"), ("视角", "Team view"),
                ("公眾號", "Article"), ("公众号", "Article"), ("召回池", "Screen"), ("篩選", "Screen"), ("筛选", "Screen"),
                ("-research-", "Research"), ("投資研究", "Research"), ("投资研究", "Research"), ("深度研究", "Research"),
                ("深度分析", "Research"), ("研究報告", "Research"), ("研究报告", "Research"), ("investment-research", "Research"), ("deep-dive", "Research")]
# The five skill groups a kind of report belongs to (the same grouping as the skills list).
REPORT_GROUPS = {"Series": "Deep research", "Research": "Deep research", "Team report": "Deep research",
                 "Team view": "Deep research", "Management": "Deep research", "Private company": "Deep research",
                 "How it works": "Deep research", "Tech stack": "Deep research", "Supply chain": "Industry & screening",
                 "Theme overview": "Industry & screening",
                 "Earnings": "Earnings", "Industry": "Industry & screening", "Funnel": "Industry & screening",
                 "Checklist": "Industry & screening", "Bottleneck scan": "Industry & screening", "Screen": "Industry & screening",
                 "Portfolio": "Portfolio", "Thesis": "Portfolio", "Income": "Portfolio", "Article": "Thinking tools"}
# A report's own date line: 報告日期 / 系列日期 / 研究日期 / Report date, as 2026年6月24日 or 2026-06-24.
REPORT_DATE_LINE = re.compile(r"(?:報告日期|报告日期|系列日期|研究日期|分析日期|更新日期|最後更新|最后更新|Report date|Date)\**\s*[:：]\s*\**\s*"
                              r"(20\d{2})\s*[年\-/.]\s*(\d{1,2})\s*[月\-/.]\s*(\d{1,2})", re.I)
_reports_cache = {"sig": None, "list": [], "hits": {}}
_reports_lock = threading.Lock()


def reports_dir():
    """Where the reports live: "reports_dir" in settings.json, else the reports folder next to this file.
    A relative "reports_dir" is taken from this folder too, not from wherever the server was started."""
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            d = (json.load(f).get("reports_dir") or "").strip()
    except (OSError, ValueError):
        d = ""
    if d:
        return os.path.realpath(os.path.join(HERE, os.path.expanduser(d)))
    os.makedirs(REPORTS_DIR_DEFAULT, exist_ok=True)   # a fresh copy has none yet; a folder you set is never made up
    return REPORTS_DIR_DEFAULT


REPORT_CATEGORY_DIRS = {"Company": "Company", "Industry-theme": "Industry & theme", "Screens-pools": "Screens & pools",
                        "People-method": "People & method", "Multi-company": "Multi-company comparison",
                        "Portfolio": "Portfolio", "Macro-index": "Macro & index", "Technical-research": "Technical research",
                        "Working-audit": "Working & audit files", "Running-logs": "Running logs", "Community": "Community"}


def _report_subjects(root):
    """The category each top-level folder stands for: "category_folders" in the reports' own _index/config.json,
    else the standard eleven."""
    out = dict(REPORT_CATEGORY_DIRS)
    try:
        with open(os.path.join(root, "_index", "config.json"), encoding="utf-8") as f:
            out.update(json.load(f).get("category_folders") or {})
    except (OSError, ValueError):
        pass
    return out


def _report_entry(root, full, subjects=None):
    rel = os.path.relpath(full, root).replace(os.sep, "/")
    name = os.path.basename(full)[:-3]
    title, stated = "", ""
    try:
        with open(full, encoding="utf-8", errors="replace") as f:
            for _ in range(40):
                line = f.readline()
                if not line:
                    break
                if not title and line.startswith("# "):
                    title = line[2:].strip()
                if not stated:
                    d = REPORT_DATE_LINE.search(line)
                    if d:
                        try:
                            stated = date(int(d.group(1)), int(d.group(2)), int(d.group(3))).isoformat()
                        except ValueError:
                            pass
                if title and stated:
                    break
        st = os.stat(full)
    except OSError:
        return None
    # The date the report gives for itself ("報告日期：2026年6月24日") comes first, then one in the file name,
    # and only then the day the file was last saved.
    m = re.search(r"(20\d{2})-?(0[1-9]|1[0-2])-?([0-3]\d)(?!\d)", rel)
    when = stated or (f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else date.fromtimestamp(st.st_mtime).isoformat())
    dated = "stated" if stated else "name" if m else "saved"
    kind = next((label for key, label in REPORT_KINDS if key in rel), "Report")
    # reports/<category>/<company or topic>/...; a file directly inside a category folder has no folder of its own
    parts = rel.split("/")
    subjects = subjects or REPORT_CATEGORY_DIRS
    if parts[0] in subjects and len(parts) > 1:
        subject, parts = subjects[parts[0]], parts[1:]
    else:
        subject = "Company" if len(parts) > 1 else "Industry & theme"   # a folder not yet filed under a category
    folder = parts[0] if len(parts) > 1 else ""
    low = rel.lower()
    if "portfolio" in low:
        subject = "Portfolio"
    elif any(k in low for k in ("/evidence/", "證據", "证据", "audit", "抽檢", "抽检", "底稿")):
        subject = "Working & audit files"   # the records behind a report, wherever they are filed
    return {"path": rel, "title": title or name, "file": name, "folder": folder,
            "group": os.path.dirname(rel), "date": when, "kind": kind, "skill": REPORT_GROUPS.get(kind, "Other"),
            "subject": subject, "dated": dated, "size": st.st_size,
            "modified": int(st.st_mtime)}


def reports_scan():
    """Every report under the reports folder, newest first. Rescans only when a file was added or changed."""
    root = reports_dir()
    files = []
    for base, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith((".", "_"))]
        for n in names:
            if n.endswith(".md") and n.lower() != "readme.md" and not n.startswith("."):
                full = os.path.join(base, n)
                try:
                    files.append((full, os.path.getmtime(full)))
                except OSError:
                    pass
    sig = (root, len(files), max((m for _, m in files), default=0))
    with _reports_lock:
        if _reports_cache["sig"] != sig:
            subjects = _report_subjects(root)
            items = [e for e in (_report_entry(root, f, subjects) for f, _ in files) if e]
            # A report with no date of its own takes the newest dated report beside it (same folder, else same
            # company or topic), since the day a file was last saved says little about when it was written.
            for key in ("group", "folder"):
                known = {}
                for e in items:
                    if e["dated"] in ("stated", "name") and e[key]:
                        known[e[key]] = max(known.get(e[key], ""), e["date"])
                for e in items:
                    if e["dated"] == "saved" and e[key] in known:
                        e["date"], e["dated"] = known[e[key]], "nearby"
            items.sort(key=lambda e: (e["date"], e["modified"]), reverse=True)
            _reports_cache.update(sig=sig, list=items, hits={})
        return root, _reports_cache["list"]


# ---- Forgetting a stock: when it is in none of the page's groups, the data kept about it can go.
# Its reports never do: they are the user's research, kept whatever happens to the stock.
PER_COMPANY_TABLES = [t for t in (   # tables, not views (annual_financials is a view of metrics)
    "facts", "filings", "metrics", "statement_lines", "statement_pages",
    "statement_read", "segment_facts", "segment_q_facts", "segment_filings_read", "debt_split", "debt_split_read",
    "holders_13f", "holders_5pct", "holders_5pct_read", "insider_trades", "insider_filings_read", "documents",
    "documents_fts", "ir_exhibits", "ir_read", "companies")]


def stock_footprint(db, tickers, keep=()):
    """What sec.db keeps about each stock. A filer shared with a ticker still in use (GOOG and GOOGL are one
    company) is marked, and its rows are not counted as going."""
    keep_ciks = {lookup_cik(db, k) for k in keep if k} - {None}
    out = []
    for t in tickers:
        t = t.strip().upper()
        cik = lookup_cik(db, t)   # GOOG finds the company saved under GOOGL
        rows = 0
        if cik and cik not in keep_ciks:
            rows = sum(db.execute(f"SELECT COUNT(*) FROM {tb} WHERE cik=?", (cik,)).fetchone()[0] for tb in PER_COMPANY_TABLES if tb != "documents_fts")
        out.append({"ticker": t, "cik": cik, "secRows": rows, "shared": bool(cik and cik in keep_ciks)})
    return out


def forget_stocks(db, tickers, keep=()):
    """Delete what sec.db keeps about these stocks. Reports are not touched. A filer shared with a ticker in `keep`
    keeps its rows."""
    feet = stock_footprint(db, tickers, keep)
    done = []
    for f in feet:
        t = f["ticker"]
        if f["cik"] and not f["shared"]:
            for tb in PER_COMPANY_TABLES:
                db.execute(f"DELETE FROM {tb} WHERE cik=?", (f["cik"],))
        db.execute("DELETE FROM transcripts WHERE upper(ticker)=?", (t,))
        db.commit()
        done.append({"ticker": t, "secRows": f["secRows"], "shared": f["shared"]})
    return {"done": done}


def reports_for(db, ticker):
    """The report list, with the ones about this stock marked: "company" when the folder or title names it,
    "mention" when the text does (with how many times)."""
    root, items = reports_scan()
    ticker = (ticker or "").strip().upper()
    name = ""
    if ticker:
        row = db.execute("SELECT name FROM companies WHERE upper(ticker)=?", (ticker,)).fetchone()
        name = (row["name"] if row else "") or ""
    out = {"dir": root, "exists": os.path.isdir(root), "ticker": ticker, "name": name, "reports": items}
    if not ticker or not items:
        return out
    with _reports_lock:
        hits = _reports_cache["hits"].get(ticker)
    if hits is None:
        short = re.sub(r"[,.]", "", name).split()
        short = short[0].lower() if short and len(short[0]) > 2 else ""
        # A one- or two-letter ticker is also an ordinary word, so it only counts where it is set off as a symbol.
        if len(ticker) <= 2:
            pat = re.compile(r"[（(|/]\s*" + re.escape(ticker) + r"\s*[）)|/]")
        else:
            pat = re.compile(r"(?<![A-Za-z0-9])" + re.escape(ticker) + r"(?![A-Za-z0-9])")
        hits = {}
        for e in items:
            head = (e["folder"] + " " + e["title"] + " " + e["file"]).lower()
            folder_is = e["folder"].lower() in (ticker.lower(), short) if e["folder"] else False
            named = bool(short) and short in head
            try:
                with open(os.path.join(root, e["path"]), encoding="utf-8", errors="replace") as f:
                    n = len(pat.findall(f.read()))
            except OSError:
                n = 0
            if folder_is or named or pat.search(e["title"] + " " + e["file"]):
                hits[e["path"]] = ("company", n)
            elif n:
                hits[e["path"]] = ("mention", n)
        with _reports_lock:
            _reports_cache["hits"][ticker] = hits
    out["reports"] = [dict(e, rel=hits[e["path"]][0], hits=hits[e["path"]][1]) if e["path"] in hits else e
                      for e in items]
    return out


def report_text(rel):
    root = reports_dir()
    full = os.path.realpath(os.path.join(root, rel))
    if not full.endswith(".md") or os.path.commonpath([root, full]) != root or not os.path.isfile(full):
        return None
    with open(full, encoding="utf-8", errors="replace") as f:
        return {"path": rel.replace(os.sep, "/"), "text": f.read()}


# ---- Themes: deep research on a subject, several reports at a time. Each theme is a folder of the reports folder
# (Industry-theme/<name>/) with a _theme.json listing its topics. Each topic is researched by its own Claude Code run
# in the background that may only search the web and read pages: no files, no shell, none of the user's connected
# services. The run's last message is the report, which this server saves into the theme's folder.
THEME_DIRS = ("Industry-theme", "Technical-research")   # folders whose subfolders count as themes; new ones go in the first
THEME_FILE = "_theme.json"
THEME_LANGS = {"en": "English", "zh-Hant": "Traditional Chinese (繁體中文)", "zh-Hans": "Simplified Chinese (简体中文)"}
THEME_KINDS = {   # kind -> (label, word in the file name, what the report should cover)
    "how": ("How it works", "how-it-works",
            "Explain how it works, from first principles down to the detail an investor needs. Cover what problem it solves "
            "and for whom; its parts and who operates each one; one worked example traced end to end (one payment, one "
            "photon, one order), drawn as a step-by-step sequence or a text diagram; the economics: who pays whom, the fees "
            "and margins at each step and where the money ends up; what limits it (physics, cost, regulation, network "
            "effects) and how those limits are being pushed; how it compares with the main alternatives; and what could "
            "change the picture."),
    "stack": ("Tech stack & progress", "tech-stack",
              "Map the technology stack from materials and components up to the applications. For each layer: what it "
              "does, the competing approaches, the state of the art in concrete figures and how those figures improved "
              "over the last five to ten years, who leads (companies, labs, countries), the main bottleneck, and the next "
              "improvements expected and when. End with where the biggest gains and risks in the stack lie and which "
              "layers capture the value."),
    "supply": ("Supply chain", "supply-chain",
               "Map the supply chain. Break the product into its bill of materials or subsystems, with the share of cost "
               "of each where sources allow. For each part: the suppliers and their shares where known, where it is "
               "made, the alternatives and how hard switching is, and single-source or capacity bottlenecks. Then "
               "assembly and logistics, geographic and political risk, how the chain has changed recently, and which "
               "listed suppliers depend most on it (share of their revenue) and how much it matters to them."),
    "industry": ("Industry & companies", "industry",
                 "Study the industry: its size and growth, its structure (who sells what to whom), the economics of the "
                 "main business models, the main companies compared in a table (growth, margins, valuation where "
                 "listed), moats and threats, what drives its cycle and the key debates. End with which companies look "
                 "best placed and why, and the main risks."),
    "open": ("Open question", "research", "Answer the question thoroughly, choosing the structure that suits it best."),
    "overview": ("Theme overview", "overview",
                 "Write the overview of the whole theme from the reports below and any checking you need: the big "
                 "picture, the most important insights across the reports, where they agree and disagree, what is still "
                 "unknown, and the listed companies that matter most. Do not repeat the reports; tie them together."),
}
RESEARCH_HOW = """How to research:
- Today is {today}. Search widely and read the sources themselves: company filings and investor materials, technical
  papers, patents, standards documents, regulators and official statistics, engineering blogs, ahead of news
  summaries and aggregators. Expect to need 15 to 40 searches and to read many pages.
- Check important numbers against a second source, give the date of every figure, and say plainly when something is
  an estimate or disputed.
- Do not ask questions. If the topic is ambiguous, choose the most useful reading, say which you chose, and go on.

How to write it:
- Write in {language}, in plain words; explain a technical term the first time it appears.
- Your final message must be the complete report in Markdown and nothing else: no preamble, no remarks about how you
  worked.
- Start with a level-1 heading (# ) giving the title, then the line "Report date: {today}", then a section
  "## In brief" of 5 to 8 bullet points a busy reader could stop after.
- Then the body under ## headings, with tables where they make comparisons clearer. Cite sources inline as Markdown
  links.
- Draw every flow, mechanism, chain of cause and effect, supply chain or hierarchy as a Mermaid diagram in a ```mermaid
  code block (flowchart LR or TD, sequenceDiagram for a step-by-step exchange between parties). Keep node labels short,
  put amounts on the arrows where they matter, and do not add colours or styling. Never draw diagrams with text art.
- When figures compare companies, years or parts of a whole, add a chart as a ```chart code block holding only JSON,
  for example {{"type": "bar", "title": "...", "unit": "$B", "x": "year", "series": ["A", "B"], "data": [{{"year": "2025",
  "A": 1.2, "B": 3.4}}]}}. type is bar, line, area or pie (pie data is [{{"name": "...", "value": 12}}]); add "stacked": true
  to stack bars. Use only figures you found, and give the same figures with their sources in the text or a table.
- If listed companies matter to the topic, end the body with a section "## Listed companies": a table with the
  columns Ticker | Company | Role in this topic, with tickers written like NVDA, 0700.HK, 600519.SH, 2330.TW.
- Finish with "## Sources", the main sources as a list of Markdown links.
- Make it as long as the subject needs; reports like this usually run 3,000 to 7,000 words."""
RESEARCH_TIMEOUT = 90 * 60
_THEME_LOCK = threading.RLock()
_THEME_JOBS = {}            # (theme folder, topic id) -> the running process (None when it was started before a restart)
_TICKER_CACHE = {}


def _claude_bin():
    return shutil.which("claude") or next((p for p in (os.path.expanduser("~/.local/bin/claude"), "/opt/homebrew/bin/claude",
                                                      "/usr/local/bin/claude") if os.path.isfile(p)), None)


def research_limit():
    """How many research runs at once: "research_parallel" in settings.json, else 3."""
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            return max(1, min(int(json.load(f).get("research_parallel") or 3), 8))
    except (OSError, ValueError, TypeError):
        return 3


def _theme_dir(where, name):
    if where not in THEME_DIRS or not name or name.startswith((".", "_")) or re.search(r"[/\\]", name) or name == "..":
        raise ValueError("No such theme.")
    return os.path.join(reports_dir(), where, name)


def _theme_read(folder):
    try:
        with open(os.path.join(folder, THEME_FILE), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _theme_write(folder, data):
    data["updated"] = datetime.now().isoformat(timespec="seconds")
    tmp = os.path.join(folder, THEME_FILE + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, os.path.join(folder, THEME_FILE))


def _theme_edit(folder, fn):
    """Change a theme's file under the lock: fn(data) edits it in place; its answer is returned."""
    with _THEME_LOCK:
        data = _theme_read(folder)
        if data is None:
            raise ValueError("No such theme.")
        out = fn(data)
        _theme_write(folder, data)
        return out


def _theme_md(folder):
    """The reports inside a theme's folder, as paths relative to it."""
    out = []
    for base, dirs, names in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if not d.startswith((".", "_")))
        for n in sorted(names):
            if n.endswith(".md") and not n.startswith(".") and n.lower() != "readme.md":
                out.append(os.path.relpath(os.path.join(base, n), folder).replace(os.sep, "/"))
    return out


def _report_tickers(full):
    """The tickers in a report: the first column of its "Listed companies" table, and any table column headed
    Ticker or 代碼."""
    try:
        mtime = os.path.getmtime(full)
    except OSError:
        return []
    hit = _TICKER_CACHE.get(full)
    if hit and hit[0] == mtime:
        return hit[1]
    out, col, listed, row = [], None, False, 0
    with open(full, encoding="utf-8", errors="replace") as f:
        for line in f:
            if re.match(r"#{2,4}\s", line):
                listed = bool(re.search(r"listed compan|上市公司|相關公司|相关公司", line, re.I))
            if not line.lstrip().startswith("|"):
                col, row = None, 0          # a table ended
                continue
            cells = [c.strip().strip("*`").strip() for c in line.strip().strip("|").split("|")]
            row += 1
            if row == 1:                    # the header row says which column holds the tickers
                col = next((i for i, c in enumerate(cells) if re.fullmatch(r"(ticker|symbol|代碼|代码|股票代[碼码號号]|code)s?", c, re.I)), 0 if listed else None)
                continue
            if col is None or col >= len(cells):
                continue
            # 2330.TW, 0700.HK, NVDA, 688012.SH; a cell like "NVDA / NVIDIA" or "0700.HK（騰訊）" gives its first word
            m = re.match(r"([A-Z0-9][A-Z0-9.\-]{0,11})(?![A-Za-z0-9])", cells[col].upper().replace(" ", ""))
            t = m.group(1).rstrip(".-") if m else ""
            if t and re.search(r"[A-Z]", t) and not re.fullmatch(r"-+|N/?A|NA|TBD", t) and t not in out:
                out.append(t)
    _TICKER_CACHE[full] = (mtime, out)
    return out


def themes_list():
    """Every theme: the ones made here (with topics to research) first, then report folders that are only read."""
    mine, read = [], []
    for where in THEME_DIRS:
        base = os.path.join(reports_dir(), where)
        try:
            names = sorted(os.listdir(base))
        except OSError:
            continue
        for name in names:
            folder = os.path.join(base, name)
            if name.startswith((".", "_")) or not os.path.isdir(folder):
                continue
            data = _theme_read(folder)
            if data is not None:
                ts = data.get("topics") or []
                mine.append({"where": where, "name": name, "managed": True, "topics": len(ts),
                             "done": sum(t.get("status") == "done" for t in ts),
                             "busy": sum(t.get("status") in ("queued", "running") for t in ts), "updated": data.get("updated", "")})
            else:
                n = len(_theme_md(folder))
                if n:
                    read.append({"where": where, "name": name, "managed": False, "topics": n, "done": n, "busy": 0, "updated": ""})
    mine.sort(key=lambda t: t["updated"], reverse=True)
    return {"themes": mine + read, "dir": reports_dir(), "kinds": {k: v[0] for k, v in THEME_KINDS.items()},
            "langs": THEME_LANGS, "parallel": research_limit(), "claude": bool(_claude_bin())}


def theme_get(where, name):
    folder = _theme_dir(where, name)
    if not os.path.isdir(folder):
        raise ValueError("No such theme.")
    data = _theme_read(folder)
    rel = where + "/" + name
    files = _theme_md(folder)
    if data is None:                   # a folder of reports: each report is a finished topic
        topics = []
        for f in files:
            e = _report_entry(os.path.join(reports_dir(), where), os.path.join(folder, f)) or {}
            topics.append({"id": f, "title": e.get("title") or f[:-3], "kind": e.get("kind"), "status": "done",
                           "path": rel + "/" + f, "date": e.get("date"), "tickers": _report_tickers(os.path.join(folder, f))})
        return {"where": where, "name": name, "managed": False, "topics": topics, "others": []}
    used = set()
    for t in data.get("topics") or []:
        for f in [t.get("file")] + (t.get("older") or []):
            if f:
                used.add(f)
        if t.get("file"):
            t["path"] = rel + "/" + t["file"]
            t["tickers"] = _report_tickers(os.path.join(folder, t["file"]))
        t["label"] = THEME_KINDS.get(t.get("type"), ("Report",))[0]
    others = [{"title": (_report_entry(os.path.join(reports_dir(), where), os.path.join(folder, f)) or {}).get("title") or f[:-3],
               "path": rel + "/" + f, "tickers": _report_tickers(os.path.join(folder, f))} for f in files if f not in used]
    return dict(data, where=where, name=name, managed=True, others=others, now=time.time())


def theme_create(name, brief="", language="en"):
    name = re.sub(r'[/\\:*?"<>|\n\r\t]+', "-", (name or "").strip()).strip(" .-_")[:80]
    if not name:
        raise ValueError("Give the theme a name.")
    folder = os.path.join(reports_dir(), THEME_DIRS[0], name)
    with _THEME_LOCK:
        if _theme_read(folder) is not None:
            raise ValueError("There is already a theme called " + name + ".")
        os.makedirs(folder, exist_ok=True)     # an existing folder of reports becomes a theme, its reports kept
        _theme_write(folder, {"name": name, "brief": (brief or "").strip(), "language": language if language in THEME_LANGS else "en",
                              "created": datetime.now().isoformat(timespec="seconds"), "topics": []})
    return {"where": THEME_DIRS[0], "name": name}


def theme_change(where, name, p):
    """Everything the page can do to a theme: edit the brief, add, edit, remove, run and stop topics."""
    folder = _theme_dir(where, name)
    stop = []

    def edit(d):
        if "brief" in p:
            d["brief"] = str(p["brief"]).strip()
        if p.get("language") in THEME_LANGS:
            d["language"] = p["language"]
        topics = d.setdefault("topics", [])
        for t in p.get("add") or []:
            title = re.sub(r"\s+", " ", str(t.get("title") or "")).strip()[:200]
            if title:
                topics.append({"id": "t%d%03d" % (time.time() * 1000, len(topics)), "title": title,
                               "type": t.get("type") if t.get("type") in THEME_KINDS else "open",
                               "notes": str(t.get("notes") or "").strip(), "status": "new"})
        by = {t["id"]: t for t in topics}
        for tid, ch in (p.get("edit") or {}).items():
            t = by.get(tid)
            if t and t.get("status") not in ("queued", "running"):
                if str(ch.get("title") or "").strip():
                    t["title"] = str(ch["title"]).strip()[:200]
                if ch.get("type") in THEME_KINDS:
                    t["type"] = ch["type"]
                if "notes" in ch:
                    t["notes"] = str(ch["notes"]).strip()
        for tid in p.get("run") or []:
            t = by.get(tid)
            if t and t.get("status") not in ("queued", "running"):
                t.update(status="queued", queued_at=time.time(), error=None, activity=None)
        for tid in (p.get("stop") or []) + (p.get("remove") or []):
            t = by.get(tid)
            if t and t.get("status") == "running":
                stop.append((tid, t.get("pid")))
                t.update(status="stopped", activity=None)
            elif t and t.get("status") == "queued":
                t["status"] = "new"
        gone = set(p.get("remove") or [])
        d["topics"] = [t for t in topics if t["id"] not in gone]   # a removed topic's report stays in the folder
    _theme_edit(folder, edit)
    for tid, pid in stop:
        job = _THEME_JOBS.get((folder, tid))
        try:
            os.killpg(job.pid if job else pid, signal.SIGTERM)
        except (OSError, TypeError):
            pass
    _research_pump()
    return theme_get(where, name)


def _research_prompt(folder, data, topic):
    label, _, guide = THEME_KINDS.get(topic.get("type"), THEME_KINDS["open"])
    today = date.today().isoformat()
    parts = ["You are a research analyst writing a deep research report for an investor who wants to understand the "
             "subject properly, not skim it.",
             "Theme: " + data.get("name", "") + ("\nWhat the investor wants from the theme: " + data["brief"] if data.get("brief") else ""),
             "Topic of this report: " + topic["title"] + ("\nNotes from the investor: " + topic["notes"] if topic.get("notes") else ""),
             "Kind of report: " + label + ". " + guide,
             RESEARCH_HOW.format(today=today, language=THEME_LANGS.get(data.get("language"), "English"))]
    if topic.get("type") == "overview":       # hand it the theme's finished reports to tie together
        for t in data.get("topics") or []:
            if t.get("file") and t["id"] != topic["id"] and t.get("type") != "overview":
                try:
                    with open(os.path.join(folder, t["file"]), encoding="utf-8", errors="replace") as f:
                        parts.append("=== Report: " + t["title"] + " ===\n" + f.read()[:60000])
                except OSError:
                    pass
    return "\n\n".join(parts)


def _research_pump():
    """Start queued topics, oldest first, while fewer than the limit are running."""
    with _THEME_LOCK:
        free = research_limit() - len(_THEME_JOBS)
        if free <= 0:
            return
        queued = []
        for th in themes_list()["themes"]:
            if th["managed"] and th["busy"]:
                folder = _theme_dir(th["where"], th["name"])
                for t in (_theme_read(folder) or {}).get("topics") or []:
                    if t.get("status") == "queued" and (folder, t["id"]) not in _THEME_JOBS:
                        queued.append((t.get("queued_at") or 0, folder, t["id"]))
        for _, folder, tid in sorted(queued)[:free]:
            _research_start(folder, tid)


def _topic_update(folder, tid, **fields):
    """Change one topic's fields; False when it has been removed meanwhile."""
    found = []
    def change(d):
        t = next((t for t in d.get("topics") or [] if t["id"] == tid), None)
        if t is not None:
            t.update(fields)
            found.append(t)
    _theme_edit(folder, change)
    return bool(found)


def _research_start(folder, tid):
    data = _theme_read(folder)
    topic = next((t for t in data.get("topics") or [] if t["id"] == tid), None)
    if topic is None:          # removed while it waited its turn
        return
    claude = _claude_bin()
    if not claude:
        _topic_update(folder, tid, status="failed", error="Claude Code (the claude command) was not found on this computer.")
        return
    prompt = _research_prompt(folder, data, topic)
    jobs = os.path.join(folder, ".jobs")
    os.makedirs(jobs, exist_ok=True)
    log = os.path.join(jobs, tid + ".jsonl")
    with open(log, "w") as out, open(os.path.join(jobs, tid + ".err"), "w") as err:
        proc = subprocess.Popen([claude, "-p", "--output-format", "stream-json", "--verbose",
                                 "--tools", "WebSearch,WebFetch", "--allowedTools", "WebSearch,WebFetch",
                                 "--strict-mcp-config", "--no-session-persistence"],
                                stdin=subprocess.PIPE, stdout=out, stderr=err, cwd=folder, start_new_session=True)

    def feed():
        try:
            proc.stdin.write(prompt.encode("utf-8"))
            proc.stdin.close()
        except OSError:
            pass
    threading.Thread(target=feed, daemon=True).start()
    if not _topic_update(folder, tid, status="running", pid=proc.pid, started=time.time(), searches=0, pages=0,
                         activity="Starting", error=None):
        proc.kill()            # removed while the run was starting
        return
    _THEME_JOBS[(folder, tid)] = proc
    threading.Thread(target=_research_watch, args=(folder, tid, proc, proc.pid), daemon=True).start()


def _alive(proc, pid):
    if proc is not None:
        return proc.poll() is None
    try:                               # started before this server restarted: still its child, but without a handle
        return os.waitpid(pid, os.WNOHANG)[0] != pid
    except ChildProcessError:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def _research_watch(folder, tid, proc, pid):
    """Follow one run's log: count its searches and pages for the page, and save the report when it finishes."""
    log = os.path.join(folder, ".jobs", tid + ".jsonl")
    dec, buf, pos = codecs.getincrementaldecoder("utf-8")("replace"), "", 0
    result, searches, pages, activity, saved, final = None, 0, 0, "Starting", (0, 0, ""), False
    started = next((t.get("started") for t in (_theme_read(folder) or {}).get("topics") or [] if t["id"] == tid), None) or time.time()
    while True:
        try:
            with open(log, "rb") as f:
                f.seek(pos)
                chunk = f.read()
            pos += len(chunk)
            buf += dec.decode(chunk)
        except OSError:
            pass
        lines = buf.split("\n")
        buf = lines.pop()
        for line in lines:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") == "assistant":
                for c in (ev.get("message") or {}).get("content") or []:
                    if c.get("type") == "tool_use" and c.get("name") == "WebSearch":
                        searches += 1
                        activity = "Searching: " + str((c.get("input") or {}).get("query", ""))[:140]
                    elif c.get("type") == "tool_use" and c.get("name") == "WebFetch":
                        pages += 1
                        activity = "Reading " + (urlparse(str((c.get("input") or {}).get("url", ""))).netloc or "a page")
                    elif c.get("type") == "text" and len(c.get("text") or "") > 2000:
                        activity = "Writing the report"
            elif ev.get("type") == "result":
                result = ev
        if result or final:
            break
        if not _alive(proc, pid):
            final = True               # one more read for anything written just before it stopped
            continue
        if time.time() - started > RESEARCH_TIMEOUT:
            try:
                os.killpg(pid, signal.SIGTERM)
            except OSError:
                pass
            activity = "timeout"
            final = True
            continue
        if (searches, pages, activity) != saved:
            saved = (searches, pages, activity)
            try:
                _theme_edit(folder, lambda d: next(t for t in d["topics"] if t["id"] == tid).update(
                    searches=searches, pages=pages, activity=activity))
            except (ValueError, StopIteration):
                break                  # the theme or topic was removed
        time.sleep(2)
    if proc is not None:
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            pass
    text = ((result or {}).get("result") or "").strip()
    ok = bool(result) and result.get("subtype") == "success" and not result.get("is_error") and len(text) > 200
    try:
        with open(os.path.join(folder, ".jobs", tid + ".err"), encoding="utf-8", errors="replace") as f:
            err = f.read()[-600:].strip()
    except OSError:
        err = ""

    def finish(d):
        t = next((x for x in d.get("topics") or [] if x["id"] == tid), None)
        if t is None:
            return
        t.update(searches=searches, pages=pages, activity=None, finished=time.time(), pid=None)
        if ok:
            m = re.search(r"(?m)^# ", text)
            body = text[m.start():] if m else "# " + t["title"] + "\n\n" + text
            slug = THEME_KINDS.get(t.get("type"), THEME_KINDS["open"])[1]
            stem = re.sub(r'[/\\:*?"<>|\n\r\t]+', "-", t["title"]).strip(" .-")[:90] or "report"
            name, n = "%s-%s-%s.md" % (stem, slug, date.today().strftime("%Y%m%d")), 2
            while os.path.exists(os.path.join(folder, name)):
                name, n = "%s-%s-%s-%d.md" % (stem, slug, date.today().strftime("%Y%m%d"), n), n + 1
            with open(os.path.join(folder, name), "w", encoding="utf-8") as f:
                f.write(body + "\n")
            if t.get("file"):
                t.setdefault("older", []).append(t["file"])
            t.update(status="done", file=name, error=None, cost=(result or {}).get("total_cost_usd"))
        elif t.get("status") != "stopped":
            why = text if result and text else "it ran longer than %d minutes" % (RESEARCH_TIMEOUT // 60) if activity == "timeout" else err or "it stopped without a report"
            t.update(status="failed", error=why[:600])
    try:
        _theme_edit(folder, finish)
    except ValueError:
        pass
    with _THEME_LOCK:
        _THEME_JOBS.pop((folder, tid), None)
    _research_pump()


def research_resume():
    """After a restart: keep following the runs still going, and start whatever is queued."""
    for th in themes_list()["themes"]:
        if not th["managed"] or not th["busy"]:
            continue
        folder = _theme_dir(th["where"], th["name"])
        for t in (_theme_read(folder) or {}).get("topics") or []:
            if t.get("status") == "running" and (folder, t["id"]) not in _THEME_JOBS:
                with _THEME_LOCK:
                    _THEME_JOBS[(folder, t["id"])] = None
                threading.Thread(target=_research_watch, args=(folder, t["id"], None, t.get("pid") or 0), daemon=True).start()
    _research_pump()


def theme_suggest(where, name):
    """Topics Claude proposes for a theme: [{"title", "type", "why"}]. Takes half a minute or so."""
    folder = _theme_dir(where, name)
    data = _theme_read(folder) or {}
    claude = _claude_bin()
    if not claude:
        raise ValueError("Claude Code (the claude command) was not found on this computer.")
    have = [t["title"] for t in data.get("topics") or []]
    prompt = ("Theme for deep research: " + data.get("name", name) + ("\nWhat the investor wants: " + data["brief"] if data.get("brief") else "")
              + ("\nTopics already planned: " + "; ".join(have) if have else "") + """

Propose 5 to 8 more research reports that together give a thorough understanding of this theme. Each is researched
and written separately, so make them distinct, each worth a long report, and not repeating the topics already planned.
Give each the kind that fits best:
- how: how a technology, product or system works (how Visa moves a payment, how Waymo's lidar sees)
- stack: a technology stack: its layers, the state of the art and the improvements coming
- supply: a supply chain: the parts, the suppliers, the bottlenecks
- industry: an industry and the companies in it
- open: any other question
Write the titles and reasons in """ + THEME_LANGS.get(data.get("language"), "English") + """.
Answer with JSON only, no other text: [{"title": "...", "type": "how", "why": "one sentence on what it will answer"}]""")
    r = subprocess.run([claude, "-p", "--output-format", "json", "--tools", "", "--strict-mcp-config", "--no-session-persistence"],
                       input=prompt, capture_output=True, text=True, timeout=300, cwd=folder)
    try:
        text = json.loads(r.stdout).get("result") or ""
        m = re.search(r"\[.*\]", text, re.S)
        items = json.loads(m.group(0)) if m else []
    except (ValueError, AttributeError):
        raise ValueError("Claude did not answer with topics: " + (r.stderr or r.stdout)[-300:])
    return {"topics": [{"title": str(x.get("title", "")).strip(), "type": x.get("type") if x.get("type") in THEME_KINDS else "open",
                        "why": str(x.get("why", "")).strip()} for x in items if isinstance(x, dict) and x.get("title")]}


# ---- Macro: economic data series for the Macro page. FRED (free API key) is the main source and covers everything,
# with search. Without a key, or while FRED cannot be reached, the core series still come from keyless sources:
# DBnomics (which mirrors the Fed's H.15, H.6, H.10 and G.17 releases and BEA's national accounts) and the BLS's own
# API for jobs and prices (DBnomics' copy of BLS data stops at January 2025).
MACRO_KEEP = 6 * 3600
# FRED id -> (source, code, scale): DBnomics values for BEA dollar series are in millions, FRED's in billions
MACRO_FALLBACK = {
    "UNRATE": ("bls", "LNS14000000"), "PAYEMS": ("bls", "CES0000000001"), "CPIAUCSL": ("bls", "CUSR0000SA0"),
    "CPILFESL": ("bls", "CUSR0000SA0L1E"), "JTSJOL": ("bls", "JTS000000000000000JOL"), "CIVPART": ("bls", "LNS11300000"),
    "EMRATIO": ("bls", "LNS12300000"), "U6RATE": ("bls", "LNS13327709"), "CES0500000003": ("bls", "CES0500000003"),
    "PPIACO": ("bls", "WPU00000000"),
    "FEDFUNDS": ("dbn", "FED/H15/RIFSPFF_N.M"), "DGS10": ("dbn", "FED/H15/RIFLGFCY10_N.M"), "DGS2": ("dbn", "FED/H15/RIFLGFCY02_N.M"),
    "DGS3MO": ("dbn", "FED/H15/RIFLGFCM03_N.M"), "DGS30": ("dbn", "FED/H15/RIFLGFCY30_N.M"), "DFII10": ("dbn", "FED/H15/RIFLGFCY10_XII_N.M"),
    "M2SL": ("dbn", "FED/H6_H6_M2/M2.M"), "INDPRO": ("dbn", "FED/G17_IP_MARKET_GROUPS/IP.B50001.S"), "DTWEXBGS": ("dbn", "FED/H10/JRXWTFB_N.M"),
    "GDPC1": ("dbn", "BEA/NIPA-T10106/A191RX-Q", 0.001), "GDP": ("dbn", "BEA/NIPA-T10105/A191RC-Q", 0.001),
    "PCEC96": ("dbn", "BEA/NIPA-T20806/DPCERX-M", 0.001), "PCE": ("dbn", "BEA/NIPA-T20805/DPCERC-M", 0.001),
    "PCEPI": ("dbn", "BEA/NIPA-T20804/DPCERG-M"), "PCEPILFE": ("dbn", "BEA/NIPA-T20804/DPCCRG-M"),
    "PSAVERT": ("dbn", "BEA/NIPA-T20600/A072RC-M"), "DSPIC96": ("dbn", "BEA/NIPA-T20600/A067RX-M", 0.001),
    "GPDIC1": ("dbn", "BEA/NIPA-T10106/A006RX-Q", 0.001), "GCEC1": ("dbn", "BEA/NIPA-T10106/A822RX-Q", 0.001),
    "NETEXP": ("dbn", "BEA/NIPA-T10105/A019RC-Q", 0.001), "UMCSENT": ("dbn", "SCSMICH/MICS/ICS"),
    "GPDI": ("dbn", "BEA/NIPA-T10105/A006RC-Q", 0.001), "GCE": ("dbn", "BEA/NIPA-T10105/A822RC-Q", 0.001),
    "W006RC1Q027SBEA": ("dbn", "BEA/NIPA-T30200/W006RC-Q", 0.001),
}
# payroll jobs by industry and CPI components, from the BLS
MACRO_FALLBACK.update({fred: ("bls", code) for fred, code in {
    "USMINE": "CES1000000001", "USCONS": "CES2000000001", "MANEMP": "CES3000000001", "USWTRADE": "CES4142000001",
    "USTRADE": "CES4200000001", "CES4300000001": "CES4300000001", "CES4422000001": "CES4422000001", "USINFO": "CES5000000001",
    "USFIRE": "CES5500000001", "USPBS": "CES6000000001", "USEHS": "CES6500000001", "USLAH": "CES7000000001",
    "USSERV": "CES8000000001", "USGOVT": "CES9000000001",
    "CPIFABSL": "CUSR0000SAF", "CPIHOSSL": "CUSR0000SAH", "CPIAPPSL": "CUSR0000SAA", "CPITRNSL": "CUSR0000SAT",
    "CPIMEDSL": "CUSR0000SAM", "CPIRECSL": "CUSR0000SAR", "CPIEDUSL": "CUSR0000SAE", "CPIOGSSL": "CUSR0000SAG",
    "CPIENGSL": "CUSR0000SA0E", "CUSR0000SAF11": "CUSR0000SAF11", "CUSR0000SETA01": "CUSR0000SETA01", "CUSR0000SEHA": "CUSR0000SEHA"}.items()})
_MACRO_LOCK = threading.Lock()
_FRED_STATUS = {"at": 0, "ok": None, "msg": ""}
_EXPLAIN_CACHE = {}


def _fred_key():
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            return (json.load(f).get("fred_api_key") or "").strip()
    except (OSError, ValueError):
        return ""


def macro_set_key(key):
    key = (key or "").strip().lower()
    if key and not re.fullmatch(r"[a-z0-9]{32}", key):
        raise ValueError("A FRED API key is 32 letters and digits.")
    with _MACRO_LOCK:
        try:
            with open(SETTINGS_PATH, encoding="utf-8") as f:
                settings = json.load(f)
        except (OSError, ValueError):
            settings = {}
        settings["fred_api_key"] = key
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
        _FRED_STATUS["at"] = 0
    return macro_status()


def _get_json(url, data=None, timeout=30):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "Mozilla/5.0 (Diligence research desk)",
                                                          "Content-Type": "application/json"} if data else
                                 {"User-Agent": "Mozilla/5.0 (Diligence research desk)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    try:
        return json.loads(raw)
    except ValueError:
        # FRED answers with an HTML page when it is down for maintenance
        title = re.search(rb"<title>(.*?)<", raw, re.S)
        raise OSError("answered with a web page" + (": " + title.group(1).decode("utf-8", "replace").strip() if title else ""))


def _fred(path, **params):
    key = _fred_key()
    if not key:
        raise OSError("no FRED API key set")
    params.update(api_key=key, file_type="json")
    d = _get_json("https://api.stlouisfed.org/fred/%s?%s" % (path, urlencode(params)))
    if d.get("error_message"):
        raise OSError(d["error_message"])
    return d


def macro_status():
    """Whether a FRED key is set and FRED answers (checked at most every ten minutes)."""
    key = bool(_fred_key())
    now = time.time()
    if key and now - _FRED_STATUS["at"] > 600:
        try:
            _fred("series", series_id="GNPCA")
            _FRED_STATUS.update(at=now, ok=True, msg="")
        except OSError as e:
            _FRED_STATUS.update(at=now, ok=False, msg=str(e))
    return {"fred_key": key, "fred_ok": _FRED_STATUS["ok"] if key else None, "fred_msg": _FRED_STATUS["msg"] if key else "",
            "fallback": sorted(MACRO_FALLBACK)}


def _cached(db, key, keep, make):
    row = db.execute("SELECT fetched_at, data FROM macro_cache WHERE key=?", (key,)).fetchone()
    if row and time.time() - row["fetched_at"] < keep:
        return json.loads(row["data"])
    data = make()
    db.execute("INSERT OR REPLACE INTO macro_cache VALUES (?, ?, ?)", (key, time.time(), json.dumps(data)))
    db.commit()
    return data


def _month_start(p):
    """A period as the first day of its month: 2026-Q2 -> 2026-04-01, 1954-07-31 -> 1954-07-01, 2007-01 -> 2007-01-01."""
    m = re.fullmatch(r"(\d{4})-Q([1-4])", p)
    if m:
        return "%s-%02d-01" % (m.group(1), int(m.group(2)) * 3 - 2)
    m = re.fullmatch(r"(\d{4})-(\d\d)(?:-\d\d)?", p)
    if m:
        return "%s-%s-01" % (m.group(1), m.group(2))
    m = re.fullmatch(r"(\d{4})", p)
    return "%s-01-01" % p if m else None


def _dbnomics(code, scale=1):
    d = _get_json("https://api.db.nomics.world/v22/series/%s?observations=1&format=json" % code, timeout=40)
    docs = (d.get("series") or {}).get("docs") or []
    if not docs:
        raise OSError("DBnomics has no series " + code)
    out = {}
    for p, v in zip(docs[0].get("period") or [], docs[0].get("value") or []):
        day = _month_start(str(p))
        if day and isinstance(v, (int, float)):
            out[day] = round(v * scale, 6)
    return out


def _bls_recent(db):
    """The last ten years of every BLS fallback series, 25 to a request (the keyless API allows 25 requests a day)."""
    def make():
        ids = [code for src, code, *_ in MACRO_FALLBACK.values() if src == "bls"]
        year = date.today().year
        found = []
        for i in range(0, len(ids), 25):
            d = _get_json("https://api.bls.gov/publicAPI/v1/timeseries/data/",
                          json.dumps({"seriesid": ids[i:i + 25], "startyear": str(year - 9), "endyear": str(year)}).encode(), timeout=40)
            if d.get("status") != "REQUEST_SUCCEEDED":
                raise OSError("BLS: " + "; ".join(d.get("message") or ["request failed"]))
            found += (d.get("Results") or {}).get("series") or []
        out = {}
        for s in found:
            pts = {}
            for x in s.get("data") or []:
                if re.fullmatch(r"M(0[1-9]|1[0-2])", x.get("period", "")):
                    try:
                        pts["%s-%s-01" % (x["year"], x["period"][1:])] = float(x["value"])
                    except (TypeError, ValueError):
                        pass
            out[s.get("seriesID")] = pts
        return out
    return _cached(db, "bls-recent", 12 * 3600, make)


def _from_fallback(db, sid):
    src, code, *rest = MACRO_FALLBACK[sid]
    if src == "dbn":
        pts = _dbnomics(code, rest[0] if rest else 1)
        label = "DBnomics (" + {"FED": "Federal Reserve", "BEA": "BEA", "SCSMICH": "University of Michigan"}.get(code.split("/")[0], code.split("/")[0]) + ")"
    else:
        dataset = {"LNS": "ln", "CES": "ce", "CUS": "cu", "JTS": "jt", "WPU": "wp"}[code[:3]]
        pts = _dbnomics("BLS/%s/%s" % (dataset, code))              # history, to January 2025
        try:
            pts.update(_bls_recent(db).get(code) or {})             # and the latest years from the BLS itself
            label = "BLS (history via DBnomics)"
        except OSError:
            label = "DBnomics (BLS, ends Jan 2025)"
    return pts, label


def macro_series(db, sid):
    """One series as {"id", "points": [[date, value], ...], "source", "note"}, monthly or less often (daily and weekly
    series come as monthly averages). Dates are the first day of the month or quarter."""
    sid = (sid or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9_.]{1,40}", sid):
        raise ValueError("not a series id")

    def make():
        errs = []
        if _fred_key():
            try:
                meta = (_fred("series", series_id=sid).get("seriess") or [{}])[0]
                extra = {"frequency": "m", "aggregation_method": "avg"} if meta.get("frequency_short") in ("D", "W", "BW") else {}
                obs = _fred("series/observations", series_id=sid, **extra).get("observations") or []
                pts = [[o["date"][:8] + "01", float(o["value"])] for o in obs if o.get("value") not in (None, "", ".")]
                return {"id": sid, "points": pts, "source": "FRED",
                        "meta": {"title": meta.get("title"), "units": meta.get("units_short") or meta.get("units"),
                                 "freq": meta.get("frequency"), "sa": meta.get("seasonal_adjustment"),
                                 "notes": (meta.get("notes") or "")[:1200], "updated": meta.get("last_updated")}}
            except OSError as e:
                errs.append("FRED " + str(e))
        if sid in MACRO_FALLBACK:
            try:
                pts, label = _from_fallback(db, sid)
                return {"id": sid, "points": sorted([k, v] for k, v in pts.items()), "source": label,
                        "note": ("; ".join(errs) + " - using " + label) if errs else ""}
            except OSError as e:
                errs.append(str(e))
        return {"id": sid, "points": [], "source": None,
                "error": "; ".join(errs) if errs else "This series needs a FRED API key (free); the keyless sources do not carry it."}
    out = _cached(db, "series:" + sid, MACRO_KEEP, make)
    if out.get("error"):          # do not keep a failure for hours
        db.execute("DELETE FROM macro_cache WHERE key=?", ("series:" + sid,))
        db.commit()
    return out


def macro_batch(ids):
    def one(sid):
        db = connect()
        try:
            return sid, macro_series(db, sid)
        except (ValueError, OSError) as e:
            return sid, {"id": sid, "points": [], "error": str(e)}
        finally:
            db.close()
    ids = list(dict.fromkeys(i.strip().upper() for i in ids if i.strip()))[:80]
    with concurrent.futures.ThreadPoolExecutor(6) as ex:
        return dict(ex.map(one, ids))


def macro_search(q):
    """Search all of FRED by popularity (needs a FRED API key)."""
    if not _fred_key():
        return {"results": [], "needs_key": True}
    try:
        d = _fred("series/search", search_text=q[:120], order_by="popularity", sort_order="desc", limit=40)
    except OSError as e:
        return {"results": [], "error": "FRED " + str(e)}
    return {"results": [{"id": s.get("id"), "title": s.get("title"), "units": s.get("units_short") or s.get("units"),
                         "freq": s.get("frequency"), "sa": s.get("seasonal_adjustment_short"), "start": s.get("observation_start"),
                         "end": s.get("observation_end"), "popularity": s.get("popularity"), "notes": (s.get("notes") or "")[:600]}
                        for s in d.get("seriess") or []]}


def macro_explain(body):
    """A plain-words explanation of a chart from Claude (no tools; about 20 seconds), kept for the session."""
    key = hashlib.sha1(json.dumps(body, sort_keys=True).encode()).hexdigest()
    if key in _EXPLAIN_CACHE:
        return _EXPLAIN_CACHE[key]
    claude = _claude_bin()
    if not claude:
        raise ValueError("Claude Code (the claude command) was not found on this computer.")
    prompt = ("You explain economic charts to an investor who is not an economist. Here is a chart, as JSON:\n\n"
              + json.dumps(body, ensure_ascii=False, indent=1)[:12000] + """

Explain it in plain words, in Markdown, in at most about 300 words, under these headings:
### What the lines measure
### What the chart shows
(over the range shown, with the actual numbers given above)
### How they connect
(the economic mechanism linking the lines, if any, and the usual lag; say plainly if they are not closely linked)
### What to watch
Do not invent figures that are not in the data above.""")
    r = subprocess.run([claude, "-p", "--output-format", "json", "--tools", "", "--strict-mcp-config", "--no-session-persistence"],
                       input=prompt, capture_output=True, text=True, timeout=240)
    try:
        text = (json.loads(r.stdout).get("result") or "").strip()
    except ValueError:
        text = ""
    if not text:
        raise ValueError("Claude did not answer: " + (r.stderr or r.stdout)[-300:])
    _EXPLAIN_CACHE[key] = {"text": text}
    return _EXPLAIN_CACHE[key]


DIAGRAMS_DIR = os.path.join(HERE, "diagrams")   # text diagrams redrawn as Mermaid, one file each (git-ignored)
_DIAGRAM_SLOTS = threading.Semaphore(2)          # Claude runs at most two at once, however many a report has
MERMAID_START = re.compile(r"^(flowchart|graph|sequenceDiagram|stateDiagram(-v2)?|classDiagram|erDiagram|journey|gantt|mindmap|timeline|block-beta|quadrantChart)\b")


def report_diagram(text):
    """A diagram drawn in plain text (boxes and arrows in a code block) redrawn as Mermaid by Claude, kept on disk so
    each one is drawn once. {"mermaid": code}."""
    text = (text or "").strip("\n")
    if not text.strip() or len(text) > 8000:
        raise ValueError("send the text of one diagram (at most 8,000 characters)")
    key = hashlib.sha1(text.encode("utf-8")).hexdigest()
    path = os.path.join(DIAGRAMS_DIR, key + ".mmd")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return {"mermaid": f.read(), "cached": True}
    claude = _claude_bin()
    if not claude:
        raise ValueError("Claude Code (the claude command) was not found on this computer, so text diagrams stay as text.")
    prompt = ("Redraw this plain-text diagram from a research report as a Mermaid diagram.\n\n" + text + """

Rules:
- Keep every box, actor, step number, label, amount and arrow it shows, and what each arrow means. Add nothing.
- Keep the labels in the diagram's own language.
- Pick the Mermaid type that fits. When parties pass messages or money to each other in numbered or timed steps, use
  sequenceDiagram (participants across the top, one arrow per step, longer detail in "Note over" lines). Use flowchart TD
  for structures, hierarchies and chains, and flowchart LR only for a short chain of at most five boxes.
- Keep arrow labels short (at most about eight words); put the rest of the detail in node labels or notes.
- Put every node label in double quotes, write a double quote inside a label as #quot;, and break long labels with <br/>.
- Never use a semicolon inside any text; use a comma.
- Group related nodes in subgraphs when the text groups them.
- Answer with the Mermaid code only: no code fence, no explanation.""")
    with _DIAGRAM_SLOTS:
        if os.path.exists(path):   # drawn while this one waited
            with open(path, encoding="utf-8") as f:
                return {"mermaid": f.read(), "cached": True}
        r = subprocess.run([claude, "-p", "--output-format", "json", "--tools", "", "--strict-mcp-config", "--no-session-persistence"],
                           input=prompt, capture_output=True, text=True, timeout=240)
    try:
        code = (json.loads(r.stdout).get("result") or "").strip()
    except ValueError:
        code = ""
    code = re.sub(r"^```(?:mermaid)?\s*|\s*```$", "", code).strip()
    if not MERMAID_START.match(code):
        raise ValueError("Claude did not answer with a diagram" + (": " + (r.stderr or "")[-200:] if r.stderr else ""))
    os.makedirs(DIAGRAMS_DIR, exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        f.write(code)
    os.replace(path + ".tmp", path)
    return {"mermaid": code, "cached": False}


def restart_when_changed(path=os.path.abspath(__file__), every=2.0):
    """Restart the server by itself when this file is edited, so a new version is never left unloaded."""
    started = os.path.getmtime(path)

    def watch():
        while True:
            time.sleep(every)
            try:
                if os.path.getmtime(path) == started:
                    continue
                with open(path, encoding="utf-8") as f:
                    compile(f.read(), path, "exec")   # an edit that is half-written or broken: keep running the old version
            except (OSError, SyntaxError):
                continue
            print("sec_db.py changed: restarting to load the new version...", flush=True)
            time.sleep(0.3)
            os.execv(sys.executable, [sys.executable] + sys.argv)

    threading.Thread(target=watch, daemon=True).start()


def cmd_serve(db, args):
    s = load_settings()
    client = SecClient(s["name"], s["email"])
    ThreadingHTTPServer.allow_reuse_address = True
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(client))
    except OSError as e:
        if e.errno in (errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", -1)):
            sys.exit(f"Port {args.port} is already in use, probably by Diligence running in another window: "
                     f"open http://localhost:{args.port}/ or stop the other one first. "
                     f"To use another port: python3 sec_db.py serve --port {args.port + 1}")
        raise
    restart_when_changed()
    threading.Thread(target=research_resume, daemon=True).start()
    print(f"Diligence desk: http://localhost:{args.port}/")
    print(f"JSON API example:    http://localhost:{args.port}/api/desk/fundamentals/MSFT")
    print("Leave this window open while you use the page. Ctrl+C stops it.")
    print("It restarts by itself whenever sec_db.py is updated.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


def cmd_fetch(db, args):
    s = load_settings()
    client = SecClient(s["name"], s["email"])
    for t in args.tickers:
        cik = fetch_company(db, client, t, docs=args.docs, insiders=args.insiders)
        fetch_debt_split(db, client, cik)  # only for companies with no debt total in quarterly reports (Caterpillar)
    if len(args.tickers) == 1:
        args.ticker, args.years = args.tickers[0], 6
        print()
        cmd_show(db, args)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Free local stock database from SEC EDGAR.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("fetch", help="download or refresh one or more companies")
    p.add_argument("tickers", nargs="+")
    p.add_argument("--docs", type=int, default=1,
                   help="how many recent annual reports to store as text (0 = none, default 1)")
    p.add_argument("--insiders", type=int, default=40,
                   help="how many recent Form 4 insider filings to read (0 = none, default 40)")
    p.set_defaults(fn=cmd_fetch)
    p = sub.add_parser("show", help="print annual financial summary")
    p.add_argument("ticker")
    p.add_argument("--years", type=int, default=6)
    p.set_defaults(fn=cmd_show)
    sub.add_parser("list", help="list companies in the database").set_defaults(fn=cmd_list)
    p = sub.add_parser("search", help="search stored annual report text")
    p.add_argument("query")
    p.add_argument("--ticker")
    p.set_defaults(fn=cmd_search)
    p = sub.add_parser("export", help="write a company's data to JSON")
    p.add_argument("ticker")
    p.add_argument("--out")
    p.set_defaults(fn=cmd_export)
    p = sub.add_parser("serve", help="run the Diligence desk and JSON API")
    p.add_argument("--port", type=int, default=8765)
    p.set_defaults(fn=cmd_serve)
    args = ap.parse_args(argv)
    db = connect()
    try:
        args.fn(db, args)
    finally:
        db.close()


if __name__ == "__main__":
    main()
