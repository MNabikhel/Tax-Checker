"""Live checks against the real government services, using publicly published registrations.

Skipped unless TAXCHECK_LIVE=1, because they need internet access and take a minute or two:

    TAXCHECK_LIVE=1 python -m unittest tests.test_live -v

Run these when something looks off: a failure here usually means a government site changed.
"""

import datetime as dt
import os
import unittest

from taxcheck.gst import GstChecker
from taxcheck.qst import QstChecker
from taxcheck.result import INVALID, NOT_CONFIRMED, NOT_REGISTERED, REGISTERED

LIVE = os.environ.get("TAXCHECK_LIVE") == "1"
TODAY = dt.date.today()


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
class LiveBc(unittest.TestCase):
    def test_etaxbc(self):
        try:
            from taxcheck.bc_pst import BcPstChecker
            bc = BcPstChecker().__enter__()
        except Exception as e:
            self.skipTest(f"browser unavailable: {e}")
        try:
            self.assertEqual(bc.check("108161779", "1000-7572").status, REGISTERED)
            self.assertEqual(bc.check("108161779", "1000-7573").status, NOT_CONFIRMED)
        finally:
            bc.__exit__(None, None, None)


if __name__ == "__main__":
    unittest.main()
