"""Check suppliers' Canadian sales-tax registrations from an Excel workbook.

    python check_suppliers.py suppliers.xlsx
    python check_suppliers.py suppliers.xlsx -o results.xlsx --date 2026-09-30

Each run writes a detailed log to logs/tax_check_<timestamp>.log for debugging.
See README.md for the expected columns and what each check does.
"""

import argparse
import contextlib
import datetime as dt
import logging
import platform
import sys
import time
from collections import Counter
from pathlib import Path

from taxcheck.checks import RowChecker
from taxcheck.gst import GstChecker
from taxcheck.logsetup import setup_logging
from taxcheck.qst import QstChecker
from taxcheck.workbook import SupplierSheet, normalize_province, parse_date

log = logging.getLogger("check_suppliers")


def start_browser(stack, debug_dir):
    """Start the headless browser for BC lookups; on failure, log why and carry on without it."""
    try:
        from taxcheck.bc_pst import BcPstChecker

        return stack.enter_context(BcPstChecker(debug_dir=debug_dir))
    except Exception:
        log.exception(
            "Couldn't start the browser for BC PST lookups; BC rows will be marked MANUAL CHECK. "
            "Run 'pip install playwright' and 'playwright install chromium' to enable them."
        )
        return None


def save_workbook(sheet, results, out, notes):
    """Save results; if the file is open in Excel (locked), save under a new name instead."""
    try:
        sheet.write_results(results, out, notes)
        return out
    except PermissionError:
        alt = out.with_name(f"{out.stem}_{dt.datetime.now():%H%M%S}{out.suffix}")
        log.warning("%s is locked (open in Excel?); saving to %s instead", out, alt)
        sheet.write_results(results, alt, notes)
        return alt


def run(args, log_path):
    src = Path(args.workbook)
    out = Path(args.output) if args.output else src.with_name(f"{src.stem}_tax_check.xlsx")
    default_date = parse_date(args.date) if args.date else dt.date.today()

    sheet = SupplierSheet(src, args.sheet)
    rows = list(sheet.rows())
    log.info("%d suppliers found (columns used: %s)", len(rows), ", ".join(sheet.cols))

    gst, qst = GstChecker(), QstChecker(nr_list_file=args.nr_list)
    needs_browser = not args.no_browser and any(
        v.get("bc_pst") or (v.get("pst") and normalize_province(v.get("province")) == "BC")
        for _, v in rows
    )

    results = {}
    started = time.monotonic()
    with contextlib.ExitStack() as stack:
        bc = start_browser(stack, log_path.parent) if needs_browser else None
        checker = RowChecker(gst, qst, bc)
        for i, (r, values) in enumerate(rows, 1):
            date = parse_date(values.get("date"))
            if values.get("date") and not date:
                log.warning("Row %d: couldn't read date %r; using %s", r, values.get("date"), default_date)
            date = date or default_date
            log.debug("---- Row %d: %s", r, values.get("name"))
            results[r] = checker.check(values, date)
            summary = ", ".join(f"{k}={v.status}" for k, v in results[r].items())
            log.info("[%d/%d] %s: %s", i, len(rows), values.get("name"), summary)

    counts = Counter(res.status for by_tax in results.values() for res in by_tax.values())
    log.info("Finished %d suppliers in %.0fs: %s", len(rows), time.monotonic() - started, dict(counts))

    notes = [
        f"Run on {dt.date.today()} from {src.name}. Log file: {log_path}",
        "GST/HST: CRA GST/HST Registry, plus CRA's simplified GST/HST registrant list.",
        "QST: Revenu Québec QST validation API (TQ numbers) and Revenu Québec's NR registrant list.",
        "BC PST: eTaxBC PST Number Verification Service.",
        "SK PST and MB RST have no automatable lookup; those rows are marked MANUAL CHECK.",
        "NOT CONFIRMED on GST/HST usually means the legal name doesn't match CRA's records; fix the name and re-run.",
    ]
    if gst.simplified.error:
        notes.append(gst.simplified.error)
    if qst.nr_error:
        notes.append(qst.nr_error)
    saved = save_workbook(sheet, results, out, notes)
    log.info("Results written to %s", saved)
    return saved


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("workbook", help="supplier list (.xlsx)")
    ap.add_argument("-o", "--output", help="where to write results (default: <workbook>_tax_check.xlsx)")
    ap.add_argument("--sheet", help="sheet name (default: first sheet)")
    ap.add_argument("--date", help="date to confirm registration on, YYYY-MM-DD (default: each row's date column, else today)")
    ap.add_argument("--no-browser", action="store_true", help="skip BC PST lookups, which need a headless browser")
    ap.add_argument("--nr-list", help="saved copy of Revenu Québec's NR registrant list page, if it can't be downloaded")
    ap.add_argument("--log-dir", default="logs", help="folder for run logs (default: logs)")
    ap.add_argument("-v", "--verbose", action="store_true", help="show the detailed log on screen too")
    args = ap.parse_args(argv)
    if args.date and not parse_date(args.date):
        ap.error("--date must look like 2026-09-30")

    log_path = setup_logging(args.log_dir, args.verbose)
    log.info("Log file: %s", log_path)
    log.debug("Arguments: %s", vars(args))
    log.debug("Python %s on %s", sys.version.split()[0], platform.platform())
    try:
        run(args, log_path)
    except Exception as e:
        log.exception("Run failed: %s", e)
        log.error("See %s for details.", log_path)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
