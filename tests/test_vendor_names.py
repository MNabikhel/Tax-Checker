"""Vendor-master names vs CRA's exact-name matching (from a live trial run, 2026-10-13: most lookups came
back REGISTERED - NAME NOT MATCHED, mostly because the name field holds 'LEGAL NAME O/A TRADE NAME')."""

import datetime as dt
import re
import unittest

from taxcheck.checks import RowChecker, same_business_name, vendor_name_candidates
from taxcheck.gst import BEFORE_GST, GstChecker, _name_key
from taxcheck.result import NAME_MATCHES, NAME_NOT_MATCHED, NAME_UNCONFIRMED, NAME_UPDATE, REGISTERED, Result


class Candidates(unittest.TestCase):
    def test_legal_part_first_then_variants_then_trade_name(self):
        self.assertEqual(vendor_name_candidates("9999901 ONTARIO INC. O/A MAPLE PRO"),
                         ["9999901 ONTARIO INC. O/A MAPLE PRO", "9999901 ONTARIO INC.", "9999901 ONTARIO INCORPORATED", "MAPLE PRO"])
        self.assertEqual(vendor_name_candidates("9999902 ONTARIO INC. (BIRCHWOOD)")[1], "9999902 ONTARIO INC.")
        self.assertEqual(vendor_name_candidates("0999903 B.C LTD DBA THE NORTHSTAR GROUP")[1], "0999903 B.C LTD")
        self.assertEqual(vendor_name_candidates("HARBOUR PRINTING O/A"), ["HARBOUR PRINTING O/A", "HARBOUR PRINTING"])
        self.assertIn("9999904 ALBERTA LTD.", vendor_name_candidates("RIDGE TOWING O/A 9999904 ALBERTA"))
        self.assertEqual(vendor_name_candidates("WIDGET SUPPLY OF SPOKANE         8515")[1], "WIDGET SUPPLY OF SPOKANE")
        self.assertEqual(vendor_name_candidates("ACME MOBILITY  #555000111222")[1], "ACME MOBILITY")
        self.assertEqual(vendor_name_candidates("NORTHFIELD SUPPLY INC -REMIT")[1], "NORTHFIELD SUPPLY INC")
        self.assertEqual(vendor_name_candidates("PRAIRIE EVENT SOLUTIONS"), ["PRAIRIE EVENT SOLUTIONS"])  # plain: no extra lookups
        self.assertEqual(vendor_name_candidates(None), [])
        self.assertLessEqual(len(vendor_name_candidates("9999910 ALBERTA LTD O/A RIVERBEND PUMPING (CALGARY) - REMIT")), 6)

    def test_more_vendor_master_shorthand(self):
        from taxcheck.checks import clean_vendor_name

        cases = {
            "(DNU) HARBOUR PRINTING O/A": ["HARBOUR PRINTING O/A", "HARBOUR PRINTING"],
            "[DO NOT USE] THE PAINT SHOP": ["THE PAINT SHOP"],
            "ACME* (INACTIVE) SUPPLIES - O.A.": ["ACME* (INACTIVE) SUPPLIES - O.A.", "ACME SUPPLIES"],
            "C.O.B. WIDGETS": ["C.O.B. WIDGETS", "WIDGETS"],
            "SMITH, JOHN DBA SMITH PLUMBING": ["SMITH, JOHN DBA SMITH PLUMBING", "SMITH, JOHN", "SMITH PLUMBING"],
            "JONES CARRYING ON BUSINESS AS JONES HVAC": ["JONES CARRYING ON BUSINESS AS JONES HVAC", "JONES", "JONES HVAC"],
        }
        for raw, expected in cases.items():
            self.assertEqual(vendor_name_candidates(clean_vendor_name(raw)[0]), expected, raw)
        # Status notes in brackets are dropped, never tried as a trade name.
        self.assertNotIn("OLD", vendor_name_candidates("ACME LTD (OLD)"))
        # Words that merely contain the shorthand are left alone.
        for name in ("COBALT MINING", "KOALA TOYS", "DOA SUPPLY", "COBRA COBBLERS"):
            self.assertEqual(vendor_name_candidates(name)[0], name)
            self.assertEqual(len(vendor_name_candidates(name)), 1)

    def test_official_name_comparison(self):
        self.assertTrue(same_business_name(["ACME WIDGET CANADA INC."], "AcmeWidget Canada Inc."))
        self.assertTrue(same_business_name(["ACME SUPPLY LTD"], "Acme Supply Limited"))
        self.assertTrue(same_business_name(["SUMMIT CONSTRUCTION INDUSTRIAL PRO"], "Summit Construction Industrial Products Ltd.",
                                           field_length=40))  # cut off at the vendor master's 40 characters
        self.assertFalse(same_business_name(["SUMMIT CONSTRUCTION"], "Summit Construction Industrial Products Ltd."))
        self.assertFalse(same_business_name(["SHOP-MOOSE"], "Moose Holdings Ltd."))
        self.assertFalse(same_business_name(["NORTHSTAR GROUP"], "NORTHSTAR SECURITY"))

    def test_name_key_follows_cra(self):
        # Pairs CRA answered the same way / differently in live tests on 2026-10-16 (see gst._name_key).
        same = [("University of British Columbia", "UNIVERSITY  OF   BRITISH COLUMBIA"),
                ("Amazon.com.ca ULC", "Amazon com ca ULC"), ("Amazon.com.ca ULC", "Amazon.com.ca, ULC"),
                ("Amazon.com.ca ULC", "Amazon&com&ca ULC"), ("Amazon.com.ca ULC", "Amazon.com.ca (ULC)"),
                ("University of British Columbia", "University.of.British.Columbia")]
        different = [("University of British Columbia", "UniversityofBritish Columbia"),
                     ("University of British Columbia", "University-of-British-Columbia"),
                     ("University of British Columbia", "University of British Columbia's"),
                     ("Amazon.com.ca ULC", "Amazon,com,ca ULC"), ("Amazon.com.ca ULC", "Amazon.com.ca U.L.C."),
                     ("ACME WIDGET CANADA INC.", "AcmeWidget Canada Inc."), ("0999911 B.C. LTD.", "0999911 BC LTD")]
        for a, b in same:
            self.assertEqual(_name_key(a), _name_key(b), (a, b))
        for a, b in different:
            self.assertNotEqual(_name_key(a), _name_key(b), (a, b))


