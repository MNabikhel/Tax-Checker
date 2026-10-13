"""Check suppliers' Canadian sales-tax registrations from an Excel workbook.

    python check_suppliers.py                 (a window opens to choose the workbook)
    python check_suppliers.py suppliers.xlsx
    python check_suppliers.py suppliers.xlsx -o results.xlsx --date 2026-09-30

Results are saved next to the chosen workbook, and each run's detailed log (plus any failure
screenshots) goes to a "logs" folder beside it.
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
from taxcheck.checks import DryRun, RowChecker
from taxcheck.fedcorp import FederalCorporations
from taxcheck.gst import GstChecker, cra_today
from taxcheck.logsetup import setup_logging
from taxcheck.mb_rst import MbRstChecker
from taxcheck.orgbook import OrgBookBC
from taxcheck.qst import QstChecker
from taxcheck.result import NAME_NOT_MATCHED, REGISTERED
from taxcheck.sk_pst import SkPstAssistant
from taxcheck.workbook import InputError, SupplierSheet, header_hints, parse_date

log = logging.getLogger("check_suppliers")

HERE = Path(__file__).resolve().parent  # the tool's own folder, for data shared between runs
LAST_FOLDER = HERE / ".last_folder"  # where the file picker opens next time


def _start_folder():
    try:
        folder = Path(LAST_FOLDER.read_text(encoding="utf-8").strip())
        if folder.is_dir():
            return str(folder)
    except OSError:
        pass
    return str(Path.home())


def _remember_folder(path):
    try:
        LAST_FOLDER.write_text(str(Path(path).resolve().parent), encoding="utf-8")
    except OSError:
        pass  # only a convenience


def choose_workbook():
    """Ask for the supplier file: a file-picker window when there's a screen, otherwise a typed path.

    Returns the chosen path, or None if the person cancelled.
    """
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)  # don't let the window open behind the console
        path = filedialog.askopenfilename(
            title="Choose the supplier workbook to check",
            initialdir=_start_folder(),
            filetypes=[("Supplier lists", "*.xlsx *.xlsm *.csv"), ("All files", "*.*")],
        )
        root.destroy()
        if path:
            _remember_folder(path)
        return path or None
    except Exception:  # no tkinter, or no display (e.g. a server): ask in the console instead
        try:
            typed = input("Path to the supplier workbook (.xlsx, .xlsm or .csv): ").strip().strip('"')
        except EOFError:
            return None
        return typed or None


def default_log_dir(workbook):
    """Logs go in a "logs" folder next to the workbook, so outputs stay together."""
    folder = Path(workbook).resolve().parent
    return folder / "logs" if folder.is_dir() else HERE / "logs"


def check_columns(args):
    """--check-columns: show how the file's columns are read and what each row would check, offline."""
    from openpyxl.utils import get_column_letter

    try:
        sheet = SupplierSheet(Path(args.workbook), args.sheet)
    except InputError as e:
        if "header row" not in str(e):
            log.error("%s", e)
            return 1
        log.error("No header row recognized (it needs a supplier-name column plus one other recognized column).")
        try:
            row, hints = header_hints(args.workbook, args.sheet)
        except Exception:
            return 1
        if hints:
            log.info("Row %d looks most like the header row. Per column:", row)
            for letter, header, field, hint in hints:
                if field:
                    log.info("  %3s '%s' -> %s (recognized)", letter, header, field)
                elif hint:
                    log.info("  %3s '%s': closest alias '%s' (%s); if it holds that, add '%s' to COLUMNS['%s']",
                             letter, header, hint[0], hint[1], header, hint[1])
                else:
                    log.info("  %3s '%s': no close alias", letter, header)
            log.info("Fields: name (legal name, required), trade_name, province, gst, bn, qst, pst, bc_pst, sk_pst, "
                     "mb_rst, date. Add spellings to COLUMNS in taxcheck/workbook.py.")
        return 1
    for line in sheet.describe_columns():
        log.info(line)
    rows = list(sheet.rows())
    log.info("%d supplier rows. What the first %d would check (no lookups are made):", len(rows), min(len(rows), args.preview))
    dry = DryRun()
    checker = RowChecker(dry, dry, bc=dry, mb=dry, sk=dry)
    for r, values in rows[: args.preview]:
        date = parse_date(values.get("date")) or cra_today()
        log.info("  row %d: %s", r, {k: v for k, v in values.items() if v not in (None, "")})
        for tax, res in checker.check(values, date).items():
            log.info("      %-7s %-14s %s", tax, res.status, res.detail)
    return 0


