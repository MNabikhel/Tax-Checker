"""Saskatchewan's assisted flow in a real browser, against a stand-in registry served locally.

The stand-in behaves like the real one as far as it is known (its pstLookup.js): a terms page where a person
passes a CAPTCHA once per session, then a search page with a #pstSearch box. Here the "person" is a script on
the terms page that submits it after a moment. A second mode asks for the CAPTCHA again after every search, which
must stop bulk checking rather than keep asking.

Needs a browser (Playwright's Chromium, Edge, Chrome, or TAXCHECK_CHROMIUM); skipped otherwise.
"""

import datetime as dt
import html
import http.server
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from taxcheck import sk_pst
from taxcheck.browser import Browser, BrowserUnavailable
from taxcheck.checks import RowChecker
from taxcheck.result import MANUAL, NOT_CONFIRMED, REGISTERED, Result

LICENSED = {"PRAIRIE HARDWARE LTD": "Vendor's Licence", "NORTHERN SUPPLY": "Registered Consumer"}

TERMS = """<html><body><h1>PST On-Line Registry</h1><form id="terms" method="post" action="/accept">
<input type="checkbox" id="termsChkBox"> I agree <div id="captcha_container">[I'm not a robot]</div>
<button id="termsNextBtn">Next</button></form>
<script>setTimeout(() => document.getElementById('terms').submit(), 400);</script></body></html>"""
# Like the real portal, every page has a site-wide search in its header; it must never be the one clicked.
HEADER = """<form method="get" action="/site-search"><input name="search_query"><button type="submit" title="Search">Search
</button></form>"""
SEARCH = HEADER + """<html><body><h1>Search</h1><form method="post" action="/search">
<input id="pstSearch" name="q"> <button type="submit">Search</button></form></body></html>"""


def results_page(term):
    rows = [(n, kind) for n, kind in LICENSED.items() if term.upper().replace(" ", "") in n.replace(" ", "")]
    body = ("<table><tr><th>Business Name</th><th>Type</th></tr>"
            + "".join(f"<tr><td>{html.escape(n)}</td><td>{k}</td></tr>" for n, k in rows) + "</table>"
            if rows else "<p>No records found.</p>")
    return f"<html><body><h1>Results</h1>{body}<a href='/registry'>New Search</a></body></html>"


class FakeRegistry(http.server.BaseHTTPRequestHandler):
    every_search = False  # True: the CAPTCHA is needed again after each search
    accepted = set()  # session ids that passed the terms page
    searches = []
    terms_shown = 0

    def log_message(self, *a):
        pass

    def _session(self):
        cookie = self.headers.get("Cookie", "")
        return cookie.split("sid=")[1].split(";")[0] if "sid=" in cookie else None

    def _send(self, body, cookie=None):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        if cookie:
            self.send_header("Set-Cookie", f"sid={cookie}; Path=/")
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self):
        if self.path.startswith("/site-search"):
            FakeRegistry.searches.append("SITE SEARCH (wrong button)")
        if not self.path.startswith("/registry"):  # favicon etc.
            self.send_response(404)
            self.end_headers()
            return
        sid = self._session()
        if sid in FakeRegistry.accepted:
            self._send(SEARCH)
        else:
            FakeRegistry.terms_shown += 1
            self._send(TERMS, cookie=sid or f"s{FakeRegistry.terms_shown}")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        form = urllib.parse.parse_qs(self.rfile.read(length).decode())
        sid = self._session()
        if self.path == "/accept":
            FakeRegistry.accepted.add(sid)
            self.send_response(303)
            self.send_header("Location", "/registry")
            self.end_headers()
            return
        if sid not in FakeRegistry.accepted:
            return self.do_GET()
        term = form.get("q", [""])[0]
        FakeRegistry.searches.append(term)
        if FakeRegistry.every_search:
            FakeRegistry.accepted.discard(sid)
        self._send(results_page(term))


def browser_or_skip(test):
    b = Browser(debug_dir=test.tmp)
    try:
        b._ensure_started()
    except BrowserUnavailable as e:
        test.skipTest(f"no browser available: {e}")
    return b


