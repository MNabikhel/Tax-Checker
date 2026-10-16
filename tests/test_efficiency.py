"""Efficiency and robustness cases from a code review (2026-10-16): each was reproduced first, then fixed.

Lookups are counted against stand-ins; the CRA stand-in answers the way the registry does (date first, then name).
"""

import datetime as dt
import threading
import time
import unittest

from taxcheck.checks import RowChecker
from taxcheck.gst import BEFORE_GST, GstChecker, _name_key
from taxcheck.result import ERROR, NOT_APPLICABLE, REGISTERED, Result

TODAY = dt.date(2026, 10, 16)


class NoList:
    def lookup(self, bn9, date):
        return None


def cra(registered_from=dt.date(2000, 1, 1), cancelled_after=None, name="ACME LTD"):
    """A GstChecker whose raw registry requests are counted (its answer memory and pacing are the real ones)."""
    g = GstChecker(delay=0)
    g.simplified = NoList()
    g.sent = []

    def raw(bn9, asked, date):
        g.sent.append((asked, date))
        if date <= BEFORE_GST or date < registered_from or (cancelled_after and date > cancelled_after):
            return "not_registered"
        return "registered" if _name_key(asked) == _name_key(name) else "no_match"

    g._registry_lookup = raw
    return g


class AnswersAreRemembered(unittest.TestCase):
    def test_same_question_is_asked_once(self):
        g = cra()
        rc = RowChecker(g, None, available={"name", "gst"}, gst_dates="end")
        for spelling in ("ACME LTD", "ACME LTD.", "Acme Ltd", "ACME  LTD"):  # the same name to CRA
            rc.check({"name": spelling, "gst": "108161779"}, dt.date(2026, 9, 1))
        self.assertEqual(len(g.sent), 2)  # the name once, "registered today?" once

    def test_names_that_dont_match_reuse_the_number_only_check(self):
        g = cra()
        rc = RowChecker(g, None, available={"name", "gst"})
        for name in ("WRONG A", "WRONG B", "WRONG C"):
            rc.check({"name": name, "gst": "108161779"}, dt.date(2026, 9, 1))
        self.assertEqual(len(g.sent), 4)  # each name once, the 1980 check once

    def test_dates_are_searched_once_per_number(self):
        g = cra(cancelled_after=dt.date(2026, 7, 14))
        rc = RowChecker(g, None, available={"name", "gst"}, gst_dates="full")
        payments = [dt.date(2026, m, 1) for m in (2, 3, 4, 5, 6)]
        outs = [rc.check({"name": "ACME LTD", "gst": "108161779"}, d)["gst"] for d in payments]
        self.assertTrue(all(o.facts["registration_end"] == "2026-07-14" for o in outs))
        self.assertTrue(all(o.facts["registered_since"] == "2000-01-01" for o in outs))
        self.assertLess(len(g.sent), 45)  # 82 before: start and cancellation were searched again for every date


class ParallelDuplicates(unittest.TestCase):
    def test_a_lookup_in_progress_is_waited_for_not_repeated(self):
        calls = []

        class SlowGst:
            def check(self, bn9, names, date, extra_names=()):
                calls.append(bn9)
                time.sleep(0.2)
                return Result(REGISTERED, "ok", names[0])

        shared = {}
        workers = [RowChecker(SlowGst(), None, available={"name", "gst"}, cache=shared) for _ in range(3)]
        threads = [threading.Thread(target=w.check, args=({"name": "ACME LTD", "gst": "108161779"}, TODAY))
                   for w in workers]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(calls, ["108161779"])

    def test_a_failed_lookup_is_retried_by_the_next_row(self):
        calls = []

        class Flaky:
            def check(self, bn9, names, date, extra_names=()):
                calls.append(1)
                if len(calls) == 1:
                    raise ConnectionError("blip")
                return Result(REGISTERED, "ok", names[0])

        rc = RowChecker(Flaky(), None, available={"name", "gst"})
        self.assertEqual(rc.check({"name": "ACME LTD", "gst": "108161779"}, TODAY)["gst"].status, ERROR)
        self.assertEqual(rc.check({"name": "ACME LTD", "gst": "108161779"}, TODAY)["gst"].status, REGISTERED)


class DigitalEconomyFailure(unittest.TestCase):
    def test_a_failed_confirmation_is_an_error_not_not_applicable(self):
        from taxcheck.gst import SimplifiedList

        g = GstChecker(delay=0)
        g.simplified = SimplifiedList(session=None)
        g.simplified._rows = SimplifiedList.parse(
            "<tr><td>AIRGSM PTE. LTD.</td><td>Airalo</td><td>751950577 RT9999</td><td>July 1, 2021</td><td>-</td></tr>")

        def down(*a, **k):
            raise ConnectionError("CRA down")

        g.registry_lookup = down
        res = RowChecker(g, None, available={"name", "gst", "country"}).check(
            {"name": "Airalo", "gst": None, "country": "US"}, TODAY)["gst"]
        self.assertEqual(res.status, ERROR)  # so --resume tries it again
        self.assertNotEqual(res.status, NOT_APPLICABLE)


if __name__ == "__main__":
    unittest.main()
