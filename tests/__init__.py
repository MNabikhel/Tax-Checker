"""Tests run full checks through check_suppliers; keep their "last run" note (for Resume Last Run.bat) out of the
tool folder, where it would point the user's next resume at a test's temporary files."""

import tempfile
from pathlib import Path

import check_suppliers

check_suppliers.LAST_RUN = Path(tempfile.mkdtemp(prefix="taxcheck_tests_")) / ".last_run"