class AssistedFlow(unittest.TestCase):
    def setUp(self):
        FakeRegistry.every_search, FakeRegistry.searches, FakeRegistry.terms_shown = False, [], 0
        FakeRegistry.accepted = set()
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeRegistry)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        url = f"http://127.0.0.1:{self.server.server_port}/registry"
        patch = mock.patch.object(sk_pst, "SK_REGISTRY", url)
        patch.start()
        self.addCleanup(patch.stop)
        # The browser must not send local requests through a proxy.
        env = mock.patch.dict("os.environ", {"HTTPS_PROXY": "", "HTTP_PROXY": ""})
        env.start()
        self.addCleanup(env.stop)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.browser = browser_or_skip(self)
        self.addCleanup(self.browser.close)

    def test_one_captcha_covers_every_search(self):
        sk = sk_pst.SkPstAssistant(self.browser, wait_minutes=0.5)
        self.assertEqual(sk.check(["PRAIRIE HARDWARE LTD"]).status, REGISTERED)
        res = sk.check(["NORTHERN SUPPLY"])
        self.assertEqual(res.status, REGISTERED)
        self.assertIn("registered consumer", res.detail)
        self.assertEqual(sk.check(["UNKNOWN WIDGETS"]).status, NOT_CONFIRMED)
        self.assertEqual(sk.captchas, 1)  # the person was asked once
        self.assertEqual(FakeRegistry.terms_shown, 1)
        self.assertEqual(FakeRegistry.searches, ["PRAIRIE HARDWARE", "NORTHERN SUPPLY", "UNKNOWN WIDGETS"])

    def test_saskatchewan_and_the_other_browser_checks_in_one_run(self):
        # Seen on a real run: the Saskatchewan window started first, then the hidden browser for Manitoba couldn't
        # start ("Playwright Sync API inside the asyncio loop"), so every Manitoba row became MANUAL CHECK.
        sk = sk_pst.SkPstAssistant(self.browser, wait_minutes=0.5)
        self.assertEqual(sk.check(["PRAIRIE HARDWARE LTD"]).status, REGISTERED)
        other = Browser(debug_dir=self.tmp)
        try:
            with other.page() as page:
                page.goto(sk_pst.SK_REGISTRY)
                self.assertTrue(page.content())
        finally:
            other.close()
        self.assertEqual(sk.check(["NORTHERN SUPPLY"]).status, REGISTERED)  # still works after the other closed

    def test_probe_saves_each_page_and_what_was_read(self):
        import json

        import sk_probe
        from taxcheck import browser as browser_module

        real = browser_module.Browser
        with mock.patch.object(sk_probe, "HERE", self.tmp), \
                mock.patch.object(browser_module, "Browser", lambda headless=True, debug_dir=None: real(debug_dir=debug_dir)), \
                mock.patch("builtins.print"):
            self.assertEqual(sk_probe.main(["Prairie Hardware Ltd", "Zzqx Imaginary Widgets"]), 0)
        out = next((self.tmp / "logs").glob("sk_probe_*"))
        report = json.loads((out / "probe_report.json").read_text(encoding="utf-8"))
        self.assertEqual([r["tool_reads"] for r in report], [REGISTERED, NOT_CONFIRMED])
        self.assertEqual(len(list(out.glob("*.html"))), 2)
        self.assertEqual(len(list(out.glob("*.png"))), 2)
        import logging
        root = logging.getLogger()
        for h in list(root.handlers):
            root.removeHandler(h)
            h.close()

    def test_daily_search_limit(self):
        counter = self.tmp / "sk_searches.json"
        sk = sk_pst.SkPstAssistant(self.browser, wait_minutes=0.5, counter_file=counter, daily_limit=2)
        self.assertEqual(sk.check(["PRAIRIE HARDWARE LTD"]).status, REGISTERED)
        self.assertEqual(sk.check(["NORTHERN SUPPLY"]).status, REGISTERED)
        res = sk.check(["ANOTHER ONE LTD"])
        self.assertEqual(res.status, MANUAL)
        self.assertIn("allows 2 searches per 24 hours", res.detail)
        self.assertEqual(len(FakeRegistry.searches), 2)
        # The count is kept between runs.
        self.assertEqual(sk_pst.SkPstAssistant(self.browser, counter_file=counter, daily_limit=2).searches_left(), 0)

    def test_captcha_on_every_search_stops_bulk_checking(self):
        FakeRegistry.every_search = True
        sk = sk_pst.SkPstAssistant(self.browser, wait_minutes=0.5)
        self.assertEqual(sk.check(["PRAIRIE HARDWARE LTD"]).status, REGISTERED)
        res = sk.check(["NORTHERN SUPPLY"])
        self.assertEqual(res.status, MANUAL)
        self.assertIn("asked for the CAPTCHA again", res.detail)
        self.assertEqual(sk.check(["ANOTHER ONE"]).status, MANUAL)  # no more asking
        self.assertEqual(sk.captchas, 1)
        self.assertEqual(len(FakeRegistry.searches), 1)

    def test_run_does_all_saskatchewan_searches_first_and_rows_reuse_them(self):
        import check_suppliers

        sk = sk_pst.SkPstAssistant(self.browser, wait_minutes=0.5)

        class Gst:
            def check(self, bn9, names, date, extra_names=()):
                return Result(REGISTERED, "fake CRA", names[0])

        checker = RowChecker(Gst(), None, sk=sk, available={"name", "gst", "province"})
        rows = [{"name": "PRAIRIE HARDWARE LTD", "gst": "108161779", "province": "SK"},
                {"name": "ACME LTD", "gst": "857305932", "province": "AB"},
                {"name": "NORTHERN SUPPLY", "gst": "108161779", "province": "SK"},
                {"name": "PRAIRIE HARDWARE LTD", "gst": "108161779", "province": "SK"}]  # duplicate: one search
        check_suppliers.prefetch_saskatchewan(checker, sk, rows)
        self.assertEqual(FakeRegistry.searches, ["PRAIRIE HARDWARE", "NORTHERN SUPPLY"])
        out = [checker.check(r, dt.date(2026, 9, 1)) for r in rows]
        self.assertEqual(FakeRegistry.searches, ["PRAIRIE HARDWARE", "NORTHERN SUPPLY"])  # nothing re-searched
        self.assertEqual(out[0]["sk_pst"].status, REGISTERED)
        self.assertNotIn("sk_pst", out[1])
        self.assertEqual(out[2]["sk_pst"].status, REGISTERED)


