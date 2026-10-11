"""Manitoba RST, Saskatchewan assisted reading, federal corporate names, and the Québec NR list fallbacks."""

import contextlib
import datetime as dt
import tempfile
import unittest
from pathlib import Path

import requests

from taxcheck.checks import RowChecker
from taxcheck.fedcorp import FederalCorporations
from taxcheck.gst import GstChecker
from taxcheck.mb_rst import name_variants as mb_names
from taxcheck.mb_rst import parse_results
from taxcheck.numbers import BadNumber, parse_mb_rst
from taxcheck.qst import QstChecker
from taxcheck.result import INVALID, MANUAL, MISSING, NOT_CONFIRMED, REGISTERED, Result
from taxcheck.sk_pst import classify, search_terms

TODAY = dt.date.today()


class Manitoba(unittest.TestCase):
    def test_rst_number(self):
        self.assertEqual(parse_mb_rst("RST 123-4567"), "1234567")
        with self.assertRaisesRegex(BadNumber, "15-digit"):
            parse_mb_rst("123456789012345")
        with self.assertRaises(BadNumber):
            parse_mb_rst("12345")

    def test_name_variants_end_with_first_word(self):
        self.assertEqual(mb_names("The Amazon Company Inc", "AMZ"), ["The Amazon Company Inc", "Amazon Company Inc", "AMZ", "Amazon"])

    def test_results_table(self):
        # Rows as read from the live TAXcess results page.
        rows = [[""], ["Confirmation #:"], [""], [":$0.00\xa0:\xa0:"],
                ["Status", "Legal Name(s)", "Operating As Name(s)"], ["Status", "Legal Name(s)", "Operating As Name(s)"],
                ["Active", "AMAZON.COM.CA ULC", ""]]
        self.assertEqual(parse_results(rows), [("Active", "AMAZON.COM.CA ULC", "")])


class Saskatchewan(unittest.TestCase):
    def test_search_terms_need_four_characters(self):
        self.assertEqual(search_terms("The ABC", "Prairie Co", None), ["The ABC", "Prairie Co"])

    def test_vendor_licence(self):
        rows = [["Business Name", "Type"], ["PRAIRIE CO LTD", "Vendor's Licence"]]
        self.assertEqual(classify(rows, "", ["Prairie Co"])[0], REGISTERED)

    def test_registered_consumer(self):
        status, detail, _ = classify([["PRAIRIE CO LTD", "Registered Consumer"]], "", ["Prairie Co"])
        self.assertEqual(status, REGISTERED)
        self.assertIn("can't be used for tax-exempt purchases", detail)

    def test_other_names_only(self):
        status, detail, _ = classify([["OTHER BUSINESS INC", "Vendor's Licence"]], "", ["Prairie Co"])
        self.assertEqual(status, NOT_CONFIRMED)
        self.assertIn("OTHER BUSINESS INC", detail)

    def test_no_results(self):
        self.assertIn("no business", classify([], "No records found for your search.", ["Prairie Co"])[1])


class FakeCorps:
    def __init__(self, names):
        self._names = names
        self.calls = 0
        self.error = None

    def names(self, bn9):
        self.calls += 1
        return self._names


def gst_with(answers, corps):
    g = GstChecker(delay=0, corporations=corps)
    g.simplified.lookup = lambda bn9, date: None
    g.tried = []

    def lookup(bn9, name, date):
        g.tried.append(name)
        return answers.get(name, "no_match")

    g.registry_lookup = lookup
    return g


class FederalNames(unittest.TestCase):
    def test_retries_with_official_name(self):
        g = gst_with({"North American Tutors Inc.": "registered"}, FakeCorps(["North American Tutors Inc."]))
        res = g.check("774075766", ["NA Tutors"], TODAY)
        self.assertEqual(res.status, REGISTERED)
        self.assertIn("official corporate name", res.detail)
        self.assertEqual(g.tried, ["NA Tutors", "North American Tutors Inc."])

    def test_not_loaded_when_sheet_name_matches(self):
        corps = FakeCorps(["X"])
        gst_with({"Good Name": "registered"}, corps).check("774075766", ["Good Name"], TODAY)
        self.assertEqual(corps.calls, 0)

    def test_official_name_also_fails(self):
        res = gst_with({}, FakeCorps(["Official Ltd."])).check("774075766", ["Sheet Name"], TODAY)
        self.assertEqual(res.status, NOT_CONFIRMED)
        self.assertIn("most likely not registered", res.detail)
        self.assertEqual(res.registered_name, "Official Ltd.")

    def test_csv_loading_from_cache(self):
        with tempfile.TemporaryDirectory() as d:
            header = "Corporation number,Business number (BN),Corporate name - form 1,Corporate name - form 2,Status\n"
            Path(d, "corporations-active-cbca-en.csv").write_text(
                "﻿" + header + "1,774075766,North American Tutors Inc.,Tuteurs Nord-Américains Inc.,Active\n2,,No BN Inc.,,Active\n",
                encoding="utf-8")
            Path(d, "corporations-active-non-cbca-en.csv").write_text(header + "3,123456782,Other Org,,Active\n", encoding="utf-8")
            corps = FederalCorporations(cache_dir=d, session=object())  # cached files are fresh: no download
            self.assertEqual(corps.names("774075766"), ["North American Tutors Inc.", "Tuteurs Nord-Américains Inc."])
            self.assertEqual(corps.names("123456782"), ["Other Org"])
            self.assertEqual(corps.names("999999999"), [])


