"""The command line end to end (with fake government services), including the run log."""

import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import openpyxl

import check_suppliers
from taxcheck.browser import BrowserUnavailable
from taxcheck.result import REGISTERED, Result


class FakeGst:
    def __init__(self, *a, **k):
        self.simplified = mock.Mock(error=None)

    def check(self, bn9, names, date):
        if bn9 == "857305932":
            raise ConnectionError("simulated network failure")
        return Result(REGISTERED, "fake CRA", names[0])


class FakeQst:
    nr_error = None

    def __init__(self, *a, **k):
        pass

    def check(self, number, suffix_assumed=False):
        return Result(REGISTERED, "fake RQ")


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.src = self.dir / "suppliers.xlsx"
        wb = openpyxl.Workbook()
        ws = wb.active
        for row in [
            ["Supplier Name", "Province", "GST/HST Number", "QST Number", "PST Number", "Transaction Date"],
            ["Good Co", "QC", "108161779RT0001", "1019288451TQ0004", None, "2026-09-30"],
            ["Flaky Co", "ON", "857305932RT0001", None, None, "not a date"],
            ["BC Co", "BC", "108161779RT0001", None, "PST-1000-7572", None],
            ["Typo Co", "ON", "123456789", None, None, None],
        ]:
            ws.append(row)
        wb.save(self.src)
        patches = [mock.patch.object(check_suppliers, "GstChecker", FakeGst),
                   mock.patch.object(check_suppliers, "QstChecker", FakeQst)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        # Detach the run's log handlers so the log file is closed before the temp folder is removed.
        root = logging.getLogger()
        for h in list(root.handlers):
            root.removeHandler(h)
            h.close()
        self.tmp.cleanup()

    def run_cli(self, *extra):
        logs = self.dir / "logs"
        code = check_suppliers.main([str(self.src), "--log-dir", str(logs), "--no-browser", "--no-name-lookup", *extra])
        log_files = list(logs.glob("tax_check_*.log"))
        self.assertEqual(len(log_files), 1)
        return code, log_files[0].read_text(encoding="utf-8")

    def test_full_run_writes_results_and_log(self):
        code, log_text = self.run_cli()
        self.assertEqual(code, 0)
        out = self.dir / "suppliers_tax_check.xlsx"
        ws = openpyxl.load_workbook(out)["Sheet"]
        headers = [c.value for c in ws[1]]
        rows = [dict(zip(headers, [c.value for c in r])) for r in ws.iter_rows(min_row=2)]
        self.assertEqual([r["Overall"] for r in rows], ["OK", "REVIEW", "REVIEW", "REVIEW"])
        self.assertEqual(rows[1]["GST/HST Status"], "ERROR")         # network failure is contained
        self.assertEqual(rows[2]["BC PST Status"], "MANUAL CHECK")   # --no-browser
        self.assertEqual(rows[3]["GST/HST Status"], "INVALID NUMBER")

        # The log has what's needed to debug afterwards.
        self.assertIn("Arguments:", log_text)
        self.assertIn("column gst", log_text)
        self.assertIn("---- Row 3: Flaky Co", log_text)
        self.assertIn("Traceback", log_text)
        self.assertIn("simulated network failure", log_text)
        self.assertIn("couldn't read date 'not a date'", log_text)
        self.assertIn("Results written to", log_text)

    def test_ctrl_c_keeps_results_so_far(self):
        calls = []

        def check(self_, bn9, names, date):
            calls.append(bn9)
            if len(calls) == 2:
                raise KeyboardInterrupt
            return Result(REGISTERED, "fake CRA", names[0])

        with mock.patch.object(FakeGst, "check", check):
            code, log_text = self.run_cli()
        self.assertEqual(code, 0)
        wb = openpyxl.load_workbook(self.dir / "suppliers_tax_check.xlsx")
        self.assertEqual(wb["Sheet"]["G2"].value, "OK")  # first supplier's result was kept
        notes = [r[0] for r in wb["Tax Check Summary"].iter_rows(values_only=True) if r and r[0]]
        self.assertTrue(any(str(n).startswith("STOPPED EARLY") for n in notes), notes)
        self.assertIn("Stopped early after 1 of 4", log_text)

    def test_missing_file_fails_cleanly_with_log(self):
        self.src = self.dir / "nope.xlsx"
        code, log_text = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("Can't find the input file", log_text)
        self.assertNotIn("Traceback", log_text)  # a missing file is the user's to fix, not a crash

    def test_browser_start_failure_falls_back_to_manual(self):
        with mock.patch(
            "taxcheck.browser.Browser._ensure_started", side_effect=BrowserUnavailable("no chromium. Run playwright install")
        ):
            code = check_suppliers.main([str(self.src), "--log-dir", str(self.dir / "logs2"), "--no-name-lookup"])
        self.assertEqual(code, 0)
        ws = openpyxl.load_workbook(self.dir / "suppliers_tax_check.xlsx")["Sheet"]
        headers = [c.value for c in ws[1]]
        row = dict(zip(headers, [c.value for c in ws[4]]))
        self.assertEqual(row["BC PST Status"], "MANUAL CHECK")
        self.assertIn("no chromium", row["BC PST Details"])

if __name__ == "__main__":
    unittest.main()
