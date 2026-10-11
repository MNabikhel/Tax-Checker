"""Live checks against the real government services, using publicly published registrations.

Skipped unless TAXCHECK_LIVE=1, because they need internet access and take a minute or two:

    TAXCHECK_LIVE=1 python -m unittest tests.test_live -v

The Saskatchewan test also needs a person: with TAXCHECK_SK_ASSIST=1 it opens a browser window where
you tick the registry's "I'm not a robot" box, then searches automatically (up to 5 minutes to tick it).

Run these when something looks off: a failure here usually means a government site changed.
"""

import datetime as dt
import os
import unittest

from taxcheck.gst import GstChecker, cra_today
from taxcheck.qst import QstChecker
from taxcheck.result import INVALID, NOT_CONFIRMED, NOT_REGISTERED, REGISTERED

LIVE = os.environ.get("TAXCHECK_LIVE") == "1"


def reachable(url):
    """True if the site answers at all, so an outage is reported as a skip rather than a code failure."""
    import requests

    try:
        requests.get(url, timeout=20)
        return True
    except requests.RequestException:
        return False
TODAY = cra_today()


@unittest.skipUnless(LIVE, "set TAXCHECK_LIVE=1 to run live tests")
class LiveCra(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gst = GstChecker(delay=1.0)

    def test_registry_outcomes(self):
        lookup = self.gst.registry_lookup
        self.assertEqual(lookup("857305932", "Amazon.com.ca ULC", TODAY), "registered")
        self.assertEqual(lookup("857305932", "Wrong Name Co", TODAY), "no_match")
        self.assertEqual(lookup("857305932", "Amazon.com.ca ULC", dt.date(1990, 1, 1)), "not_registered")
        # CRA answers "not registered on this date" before it looks at the name.
        self.assertEqual(lookup("857305932", "Wrong Name Co", dt.date(1990, 1, 1)), "not_registered")

    def test_full_check_with_name_variant(self):
        res = self.gst.check("108161779", ["The University of British Columbia"], TODAY)
        self.assertEqual(res.status, REGISTERED, res.detail)

    def test_simplified_registrant(self):
        res = self.gst.check("751950577", ["AIRGSM PTE. LTD."], TODAY)
        self.assertEqual(res.status, REGISTERED, res.detail)
        self.assertIsNone(self.gst.simplified.error)
        self.assertGreater(len(self.gst.simplified._rows), 1000)
        self.assertIn("input tax credits", res.detail)

    def test_not_registered_before_registration(self):
        res = self.gst.check("857305932", ["Amazon.com.ca ULC"], dt.date(1990, 1, 1))
        self.assertEqual(res.status, NOT_REGISTERED, res.detail)


@unittest.skipUnless(LIVE, "set TAXCHECK_LIVE=1 to run live tests")
class LiveQst(unittest.TestCase):
    def test_api(self):
        qst = QstChecker()
        res = qst.check("1019288451TQ0004")
        self.assertEqual(res.status, REGISTERED, res.detail)
        self.assertIn("AVIENT", res.registered_name)
        self.assertEqual(qst.check("1234567890TQ0001").status, INVALID)
        self.assertEqual(qst.check("1019288451TQ0001").status, NOT_REGISTERED)  # cancelled account


@unittest.skipUnless(LIVE, "set TAXCHECK_LIVE=1 to run live tests")
class LiveBrowserChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from taxcheck.browser import Browser

        cls.browser = Browser().__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.browser.__exit__(None, None, None)

    def test_etaxbc(self):
        from taxcheck.bc_pst import ETAXBC, BcPstChecker

        if not reachable(ETAXBC):
            self.skipTest("eTaxBC isn't responding (it has scheduled maintenance windows); try again later")

        bc = BcPstChecker(self.browser)
        self.assertEqual(bc.check("108161779", "1000-7572").status, REGISTERED)
        self.assertEqual(bc.check("108161779", "1000-7573").status, NOT_CONFIRMED)

    def test_manitoba_taxcess(self):
        from taxcheck.mb_rst import MbRstChecker

        mb = MbRstChecker(self.browser)
        res = mb.check(["Amazon Canada Fulfillment"], bn9="857305932")  # falls back to the first word
        self.assertEqual(res.status, REGISTERED, res.detail)
        self.assertIn("AMAZON.COM.CA ULC", res.registered_name)
        self.assertEqual(mb.check(["University of British Columbia"], bn9="108161779").status, NOT_CONFIRMED)


@unittest.skipUnless(LIVE, "set TAXCHECK_LIVE=1 to run live tests")
class LiveQuebecNrList(unittest.TestCase):
    def test_nr_list_loads_and_checks(self):
        from taxcheck.browser import Browser

        with Browser() as browser:
            qst = QstChecker(browser=browser)  # no cache: always the live page
            rows = qst._load_nr_list()
        # Skip only if every source was refused outright (Revenu Québec blocks some networks). A page that
        # loads but yields no NR numbers is a real failure: the parser needs updating.
        if not rows and qst.nr_refusals and "had no NR numbers" not in (qst.nr_error or ""):
            self.skipTest(f"Revenu Québec refused this network: {qst.nr_error}")
        self.assertIsNone(qst.nr_error)
        self.assertGreater(len(rows), 1000, "the NR list should have ~2,000 registrants")
        number, entry = next(iter(rows.items()))
        self.assertTrue(entry["legal_name"] or entry["trade_name"], f"no name parsed for {number}: {entry}")
        self.assertEqual(qst.check_nr(number).status, REGISTERED)
        print(f"\n  NR list: {len(rows)} registrants; first: {number} {entry}")


@unittest.skipUnless(LIVE and os.environ.get("TAXCHECK_SK_ASSIST") == "1",
                     "set TAXCHECK_LIVE=1 and TAXCHECK_SK_ASSIST=1 (needs a person to tick a CAPTCHA)")
class LiveSaskatchewanAssisted(unittest.TestCase):
    def test_known_vendor(self):
        from taxcheck.browser import Browser
        from taxcheck.sk_pst import SkPstAssistant

        with Browser(headless=False, debug_dir=".") as browser:
            sk = SkPstAssistant(browser)
            # Federated Co-operatives Limited (Saskatoon) sells at retail across Saskatchewan.
            res = sk.check(["Federated Co-operatives Limited"])
        print(f"\n  SK result: {res.status} | {res.detail} | {res.registered_name}")
        self.assertEqual(res.status, REGISTERED, "check the sk_*.png screenshot and the log, then adjust sk_pst.classify()")


@unittest.skipUnless(LIVE, "set TAXCHECK_LIVE=1 to run live tests")
class LiveFederalNames(unittest.TestCase):
    def test_name_mismatch_recovered(self):
        from taxcheck.fedcorp import FederalCorporations

        gst = GstChecker(delay=1.0, corporations=FederalCorporations(cache_dir="cache"))
        res = gst.check("774075766", ["NA Tutors"], TODAY)  # sheet name doesn't match CRA's
        self.assertEqual(res.status, REGISTERED, res.detail)
        self.assertIn("North American Tutors", res.detail)


if __name__ == "__main__":
    unittest.main()
