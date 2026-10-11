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

from taxcheck.bc_pst import BcPstChecker
from taxcheck.browser import Browser
from taxcheck.checks import RowChecker
from taxcheck.fedcorp import FederalCorporations
from taxcheck.gst import GstChecker, cra_today
from taxcheck.logsetup import setup_logging
from taxcheck.mb_rst import MbRstChecker
from taxcheck.qst import QstChecker
from taxcheck.sk_pst import SkPstAssistant
from taxcheck.workbook import InputError, SupplierSheet, parse_date

log = logging.getLogger("check_suppliers")


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
    if out.suffix.lower() != ".xlsx":
        log.warning("Results are always saved as an Excel workbook; writing %s", out.with_suffix(".xlsx"))
        out = out.with_suffix(".xlsx")
    default_date = parse_date(args.date) if args.date else cra_today()

    sheet = SupplierSheet(src, args.sheet)
    rows = list(sheet.rows())
    log.info("%d suppliers found (columns used: %s)", len(rows), ", ".join(sheet.cols))

    corporations = None if args.no_name_lookup else FederalCorporations(cache_dir=args.cache_dir)
    gst = GstChecker(corporations=corporations)

    results = {}
    started = time.monotonic()
    with contextlib.ExitStack() as stack:
        # Browsers start lazily, only if some row needs one.
        browser = None if args.no_browser else stack.enter_context(Browser(debug_dir=log_path.parent))
        qst = QstChecker(nr_list_file=args.nr_list, browser=browser, cache_dir=args.cache_dir)
        sk = None
        if args.sk_assist:
            sk = SkPstAssistant(stack.enter_context(Browser(headless=False, debug_dir=log_path.parent)))
        checker = RowChecker(
            gst,
            qst,
            bc=browser and BcPstChecker(browser),
            mb=browser and MbRstChecker(browser),
            sk=sk,
        )
        try:
            for i, (r, values) in enumerate(rows, 1):
                date = parse_date(values.get("date"))
                if values.get("date") and not date:
                    log.warning("Row %d: couldn't read date %r; using %s", r, values.get("date"), default_date)
                date = date or default_date
                log.debug("---- Row %d: %s", r, values.get("name"))
                results[r] = checker.check(values, date)
                summary = ", ".join(f"{k}={v.status}" for k, v in results[r].items())
                log.info("[%d/%d] %s: %s", i, len(rows), values.get("name"), summary)
        except KeyboardInterrupt:
            # A long run stopped with Ctrl+C keeps what it has; re-run on the output to finish the rest.
            log.warning("Stopped early after %d of %d suppliers; saving the results so far.", len(results), len(rows))
            stopped_early = True
        else:
            stopped_early = False

    counts = Counter(res.status for by_tax in results.values() for res in by_tax.values())
    log.info("Finished %d suppliers in %.0fs: %s", len(rows), time.monotonic() - started, dict(counts))

    notes = [
        f"Run on {dt.date.today()} from {src.name}. Log file: {log_path}",
        "GST/HST: CRA GST/HST Registry, plus CRA's simplified GST/HST registrant list.",
        "QST: Revenu Québec QST validation API (TQ numbers) and Revenu Québec's NR registrant list.",
        "BC PST: eTaxBC PST Number Verification Service. MB RST: Manitoba TAXcess RST Registration Registry.",
        "SK PST: SETS PST On-Line Registry (needs --sk-assist, because of its CAPTCHA).",
        "GST/HST names that don't match CRA are retried with the official name from Corporations Canada's open data.",
        "NOT CONFIRMED on GST/HST usually means the legal name doesn't match CRA's records; fix the name and re-run.",
    ]
    if gst.simplified.error:
        notes.append(gst.simplified.error)
    if corporations and corporations.error:
        notes.append(corporations.error)
    if qst.nr_error:
        notes.append(qst.nr_error)
    if stopped_early:
        notes.insert(0, f"STOPPED EARLY: only {len(results)} of {len(rows)} suppliers were checked. Rows without results weren't checked.")
    saved = save_workbook(sheet, results, out, notes)
    log.info("Results written to %s", saved)
    return saved


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("workbook", help="supplier list (.xlsx)")
    ap.add_argument("-o", "--output", help="where to write results (default: <workbook>_tax_check.xlsx)")
    ap.add_argument("--sheet", help="sheet name (default: first sheet)")
    ap.add_argument("--date", help="date to confirm registration on, YYYY-MM-DD (default: each row's date column, else today)")
    ap.add_argument("--no-browser", action="store_true", help="skip BC and Manitoba lookups, which need a headless browser")
    ap.add_argument("--sk-assist", action="store_true",
                    help="check Saskatchewan PST: opens a browser window where you tick its CAPTCHA once")
    ap.add_argument("--no-name-lookup", action="store_true",
                    help="don't retry GST/HST name mismatches with the federal corporate name (skips a ~110 MB download)")
    ap.add_argument("--cache-dir", default="cache", help="folder for downloaded reference data (default: cache)")
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
    except InputError as e:
        log.error("Can't run: %s", e)
        return 1
    except Exception as e:
        # One line on screen; the traceback goes to the log file.
        log.error("Run failed: %s: %s", type(e).__name__, e)
        log.debug("Traceback for the failure above", exc_info=True)
        log.error("See %s for details.", log_path)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
