"""Time zones, and giving up on a site that's down."""

import datetime as dt
import unittest

from taxcheck.bc_pst import BcPstChecker
from taxcheck.gst import cra_today
from taxcheck.http import SiteHealth
from taxcheck.mb_rst import MbRstChecker
from taxcheck.result import ERROR

try:
    from zoneinfo import ZoneInfo

    OTTAWA = ZoneInfo("America/Toronto")
except Exception:  # Windows without the tzdata package
    OTTAWA = None


class CraToday(unittest.TestCase):
    @unittest.skipUnless(OTTAWA, "needs a time-zone database")
    def test_never_ahead_of_ottawa_all_year(self):
        # Every hour of a year, including both daylight-saving switches.
        start = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        behind = 0
        for h in range(366 * 24):
            now = start + dt.timedelta(hours=h)
            ours = cra_today(now)
            ottawa = now.astimezone(OTTAWA).date()
            self.assertLessEqual(ours, ottawa, f"ahead of Ottawa at {now}")
            self.assertGreaterEqual(ours, ottawa - dt.timedelta(days=1))
            behind += ours < ottawa
        # Only the summer hour between Ottawa's midnight and UTC-5's midnight lags a day.
        self.assertLess(behind, 250)

    def test_utc_just_after_midnight_is_still_yesterday(self):
        self.assertEqual(cra_today(dt.datetime(2026, 10, 11, 3, 30, tzinfo=dt.timezone.utc)), dt.date(2026, 10, 10))
        self.assertEqual(cra_today(dt.datetime(2026, 10, 11, 12, 0, tzinfo=dt.timezone.utc)), dt.date(2026, 10, 11))


class FailingBrowser:
    debug_dir = None

    def __init__(self):
        self.pages = 0

    def page(self):
        self.pages += 1
        raise RuntimeError("net::ERR_CONNECTION_REFUSED")

    def record_failure(self, page, label):
        pass


class SiteDown(unittest.TestCase):
    def test_health_counts_consecutive_failures(self):
        h = SiteHealth("X", limit=2)
        h.record(False, "boom")
        h.record(True)
        h.record(False, "boom")
        self.assertFalse(h.down)
        h.record(False, "boom again")
        self.assertTrue(h.down)
        self.assertIn("boom again", h.down_message())

    def test_bc_stops_trying_after_two_failed_rows(self):
        browser = FailingBrowser()
        bc = BcPstChecker(browser, delay=0)
        results = [bc.check("108161779", "1000-7572") for _ in range(5)]
        self.assertTrue(all(r.status == ERROR for r in results))
        self.assertEqual(browser.pages, 6)  # 2 rows x 3 attempts, then no more page loads
        self.assertIn("wasn't responding", results[-1].detail)

    def test_manitoba_stops_trying_after_two_failed_rows(self):
        browser = FailingBrowser()
        mb = MbRstChecker(browser, delay=0)
        results = [mb.check(["Some Co"], bn9="857305932") for _ in range(4)]
        self.assertTrue(all(r.status == ERROR for r in results))
        self.assertEqual(browser.pages, 6)
        self.assertIn("wasn't responding", results[-1].detail)


if __name__ == "__main__":
    unittest.main()