def save_workbook(sheet, results, out, notes, ok_statuses):
    """Save results; if the file is open in Excel (locked), save under a new name instead."""
    try:
        sheet.write_results(results, out, notes, ok_statuses)
        return out
    except PermissionError:
        alt = out.with_name(f"{out.stem}_{dt.datetime.now():%H%M%S}{out.suffix}")
        log.warning("%s is locked (open in Excel?); saving to %s instead", out, alt)
        sheet.write_results(results, alt, notes, ok_statuses)
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
    orgbook = None if args.no_name_lookup else OrgBookBC()
    gst = GstChecker(corporations=corporations, name_sources=[orgbook] if orgbook else None)

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
        "GST/HST names that don't match CRA are retried with official names: Revenu Québec's (when a QST number "
        "is given), Corporations Canada's open data, and OrgBook BC.",
        "GST/HST numbers whose name doesn't match are still checked by number: 'REGISTERED - NAME NOT MATCHED' means "
        "the number is registered but not under the sheet's name (CRA's name is shown when known).",
        "Vendor-master shorthand such as '(DNU)', 'O/A' and 'DBA' is removed from names before asking CRA. When a "
        "transaction date is in the past, registrations are also checked as of today, and ones that have ended are noted.",
    ]
    if gst.simplified.error:
        notes.append(gst.simplified.error)
    for source in (corporations, orgbook):
        if source and source.error:
            notes.append(source.error)
    if qst.nr_error:
        notes.append(qst.nr_error)
    if stopped_early:
        notes.insert(0, f"STOPPED EARLY: only {len(results)} of {len(rows)} suppliers were checked. Rows without results weren't checked.")
    ok_statuses = (REGISTERED, NAME_NOT_MATCHED) if args.trust_gst_number else (REGISTERED,)
    if args.trust_gst_number:
        notes.append("--trust-gst-number: 'REGISTERED - NAME NOT MATCHED' counts as OK in the Overall column.")
    saved = save_workbook(sheet, results, out, notes, ok_statuses)
    log.info("Results written to %s", saved)
    follow_up = sum(1 for by_tax in results.values() if (g := by_tax.get("gst")) and (g.status == NAME_NOT_MATCHED or g.ended))
    if follow_up:
        log.info("%d GST/HST numbers need a person to look at the vendor name or status: see the 'Name Follow-up' sheet.", follow_up)
    log.info("Log written to %s", log_path)
    return saved


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("workbook", nargs="?", help="supplier list (.xlsx, .xlsm or .csv); leave out to choose it in a window")
    ap.add_argument("-o", "--output", help="where to write results (default: <workbook>_tax_check.xlsx next to the workbook)")
    ap.add_argument("--sheet", help="sheet name (default: first sheet)")
    ap.add_argument("--date", help="date to confirm registration on, YYYY-MM-DD (default: each row's date column, else today)")
    ap.add_argument("--no-browser", action="store_true", help="skip BC and Manitoba lookups, which need a headless browser")
    ap.add_argument("--sk-assist", action="store_true",
                    help="check Saskatchewan PST: opens a browser window where you tick its CAPTCHA once")
    ap.add_argument("--no-name-lookup", action="store_true",
                    help="don't look up official names (federal registry ~110 MB download, OrgBook BC) for GST/HST name mismatches")
    ap.add_argument("--cache-dir", default=str(HERE / "cache"),
                    help="folder for downloaded reference data (default: cache in the tool's folder)")
    ap.add_argument("--nr-list", help="saved copy of Revenu Québec's NR registrant list page, if it can't be downloaded")
    ap.add_argument("--log-dir", help="folder for run logs and failure screenshots (default: logs next to the workbook)")
    ap.add_argument("-v", "--verbose", action="store_true", help="show the detailed log on screen too")
    ap.add_argument("--trust-gst-number", action="store_true",
                    help="count 'REGISTERED - NAME NOT MATCHED' (GST number registered, name not confirmed) as OK")
    ap.add_argument("--check-columns", action="store_true",
                    help="don't check anything: show how the columns are read and what the first rows would check")
    ap.add_argument("--preview", type=int, default=10, help="rows to show with --check-columns (default: 10)")
    args = ap.parse_args(argv)
    if args.date and not parse_date(args.date):
        ap.error("--date must look like 2026-09-30")
    if not args.workbook:
        args.workbook = choose_workbook()
        if not args.workbook:
            print("No workbook chosen; nothing to do.")
            return 1
        print(f"Checking {args.workbook}")

    log_path = setup_logging(args.log_dir or default_log_dir(args.workbook), args.verbose)
    log.info("Log file: %s", log_path)
    log.debug("Arguments: %s", vars(args))
    log.debug("Python %s on %s", sys.version.split()[0], platform.platform())
    if args.check_columns:
        return check_columns(args)
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
