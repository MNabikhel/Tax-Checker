"""Fallbacks added after the first run on a corporate laptop: Edge when Playwright's Chromium isn't downloaded,
and the CRA simplified list through the browser when canada.ca stalls for Python."""

import contextlib
import datetime as dt
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import requests

from taxcheck.browser import Browser, BrowserUnavailable
from taxcheck.gst import SimplifiedList

ROWS = """<tr><td>AIRGSM PTE. LTD.</td><td>Airalo</td><td>751950577 RT9999</td><td>2021-07-01</td><td>-</td></tr>
<tr><td>Other Ltd</td><td>-</td><td>123456782 RT9999</td><td>2021-07-01</td><td>-</td></tr>"""
# CRA's real list has ~1,800 numbers; a page with far fewer is treated as incomplete, so pad with made-up ones.
FILLER = "".join(f"<tr><td>Made Up Seller {i}</td><td>-</td><td>{200000000 + i} RT9999</td><td>2023-07-01</td>"
                 f"<td>-</td></tr>" for i in range(SimplifiedList.MIN_NUMBERS))
PAGE = f"<table>{ROWS}{FILLER}</table>"
HALF_BUILT = f"<table>{ROWS}</table>"  # seen live: a browser's view read while the page was still paging its table


class FakePage:
    sent = PAGE  # the page as the server sent it
    view = PAGE  # the browser's live view

    def goto(self, url, **kw):
        return mock.Mock(status=200, text=lambda: self.sent)

    def content(self):
        return self.view


class FakeBrowser:
    @contextlib.contextmanager
    def page(self):
        yield FakePage()


class SimplifiedFallback(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_browser_used_when_download_stalls_then_cached(self):
        session = requests.Session()
        with mock.patch("taxcheck.gst.requests.get", side_effect=requests.exceptions.ReadTimeout("stalled")) as get:
            sl = SimplifiedList(session, browser=FakeBrowser(), cache_dir=self.tmp.name)
            self.assertIsNotNone(sl.lookup("751950577", dt.date(2026, 10, 1)))
            self.assertIsNone(sl.error)
            self.assertEqual(get.call_count, 1)  # one quick try, no retries
        self.assertTrue((Path(self.tmp.name) / "cra_simplified_list.html").exists())
        with mock.patch("taxcheck.gst.requests.get", side_effect=AssertionError("should use the cache")):
            again = SimplifiedList(session, cache_dir=self.tmp.name)
            self.assertIsNotNone(again.lookup("751950577", dt.date(2026, 10, 1)))

    def test_page_read_as_sent_not_the_half_built_view(self):
        with mock.patch("taxcheck.gst.requests.get", side_effect=requests.exceptions.ReadTimeout("stalled")), \
                mock.patch.object(FakePage, "view", HALF_BUILT):
            sl = SimplifiedList(requests.Session(), browser=FakeBrowser(), cache_dir=self.tmp.name)
            self.assertIsNotNone(sl.lookup("751950577", dt.date(2026, 10, 1)))
        self.assertGreaterEqual(len(sl._rows), SimplifiedList.MIN_NUMBERS)

    def test_incomplete_list_is_neither_used_nor_cached(self):
        cache = Path(self.tmp.name) / "cra_simplified_list.html"
        cache.write_text(HALF_BUILT, encoding="utf-8")  # left by an earlier version's setup check
        answer = mock.Mock(text=PAGE, raise_for_status=lambda: None)
        with mock.patch("taxcheck.gst.requests.get", return_value=answer):
            sl = SimplifiedList(requests.Session(), cache_dir=self.tmp.name)
            self.assertIsNotNone(sl.lookup("751950577", dt.date(2026, 10, 1)))
        self.assertGreaterEqual(len(SimplifiedList.parse(cache.read_text(encoding="utf-8"))), SimplifiedList.MIN_NUMBERS)
        answer.text = HALF_BUILT
        cache.unlink()
        with mock.patch("taxcheck.gst.requests.get", return_value=answer):
            sl = SimplifiedList(requests.Session(), cache_dir=self.tmp.name)
            self.assertIsNone(sl.lookup("751950577", dt.date(2026, 10, 1)))
        self.assertIn("incomplete page", sl.error)  # shown on the summary sheet, not a silent miss
        self.assertFalse(cache.exists())

    def test_no_browser_reports_error_and_check_continues(self):
        with mock.patch("taxcheck.gst.requests.get", side_effect=requests.exceptions.ReadTimeout("stalled")):
            sl = SimplifiedList(requests.Session())
            self.assertIsNone(sl.lookup("751950577", dt.date(2026, 10, 1)))
        self.assertIn("couldn't load CRA simplified registrant list", sl.error)


class BrowserFallback(unittest.TestCase):
    def setUp(self):
        # A browser named in TAXCHECK_CHROMIUM would be tried first; these tests are about the fallbacks after it.
        env = mock.patch.dict("os.environ", {"TAXCHECK_CHROMIUM": ""})
        env.start()
        self.addCleanup(env.stop)

    def fake_playwright(self, works):
        launched = []

        def launch(**kw):
            label = kw.get("channel") or kw.get("executable_path") or "bundled"
            launched.append(label)
            if label not in works:
                raise RuntimeError(f"BrowserType.launch: Executable doesn't exist ({label})")
            return mock.Mock(version="141.0")

        pw = mock.Mock()
        pw.chromium.launch = launch
        module = types.SimpleNamespace(sync_playwright=lambda: mock.Mock(start=lambda: pw))
        return launched, mock.patch.dict(sys.modules, {"playwright": types.ModuleType("playwright"), "playwright.sync_api": module})

    def test_edge_used_when_bundled_chromium_missing(self):
        launched, patch = self.fake_playwright(works={"msedge"})
        with patch:
            b = Browser()
            b._ensure_started()
            b.close()  # releases the (fake) shared driver
        self.assertEqual(launched, ["bundled", "msedge"])
        self.assertTrue(b.engine.startswith("Microsoft Edge"))

    def test_all_fail_gives_one_clear_error(self):
        launched, patch = self.fake_playwright(works=set())
        with patch:
            b = Browser()
            with self.assertRaises(BrowserUnavailable) as cm:
                b._ensure_started()
        self.assertEqual(launched, ["bundled", "msedge", "chrome"])
        self.assertIn("Microsoft Edge", str(cm.exception))


if __name__ == "__main__":
    unittest.main()


class BrowserCloseNeverRaises(unittest.TestCase):
    """Found in a stress test: Ctrl+C also stopped the browser's driver; closing it then raised, and the results
    workbook wasn't saved."""

    def test_close_after_the_driver_died(self):
        from taxcheck.browser import Browser

        class Dead:
            def close(self):
                raise RuntimeError("Browser.close: Connection closed while reading from the driver")

        b = Browser()
        b._browser = Dead()
        b.close()  # must not raise
        self.assertIsNone(b._browser)
