"""GST/HST registration start/end dates found by asking the CRA registry about specific days.

The stand-in registry answers the way CRA does (verified behaviour, see gst_dates.py): with a name that doesn't
match, "not_registered" on days the number wasn't registered and "no_match" on days it was.
"""

import datetime as dt
import unittest

from taxcheck import gst_dates
from taxcheck.checks import RowChecker
from taxcheck.gst import cra_today
from taxcheck.report import gst_issue
from taxcheck.result import NOT_REGISTERED, REGISTERED, Result

TODAY = cra_today()


class FakeCra:
    """Registered from `start` until the day before `end` (end=None: still registered)."""

    def __init__(self, start, end=None):
        self.start, self.end, self.asked = start, end, []

    def registered(self, day):
        return self.start <= day and (self.end is None or day < self.end)

    def registry_lookup(self, bn9, name, date, attempts=3):
        self.asked.append(date)
        return "no_match" if self.registered(date) else "not_registered"

    def check(self, bn9, names, date, extra_names=()):  # the main lookup: the sheet name matches
        if self.registered(date):
            return Result(REGISTERED, "ok", names[0], facts={"accepted_name": names[0], "name_source": "sheet"})
        return Result(NOT_REGISTERED, f"not registered on {date}", facts={"cra_answer": "not_registered_on_date"})


def run(cra, payment, mode):
    checker = RowChecker(cra, None, available={"name", "gst"}, gst_dates=mode)
    return checker.check({"name": "ACME LTD", "gst": "108161779RT0001"}, payment)["gst"]


class Search(unittest.TestCase):
    def test_start_and_cancellation_found_to_the_day(self):
        cra = FakeCra(dt.date(2015, 3, 17), dt.date(2026, 5, 2))
        self.assertEqual(gst_dates.registration_start(cra, "x", dt.date(2020, 1, 1)), dt.date(2015, 3, 17))
        self.assertEqual(gst_dates.cancellation_date(cra, "x", dt.date(2026, 1, 1), TODAY), dt.date(2026, 5, 1))  # last day registered
        self.assertLessEqual(len(cra.asked), 1 + 15 + 10)  # halving: ~14 for 30 years, ~9 for a year

    def test_registered_since_the_start_of_gst(self):
        self.assertEqual(gst_dates.registration_start(FakeCra(dt.date(1990, 12, 1)), "x", dt.date(2020, 1, 1)),
                         dt.date(1990, 12, 1))
        self.assertIsNone(gst_dates.registration_start(FakeCra(dt.date(1989, 6, 1)), "x", dt.date(2020, 1, 1)))


