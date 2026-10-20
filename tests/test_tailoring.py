"""tailoring.json: tailoring the tool to an export without code changes (taxcheck/tailoring.py)."""

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

import openpyxl

from taxcheck import tailoring
from taxcheck.checks import clean_vendor_name
from taxcheck.workbook import SupplierSheet, normalize_country, normalize_province, parse_date


class Tailoring(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(tailoring.reset)

    def write(self, data):
        path = self.dir / "tailoring.json"
        path.write_text(json.dumps(data) if not isinstance(data, str) else data, encoding="utf-8")
        return path

    def test_no_file_changes_nothing(self):
        self.assertEqual(tailoring.apply(self.dir / "tailoring.json"), "")

    def test_every_setting(self):
        summary = tailoring.apply(self.write({
            "columns": {"gst": ["Vendor Tax No"], "bn": ["Tax ID"]},
            "provinces": {"QC": ["Qc"]}, "canada_values": ["CA - Canada"], "date_formats_first": ["%m/%d/%Y"],
            "remove_from_names": ["- INACTIVE"], "do_not_use_markers": ["DO NOT PAY"], "notes": "test"}))
        self.assertIn("columns gst: vendor tax no", summary)
        path = self.dir / "v.xlsx"
        wb = openpyxl.Workbook()
        wb.active.append(["Supplier", "Vendor Tax No", "Tax ID"])
        wb.active.append(["ACME LTD", "123456782RT0001", "123456782"])
        wb.save(path)
        cols = SupplierSheet(path).cols
        self.assertEqual((cols["gst"], cols["bn"]), (2, 3))  # "Tax ID" moved from gst to bn as asked
        self.assertEqual(normalize_province("Qc"), "QC")
        self.assertEqual(normalize_country("CA - Canada"), "CA")
        self.assertEqual(parse_date("09/10/2026"), dt.date(2026, 9, 10))  # month first, as configured
        self.assertEqual(clean_vendor_name("ACME LTD - INACTIVE"), ("ACME LTD", False))
        self.assertEqual(clean_vendor_name("ACME LTD DO NOT PAY"), ("ACME LTD", True))
        tailoring.reset()
        self.assertEqual(parse_date("09/10/2026"), dt.date(2026, 10, 9))  # back to day first
        self.assertEqual(clean_vendor_name("ACME LTD - INACTIVE"), ("ACME LTD - INACTIVE", False))

    def test_mistakes_are_reported_not_ignored(self):
        for data, says in (("{not json", "isn't valid JSON"), ({"colums": {}}, "unknown setting"),
                           ({"columns": {"gstt": ["X"]}}, "isn't one the tool reads"),
                           ({"provinces": {"QB": ["X"]}}, "province code 'QB' unknown"),
                           ({"date_formats_first": ["mm/dd/yyyy"]}, "isn't a date format"),
                           ({"remove_from_names": "INACTIVE"}, "must be a list")):
            with self.assertRaises(tailoring.TailoringError) as e:
                tailoring.apply(self.write(data))
            self.assertIn(says, str(e.exception))

    def test_excel_version_of_the_settings(self):
        import shutil

        template = Path(__file__).resolve().parent.parent / "examples" / "tailoring_template.xlsx"
        shutil.copy(template, self.dir / "tailoring.xlsx")
        summary = tailoring.apply(self.dir / "tailoring.json")  # no JSON: the Excel file beside it is used
        self.assertIn("tailoring.xlsx applied", summary)
        self.assertEqual(normalize_province("Qc"), "QC")
        self.assertEqual(clean_vendor_name("ACME LTD DO NOT PAY"), ("ACME LTD", True))
        wb = openpyxl.Workbook()
        wb.active.append(["Setting", "Field or code", "Value"])
        wb.active.append(["colum", "gst", "X"])
        wb.save(self.dir / "tailoring.xlsx")
        with self.assertRaises(tailoring.TailoringError) as e:
            tailoring.apply(self.dir / "tailoring.json")
        self.assertIn("row 2: unknown setting 'colum'", str(e.exception))

    def test_example_file_is_valid(self):
        example = Path(__file__).resolve().parent.parent / "tailoring.example.json"
        self.assertIn("applied", tailoring.apply(example))
