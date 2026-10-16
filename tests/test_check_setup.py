"""The setup test script (check_setup.py): error explanations and the workbook step, offline."""

import io
import logging
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import requests

import check_setup
from tests.test_vendor_export import ROWS, make_export


class ExplainError(unittest.TestCase):
    def test_certificate_problem_is_named(self):
        e = requests.exceptions.SSLError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate")
        what, fix = check_setup.explain_error(e)
        self.assertIn("certificate", what)
        self.assertIn("Test Setup.bat", fix)

    def test_timeout_and_dns(self):
        self.assertIn("20 seconds", check_setup.explain_error(requests.exceptions.ConnectTimeout("x"))[0])
        dns = requests.exceptions.ConnectionError("Failed to resolve 'x' (NameResolutionError)")
        self.assertIn("internet", check_setup.explain_error(dns)[0])


class WorkbookStep(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "vendor_export.xlsx"
        make_export(self.path)
        (Path(self.tmp.name) / "logs").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_counts_and_samples(self):
        rep = check_setup.Report()
        with redirect_stdout(io.StringIO()):
            samples = check_setup.check_workbook(rep, self.path)
        text = "\n".join(rep.lines)
        self.assertIn(f"{len(ROWS)} supplier rows", text)
        self.assertIn("not applicable (foreign) 2", text)
        self.assertNotIn(check_setup.FAIL, [r[0] for r in rep.results])
        self.assertEqual(samples[0][2], "108161779")  # business number taken from the Tax ID
        self.assertTrue(all(len(s[2]) == 9 for s in samples))

    def test_lookups_skipped_when_cra_unreachable(self):
        rep = check_setup.Report()
        with redirect_stdout(io.StringIO()):
            check_setup.check_lookups(rep, [], {"CRA GST/HST Registry": False}, False, Path(self.tmp.name))
        self.assertEqual(rep.results[0][0], check_setup.FAIL)


if __name__ == "__main__":
    unittest.main()