class AgainstStrictRegistry(unittest.TestCase):
    """The real GstChecker logic against a stand-in registry that, like CRA, only accepts the exact legal name."""

    LEGAL = {"900000001": "9999901 ONTARIO INC.", "900000019": "ACMEWIDGET CANADA INC.", "900000027": "ALPINE FIRE SYSTEMS LTD."}
    OFFICIAL = {"900000019": ["AcmeWidget Canada Inc."]}

    def setUp(self):
        test = self

        class Source:
            label = "the BC registry (OrgBook BC)"

            def names(self, bn9):
                return test.OFFICIAL.get(bn9, [])

        self.gst = GstChecker(delay=0, name_sources=[Source()])
        self.gst.simplified.lookup = lambda bn9, date: None
        self.sent = []

        def lookup(bn9, name, date, attempts=3):
            self.sent.append(name)
            if date <= BEFORE_GST:
                return "not_registered"
            return "registered" if _name_key(name) == _name_key(self.LEGAL[bn9]) else "no_match"

        self.gst.registry_lookup = lookup
        self.checker = RowChecker(self.gst, None, available={"name", "gst"})

    def check(self, name, bn):
        return self.checker.check({"name": name, "gst": bn + "RT0001"}, dt.date(2026, 9, 1))["gst"]

    def test_operating_as_name_now_registered(self):
        res = self.check("9999901 ONTARIO INC. O/A MAPLE PRO", "900000001")
        self.assertEqual(res.status, REGISTERED)
        self.assertEqual(self.sent, ["9999901 ONTARIO INC. O/A MAPLE PRO", "9999901 ONTARIO INC."])

    def test_official_name_with_other_spacing_is_tried_and_confirms(self):
        res = self.check("ACME WIDGET CANADA INC.", "900000019")
        self.assertEqual(res.status, REGISTERED, res.detail)
        self.assertIn("AcmeWidget Canada Inc.", self.sent)
        self.assertIn("official name 'AcmeWidget Canada Inc.'", res.detail)

    def test_vendor_name_check_column(self):
        self.LEGAL["111111118"] = "EXACT NAME LTD."
        self.assertEqual(self.check("EXACT NAME LTD", "111111118").name_check, NAME_MATCHES)
        res = self.check("9999901 ONTARIO INC. O/A MAPLE PRO", "900000001")
        self.assertEqual((res.status, res.name_check), (REGISTERED, NAME_UPDATE))
        self.assertIn("CRA accepted '9999901 ONTARIO INC.'", res.detail)
        self.assertEqual(self.check("ACME WIDGET CANADA INC.", "900000019").name_check, NAME_UPDATE)
        self.assertEqual(self.check("ALPINE FIRE SYSTEMS INC", "900000027").name_check, NAME_UNCONFIRMED)

    def test_genuinely_different_legal_name_still_flagged(self):
        res = self.check("ALPINE FIRE SYSTEMS INC", "900000027")
        self.assertEqual(res.status, NAME_NOT_MATCHED)
        self.assertLessEqual(len(self.sent), 3)  # name, Incorporated, number-only check


if __name__ == "__main__":
    unittest.main()
