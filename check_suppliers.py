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
import json
import os
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
from taxcheck.cbr import BusinessRegistries
from taxcheck.orgbook import OrgBookBC
from taxcheck.progress import ProgressReport, load_resume
from taxcheck.qst import QstChecker
from taxcheck.result import NAME_NOT_MATCHED, NOT_CHECKED, REGISTERED, Result
from taxcheck.sk_pst import SkPstAssistant, search_terms
from taxcheck.tailoring import FILE_NAME as TAILORING_FILE
from taxcheck.tailoring import TailoringError
from taxcheck.tailoring import apply as apply_tailoring
from taxcheck.workbook import InputError, SupplierSheet, header_hints, parse_date

log = logging.getLogger("check_suppliers")

MAX_WORKERS = 6  # parallel lookups; each keeps its own pacing, so this caps the load on each government site
HERE = Path(__file__).resolve().parent  # the tool's own folder, for data shared between runs
LAST_FOLDER = HERE / ".last_folder"  # where the file picker opens next time
LAST_RUN = HERE / ".last_run"  # the latest full run's workbook, progress file and menu answers ("Resume Last Run.bat")
# Run Test.bat sets this to its own folder: the test keeps its own "last run" note there (so it can never resume,
# or overwrite the note of, the user's real run) and ignores the tailoring file (the test data is fixed).
TEST_RUN = os.environ.get("TAXCHECK_TEST_RUN")
if TEST_RUN:
    LAST_RUN = Path(TEST_RUN) / ".last_run"
# Menu answers and options a resumed run reuses, so it checks the same things without asking again.
RUN_SETTINGS = ("sheet", "output", "date", "taxes", "sk_assist", "gst_dates", "paid_since", "paid_within", "as_of_today",
                "no_browser", "no_number_search", "no_name_lookup", "trust_gst_number", "nr_list", "workers")


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


def remember_run(args, progress_path, finished=False):
    """Note the latest full run (not a trial or offline run) so --resume last can pick it up after a stop."""
    if args.limit or args.offline or getattr(args, "sk_only", False) is True:
        return  # trials, offline runs and Saskatchewan batches never replace the full run's note
    data = {k: getattr(args, k, None) for k in RUN_SETTINGS}
    data["taxes"] = sorted(args.taxes) if args.taxes else None
    data.update(workbook=str(Path(args.workbook).resolve()), progress=str(Path(progress_path).resolve()),
                finished=finished)
    try:
        LAST_RUN.write_text(json.dumps(data, indent=1), encoding="utf-8")
    except OSError:
        pass  # only a convenience


def resume_last_run(args):
    """--resume last: the latest full run's workbook, progress file and menu answers. Returns an error message, or ""."""
    try:
        data = json.loads(LAST_RUN.read_text(encoding="utf-8"))
        progress = Path(data["progress"])
    except (OSError, ValueError, KeyError, TypeError):
        return ("There's no earlier full run in this folder to resume. Start one in Tax Checker, or resume "
                "a progress file by hand: --resume \"<...>\\logs\\progress_<date>_<time>.csv\"")
    if not progress.exists():
        return f"The last run's progress file is missing ({progress}); resume a progress file by hand with --resume."
    args.resume, args.workbook = str(progress), args.workbook or data.get("workbook")
    for k in RUN_SETTINGS:
        if k in data and not getattr(args, k, None):
            setattr(args, k, data[k])
    if data.get("taxes") is not None:
        args.taxes = set(data["taxes"])
    args.ask = False  # same answers as the run being resumed
    print(f"Resuming the run of {args.workbook}" + (" (it had finished: the results are rewritten, and anything "
                                                       "left for a re-run is tried again)" if data.get("finished") else ""))
    return ""


