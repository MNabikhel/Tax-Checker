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
        for name in ("AIRGSM HOLDINGS INC", "CANVAS", "MEDIA LTD", "CANVAS MEDIA GROUP"):
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


if __name__ == "__main__":
    unittest.main()
