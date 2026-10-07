"""The command line end to end (with fake government services), including the run log."""

import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import openpyxl

import check_suppliers
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
        code = check_suppliers.main([str(self.src), "--log-dir", str(logs), "--no-browser", *extra])
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

    def test_missing_file_fails_cleanly_with_log(self):
        self.src = self.dir / "nope.xlsx"
        code, log_text = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("Run failed", log_text)

    def test_browser_start_failure_falls_back_to_manual(self):
        with mock.patch("taxcheck.bc_pst.BcPstChecker.__enter__", side_effect=RuntimeError("no chromium")):
            logs = self.dir / "logs2"
            code = check_suppliers.main([str(self.src), "--log-dir", str(logs)])
        self.assertEqual(code, 0)
        ws = openpyxl.load_workbook(self.dir / "suppliers_tax_check.xlsx")["Sheet"]
        headers = [c.value for c in ws[1]]
        self.assertEqual(dict(zip(headers, [c.value for c in ws[4]]))["BC PST Status"], "MANUAL CHECK")
        self.assertIn("no chromium", next(logs.glob("*.log")).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