def saskatchewan_counts(results, total, saved_elsewhere=0):
    """(answered, need a search by hand, still to search) among `total` Saskatchewan suppliers."""
    from taxcheck.result import ERROR, MANUAL

    sk_results = [r["sk_pst"] for r in results.values() if "sk_pst" in r]
    by_hand = sum(1 for r in sk_results if r.status == MANUAL and ("at least 4 letters" in r.detail
                                                                  or "didn't answer this search" in r.detail))
    answered = sum(1 for r in sk_results if r.status not in (MANUAL, ERROR)) + saved_elsewhere
    return answered, by_hand, max(total - answered - by_hand, 0)


SK_PROGRESS = "saskatchewan_progress.json"  # in the cache folder: how far each workbook's Saskatchewan checks are


def record_saskatchewan_progress(args, src, results, total, saved_elsewhere=0):
    """Note, per workbook, how many Saskatchewan suppliers have an answer (the window shows it and suggests the next
    batch). `in_results_waiting` is what the full run's results workbook still lacks (a batch doesn't change it)."""
    if args.limit or args.offline or TEST_RUN or (not total and args.sk_only):
        return
    path = Path(args.cache_dir) / SK_PROGRESS
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        data = {}
    if not total:  # a full run with no Saskatchewan suppliers (e.g. none paid since the cut-off): nothing pending
        data.pop(str(Path(src).resolve()), None)
    else:
        answered, by_hand, waiting = saskatchewan_counts(results, total, saved_elsewhere)
        entry = data.get(str(Path(src).resolve()), {})
        entry.update(total=total, answered=answered, by_hand=by_hand, waiting=waiting,
                     updated=dt.datetime.now().isoformat(timespec="seconds"))
        if not args.sk_only:
            entry["in_results_waiting"] = waiting
        data[str(Path(src).resolve())] = entry
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass  # only for the window's next-step suggestion


@contextlib.contextmanager
def keep_awake():
    """Keep Windows from going to sleep while a run is going (a full run takes hours). No-op elsewhere."""
    done = False
    if platform.system() == "Windows":
        try:
            import ctypes

            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)  # ES_CONTINUOUS | SYSTEM_REQUIRED
            done = True
        except Exception:
            pass
    try:
        yield done
    finally:
        if done:
            with contextlib.suppress(Exception):
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)


def saskatchewan_batch_summary(results, sk, total, stopped_early=False, saved_elsewhere=0):
    """One line for the screen and the summary sheet: how far the Saskatchewan batches have got. `total` is the
    number of Saskatchewan suppliers; a supplier with no result yet (the batch stopped) is still to search, unless
    it's one of `saved_elsewhere` (answered in this or an earlier batch, but not written in this one)."""
    answered, by_hand, waiting = saskatchewan_counts(results, total, saved_elsewhere)
    hand = f" {by_hand} need a search by hand (see their details)." if by_hand else ""
    if not waiting and not stopped_early:
        return (f"SASKATCHEWAN BATCH: all {total} Saskatchewan suppliers are done: {answered} have an answer.{hand} To "
                "add them to the full run's results workbook, click \"Add the Saskatchewan answers\" in Tax Checker (no CAPTCHA).")
    stopped = (sk.unavailable or "") if sk else ""
    m = re.search(r"The next full batch is possible from [^)]*\)", stopped)
    why = ("This batch was stopped before the end." if stopped_early and not stopped
           else f"Today's searches are used up. {m.group(0)}." if m
           else f"The searches stopped: {stopped.split(' Search by hand')[0].rstrip('.')}." if stopped else "")
    return (f"SASKATCHEWAN BATCH: {answered} of {total} Saskatchewan suppliers have an answer; {waiting} still to "
            f"search.{hand} {why} Click \"Saskatchewan batch\" in Tax Checker again for the next batch (answers so "
            "far are kept 60 days and aren't searched again).").replace("  ", " ").replace("..", ".")


def remember_workbook(path):
    """So "Test Setup.bat" can offer the trial on the same workbook without asking again."""
    if TEST_RUN:
        return
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