class InTheCheck(unittest.TestCase):
    def test_quick_mode_confirms_active_today_with_one_lookup(self):
        cra = FakeCra(dt.date(2010, 1, 1))
        g = run(cra, dt.date(2026, 6, 1), "end")
        self.assertEqual(g.facts["registration_end"], f"Active on {TODAY}")
        self.assertNotIn("registered_since", g.facts)
        self.assertEqual(len(cra.asked), 1)

    def test_quick_mode_finds_cancellation_after_payment(self):
        cra = FakeCra(dt.date(2010, 1, 1), dt.date(2026, 7, 15))
        g = run(cra, dt.date(2026, 6, 1), "end")
        self.assertEqual(g.status, REGISTERED)
        self.assertEqual(g.facts["registration_end"], "2026-07-14")  # last day registered, as on CRA's list
        self.assertEqual(gst_issue(g)[0], "Registration cancelled since payment date")
        self.assertIn("dated after 2026-07-14", gst_issue(g)[1])

    def test_cancelled_shortly_before_payment_gets_its_cancellation_date(self):
        # Seen in the user's run: CRA recorded cancellations weeks late, back-dated to Sept 30, so a payment on
        # Oct 13 was "not registered" with no hint the supplier had been registered until just before.
        cra = FakeCra(dt.date(2015, 1, 1), dt.date(2026, 10, 1))
        g = run(cra, dt.date(2026, 10, 13), "end")
        self.assertEqual(g.status, NOT_REGISTERED)
        self.assertEqual(g.facts["registration_end"], "2026-09-30")
        self.assertIn("registered until 2026-09-30", g.detail)
        issue, action, _ = gst_issue(g)
        self.assertEqual(issue, "Registration cancelled before payment date")
        self.assertIn("2026-09-30", action)

    def test_whose_number_and_a_possible_current_number(self):
        # Seen on the user's run: a supplier's number cancelled in March 2025; the row didn't say whose number it was.
        cra = FakeCra(dt.date(2015, 1, 1), dt.date(2025, 3, 9))
        new_number = "123456782"

        class Registry:
            label = "Canada's Business Registries"

            def names(self, bn9):
                return ["PRAIRIE OFFICE SUPPLY INC."] if bn9 == "108161779" else []

            def numbers_for_name(self, name):
                return [("108161779", "PRAIRIE OFFICE SUPPLY INC."), (new_number, "PRAIRIE OFFICE SUPPLY INC.")]

        from taxcheck.gst import GstChecker

        cra.name_sources = [Registry()]
        cra._official_name = lambda bn9, extra_names=(): GstChecker._official_name(cra, bn9)
        lookup = cra.registry_lookup
        cra.registry_lookup = lambda bn9, name, date, attempts=3: ("registered" if bn9 == new_number
                                                                   else lookup(bn9, name, date))
        g = run(cra, dt.date(2026, 3, 31), "end")
        self.assertEqual(g.facts["registration_end"], "2025-03-08")
        self.assertEqual(g.facts["official_name"], "PRAIRIE OFFICE SUPPLY INC.")
        self.assertEqual(g.facts["possible_number"], new_number)  # the old number itself isn't offered
        issue, action, _ = gst_issue(g)
        self.assertIn("belongs to 'PRAIRIE OFFICE SUPPLY INC.'", action)
        self.assertIn(f"Possible current number: Canada's Business Registries lists 'PRAIRIE OFFICE SUPPLY INC.' with "
                      f"business number {new_number}", action)
        self.assertEqual(g.status, NOT_REGISTERED)  # a lead, never a status change

    def test_never_registered_costs_one_extra_lookup(self):
        cra = FakeCra(dt.date(2030, 1, 1))  # not registered on any day asked
        g = run(cra, dt.date(2026, 10, 13), "end")
        self.assertEqual(gst_issue(g)[0], "Not registered")
        self.assertEqual(len(cra.asked), 2)  # today, and two years before the payment date

    def test_full_mode_adds_start_date(self):
        g = run(FakeCra(dt.date(2012, 8, 9)), dt.date(2026, 6, 1), "full")
        self.assertEqual(g.facts["registered_since"], "2012-08-09")
        self.assertEqual(g.facts["registration_end"], f"Active on {TODAY}")

    def test_registered_after_payment_gets_its_start_date(self):
        cra = FakeCra(dt.date(2026, 4, 20))
        g = run(cra, dt.date(2026, 2, 1), "end")
        self.assertEqual(g.status, NOT_REGISTERED)
        self.assertEqual(g.facts["registered_since"], "2026-04-20")
        self.assertEqual(gst_issue(g)[0], "Registered after payment date")
        self.assertIn("registered since 2026-04-20", gst_issue(g)[1])

    def test_none_mode_makes_no_extra_lookups(self):
        cra = FakeCra(dt.date(2010, 1, 1))
        g = run(cra, dt.date(2026, 6, 1), "none")
        self.assertEqual(cra.asked, [])
        self.assertNotIn("registration_end", g.facts)

    def test_same_number_searched_once(self):
        cra = FakeCra(dt.date(2010, 1, 1), dt.date(2026, 7, 15))
        checker = RowChecker(cra, None, available={"name", "gst"}, gst_dates="end")
        for _ in range(3):
            checker.check({"name": "ACME LTD", "gst": "108161779RT0001"}, dt.date(2026, 6, 1))
        first = len(cra.asked)
        self.assertLess(first, 15)
        checker.check({"name": "ACME LTD", "gst": "108161779RT0001"}, dt.date(2026, 6, 1))
        self.assertEqual(len(cra.asked), first)


if __name__ == "__main__":
    unittest.main()
