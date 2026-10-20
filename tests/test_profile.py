"""profile_workbook.py: the shape of a supplier file without its contents."""

import datetime as dt
import tempfile
import unittest
from pathlib import Path

import openpyxl

import profile_workbook


class Profile(unittest.TestCase):
    def test_counts_patterns_and_never_names_or_numbers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "vendors.xlsx"
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.append(["Supplier #", "Company Name", "State", "Country", "Postal Code", "Tax ID", "Check/ Itm Date"])
            rows = [
                ["V1", "(DNU) PRAIRIE WIDGET INDUSTRIAL PRODUCTS"[:40], "MB", "CA", "R3H 0H3", "123456782RT0001", dt.datetime(2026, 1, 5)],
                ["V2", "1234567 ONTARIO INC. O/A MAPLE PRO SERVICES"[:40], "Ont.", "CA", "L2V1W9", "123456782 RT0001", "2025-11-13"],
                ["V3", "NORTHWIND SUPPLY LLC", "WA", "US", "98101", "12-3456789", dt.datetime(2026, 3, 1)],
                ["V4", "ACME WIDGETS*", "", "CA", "T5M 3N8", "", None],
                ["V5", "KESTREL DESIGN STUDIO HOLDINGS LIMITED OF"[:40], "AB", "CA", "T6N 1C2", "123456782RC0001", dt.datetime(2026, 2, 1)],
            ]
            for r in rows:
                ws.append(r)
            wb.save(path)
            text = "\n".join(profile_workbook.profile(str(path)))
        for r in rows:  # nothing identifying leaks: names, tax numbers, postal codes, supplier ids
            for value in (r[1], r[5], r[4], r[0]):
                if value and len(str(value)) > 3:
                    self.assertNotIn(str(value), text)
        self.assertIn("5 data rows", text)
        self.assertIn("999999999AA9999", text)  # Tax ID shapes
        self.assertIn("Ont.", text)  # province spellings as written
        self.assertEqual({len(r[1]) for r in rows if len(r[1]) > 30}, {40})
        self.assertIn("Longest name: 40 characters, reached by 3 names (probably the vendor master field limit", text)
        self.assertIn("do-not-use marker", text)
        self.assertIn("operating-as", text)
        self.assertIn("Corporate income tax account (RC)", text)
        self.assertIn("SHARED NUMBERS: 1", text)
        self.assertIn("gst     WOULD CHECK", text)
        self.assertIn("gst     NOT APPLICABLE", text)  # the US vendor
        self.assertIn("4 Canadian, 1 foreign", text)
        self.assertIn("GST COLUMN, foreign rows", text)

    def test_unrecognised_headers_are_listed_without_supplier_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "other.xlsx"
            wb = openpyxl.Workbook()
            wb.active.append(["Vendor #", "Supplier Legal Name", "Prov/St", "Vendor Tax No"])
            wb.active.append(["V1", "KESTREL WIDGETS", "Man", "123456782RT0001"])
            wb.save(path)
            text = "\n".join(profile_workbook.profile(str(path)))
        self.assertIn("NO HEADER ROW RECOGNISED", text)
        self.assertIn("Supplier Legal Name | Prov/St | Vendor Tax No", text)  # the header row as written
        for secret in ("KESTREL", "123456782", "Man |"):
            self.assertNotIn(secret, text)

    def test_free_text_in_province_or_country_is_masked(self):
        self.assertEqual(profile_workbook.safe_value("Ont."), "Ont.")
        self.assertEqual(profile_workbook.safe_value("123 Main Street Suite 4"), "999 AAAA AAAAAA AAAA...")
        self.assertEqual(profile_workbook.safe_value("Newfoundland and Labrador"), "Newfoundland and Labrador")


if __name__ == "__main__":
    unittest.main()
