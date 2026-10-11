"""Run logging: a detailed log file per run (for debugging later) plus brief console output."""

import contextlib
import datetime as dt
import logging
import sys
from pathlib import Path

FILE_FORMAT = "%(asctime)s %(levelname)-7s %(name)-18s %(message)s"


def setup_logging(log_dir="logs", verbose=False):
    """Log everything (DEBUG) to logs/tax_check_<timestamp>.log; INFO and up to the console.

    Returns the log file path.
    """
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"tax_check_{dt.datetime.now():%Y%m%d_%H%M%S}.log"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()

    file_handler = logging.FileHandler(path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(FILE_FORMAT))
    root.addHandler(file_handler)

    # Never crash on a supplier name the console can't display (e.g. accents on an old Windows code page).
    with contextlib.suppress(Exception):
        sys.stderr.reconfigure(errors="backslashreplace")
    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(console)

    # Library chatter (connection pools, browser driver) only matters when something is badly wrong.
    for noisy in ("urllib3", "asyncio", "playwright"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return path
