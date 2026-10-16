"""Check suppliers' Canadian sales-tax registrations from an Excel workbook.

    python check_suppliers.py                 (a window opens to choose the workbook)
    python check_suppliers.py suppliers.xlsx
    python check_suppliers.py suppliers.xlsx -o results.xlsx --date 2026-09-30

Results are saved next to the chosen workbook, and each run's detailed log (plus any failure
screenshots) goes to a "logs" folder beside it.
See README.md for the expected columns and what each check does.
"""

import argparse
import collections
import contextlib
import queue
import re
import datetime as dt
import logging
import platform
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from pathlib import Path

from taxcheck.bc_pst import BcPstChecker
from taxcheck.certs import use_system_certificates
from taxcheck.diagnostics import copy_to_diagnostics
from taxcheck.browser import Browser
from taxcheck.checks import DryRun, Offline, RowChecker
from taxcheck.fedcorp import FederalCorporations
from taxcheck.gst import GstChecker, cra_today
from taxcheck.logsetup import setup_logging
from taxcheck.mb_rst import MbRstChecker
from taxcheck.orgbook import OrgBookBC
from taxcheck.progress import ProgressReport, load_resume
from taxcheck.qst import QstChecker
from taxcheck.result import NAME_NOT_MATCHED, REGISTERED
from taxcheck.sk_pst import SkPstAssistant
from taxcheck.workbook import InputError, SupplierSheet, header_hints, parse_date

log = logging.getLogger("check_suppliers")

MAX_WORKERS = 6  # parallel lookups; each keeps its own pacing, so this caps the load on each government site
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


def show_in_explorer(path):
    """Open File Explorer with the results workbook selected (Windows launchers only; harmless elsewhere)."""
    if platform.system() != "Windows":
        return
    try:
        import subprocess

        subprocess.Popen(["explorer", "/select,", str(Path(path).resolve())])
    except Exception as e:
        log.debug("Couldn't open Explorer: %s", e)


def remember_workbook(path):
    """So "Test Setup.bat" can offer the trial on the same workbook without asking again."""
    try:
        (HERE / ".last_workbook").write_text(str(Path(path).resolve()), encoding="utf-8")
    except OSError:
        pass


TAX_CODES = {"G": "gst", "Q": "qst", "B": "bc_pst", "M": "mb_rst", "S": "sk_pst"}
TAX_LABELS = {"gst": "GST/HST", "qst": "QST", "bc_pst": "BC PST", "mb_rst": "Manitoba RST", "sk_pst": "Saskatchewan PST"}
TAX_WORDS = {"gst": "gst", "hst": "gst", "qst": "qst", "bc": "bc_pst", "pst": "bc_pst", "mb": "mb_rst", "rst": "mb_rst",
             "sk": "sk_pst"}


def parse_taxes(text):
    """"gst,mb" / "G M" / "all" -> set of tax keys (GST/HST always included), or None for all."""
    text = str(text or "").strip().lower()
    if not text or text == "all":
        return None
    keys = {"gst"}
    for token in re.split(r"[\s,;+]+", text):
        if not token:
            continue
        key = TAX_WORDS.get(token) or (TAX_CODES.get(token.upper()) if len(token) == 1 else None)
        if not key:
            raise ValueError(f"unknown tax '{token}' (use gst, qst, bc, mb, sk or all)")
        keys.add(key)
    return keys


DATE_MODES = {"none", "end", "full"}


def ask_gst_dates():
    """Second menu question: how much effort to spend on GST/HST registration dates."""
    print()
    print("GST/HST registration dates (CRA's registry gives no dates; they're found by asking about specific days):")
    print("  1  Quick: confirm each supplier is still registered today, and the cancellation date if not")
    print("     (about +1 hour on a full run; QST dates and registrations starting after the payment date always shown)")
    print("  2  Full: also find every supplier's GST/HST start date (slow: hours more; best for smaller lists)")
    print("  3  None (fastest)")
    while True:
        try:
            answer = input("Type 1, 2 or 3 and press Enter [1]: ").strip()
        except EOFError:
            return "end"
        if answer in ("", "1"):
            return "end"
        if answer == "2":
            return "full"
        if answer == "3":
            return "none"
        print("  Please type 1, 2 or 3.")


