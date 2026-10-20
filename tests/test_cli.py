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
        code = check_suppliers.main([str(self.src), "--log-dir", str(logs), "--no-browser", "--no-name-lookup",
                                    "--workers", "1", *extra])
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

    def test_check_columns_is_offline_and_explains_mapping(self):
        with mock.patch.object(FakeGst, "check", side_effect=AssertionError("no lookups in --check-columns")):
            code, log_text = self.run_cli("--check-columns", "--preview", "2")
        self.assertEqual(code, 0)
        self.assertIn("'GST/HST Number' -> gst", log_text)
        self.assertIn("WOULD CHECK", log_text)
        self.assertIn("What the first 2 would check", log_text)
        self.assertFalse((self.dir / "suppliers_tax_check.xlsx").exists())

    def test_check_columns_on_unrecognized_headers_lists_them(self):
        wb = openpyxl.Workbook()
        wb.active.append(["Vendor ID", "Entity", "Tax Reg #"])
        wb.save(self.src)
        code, log_text = self.run_cli("--check-columns")
        self.assertEqual(code, 1)
        self.assertIn("'Tax Reg #'", log_text)
        self.assertIn("COLUMNS", log_text)

    def test_no_path_opens_picker_and_outputs_go_next_to_workbook(self):
        with mock.patch.object(check_suppliers, "choose_workbook", return_value=str(self.src)) as picker:
            code = check_suppliers.main(["--no-browser", "--no-name-lookup"])
        self.assertEqual(code, 0)
        picker.assert_called_once()
        self.assertTrue((self.dir / "suppliers_tax_check.xlsx").exists())
        self.assertEqual(len(list((self.dir / "logs").glob("tax_check_*.log"))), 1)  # logs beside the workbook

    def test_cancelled_picker_does_nothing(self):
        with mock.patch.object(check_suppliers, "choose_workbook", return_value=None):
            self.assertEqual(check_suppliers.main(["--no-browser"]), 1)
        self.assertFalse((self.dir / "suppliers_tax_check.xlsx").exists())

    def test_picker_falls_back_to_typed_path_without_a_screen(self):
        import builtins

        with mock.patch.dict("sys.modules", {"tkinter": None}), \
                mock.patch.object(builtins, "input", return_value=f'"{self.src}"'):
            self.assertEqual(check_suppliers.choose_workbook(), str(self.src))  # quotes from "Copy as path" removed

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


class ResumeSaskatchewan(unittest.TestCase):
    """--resume checks again Saskatchewan rows whose searches couldn't run (the daily limit...), as the run says."""

    def test_rows_left_for_a_manual_check_by_the_searches_are_redone(self):
        import csv
        import json
        import tempfile
        from pathlib import Path

        from taxcheck.progress import load_resume

        rows = {
            2: {"gst": ["REGISTERED", "ok", ""], "sk_pst": ["REGISTERED", "vendor's licence", ""]},
            3: {"gst": ["REGISTERED", "ok", ""], "sk_pst": ["MANUAL CHECK", "Saskatchewan's registry allows 100 "
                                                             "searches per 24 hours and they're used up.", ""]},
            4: {"gst": ["REGISTERED", "ok", ""], "sk_pst": ["MANUAL CHECK", "needs at least 4 letters of the "
                                                             "business name", ""]},
            5: {"gst": ["ERROR", "down", ""]},
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "progress.csv"
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=["sheet_row", "results_json"])
                w.writeheader()
                for r, data in rows.items():
                    w.writerow({"sheet_row": r, "results_json": json.dumps(data)})
            self.assertEqual(sorted(load_resume(path)), [2, 4])  # 3 (limit) and 5 (error) are checked again