def ask_saskatchewan():
    """Saskatchewan's registry needs a person to tick a CAPTCHA once, at the start of the run."""
    print()
    print("Saskatchewan PST: the registry has an \"I'm not a robot\" check. If you check Saskatchewan, a browser")
    print("window opens at the start: tick it once and click Next, and every Saskatchewan search then runs by itself.")
    print("Otherwise Saskatchewan suppliers are marked for a manual check.")
    while True:
        try:
            answer = input("Check Saskatchewan now? Type Y or N and press Enter [Y]: ").strip().lower()
        except EOFError:
            return False
        if answer in ("", "y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("  Please type Y or N.")


def ask_taxes():
    """Console menu used by the .bat launchers. Enter (or no console) = everything."""
    print()
    print("Which taxes should be checked?")
    print("  1  GST/HST only (fastest; no browser checks)")
    print("  2  Everything: GST/HST, QST, BC PST, Manitoba RST, Saskatchewan PST")
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


def paid_cutoff(args):
    """The --paid-since / --paid-within cut-off date, or None (check everyone, the default)."""
    if args.paid_since:
        return parse_date(args.paid_since)
    if args.paid_within:
        return cra_today() - dt.timedelta(days=round(args.paid_within * 365.25))
    return None


def ask_paid_since():
    """Optional cut-off by last payment date (never by amount). Enter = check everyone."""
    print()
    print("Only check vendors paid recently? Type a date (e.g. 2022-01-01) to skip vendors last paid before it;")
    print("they're listed as NOT CHECKED. Vendors with no payment date are always checked.")
    while True:
        try:
            answer = input("Cut-off date, or press Enter to check everyone: ").strip()
        except EOFError:
            return None
        if not answer:
            return None
        if parse_date(answer):
            return answer
        print("  That isn't a date I can read; try 2022-01-01.")


def save_final(sheet, results, out, notes, ok_statuses):
    """The final save, with Ctrl+C ignored while it's written (a second Ctrl+C used to lose the workbook)."""
    import signal

    try:
        previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    except ValueError:  # not the main thread (tests): nothing to protect against
        return save_workbook(sheet, results, out, notes, ok_statuses)
    log.info("Saving the results workbook (Ctrl+C is ignored until it's saved)...")
    try:
        return save_workbook(sheet, results, out, notes, ok_statuses)
    finally:
        signal.signal(signal.SIGINT, previous)


def prefetch_saskatchewan(checker, sk, rows, stop=None):
    """Run every Saskatchewan search right after the person passes the registry's CAPTCHA, at the start.

    The CAPTCHA is needed once per browser session (see sk_pst.SkPstAssistant), but in a long run the
    Saskatchewan rows can be hours apart: the session could expire and nobody would be there to tick it again.
    Done up front, the person ticks it once while they're starting the run and can then leave. The results go
    into the row checker's cache, so the main loop reuses them.
    """
    jobs = checker.saskatchewan_searches(rows)
    if not jobs:
        log.info("Saskatchewan: no Saskatchewan suppliers in this file, so the CAPTCHA window won't open.")
        return
    left = sk.searches_left()
    answered = getattr(sk, "answered", lambda names: False)
    fresh = sum(1 for names, _ in jobs if not answered(names))
    if len(jobs) > fresh:
        log.info("Saskatchewan: %d suppliers answered from this tool's earlier searches (no CAPTCHA, no daily searches "
                 "used for them).", len(jobs) - fresh)
    if fresh > left:
        log.warning("Saskatchewan: %d suppliers still to search, and %d of the registry's %d searches per 24 hours are "
                    "left. The rest wait for the next batch: click \"Saskatchewan batch\" in Tax Checker on the "
                    "following days.", fresh, left, sk.daily_limit)
    if fresh and not left:
        log.warning("Saskatchewan: today's searches are used up, so the window won't open. %s",
                    sk._limit_message().split(". ", 1)[1].split(" Double-click")[0])
    elif fresh:
        log.warning(">>> Saskatchewan: up to %d searches now. A browser window opens: tick 'I agree' and 'I'm not a "
                    "robot' once and click Next. The searches then run by themselves.", min(fresh, left))
    started, stopped = time.monotonic(), False
    for i, (names, had_number) in enumerate(jobs, 1):
        if stop and stop():
            raise KeyboardInterrupt  # the window's Stop button
        # Every supplier is gone through even after the searches stop: saved answers still count.
        res = checker._guard(checker._sk_noted, names, had_number)
        log.info("[Saskatchewan %d/%d] %s: %s", i, len(jobs), next((n for n in names if n), ""), res.status)
        if sk.unavailable and not stopped:
            stopped = True
            log.warning("Saskatchewan: no more searches in this run (%s); suppliers without a saved answer are "
                        "marked for a manual check.", sk.unavailable)
    log.info("Saskatchewan searches done in %.0fs (CAPTCHA asked %d time(s)); continuing with the other checks.",
             time.monotonic() - started, sk.captchas)


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
    suffix = "_saskatchewan" if args.sk_only else "_tax_check_TRIAL" if args.limit else "_tax_check"
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
    if args.sk_only:
        # Saskatchewan batch: only the suppliers with a Saskatchewan search; everything else is the full run's job.
        results, scout = {}, RowChecker(None, None, taxes={"sk_pst"}, available=sheet.cols)
        to_check = [(r, v) for r, v in rows if scout.saskatchewan_searches([v])]
        log.info("Saskatchewan batch: %d Saskatchewan suppliers in %s", len(to_check), src.name)
        if not to_check:
            log.warning("No Saskatchewan suppliers in this workbook: nothing to do.")
            return None
    cutoff, skipped = paid_cutoff(args), 0
    if cutoff:
        if "date" not in sheet.cols:
            log.warning("--paid-since/--paid-within: the workbook has no date column, so every vendor is checked")
        kept = []
        for r, v in to_check:
            paid = None if args.as_of_today else parse_date(v.get("date"))
            if paid and paid < cutoff:
                results[r] = {"gst": Result(NOT_CHECKED, f"Not checked: last paid {paid}, before the cut-off date "
                                                         f"{cutoff} chosen for this run.")}
                skipped += 1
            else:
                kept.append((r, v))  # paid since the cut-off, or no readable date: checked
        to_check = kept
        log.info("Paid-since cut-off %s: %d vendors last paid before it are listed as NOT CHECKED; %d will be checked",
                 cutoff, skipped, len(to_check))
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
        if found < args.limit:
            log.info("--limit %d: only %d suppliers need an online lookup, so all %d are checked", args.limit, found,
                     len(to_check))
        else:
            log.info("--limit %d: checking the first %d suppliers, which include %d that need an online lookup",
                     args.limit, len(to_check), found)

    offline = args.offline
    if offline:
        log.info("--offline: no government site is contacted; numbers are only checked for format and check digit")
        corporations = orgbook = registries = None
        gst = Offline()
    else:
        corporations = None if args.no_name_lookup else FederalCorporations(cache_dir=args.cache_dir)
        orgbook = None if args.no_name_lookup else OrgBookBC()
        # Provincial companies (and full names behind cut-off vendor names), after the federal data.
        registries = None if args.no_name_lookup else BusinessRegistries()
        gst = GstChecker(corporations=corporations, name_sources=[s for s in (registries, orgbook) if s],
                         cache_dir=args.cache_dir)

    stop_file = Path(args.stop_file) if getattr(args, "stop_file", None) else None
    stop_requested = (lambda: stop_file.exists()) if stop_file else (lambda: False)
    progress = ProgressReport(log_path.parent, len(to_check), every_rows=args.progress_every)
    remember_run(args, progress.path)
    for r, values in rows:  # carry resumed rows into the new progress file, so the latest file is complete
        if r in results:
            progress.record(r, values, results[r], 0.0, source="resumed")

    started = time.monotonic()
    checkpoints = CheckpointSaver(sheet, out)
    failure = None
    with contextlib.ExitStack() as stack:
        sk = None  # the Saskatchewan assistant, only with --sk-assist
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
                sk = SkPstAssistant(stack.enter_context(Browser(headless=False, debug_dir=log_path.parent)),
                                    counter_file=Path(args.cache_dir) / "saskatchewan_searches.json",
                                    answers_file=Path(args.cache_dir) / "saskatchewan_answers.json")
            checker = RowChecker(
                gst,
                qst,
                bc=browser and BcPstChecker(browser),
                mb=browser and MbRstChecker(browser),
                sk=sk,
                available=sheet.cols, taxes=args.taxes, gst_dates=args.gst_dates,
            )
            checker.number_search = not getattr(args, "no_number_search", False)
        check_row = checker.check_saskatchewan if args.sk_only else checker.check

        def row_date(r, values):
            date = None if args.as_of_today else parse_date(values.get("date"))
            if values.get("date") and not date and not args.as_of_today:
                log.warning("Row %d: couldn't read date %r; using %s", r, values.get("date"), default_date)
            date = date or default_date
            if date > cra_today():
                log.debug("Row %d: date %s is in the future; CRA is asked about today instead", r, date)
                date = cra_today()
            return date

        executor, worker_orgbooks = None, []
        workers = 1 if offline or args.sk_only else max(1, min(args.workers, MAX_WORKERS))
        if workers > 1:
            # Each worker has its own CRA/Revenu Québec/OrgBook session and its own throttle (so each still waits
            # between its requests); they share the duplicate-lookup cache, the federal names and the simplified list.
            # Browser checks (Manitoba/BC/SK) can't leave the main thread, so they run there as rows come back.
            if hasattr(gst.simplified, "_load") and gst.simplified._rows is None:
                gst.simplified._load()  # once, on the main thread, where the browser fallback works
            # Québec's NR list may need the browser (some networks block the download), which only works on the
            # main thread: load it here once if any supplier has an NR number, and give the workers that copy.
            if any(re.search(r"\bNR\s*\d", f"{v.get('qst') or ''} {v.get('gst') or ''}", re.I) for _, v in to_check):
                qst._load_nr_list()
            pool, worker_orgbooks = [], []
            for _ in range(workers):
                worker_orgbook = worker_registries = None
                if orgbook:
                    worker_orgbook = OrgBookBC()
                    worker_orgbook.health = orgbook.health
                    worker_orgbooks.append(worker_orgbook)
                if registries:
                    worker_registries = BusinessRegistries(cache=registries._cache)
                    worker_registries.health = registries.health
                    worker_orgbooks.append(worker_registries)
                g = GstChecker(corporations=corporations, name_sources=[s for s in (worker_registries, worker_orgbook) if s],
                               cache_dir=args.cache_dir)
                g.simplified = gst.simplified
                if hasattr(gst, "answers"):
                    g.answers = gst.answers  # one memory of CRA's answers for all workers
                    g.health = gst.health  # an outage is noticed once, and all workers pause together
                worker_qst = QstChecker(nr_list_file=args.nr_list, cache_dir=args.cache_dir)
                worker_qst._nr_rows = getattr(qst, "_nr_rows", None)  # None: loaded if needed
                worker_qst.nr_error = getattr(qst, "nr_error", None)
                pool.append(RowChecker(g, worker_qst,
                                       available=sheet.cols, taxes=args.taxes, cache=checker._cache, defer_browser=True,
                                       gst_dates=args.gst_dates))
            free = queue.SimpleQueue()
            for c in pool:
                c.number_search = checker.number_search
                free.put(c)

            def work(r, values, date):
                c = free.get()
                try:
                    t0 = time.monotonic()
                    log.debug("---- Row %d: %s", r, values.get("name"))
                    return c.check(values, date), time.monotonic() - t0
                finally:
                    free.put(c)

            # Not in the ExitStack: leaving it would wait for lookups still running, and a stopped run should save
            # its results first. Shut down explicitly below; Ctrl+C cancels what's queued and doesn't wait.
            executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="lookup")
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
                    while True:  # short waits so Ctrl+C (and the window's Stop button) is noticed on Windows
                        if stop_requested():
                            raise KeyboardInterrupt
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
                    out_row = check_row(values, row_date(r, values))
                    yield r, values, out_row, time.monotonic() - t0

        stopped_early = None  # set below; None if something stops the run before it's decided
        try:
            if sk:
                prefetch_saskatchewan(checker, sk, [values for _, values in to_check], stop=stop_requested)
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
                if args.stop_after and i >= args.stop_after and i < len(to_check):
                    raise KeyboardInterrupt  # --stop-after: exactly what Ctrl+C does (Run Test.bat tests resuming)
                if stop_requested():
                    raise KeyboardInterrupt  # --stop-file: the window's Stop button
        except KeyboardInterrupt:
            # A long run stopped with Ctrl+C keeps what it has; --resume with the progress file finishes the rest.
            log.warning("Stopped early after %d of %d suppliers; saving the results so far.%s",
                        sum(1 for x in results.values() if "sk_pst" in x) if args.sk_only else len(results),
                        len(to_check) if args.sk_only else len(rows),
                        "" if args.limit or offline else " To continue later, click \"%s\" in Tax Checker."
                        % ("Saskatchewan batch" if args.sk_only else "Continue the last check"))
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
            if executor:
                executor.shutdown(wait=stopped_early is False, cancel_futures=True)
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
        "SK PST: SETS PST On-Line Registry, searched in a browser window after a person ticks its CAPTCHA once "
        "(--sk-assist, or Y in the menu); otherwise a manual check.",
        "GST/HST names that don't match CRA are retried with official names: Revenu Québec's (when a QST number "
        "is given), Corporations Canada's open data, Canada's Business Registries (provincial companies) and OrgBook BC.",
        "GST/HST numbers whose name doesn't match are still checked by number: 'REGISTERED - NAME NOT MATCHED' means "
        "the number is registered but not under the sheet's name (CRA's name is shown when known).",
    ]
    notes.append(f"Progress file (one line per supplier, used by --resume): {progress.path}")
    notes.insert(0, "GST/HST registration dates: " + {"none": "not looked up.", "end": "checked whether each supplier "
                    "is still registered today, with the cancellation date if not.", "full": "start and cancellation "
                    "dates looked up."}[args.gst_dates or "end"])
    if getattr(args, "tailoring", ""):
        notes.insert(0, args.tailoring + ".")
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
        notes.insert(0, f"RUN FAILED: {type(failure).__name__}: {failure}. See the log; click \"Continue the last check\" in Tax Checker to continue.")
    if gst.__class__ is Offline:
        pass
    elif gst.simplified.error:
        notes.append(gst.simplified.error)
    for source in (corporations, orgbook, registries, *worker_orgbooks):
        if source and source.error and source.error not in notes:
            notes.append(source.error)
    if qst and qst.nr_error:
        notes.append(qst.nr_error)
    if args.sk_only:
        # Stopped during the searches: suppliers answered so far aren't in `results` yet, but are saved.
        unwritten = [v for r, v in to_check if "sk_pst" not in results.get(r, {})]
        saved = sum(1 for v in unwritten for names, _ in checker.saskatchewan_searches([v])
                    if sk and search_terms(*names) and not sk.gave_up(names) and sk.answered(names)) if stopped_early else 0
        notes.insert(0, saskatchewan_batch_summary(results, sk, len(to_check), bool(stopped_early), saved))
        record_saskatchewan_progress(args, src, results, len(to_check), saved)
        log.warning("%s", "=" * 70)
        log.warning("%s", notes[0])
        log.warning("%s", "=" * 70)
    if not args.sk_only and not stopped_early:  # (a stopped run's counts are incomplete)
        record_saskatchewan_progress(args, src, results, sum(1 for x in results.values() if "sk_pst" in x))
    if stopped_early:
        notes.insert(0, f"STOPPED EARLY: only {len(results)} of {len(to_check) if args.sk_only else len(rows)} suppliers "
                        "were checked. Rows without results weren't checked: click "
                        f"\"{'Saskatchewan batch' if args.sk_only else 'Continue the last check'}\" in Tax Checker to check them.")
    ok_statuses = (REGISTERED, NAME_NOT_MATCHED) if args.trust_gst_number else (REGISTERED,)
    if args.trust_gst_number:
        notes.insert(0, "NUMBER-ONLY RESULTS (--accept-number-only): 'REGISTERED - NAME NOT MATCHED' counts as OK in the "
                        "Overall column. Those numbers are registered, but CRA did NOT confirm them under the supplier's "
                        "name; they may belong to another business. Not a confirmed result.")
    if cutoff:
        notes.insert(0, f"PAID-SINCE CUT-OFF: vendors last paid before {cutoff} were not checked ({skipped} suppliers, "
                        "Overall 'NOT CHECKED'). Vendors with a blank or unreadable date were checked.")
    saved = save_final(sheet, results, out, notes, ok_statuses)
    if failure is None and not stopped_early:
        remember_run(args, progress.path, finished=True)
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
    ap.add_argument("--no-number-search", action="store_true",
                    help="don't search Canada's Business Registries by name for a possible GST/HST number when a "
                         "Canadian supplier has none (saves a search per such supplier)")
    ap.add_argument("--no-name-lookup", action="store_true",
                    help="don't look up official names (federal registry ~110 MB download, Canada's Business Registries, "
                         "OrgBook BC) for GST/HST name mismatches")
    ap.add_argument("--cache-dir", default=str(HERE / "cache"),
                    help="folder for downloaded reference data (default: cache in the tool's folder)")
    ap.add_argument("--nr-list", help="saved copy of Revenu Québec's NR registrant list page, if it can't be downloaded")
    ap.add_argument("--log-dir", help="folder for run logs and failure screenshots (default: logs next to the workbook)")
    ap.add_argument("-v", "--verbose", action="store_true", help="show the detailed log on screen too")
    ap.add_argument("--accept-number-only", "--trust-gst-number", dest="trust_gst_number", action="store_true",
                    help="count 'REGISTERED - NAME NOT MATCHED' (number registered, vendor name not confirmed) as OK; "
                         "for a quick number-only pass. Default: REVIEW, because the number may be another business's")
    ap.add_argument("--paid-since", help="only check vendors last paid on or after this date (e.g. 2022-01-01); others "
                                         "are listed as NOT CHECKED. Vendors with no readable date are always checked")
    ap.add_argument("--paid-within", type=float, help="only check vendors last paid within this many years (e.g. 4)")
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
    ap.add_argument("--sk-only", action="store_true",
                    help="Saskatchewan batch: check only Saskatchewan PST, for the Saskatchewan suppliers not answered "
                         "yet, up to the registry's daily limit (Run Saskatchewan Batch.bat); results in "
                         "<workbook>_saskatchewan.xlsx")
    ap.add_argument("--sk-later", action="store_true",
                    help="with --resume last: leave Saskatchewan for the daily batches without asking (the window)")
    ap.add_argument("--stop-file", help="stop (as if Ctrl+C was pressed) when this file appears (the window's Stop "
                                        "button)")
    ap.add_argument("--stop-after", type=int, help="stop after N suppliers as if Ctrl+C was pressed (Run Test.bat "
                                                   "uses it to test resuming)")
    ap.add_argument("--resume", help="progress_*.csv from an earlier run that stopped: skip the suppliers it finished; "
                                     "'last' = the latest full run, with the same workbook and answers (Resume Last Run.bat)")
    # Each save rewrites the whole ~20,000-row workbook (tens of seconds, during which lookups pause), so not too
    # often; the progress file already records every supplier as it finishes and is what --resume uses.
    ap.add_argument("--checkpoint-every", type=int, default=2000,
                    help="save the results workbook every N suppliers during the run (default 2000; 0 = only at the end)")
    ap.add_argument("--progress-every", type=int, default=25,
                    help="write a progress line (done, speed, time left) every N suppliers (default 25)")
    args = ap.parse_args(argv)
    if args.sk_only:
        args.taxes, args.sk_assist, args.no_name_lookup, args.workers, args.gst_dates = {"sk_pst"}, True, True, 1, "none"
        args.ask, args.resume, args.limit = False, None, None
        if not args.workbook:  # the full run's workbook and choices, so the batch needs no questions
            with contextlib.suppress(OSError, ValueError, KeyError, TypeError):
                last = json.loads(LAST_RUN.read_text(encoding="utf-8"))
                if Path(last["workbook"]).exists():
                    args.workbook = last["workbook"]
                    for k in ("sheet", "date", "paid_since", "paid_within", "as_of_today"):  # the same suppliers
                        if last.get(k) and not getattr(args, k, None):
                            setattr(args, k, last[k])
                    print(f"Saskatchewan batch for {args.workbook} (the workbook of the last full run)")
                else:
                    print(f"The last full run's workbook isn't where it was ({last['workbook']}): choose it in the "
                          "window that opens.")
    if str(args.resume or "").lower() == "last":
        problem = resume_last_run(args)
        if problem:
            print(problem)
            return 1
        if not args.sk_assist and not args.sk_later and (args.taxes is None or "sk_pst" in args.taxes):
            args.sk_assist = ask_saskatchewan()  # skipped last time: the one question worth asking again
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
        args.tailoring = "" if TEST_RUN else apply_tailoring(HERE / TAILORING_FILE)
    except TailoringError as e:
        log.error("%s", e)
        return 1
    try:
        args.taxes = parse_taxes(args.taxes) if isinstance(args.taxes, str) else args.taxes
    except ValueError as e:
        log.error("--taxes: %s", e)
        return 1
    if args.ask and args.taxes is None and not args.check_columns:
        args.taxes = ask_taxes()
    if (args.ask and not args.sk_assist and not args.check_columns and not args.offline
            and (args.taxes is None or "sk_pst" in args.taxes)):
        args.sk_assist = ask_saskatchewan()
    if args.ask and not args.paid_since and not args.paid_within and not args.check_columns:
        args.paid_since = ask_paid_since()
    if args.paid_since and not parse_date(args.paid_since):
        log.error("--paid-since must be a date like 2022-01-01")
        return 1
    if args.ask and args.gst_dates is None and not args.check_columns and not args.offline:
        args.gst_dates = ask_gst_dates()
    args.gst_dates = args.gst_dates or "end"
    log.info("GST/HST registration dates: %s", {"none": "not looked up", "end": "still registered today / cancellation date",
                                                 "full": "start and cancellation dates"}[args.gst_dates])
    log.info("Checking: %s", ", ".join(TAX_LABELS[k] for k in TAX_LABELS if args.taxes is None or k in args.taxes))
    if args.trust_gst_number:
        log.warning("--accept-number-only: registered numbers whose name CRA didn't confirm count as OK (number-only pass)")
    log.debug("Arguments: %s", vars(args))
    log.debug("Python %s on %s", sys.version.split()[0], platform.platform())
    if args.check_columns:
        return check_columns(args)
    # Corporate networks with TLS inspection re-sign web traffic; use Windows' certificates so lookups aren't refused.
    log.debug("System certificate store in use: %s", use_system_certificates())
    try:
        with keep_awake():
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
