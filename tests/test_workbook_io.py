"""Reading messy workbooks and writing results back."""

import tempfile
import unittest
from pathlib import Path

import openpyxl

from taxcheck.result import MANUAL, NOT_CONFIRMED, REGISTERED, Result
from taxcheck.workbook import SupplierSheet, clean_value


def make_workbook(path, rows):
    wb = openpyxl.Workbook()
    for row in rows:
        wb.active.append(row)
    wb.save(path)


class WorkbookIO(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_numbers_typed_as_numbers(self):
        self.assertEqual(clean_value(857305932.0), 857305932)
        self.assertEqual(clean_value("  857305932RT0001 "), "857305932RT0001")
        self.assertIsNone(clean_value("   "))
        path = self.dir / "in.xlsx"
        make_workbook(path, [["Supplier Name", "GST/HST Number", "QST Number"], ["Amazon.com.ca ULC", 857305932.0, 1019288451]])
        (_, values), = SupplierSheet(path).rows()
        self.assertEqual(values["gst"], 857305932)
        self.assertEqual(values["qst"], 1019288451)

    def test_output_keeps_original_columns_and_formulas(self):
        path, out = self.dir / "in.xlsx", self.dir / "out.xlsx"
        make_workbook(path, [["Supplier Name", "GST/HST Number", "Note"], ["A Co", "108161779RT0001", "=1+1"]])
        sheet = SupplierSheet(path)
        sheet.write_results({2: {"gst": Result(REGISTERED, "ok", "A CO")}}, out, ["note"])
        ws = openpyxl.load_workbook(out)["Sheet"]
        headers = [c.value for c in ws[1]]
        self.assertEqual(headers[:4], ["Supplier Name", "GST/HST Number", "Note", "Overall"])
        self.assertEqual(ws["C2"].value, "=1+1")
        row = dict(zip(headers, [c.value for c in ws[2]]))
        self.assertEqual(row["Overall"], "OK")
        self.assertEqual(row["GST/HST Status"], REGISTERED)
        self.assertEqual(row["Name on Government Record"], "A CO")
        self.assertIn("Tax Check Summary", openpyxl.load_workbook(out).sheetnames)

    def test_rerun_on_results_file_overwrites_result_columns(self):
        path, out1, out2 = self.dir / "in.xlsx", self.dir / "r1.xlsx", self.dir / "r2.xlsx"
        make_workbook(path, [["Supplier Name", "GST/HST Number", "QST Number"], ["A Co", "108161779", "1019288451TQ0004"]])
        SupplierSheet(path).write_results(
            {2: {"gst": Result(NOT_CONFIRMED, "first"), "qst": Result(REGISTERED, "q")}}, out1, []
        )
        SupplierSheet(out1).write_results({2: {"gst": Result(REGISTERED, "second")}}, out2, [])
        wb = openpyxl.load_workbook(out2)
        ws = wb["Sheet"]
        headers = [c.value for c in ws[1]]
        self.assertEqual(headers.count("Overall"), 1)
        row = dict(zip(headers, [c.value for c in ws[2]]))
        self.assertEqual(row["GST/HST Details"], "second")
        self.assertIsNone(row["QST Status"])  # stale value from the first run is cleared
        self.assertEqual(wb.sheetnames.count("Tax Check Summary"), 1)

    def test_overall_review_when_anything_not_registered(self):
        path, out = self.dir / "in.xlsx", self.dir / "out.xlsx"
        make_workbook(path, [["Supplier Name", "Province"], ["A Co", "SK"]])
        SupplierSheet(path).write_results(
            {2: {"gst": Result(REGISTERED, ""), "sk_pst": Result(MANUAL, "")}}, out, []
        )
        ws = openpyxl.load_workbook(out)["Sheet"]
        self.assertEqual(ws["C2"].value, "REVIEW")

    def test_no_header_row_is_a_clear_error(self):
        path = self.dir / "in.xlsx"
        make_workbook(path, [["foo", "bar"], [1, 2]])
        with self.assertRaisesRegex(ValueError, "header row"):
            SupplierSheet(path)

    def test_named_sheet(self):
        path = self.dir / "in.xlsx"
        wb = openpyxl.Workbook()
        wb.active.title = "Cover"
        ws = wb.create_sheet("Vendors")
        ws.append(["Vendor", "HST #"])
        ws.append(["A Co", "108161779"])
        wb.save(path)
        self.assertEqual(len(list(SupplierSheet(path, "Vendors").rows())), 1)


if __name__ == "__main__":
    unittest.main()