def ask_taxes():
    """Console menu used by the .bat launchers. Enter (or no console) = everything."""
    print()
    print("Which taxes should be checked?")
    print("  1  GST/HST only (fastest; no browser checks)")
    print("  2  Everything: GST/HST, QST, BC PST, Manitoba RST, Saskatchewan PST (Saskatchewan = manual-check note)")
    print("  3  Choose: type the letters, e.g.  G M    (G GST/HST  Q QST  B BC PST  M Manitoba RST  S Saskatchewan)")
    while True:
        try:
            answer = input("Type 1, 2 or 3 and press Enter [2]: ").strip()
        except EOFError:
            return None
        if answer in ("", "2"):
            return None
        if answer == "1":
            return {"gst"}
        if answer == "3":
            try:
                answer = input("Letters (GST/HST is always included): ")
            except EOFError:
                return None
        try:
            return parse_taxes(answer)
        except ValueError as e:
            print(f"  {e}. Try again.")


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
    checker = RowChecker(dry, dry, bc=dry, mb=dry, sk=dry, available=sheet.cols, taxes=args.taxes)
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


class CheckpointSaver:
    """Saves checkpoint copies of the results workbook in a background thread, from a snapshot of the results,
    so lookups carry on while it's written (a 20,000-row workbook took 5-90 s per save, and the whole run
    waited each time). One save at a time; the workbook object is only touched by the saving thread until
    wait() returns."""

    NOTES = ["IN PROGRESS: checkpoint save; the run hadn't finished when this was written."]

    def __init__(self, sheet, out):
        self.sheet, self.out, self._thread = sheet, out, None

    def save(self, results):
        self.wait()  # the previous save (rarely still running: checkpoints are thousands of rows apart)
        snapshot = dict(results)  # rows already finished; their results aren't changed afterwards

        def write():
            t0 = time.monotonic()
            try:
                saved = save_workbook(self.sheet, snapshot, self.out, self.NOTES, (REGISTERED,))
                log.info("CHECKPOINT: %d suppliers saved to %s (%.0fs, in the background)", len(snapshot), saved,
                         time.monotonic() - t0)
            except Exception as e:  # the progress file has every result; the run goes on
                log.warning("Checkpoint save failed (the run continues): %s: %s", type(e).__name__, e)

        self._thread = threading.Thread(target=write, name="checkpoint", daemon=True)
        self._thread.start()

    def wait(self):
        if self._thread:
            self._thread.join()
            self._thread = None