class BlockedSession:
    def get(self, url, timeout=None):
        response = requests.Response()
        response.status_code = 403
        return response


NR_PAGE = "<table><tr><td>ExampleStream</td><td>ExampleStream B.V.</td><td>NR 0013 0061</td></tr></table>"


class FakeBrowser:
    def __init__(self, html):
        self.html = html
        self.loads = 0

    @contextlib.contextmanager
    def page(self):
        browser = self

        class Page:
            def goto(self, url, **kw):
                browser.loads += 1

            def content(self):
                return browser.html

        yield Page()


class QuebecNrList(unittest.TestCase):
    def test_browser_fallback_then_cache(self):
        with tempfile.TemporaryDirectory() as d:
            browser = FakeBrowser(NR_PAGE)
            q = QstChecker(browser=browser, cache_dir=d)
            q.session = BlockedSession()
            self.assertEqual(q.check_nr("NR00130061").status, REGISTERED)
            self.assertEqual(browser.loads, 1)
            self.assertTrue(Path(d, "qst_nr_list.html").exists())

            q2 = QstChecker(browser=FakeBrowser(""), cache_dir=d)  # a second run reuses the cached copy
            q2.session = BlockedSession()
            self.assertEqual(q2.check_nr("NR00130061").status, REGISTERED)

    def test_everything_blocked_explains_what_to_do(self):
        q = QstChecker(browser=FakeBrowser("<html>blocked</html>"))
        q.session = BlockedSession()
        res = q.check_nr("NR00130061")
        self.assertEqual(res.status, "ERROR")
        self.assertIn("--nr-list", res.detail)


class Routing(unittest.TestCase):
    class Gst:
        def check(self, bn9, names, date):
            return Result(REGISTERED, "")

    class Mb:
        def __init__(self):
            self.calls = []

        def check(self, names, bn9=None, rst=None):
            self.calls.append((bn9, rst))
            return Result(REGISTERED, "")

    def test_manitoba_uses_bn_from_gst_number(self):
        mb = self.Mb()
        out = RowChecker(self.Gst(), None, mb=mb).check({"name": "A", "province": "MB", "gst": "857305932RT0001"}, TODAY)
        self.assertEqual(out["mb_rst"].status, REGISTERED)
        self.assertEqual(mb.calls, [("857305932", None)])

    def test_manitoba_rst_number_column(self):
        mb = self.Mb()
        RowChecker(self.Gst(), None, mb=mb).check({"name": "A", "province": "MB", "pst": "1234567"}, TODAY)
        self.assertEqual(mb.calls, [(None, "1234567")])

    def test_manitoba_bad_rst(self):
        out = RowChecker(self.Gst(), None, mb=self.Mb()).check({"name": "A", "mb_rst": "12"}, TODAY)
        self.assertEqual(out["mb_rst"].status, INVALID)

    def test_manitoba_bad_gst_number_falls_back_to_rst(self):
        mb = self.Mb()
        RowChecker(self.Gst(), None, mb=mb).check({"name": "A", "province": "MB", "gst": "123456789", "pst": "1234567"}, TODAY)
        self.assertEqual(mb.calls, [(None, "1234567")])

    def test_bc_without_business_number_is_missing_data(self):
        out = RowChecker(self.Gst(), None).check({"name": "A", "province": "BC", "pst": "PST-1000-7572"}, TODAY)
        self.assertEqual(out["bc_pst"].status, MISSING)

    def test_manitoba_nothing_to_search_with(self):
        out = RowChecker(self.Gst(), None, mb=self.Mb()).check({"name": "A", "province": "MB"}, TODAY)
        self.assertEqual(out["mb_rst"].status, MISSING)

    def test_pst_with_unknown_province_is_warned_about(self):
        with self.assertLogs("taxcheck.checks", "WARNING") as logs:
            out = RowChecker(self.Gst(), None).check({"name": "A", "province": "ON", "pst": "999"}, TODAY)
        self.assertEqual(set(out), {"gst"})
        self.assertIn("isn't BC, SK or MB", logs.output[0])

    def test_saskatchewan_number_is_noted_as_unused(self):
        class Sk:
            def check(self, names):
                return Result(REGISTERED, "Saskatchewan PST registry: holds a vendor's licence.")

        out = RowChecker(self.Gst(), None, sk=Sk()).check({"name": "Prairie Co", "province": "SK", "pst": "1234567"}, TODAY)
        self.assertIn("wasn't used", out["sk_pst"].detail)

    def test_saskatchewan_without_assist_is_manual(self):
        out = RowChecker(self.Gst(), None).check({"name": "Prairie Co", "province": "SK"}, TODAY)
        self.assertEqual(out["sk_pst"].status, MANUAL)
        self.assertIn("--sk-assist", out["sk_pst"].detail)


if __name__ == "__main__":
    unittest.main()
