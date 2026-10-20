"""Digital-economy (simplified GST/HST) registrants among vendors with no usable Canadian number.

The list entries are CRA's published format; AIRGSM PTE. LTD. (Airalo) is a public simplified registrant
already used in the live tests. The registry is a stand-in that answers the way CRA does.
"""

import datetime as dt
import unittest

from taxcheck.checks import RowChecker
from taxcheck.gst import GstChecker, SimplifiedList
from taxcheck.report import gst_issue, row_summary
from taxcheck.result import MISSING, NOT_APPLICABLE, REGISTERED

DATE = dt.date(2026, 9, 1)
PAGE = """<table>
<tr><td>AIRGSM PTE. LTD.</td><td>Airalo</td><td>751950577 RT9999</td><td>July 1, 2021</td><td>-</td></tr>
<tr><td>Canvas Media Ltd</td><td>-</td><td>123456782 RT9999</td><td>July 1, 2021</td><td>-</td></tr>
<tr><td>BRIGHTLY PTY LTD</td><td>-</td><td>111111118 RT9999</td><td>July 1, 2023</td><td>-</td></tr>
<tr><td>BRIGHTLY PTY LTD</td><td>-</td><td>111111118 RT0001</td><td>July 1, 2021</td><td>June 30, 2023</td></tr>
<tr><td>Hexwave Technologies, Inc.</td><td>-</td><td>222222226 RT9999</td><td>July 1, 2021</td><td>-</td></tr>
<tr><td>Tidal Docs Ireland Limited</td><td>-</td><td>333333334 RT9999</td><td>July 1, 2021</td><td>-</td></tr>
<tr><td>Orbit Labs LLC</td><td>-</td><td>444444440 RT9999</td><td>July 1, 2021</td><td>-</td></tr>
<tr><td>Orbit Software Ltd</td><td>-</td><td>555555556 RT9999</td><td>July 1, 2021</td><td>-</td></tr>
</table>"""


class FakeList(SimplifiedList):
    def __init__(self):
        super().__init__(session=None)
        self._rows = self.parse(PAGE)


def checker(registered=("751950577",)):
    g = GstChecker(delay=0)
    g.simplified = FakeList()
    g.asked = []

    def lookup(bn9, name, date, attempts=3):
        g.asked.append((bn9, name))
        return "registered" if bn9 in registered else "no_match"

    g.registry_lookup = lookup
    return g, RowChecker(g, None, available={"name", "gst", "country"})


class DigitalEconomy(unittest.TestCase):
    def test_foreign_vendor_on_the_list_is_found_and_confirmed(self):
        g, rc = checker()
        res = rc.check({"name": "AIRGSM PTE LTD", "gst": "123-45-6789", "country": "SG"}, DATE)["gst"]
        self.assertEqual(res.status, REGISTERED)
        self.assertEqual(res.facts["number_checked"], "751950577RT9999")
        self.assertEqual(res.facts["registered_since"], "2021-07-01")
        self.assertEqual(g.asked, [("751950577", "AIRGSM PTE. LTD.")])  # confirmed under the listed legal name
        issue, action, _ = gst_issue(res)
        self.assertEqual(issue, "Simplified (digital economy) registrant: number not on file")
        self.assertIn("give them our GST/HST number", action)
        self.assertGreaterEqual(row_summary({"gst": res}, [("gst", "GST/HST")])["severity"], 5)  # Overall REVIEW

    def test_trade_name_matches_too(self):
        _, rc = checker()
        res = rc.check({"name": "Airalo", "gst": None, "country": "US"}, DATE)["gst"]
        self.assertEqual(res.status, REGISTERED)

    def test_partial_or_common_word_names_dont_match(self):
        g, rc = checker(registered=("751950577", "123456782"))
        for name in ("AIRGSM HOLDINGS INC", "MEDIA LTD", "CANVAS MEDIA GROUP"):
            res = rc.check({"name": name, "gst": "910000001", "country": "US"}, DATE)["gst"]
            self.assertEqual(res.status, NOT_APPLICABLE, name)
            self.assertEqual(res.facts["number_checked"], "")  # a US EIN isn't looked up, so it isn't reported as one
        self.assertEqual(g.asked, [])

    def test_not_confirmed_by_cra_stays_not_applicable(self):
        g, rc = checker(registered=())
        res = rc.check({"name": "AIRGSM PTE. LTD.", "gst": None, "country": "SG"}, DATE)["gst"]
        self.assertEqual(res.status, NOT_APPLICABLE)
        self.assertEqual(len(g.asked), 1)

    def test_canadian_row_with_no_number(self):
        _, rc = checker()
        res = rc.check({"name": "Canvas Media Ltd.", "gst": None, "country": "CA"}, DATE)["gst"]
        self.assertEqual(res.status, MISSING)  # on the list, but CRA didn't confirm it
        _, rc = checker(registered=("123456782",))
        res = rc.check({"name": "Canvas Media Ltd.", "gst": None, "country": "CA"}, DATE)["gst"]
        self.assertEqual(res.status, REGISTERED)
        self.assertIn("No GST/HST number provided", res.detail)

    def test_confirmation_is_asked_once_per_number_and_date(self):
        g, rc = checker()
        for _ in range(3):
            rc.check({"name": "Airalo", "gst": None, "country": "US"}, DATE)
        self.assertEqual(len(g.asked), 1)



