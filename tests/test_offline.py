"""Offline tests (no network): number parsing, page parsing, workbook reading and check routing.

    python -m unittest discover tests
"""

import datetime as dt
import tempfile
import unittest
from pathlib import Path

import openpyxl

from taxcheck.checks import RowChecker
from taxcheck.gst import SimplifiedList, name_variants
from taxcheck.numbers import BadNumber, bn_check_digit_ok, parse_bc_pst, parse_gst, parse_qst
from taxcheck.qst import parse_nr_list
from taxcheck.result import INVALID, MANUAL, MISSING, REGISTERED, Result
from taxcheck.workbook import SupplierSheet, normalize_province, parse_date

TODAY = dt.date(2026, 10, 7)


class Numbers(unittest.TestCase):
    def test_check_digit(self):
        for good in ("857305932", "108161779", "751950577"):
            self.assertTrue(bn_check_digit_ok(good))
        for bad in ("123456789", "108161778"):
            self.assertFalse(bn_check_digit_ok(bad))

    def test_gst_formats(self):
        self.assertEqual(parse_gst("10816 1779 RT 0001"), ("108161779", "RT0001"))
        self.assertEqual(parse_gst("108161779"), ("108161779", None))
        with self.assertRaises(BadNumber):
            parse_gst("123456789RT0001")
        with self.assertRaises(BadNumber):
            parse_gst("12345")

    def test_qst_formats(self):
        self.assertEqual(parse_qst("1019288451 TQ 0004"), ("TQ", "1019288451TQ0004"))
        self.assertEqual(parse_qst("1019288451"), ("TQ", "1019288451TQ0001"))
        self.assertEqual(parse_qst("NR 0013 0061"), ("NR", "NR00130061"))
        self.assertEqual(parse_qst("108161779"), ("FI", "108161779"))
        with self.assertRaises(BadNumber):
            parse_qst("TQ123")

    def test_bc_pst(self):
        self.assertEqual(parse_bc_pst("PST-1000-7572"), "1000-7572")
        self.assertEqual(parse_bc_pst("10007572"), "1000-7572")
        with self.assertRaises(BadNumber):
            parse_bc_pst("1000-757")


class Parsing(unittest.TestCase):
    def test_name_variants(self):
        self.assertEqual(
            name_variants("The University of British Columbia", "UBC", "the university of british columbia"),
            ["The University of British Columbia", "University of British Columbia", "UBC"],
        )

    def test_simplified_list_picks_account_active_on_date(self):
        page = """<table><tr><th>Legal name</th></tr>
        <tr><td>Akindele T. Francis</td><td>-</td><td>767691215RT9999</td><td>July 1, 2023</td><td>-</td></tr>
        <tr><td>Akindele T. Francis</td><td>-</td><td>767691215RT0001</td><td>February 22, 2023</td><td>June 30, 2023</td></tr>
        </table>"""
        sl = SimplifiedList(session=None)
        sl._rows = SimplifiedList.parse(page)
        self.assertEqual(sl.lookup("767691215", dt.date(2023, 3, 1))["number"], "767691215RT0001")
        self.assertEqual(sl.lookup("767691215", TODAY)["number"], "767691215RT9999")
        self.assertIsNone(sl.lookup("108161779", TODAY))

    def test_nr_list(self):
        page = "<table><tr><th>Trade name</th><th>Legal name</th><th>QST number</th></tr>" \
               "<tr><td>ExampleStream</td><td>ExampleStream B.V.</td><td>NR 0013 0061</td></tr></table>"
        self.assertEqual(parse_nr_list(page), {"NR00130061": {"trade_name": "ExampleStream", "legal_name": "ExampleStream B.V."}})


class Workbook(unittest.TestCase):
    def test_header_detection_and_dates(self):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Vendor list, Q3"])
        ws.append(["Vendor Name", "Prov.", "GST #", "Invoice Date"])
        ws.append(["Amazon.com.ca ULC", "Ontario", "857305932RT0001", dt.datetime(2026, 9, 30)])
        ws.append([None, None, None, None])
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "in.xlsx"
            wb.save(path)
            sheet = SupplierSheet(path)
            self.assertEqual(sheet.header_row, 2)
            rows = list(sheet.rows())
            self.assertEqual(len(rows), 1)
            values = rows[0][1]
            self.assertEqual(normalize_province(values["province"]), "ON")
            self.assertEqual(parse_date(values["date"]), dt.date(2026, 9, 30))


class FakeGst:
    def __init__(self):
        self.calls = []

    def check(self, bn9, names, date):
        self.calls.append(bn9)
        return Result(REGISTERED, "fake")


class FakeBc:
    def check(self, bn9, pst):
        return Result(REGISTERED, f"{bn9} {pst}")


class Routing(unittest.TestCase):
    def setUp(self):
        self.gst = FakeGst()
        self.checker = RowChecker(self.gst, qst=None, bc=FakeBc())

    def test_bc_uses_bn_from_gst_number(self):
        out = self.checker.check({"name": "UBC", "province": "BC", "gst": "108161779RT0001", "pst": "PST-1000-7572"}, TODAY)
        self.assertEqual(out["bc_pst"].detail, "108161779 1000-7572")

    def test_missing_and_manual(self):
        out = self.checker.check({"name": "X", "province": "Québec"}, TODAY)
        self.assertEqual(out["gst"].status, MISSING)
        self.assertEqual(out["qst"].status, MISSING)
        out = self.checker.check({"name": "X", "province": "SK", "pst": "1234567"}, TODAY)
        self.assertEqual(out["sk_pst"].status, MANUAL)
        self.assertNotIn("bc_pst", out)

    def test_bad_number_reported_not_raised(self):
        out = self.checker.check({"name": "X", "gst": "123456789RT0001"}, TODAY)
        self.assertEqual(out["gst"].status, INVALID)
        self.assertEqual(self.gst.calls, [])

    def test_qst_without_suffix_is_flagged(self):
        calls = []

        class FakeQst:
            def check(self, number, suffix_assumed=False):
                calls.append((number, suffix_assumed))
                return Result(REGISTERED, "")

        checker = RowChecker(self.gst, FakeQst())
        checker.check({"name": "X", "qst": 1019288451}, TODAY)
        checker.check({"name": "X", "qst": "1019288451 TQ 0004"}, TODAY)
        self.assertEqual(calls, [("1019288451TQ0001", True), ("1019288451TQ0004", False)])

    def test_duplicate_numbers_looked_up_once(self):
        self.checker.check({"name": "A", "gst": "108161779RT0001"}, TODAY)
        self.checker.check({"name": "A", "gst": "108161779RT0001"}, TODAY)
        self.assertEqual(self.gst.calls, ["108161779"])

    def test_financial_institution_qst_goes_to_cra(self):
        out = self.checker.check({"name": "Bank", "qst": "108161779"}, TODAY)
        self.assertEqual(out["qst"].status, REGISTERED)
        self.assertIn("listed financial institution", out["qst"].detail)


if __name__ == "__main__":
    unittest.main()
