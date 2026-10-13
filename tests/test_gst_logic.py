"""GstChecker decision logic, with the CRA registry and simplified list replaced by fakes."""

import datetime as dt
import unittest

from taxcheck.gst import GstChecker, cra_today
from taxcheck.result import INVALID, NAME_NOT_MATCHED, NOT_CONFIRMED, NOT_REGISTERED, REGISTERED

TODAY = cra_today()


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

    def test_unknown_number_when_no_name_matches_at_any_date(self):
        # CRA answers "Insufficient information" for every date: it has no account under this number.
        res = checker({}).check("108161779", ["A", "B"], TODAY)
        self.assertEqual(res.status, NOT_REGISTERED)
        self.assertIn("no GST/HST account", res.detail)


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
        # Registered, but only under CRA's name for it, not the sheet's.
        self.assertEqual(res.status, NAME_NOT_MATCHED)
        self.assertIn("'Airalo'", res.detail)
        self.assertIn("input tax credits", res.detail)
        self.assertEqual(checker({"Airalo": "registered"}, listed).check("751950577", ["Airalo"], TODAY).status, REGISTERED)

    def test_simplified_deregistered(self):
        listed = {"legal_name": "OLD LTD", "trade_name": "", "number": "751950577RT9999",
                  "registered": dt.date(2020, 1, 1), "deregistered": dt.date(2021, 1, 1)}
        res = checker({}, listed).check("751950577", ["OLD LTD"], dt.date(2022, 1, 1))
        self.assertEqual(res.status, NOT_REGISTERED)
        self.assertIn("2021-01-01", res.detail)


def cra_like(registered_names, account=True, registered_on_date=True, ended_on=None):
    """A fake registry that answers the way CRA does: date first, then name. `ended_on`: the first date
    the number is no longer registered."""
    from taxcheck.gst import BEFORE_GST

    g = GstChecker(delay=0)
    g.simplified = FakeList(None)
    g.calls = []

    def lookup(bn9, name, date):
        g.calls.append((name, date))
        if not account:
            return "no_match"
        if date <= BEFORE_GST or not registered_on_date or (ended_on and date >= ended_on):
            return "not_registered"
        return "registered" if name in registered_names else "no_match"

    g.registry_lookup = lookup
    return g


class NumberOnly(unittest.TestCase):
    def test_registered_under_another_name(self):
        from taxcheck.result import NAME_NOT_MATCHED

        g = cra_like({"Real Legal Name Inc."})
        res = g.check("869744920", ["Wrong Vendor Name"], TODAY)
        self.assertEqual(res.status, NAME_NOT_MATCHED)
        self.assertIn("Confirm the number belongs to this supplier", res.detail)
        self.assertEqual(len(g.calls), 2)  # the sheet's name, then one probe before GST existed

    def test_no_name_in_sheet(self):
        from taxcheck.result import NAME_NOT_MATCHED

        self.assertEqual(cra_like({"X"}).check("869744920", [None, None], TODAY).status, NAME_NOT_MATCHED)
        res = cra_like({"X"}, registered_on_date=False).check("869744920", [None], TODAY)
        self.assertEqual(res.status, NOT_REGISTERED)
        self.assertEqual(cra_like(set(), account=False).check("298277534", [None], TODAY).status, NOT_REGISTERED)

    def test_matching_name_still_gives_full_registered(self):
        self.assertEqual(cra_like({"Real Inc."}).check("869744920", ["Real Inc."], TODAY).status, REGISTERED)


class VendorMasterNames(unittest.TestCase):
    def test_shorthand_is_cleaned_before_asking_cra(self):
        from taxcheck.gst import name_variants

        cases = {
            "(DNU) IMPERIAL PRINTING O/A": ["IMPERIAL PRINTING"],
            "1234567 ALBERTA LTD O/A ACME PAINTING": ["1234567 ALBERTA LTD", "ACME PAINTING"],
            "Smith, John DBA Smith Plumbing": ["Smith, John", "Smith Plumbing"],
            "ACME* (INACTIVE) Supplies - O.A.": ["ACME Supplies"],
            "C.O.B. Widgets": ["Widgets"],
            "[DO NOT USE] The Paint Shop": ["The Paint Shop", "Paint Shop"],
        }
        for raw, cleaned in cases.items():
            self.assertEqual(name_variants(raw), [raw, *cleaned], raw)

    def test_words_containing_the_shorthand_are_left_alone(self):
        from taxcheck.gst import name_variants

        for name in ["Dbacon Foods Ltd.", "Boa Constructors Inc.", "Cobalt Mining Corp.", "Koala Toys"]:
            self.assertEqual(name_variants(name), [name])

    def test_cleaned_name_confirms_registration(self):
        g = cra_like({"IMPERIAL PRINTING"})
        res = g.check("813072139", ["(DNU) IMPERIAL PRINTING O/A"], TODAY)
        self.assertEqual(res.status, REGISTERED)
        self.assertIn("IMPERIAL PRINTING", res.detail)


class RegistrationEnded(unittest.TestCase):
    PAST = TODAY - dt.timedelta(days=200)

    def test_past_registration_that_has_ended_is_noted(self):
        g = cra_like({"Real Inc."}, ended_on=TODAY - dt.timedelta(days=30))
        res = g.check("813072139", ["Real Inc."], self.PAST)
        self.assertEqual(res.status, REGISTERED)  # it was registered on the date asked about
        self.assertTrue(res.ended)
        self.assertIn(f"no longer registered for GST/HST as of {TODAY}", res.detail)
        self.assertEqual(g.calls[-1][1], TODAY)

    def test_still_registered_gets_no_note(self):
        g = cra_like({"Real Inc."})
        res = g.check("813072139", ["Real Inc."], self.PAST)
        self.assertFalse(res.ended)
        self.assertNotIn("no longer", res.detail)
        self.assertEqual(len(g.calls), 2)  # one extra lookup for today

    def test_name_not_matched_can_also_have_ended(self):
        res = cra_like({"Real Inc."}, ended_on=TODAY).check("813072139", ["Wrong"], self.PAST)
        self.assertEqual(res.status, NAME_NOT_MATCHED)
        self.assertTrue(res.ended)

    def test_no_extra_lookup_for_today_or_unregistered(self):
        g = cra_like({"Real Inc."})
        g.check("813072139", ["Real Inc."], TODAY)
        self.assertEqual(len(g.calls), 1)
        g = cra_like({"Real Inc."}, registered_on_date=False)
        g.check("813072139", ["Real Inc."], self.PAST)
        self.assertEqual(len(g.calls), 1)

    def test_failed_extra_lookup_keeps_the_answer(self):
        g = cra_like({"Real Inc."})
        real = g.registry_lookup

        def lookup(bn9, name, date):
            if date == TODAY:
                raise ConnectionError("down")
            return real(bn9, name, date)

        g.registry_lookup = lookup
        res = g.check("813072139", ["Real Inc."], self.PAST)
        self.assertEqual(res.status, REGISTERED)
        self.assertFalse(res.ended)


class RegistryRetry(unittest.TestCase):
    def test_dropped_connection_is_retried(self):
        import requests
        from unittest import mock

        g = GstChecker(delay=0)
        calls = []

        def flaky(bn9, name, date):
            calls.append(1)
            if len(calls) == 1:
                raise requests.ConnectionError("Connection reset by peer")
            return "registered"

        g._registry_lookup = flaky
        with mock.patch("taxcheck.gst.time.sleep"):
            self.assertEqual(g.registry_lookup("108161779", "X", TODAY), "registered")
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