ALL = ("751950577", "123456782", "111111118", "222222226", "333333334", "444444440", "555555556")


class LooserNames(unittest.TestCase):
    """Vendor-master spellings of listed sellers, measured on CRA's real list (2026-10-20): foreign legal forms
    ("Pty", "AB"), regions ("Ireland"), ".com" and words like "Technologies" left out. Such a match is
    'probable' (flagged for a person) and only for foreign vendors."""

    def status(self, name, country="US", gst=None, registered=ALL):
        _, rc = checker(registered=registered)
        return rc.check({"name": name, "gst": gst, "country": country}, DATE)["gst"]

    def test_foreign_legal_form_is_an_exact_match(self):
        res = self.status("BRIGHTLY")  # listed BRIGHTLY PTY LTD: "Pty" is a legal form
        self.assertEqual(res.status, REGISTERED)
        self.assertEqual(res.facts["number_checked"], "111111118RT9999")  # the current account, not RT0001

    def test_descriptor_or_region_left_out_is_probable(self):
        for name in ("HEXWAVE", "TIDAL DOCS"):
            res = self.status(name)
            self.assertEqual(res.status, REGISTERED, name)
            self.assertEqual(res.facts["digital_economy_match"], "probable", name)
            self.assertIn("First check that this vendor is", gst_issue(res)[1])
        self.assertEqual(self.status("CANVAS").facts["digital_economy_match"], "probable")  # Canvas Media Ltd

    def test_descriptors_are_only_dropped_from_the_listed_name(self):
        self.assertEqual(self.status("HEXWAVE MEDIA INC").status, NOT_APPLICABLE)  # not "Hexwave Technologies"

    def test_probable_only_when_one_business_fits(self):
        self.assertEqual(self.status("ORBIT").status, NOT_APPLICABLE)  # Orbit Labs and Orbit Software both fit

    def test_probable_only_for_foreign_vendors(self):
        self.assertEqual(self.status("HEXWAVE", country="CA").status, MISSING)

    def test_card_statement_style_name(self):
        self.assertEqual(self.status("BRIGHTLY *DESIGN").status, REGISTERED)

    def test_bare_nine_digit_number_on_the_list_is_not_an_ein(self):
        res = self.status("SOMETHING ELSE", gst="111111118")
        self.assertNotEqual(res.status, NOT_APPLICABLE)
        self.assertEqual(res.facts["number_checked"], "111111118")
        self.assertIn("111111118RT9999", res.facts["tax_id_problem"])

    def test_old_rt0001_number_is_flagged(self):
        res = self.status("BRIGHTLY PTY LTD", country="AU", gst="111111118RT0001")
        self.assertIn("111111118RT9999", res.facts["tax_id_problem"])
        self.assertEqual(self.status("BRIGHTLY PTY LTD", country="AU", gst="111111118RT9999").facts["tax_id_problem"], "")


if __name__ == "__main__":
    unittest.main()
