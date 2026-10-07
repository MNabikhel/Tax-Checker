"""GstChecker decision logic, with the CRA registry and simplified list replaced by fakes."""

import datetime as dt
import unittest

from taxcheck.gst import GstChecker
from taxcheck.result import INVALID, NOT_CONFIRMED, NOT_REGISTERED, REGISTERED

TODAY = dt.date.today()


class FakeList:
    def __init__(self, entry=None):
        self.entry = entry
        self.error = None

    def lookup(self, bn9, date):
        return self.entry


def checker(answers, listed=None):
    """answers: {name: outcome}; any other name gets 'no_match'."""
    g = GstChecker(delay=0)
    g.simplified = FakeList(listed)
    g.calls = []

    def lookup(bn9, name, date):
        g.calls.append((name, date))
        return answers.get(name, "no_match")

    g.registry_lookup = lookup
    return g


class GstLogic(unittest.TestCase):
    def test_retries_without_the_then_trade_name(self):
        g = checker({"Example Trade": "registered"})
        res = g.check("108161779", ["The Example Co", "Example Trade"], TODAY)
        self.assertEqual(res.status, REGISTERED)
        self.assertEqual([n for n, _ in g.calls], ["The Example Co", "Example Co", "Example Trade"])

    def test_stops_at_first_match(self):
        g = checker({"Example Co": "registered"})
        g.check("108161779", ["Example Co", "Other"], TODAY)
        self.assertEqual(len(g.calls), 1)

    def test_no_match_on_every_name(self):
        res = checker({}).check("108161779", ["A", "B"], TODAY)
        self.assertEqual(res.status, NOT_CONFIRMED)
        self.assertIn("'A'; 'B'", res.detail)

    def test_not_registered_on_date_is_definite(self):
        res = checker({"Example Co": "not_registered"}).check("108161779", ["Example Co"], TODAY)
        self.assertEqual(res.status, NOT_REGISTERED)

    def test_invalid(self):
        self.assertEqual(checker({"X": "invalid"}).check("108161779", ["X"], TODAY).status, INVALID)

    def test_future_date_checked_as_today(self):
        g = checker({"X": "registered"})
        res = g.check("108161779", ["X"], TODAY + dt.timedelta(days=30))
        self.assertEqual(g.calls[0][1], TODAY)
        self.assertIn("in the future", res.detail)

    def test_simplified_list_names_are_tried_and_noted(self):
        listed = {"legal_name": "AIRGSM PTE. LTD.", "trade_name": "Airalo", "number": "751950577RT9999",
                  "registered": dt.date(2026, 9, 1), "deregistered": None}
        g = checker({"Airalo": "registered"}, listed)
        res = g.check("751950577", ["Wrong Name"], TODAY)
        self.assertEqual(res.status, REGISTERED)
        self.assertIn("input tax credits", res.detail)
        self.assertEqual(res.registered_name, "AIRGSM PTE. LTD.")

    def test_simplified_deregistered(self):
        listed = {"legal_name": "OLD LTD", "trade_name": "", "number": "751950577RT9999",
                  "registered": dt.date(2020, 1, 1), "deregistered": dt.date(2021, 1, 1)}
        res = checker({}, listed).check("751950577", ["OLD LTD"], dt.date(2022, 1, 1))
        self.assertEqual(res.status, NOT_REGISTERED)
        self.assertIn("2021-01-01", res.detail)


if __name__ == "__main__":
    unittest.main()