def run(args, log_path):
    src = Path(args.workbook)
    # A trial gets its own file, so it's obvious which results are partial and the full run never mixes with it.
    suffix = "_tax_check_TRIAL" if args.limit else "_tax_check"
    out = Path(args.output) if args.output else src.with_name(f"{src.stem}{suffix}.xlsx")
    if out.suffix.lower() != ".xlsx":
        log.warning("Results are always saved as an Excel workbook; writing %s", out.with_suffix(".xlsx"))
        out = out.with_suffix(".xlsx")
    default_date = parse_date(args.date) if args.date else cra_today()

    sheet = SupplierSheet(src, args.sheet)
    sheet.taxes = args.taxes  # only the chosen taxes get result columns
    rows = list(sheet.rows())
    log.info("%d suppliers found (columns used: %s)", len(rows), ", ".join(sheet.cols))

    # --resume: reuse what an earlier (stopped or crashed) run already finished, from its progress file.
    results = load_resume(args.resume) if args.resume else {}
    to_check = [(r, v) for r, v in rows if r not in results]
    if args.limit:
        # Count suppliers that will actually be looked up online: foreign rows and rows with a missing/invalid
        # number are instant, so a plain "first N rows" trial can contain only a handful of real lookups
        # (in the vendor export it was built for, only a fifth of the first rows are Canadian). Rows passed on the way are kept.
        from taxcheck.result import NOT_LOOKED_UP

        off = Offline()
        scout = RowChecker(off, off, bc=off, mb=off, sk=off, available=sheet.cols, taxes=args.taxes)
        found, cut = 0, len(to_check)
        for i, (r, v) in enumerate(to_check):
            if any(x.status == NOT_LOOKED_UP for x in scout.check(v, cra_today()).values()):
                found += 1
                if found == args.limit:
                    cut = i + 1
                    break
        to_check = to_check[:cut]
        log.info("--limit %d: checking the first %d suppliers, which include %d that need an online lookup",
                 args.limit, len(to_check), found)

    offline = args.offline
    if offline:
        log.info("--offline: no government site is contacted; numbers are only checked for format and check digit")
        corporations = orgbook = None
        gst = Offline()
    else:
        corporations = None if args.no_name_lookup else FederalCorporations(cache_dir=args.cache_dir)
        orgbook = None if args.no_name_lookup else OrgBookBC()
        gst = GstChecker(corporations=corporations, name_sources=[orgbook] if orgbook else None, cache_dir=args.cache_dir)

    progress = ProgressReport(log_path.parent, len(to_check), every_rows=args.progress_every)
    for r, values in rows:  # carry resumed rows into the new progress file, so the latest file is complete
        if r in results:
            progress.record(r, values, results[r], 0.0, source="resumed")

    started = time.monotonic()
    checkpoints = CheckpointSaver(sheet, out)
    failure = None
    with contextlib.ExitStack() as stack:
        if offline:
            qst = None
            off = Offline()
            checker = RowChecker(gst, off, bc=off, mb=off, sk=off, available=sheet.cols, taxes=args.taxes)
        else:
            # Browsers start lazily, only if some row needs one.
            browser = None if args.no_browser else stack.enter_context(Browser(debug_dir=log_path.parent))
            if getattr(gst, "simplified", None) is not None:
                gst.simplified.browser = browser  # fallback for canada.ca, which some networks stall
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
                available=sheet.cols, taxes=args.taxes, gst_dates=args.gst_dates,
            )
        def row_date(r, values):
            date = None if args.as_of_today else parse_date(values.get("date"))
            if values.get("date") and not date and not args.as_of_today:
                log.warning("Row %d: couldn't read date %r; using %s", r, values.get("date"), default_date)
            date = date or default_date
            if date > cra_today():
                log.debug("Row %d: date %s is in the future; CRA is asked about today instead", r, date)
                date = cra_today()
            return date

        executor = None
        workers = 1 if offline else max(1, min(args.workers, MAX_WORKERS))
        if workers > 1:
            # Each worker has its own CRA/Revenu Québec/OrgBook session and its own throttle (so each still waits
            # between its requests); they share the duplicate-lookup cache, the federal names and the simplified list.
            # Browser checks (Manitoba/BC/SK) can't leave the main thread, so they run there as rows come back.
            if hasattr(gst.simplified, "_load") and gst.simplified._rows is None:
                gst.simplified._load()  # once, on the main thread, where the browser fallback works
            pool = []
            for _ in range(workers):
                g = GstChecker(corporations=corporations, name_sources=[OrgBookBC()] if orgbook else None,
                               cache_dir=args.cache_dir)
                g.simplified = gst.simplified
                pool.append(RowChecker(g, QstChecker(nr_list_file=args.nr_list, cache_dir=args.cache_dir),
                                       available=sheet.cols, taxes=args.taxes, cache=checker._cache, defer_browser=True,
                                       gst_dates=args.gst_dates))
            free = queue.SimpleQueue()
            for c in pool:
                free.put(c)

            def work(r, values, date):
                c = free.get()
                try:
                    t0 = time.monotonic()
                    log.debug("---- Row %d: %s", r, values.get("name"))
                    return c.check(values, date), time.monotonic() - t0
                finally:
                    free.put(c)

            executor = stack.enter_context(ThreadPoolExecutor(max_workers=workers, thread_name_prefix="lookup"))
            # Submit a window ahead (not everything at once). It's wide so the workers keep going while the main
            # thread spends ~6 s on a Manitoba browser lookup; Ctrl+C cancels whatever is still queued.
            pending = collections.deque()
            source = iter(to_check)

            def top_up():
                for r, values in source:
                    pending.append((r, values, executor.submit(work, r, values, row_date(r, values))))
                    if len(pending) >= workers * 50:
                        break

            def results_in_order():
                top_up()
                while pending:
                    r, values, fut = pending.popleft()
                    while True:  # short waits so Ctrl+C is noticed on Windows
                        try:
                            out_row, secs = fut.result(timeout=0.5)
                            break
                        except FuturesTimeout:
                            continue
                    top_up()
                    yield r, values, out_row, secs
            log.info("Checking with %d parallel lookups (each paced as before); browser checks run alongside", workers)
        else:
            def results_in_order():
                for r, values in to_check:
                    t0 = time.monotonic()
                    log.debug("---- Row %d: %s", r, values.get("name"))
                    out_row = checker.check(values, row_date(r, values))
                    yield r, values, out_row, time.monotonic() - t0

        try:
            for i, (r, values, out_row, secs) in enumerate(results_in_order(), 1):
                t0 = time.monotonic()
                results[r] = checker.run_deferred(out_row)  # browser checks, main thread (no-op if none deferred)
                secs += time.monotonic() - t0
                country, prov, _, _ = RowChecker.locate(values)
                progress.record(r, values, results[r], secs, location=(country, prov))
                summary = ", ".join(f"{k}={v.status}" for k, v in results[r].items())
                log.info("[%d/%d] row %d %s: %s", i, len(to_check), r, values.get("name"), summary)
                if args.checkpoint_every and i % args.checkpoint_every == 0 and i < len(to_check):
                    checkpoints.save(results)
        except KeyboardInterrupt:
            # A long run stopped with Ctrl+C keeps what it has; --resume with the progress file finishes the rest.
            log.warning("Stopped early after %d of %d suppliers; saving the results so far.", len(results), len(rows))
            stopped_early = True
            if executor:
                executor.shutdown(wait=False, cancel_futures=True)
        except Exception as e:  # something outside a row's own error handling: keep what's done, then report it
            log.error("Run stopped by an unexpected error after %d suppliers: %s: %s", len(results), type(e).__name__, e)
            log.debug("Traceback for the failure above", exc_info=True)
            stopped_early, failure = True, e
            if executor:
                executor.shutdown(wait=False, cancel_futures=True)
        else:
            stopped_early = False
        finally:
            progress.close()
            checkpoints.wait()  # before the final save, which uses the same workbook

    counts = Counter(res.status for by_tax in results.values() for res in by_tax.values())
    if stopped_early:
        log.info("%s", progress.summary_line())
    log.info("Finished %d suppliers in %.0fs: %s", len(results), time.monotonic() - started, dict(counts))

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
    ]
    notes.append(f"Progress file (one line per supplier, used by --resume): {progress.path}")
    notes.insert(0, "GST/HST registration dates: " + {"none": "not looked up.", "end": "checked whether each supplier "
                    "is still registered today, with the cancellation date if not.", "full": "start and cancellation "
                    "dates looked up."}[args.gst_dates or "end"])
    notes.insert(0, "Taxes checked in this run: " + ", ".join(TAX_LABELS[k] for k in TAX_LABELS
                                                             if args.taxes is None or k in args.taxes) + ".")
    if offline:
        notes.insert(0, "OFFLINE RUN: nothing was looked up online. Numbers were checked for format and CRA check "
                        "digit only; 'NOT LOOKED UP' rows (Overall 'PENDING') still need the online run.")
    missing_tax_cols = [label for label, fields in (("QST", ("qst",)), ("BC PST", ("bc_pst", "pst")))
                        if not any(f in sheet.cols for f in fields)]
    if missing_tax_cols:
        notes.append(f"The workbook has no column for {', '.join(missing_tax_cols)} numbers, so those taxes are only "
                     "checked when a number turns up elsewhere (e.g. a QST number in the Tax ID column); suppliers "
                     "aren't flagged NO NUMBER for them.")
    if "country" in sheet.cols:
        notes.append("Non-Canadian suppliers (Country column, or a non-Canadian state when it's blank) are NOT APPLICABLE unless "
                     "their Tax ID is an explicit Canadian GST/HST number (9 digits + RT); those are checked.")
    if "postal" in sheet.cols:
        notes.append("A blank or non-Canadian province on a Canadian supplier is filled in from the postal code.")
    if args.resume:
        notes.append(f"Resumed from {args.resume}: rows finished in the earlier run were kept, rows with errors were re-checked.")
    if args.limit and len(results) < len(rows):
        notes.insert(0, f"TRIAL RUN (--limit {args.limit}): only {len(results)} of {len(rows)} suppliers have results.")
    if failure is not None:
        notes.insert(0, f"RUN FAILED: {type(failure).__name__}: {failure}. See the log; re-run with --resume to continue.")
    if gst.__class__ is Offline:
        pass
    elif gst.simplified.error:
        notes.append(gst.simplified.error)
    for source in (corporations, orgbook):
        if source and source.error:
            notes.append(source.error)
    if qst and qst.nr_error:
        notes.append(qst.nr_error)
    if stopped_early:
        notes.insert(0, f"STOPPED EARLY: only {len(results)} of {len(rows)} suppliers were checked. Rows without results weren't checked.")
    ok_statuses = (REGISTERED, NAME_NOT_MATCHED) if args.trust_gst_number else (REGISTERED,)
    if args.trust_gst_number:
        notes.append("--trust-gst-number: 'REGISTERED - NAME NOT MATCHED' counts as OK in the Overall column.")
    saved = save_workbook(sheet, results, out, notes, ok_statuses)
    log.info("Results written to %s", saved)
    log.info("")
    log.info("=" * 70)
    log.info("RESULTS WORKBOOK: %s", Path(saved).name)
    log.info("SAVED IN:         %s", Path(saved).resolve().parent)
    log.info("=" * 70)
    if args.show_result:
        show_in_explorer(saved)
    log.info("Log written to %s", log_path)
    log.info("Progress file: %s", progress.path)
    if failure is not None:
        raise failure
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
    ap.add_argument("--offline", action="store_true",
                    help="look nothing up: check number formats/check digits and routing for every row, write the results file")
    ap.add_argument("--taxes", help="which taxes to check, e.g. 'gst' or 'gst,mb' (gst, qst, bc, mb, sk; default all). "
                                    "GST/HST is always checked")
    ap.add_argument("--gst-dates", choices=sorted(DATE_MODES),
                    help="GST/HST registration dates: none; end (default: still registered today, cancellation date); "
                         "full (also the start date; slow)")
    ap.add_argument("--ask", action="store_true", help="ask which taxes to check (used by the .bat launchers)")
    ap.add_argument("--show-result", action="store_true",
                    help="open File Explorer at the results workbook when done (used by the .bat launchers)")
    ap.add_argument("--workers", type=int, default=3,
                    help=f"parallel lookups (default 3, max {MAX_WORKERS}; 1 = one at a time as before)")
    ap.add_argument("--as-of-today", action="store_true",
                    help="ignore the date column and confirm every registration as of today (or --date)")
    ap.add_argument("--limit", type=int, help="only check the first N suppliers (a trial run)")
    ap.add_argument("--resume", help="progress_*.csv from an earlier run that stopped: skip the suppliers it finished")
    # Each save rewrites the whole ~20,000-row workbook (tens of seconds, during which lookups pause), so not too
    # often; the progress file already records every supplier as it finishes and is what --resume uses.
    ap.add_argument("--checkpoint-every", type=int, default=2000,
                    help="save the results workbook every N suppliers during the run (default 2000; 0 = only at the end)")
    ap.add_argument("--progress-every", type=int, default=25,
                    help="write a progress line (done, speed, time left) every N suppliers (default 25)")
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
    try:
        args.taxes = parse_taxes(args.taxes) if isinstance(args.taxes, str) else args.taxes
    except ValueError as e:
        log.error("--taxes: %s", e)
        return 1
    if args.ask and args.taxes is None and not args.check_columns:
        args.taxes = ask_taxes()
    if args.ask and args.gst_dates is None and not args.check_columns and not args.offline:
        args.gst_dates = ask_gst_dates()
    args.gst_dates = args.gst_dates or "end"
    log.info("GST/HST registration dates: %s", {"none": "not looked up", "end": "still registered today / cancellation date",
                                                 "full": "start and cancellation dates"}[args.gst_dates])
    log.info("Checking: %s", ", ".join(TAX_LABELS[k] for k in TAX_LABELS if args.taxes is None or k in args.taxes))
    log.debug("Arguments: %s", vars(args))
    log.debug("Python %s on %s", sys.version.split()[0], platform.platform())
    if args.check_columns:
        return check_columns(args)
    # Corporate networks with TLS inspection re-sign web traffic; use Windows' certificates so lookups aren't refused.
    log.debug("System certificate store in use: %s", use_system_certificates())
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
    finally:
        # A copy of the log in the configured diagnostics folder, for debugging (no-op elsewhere).
        for h in logging.getLogger().handlers:
            h.flush()
        folder = copy_to_diagnostics(log_path)
        if folder:
            log.info("Log copied to %s", folder)
    return 0


if __name__ == "__main__":
    sys.exit(main())
