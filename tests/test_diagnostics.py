"""Diagnostics folder: off unless configured, by environment variable or the per-user setting file."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from taxcheck import diagnostics


class Diagnostics(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.setting = self.dir / "diagnostics_dir.txt"
        p = mock.patch.object(diagnostics, "SETTING_FILE", self.setting)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_off_by_default(self):
        with mock.patch.dict(os.environ, {"TAXCHECK_DIAGNOSTICS_DIR": ""}):
            self.assertIsNone(diagnostics.diagnostics_dir())

    def test_setting_file_with_environment_variable(self):
        self.setting.write_text("# comment\n%TC_TEST_ROOT%/Diagnostics\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"TAXCHECK_DIAGNOSTICS_DIR": "", "TC_TEST_ROOT": str(self.dir)}):
            self.assertEqual(diagnostics.diagnostics_dir(), Path(self.dir) / "Diagnostics")
            log = self.dir / "run.log"
            log.write_text("x", encoding="utf-8")
            self.assertEqual(diagnostics.copy_to_diagnostics(log), Path(self.dir) / "Diagnostics")
            self.assertTrue((self.dir / "Diagnostics" / "run.log").exists())

    def test_unset_variable_or_missing_parent_means_off(self):
        self.setting.write_text("%TC_NOT_SET_ANYWHERE%/Diagnostics\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"TAXCHECK_DIAGNOSTICS_DIR": ""}):
            os.environ.pop("TC_NOT_SET_ANYWHERE", None)
            self.assertIsNone(diagnostics.diagnostics_dir())
        with mock.patch.dict(os.environ, {"TAXCHECK_DIAGNOSTICS_DIR": str(self.dir / "no" / "such" / "Diagnostics")}):
            self.assertIsNone(diagnostics.diagnostics_dir())

    def test_environment_off_wins(self):
        self.setting.write_text(str(self.dir / "Diagnostics"), encoding="utf-8")
        with mock.patch.dict(os.environ, {"TAXCHECK_DIAGNOSTICS_DIR": "off"}):
            self.assertIsNone(diagnostics.diagnostics_dir())


if __name__ == "__main__":
    unittest.main()