class Menu(unittest.TestCase):
    def test_saskatchewan_question(self):
        import check_suppliers

        for answers, expected in ((["", ], True), (["y"], True), (["N"], False), (["maybe", "n"], False)):
            with mock.patch("builtins.input", side_effect=answers), mock.patch("builtins.print"):
                self.assertEqual(check_suppliers.ask_saskatchewan(), expected, answers)

    def test_only_asked_when_saskatchewan_is_chosen(self):
        import check_suppliers

        asked = []
        with mock.patch.object(check_suppliers, "ask_taxes", return_value={"gst"}), \
                mock.patch.object(check_suppliers, "ask_saskatchewan", side_effect=lambda: asked.append(1) or True), \
                mock.patch.object(check_suppliers, "ask_gst_dates", return_value="none"), \
                mock.patch.object(check_suppliers, "ask_paid_since", return_value=None), \
                mock.patch.object(check_suppliers, "run", return_value=None), \
                tempfile.TemporaryDirectory() as tmp:
            check_suppliers.main([str(Path(tmp) / "x.xlsx"), "--ask", "--log-dir", tmp])
            self.assertEqual(asked, [])  # GST/HST only: no Saskatchewan question
            with mock.patch.object(check_suppliers, "ask_taxes", return_value={"gst", "sk_pst"}):
                check_suppliers.main([str(Path(tmp) / "x.xlsx"), "--ask", "--log-dir", tmp])
            self.assertEqual(asked, [1])
        import logging
        root = logging.getLogger()
        for h in list(root.handlers):
            root.removeHandler(h)
            h.close()


class SaskatchewanSearchList(unittest.TestCase):
    def test_searches_needed_match_the_row_checks(self):
        checker = RowChecker(None, None, available={"name", "province", "pst", "country"})
        rows = [{"name": "(DNU) PRAIRIE HARDWARE LTD O/A PH", "province": "SK"},
                {"name": "ACME LTD", "province": "AB", "pst": "1234567"},
                {"name": "WESTERN LTD", "province": "SK", "country": "US"},
                {"name": "SASK CO", "province": "AB", "sk_pst": "7654321"}]
        jobs = checker.saskatchewan_searches(rows)
        self.assertEqual([j[0][:2] for j in jobs], [["PRAIRIE HARDWARE LTD O/A PH", "PRAIRIE HARDWARE LTD"],
                                                   ["SASK CO", "SASK COMPANY"]])
        self.assertEqual([j[1] for j in jobs], [False, True])
        self.assertEqual(RowChecker(None, None, taxes={"gst"}).saskatchewan_searches(rows), [])


if __name__ == "__main__":
    unittest.main()
