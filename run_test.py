"""One-click test of the whole tool on public test data (what "Run Test.bat" runs).

No vendor data is used: it copies examples/Test_Suppliers.xlsx (public businesses and made-up suppliers) to a
new folder under test_run/, then runs, as the user would:

  1. the setup check (check_setup.py) on that file,
  2. the workbook profile (profile_workbook.py),
  3. a full run of every tax (Saskatchewan included: the person ticks its CAPTCHA once), stopped on purpose part
     way, exactly as Ctrl+C would (--stop-after),
  4. the resume, the way "Resume Last Run.bat" does it (--resume last),

then checks every supplier has a result, and puts every log, report and results workbook in one zip next to
this file, to send to whoever maintains the tool.

TAXCHECK_TEST_RUN (set for every step) keeps the test apart from the user's real work: the test's "last run"
note lives in its own folder, so step 4 can only resume the test, and the user's tailoring file isn't applied.
"""

import datetime as dt
import os
import shutil
import subprocess
import sys
import zipfile
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEST_FILE = HERE / "examples" / "Test_Suppliers.xlsx"
STOP_AFTER = 8  # suppliers checked before the deliberate stop


def step(title):
    print()
    print("=" * 78)
    print(title)
    print("=" * 78, flush=True)


def run(title, args, env, record):
    """Run one step with this Python, showing its output; note how it ended."""
    started = dt.datetime.now()
    code = subprocess.call([sys.executable, *args], cwd=HERE, env=env, stdin=subprocess.DEVNULL)
    took = (dt.datetime.now() - started).seconds
    record.append(f"{title}: {'OK' if code == 0 else f'PROBLEM (exit code {code}; see its log)'} ({took} s)")
    return code


def every_row_checked(folder):
    """After the resume, every supplier in the test file should have a result. Reads the newest results
    workbook (if the first was locked, e.g. by OneDrive, the tool saved under a new name)."""
    import openpyxl

    found = sorted(folder.glob("*_tax_check*.xlsx"), key=lambda p: p.stat().st_mtime)
    if not found:
        return ["RESULT: no results workbook was written (see the logs)"]
    try:
        ws = openpyxl.load_workbook(found[-1], read_only=True).worksheets[0]
        rows = list(ws.iter_rows(values_only=True))
        head = list(rows[0])
        overall = head.index("Overall")
        sk = head.index("SK PST Status") if "SK PST Status" in head else None
    except Exception as e:
        return [f"RESULT: couldn't read {found[-1].name} ({type(e).__name__}: {e})"]
    suppliers = [r for r in rows[1:] if r and r[0]]
    missing = [str(r[0]) for r in suppliers if not r[overall]]
    errors = sum(1 for r in suppliers if "ERROR" in r)
    lines = [f"RESULT: {len(suppliers) - len(missing)} of {len(suppliers)} suppliers checked; {errors} with an ERROR "
             "status" + (f"; no result for: {', '.join(missing[:5])}" if missing else "")]
    if sk is not None:
        statuses = Counter(r[sk] for r in suppliers if r[sk])
        lines.append("Saskatchewan: " + (", ".join(f"{n} {s}" for s, n in statuses.items()) or "no rows")
                     + (" (MANUAL CHECK = not searched, e.g. the CAPTCHA wasn't ticked)" if "MANUAL CHECK" in statuses else ""))
    return lines


def main():
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    folder = HERE / "test_run" / stamp
    folder.mkdir(parents=True)
    workbook = folder / TEST_FILE.name
    shutil.copy2(TEST_FILE, workbook)
    env = dict(os.environ, TAXCHECK_TEST_RUN=str(folder))
    record = []

    print("Tax-Checker test run: about 10 minutes, public test data only (no vendor data).")
    print("Your only job: when the Saskatchewan window opens (in step 3), tick 'I agree' and 'I'm not a robot',")
    print("then click Next. You have 5 minutes. Everything else runs by itself; leave this window open.")
    try:
        step("Step 1 of 4: setup check (government sites, browser, a few real lookups)")
        run("Step 1 setup check", ["check_setup.py", str(workbook)], env, record)
        step("Step 2 of 4: workbook profile")
        run("Step 2 profile", ["profile_workbook.py", str(workbook)], env, record)
        step(f"Step 3 of 4: full run of every tax, stopped ON PURPOSE after {STOP_AFTER} suppliers to test resuming")
        print(">>> Watch for the Saskatchewan window: tick the boxes and click Next. <<<", flush=True)
        run("Step 3 full run (stopped on purpose)", ["check_suppliers.py", str(workbook), "--sk-assist", "--gst-dates",
                                                     "end", "--stop-after", str(STOP_AFTER)], env, record)
        print()
        print(f"(That stop after {STOP_AFTER} suppliers was on purpose. Nothing to do: step 4 continues the run.)")
        step("Step 4 of 4: resuming the stopped run, as Resume Last Run.bat does: checks every remaining supplier")
        if (folder / ".last_run").exists():
            run("Step 4 resume", ["check_suppliers.py", "--resume", "last"], env, record)
        else:
            record.append("Step 4 resume: SKIPPED (step 3 didn't get far enough to be resumed; see its log)")
            print(record[-1])
    except KeyboardInterrupt:
        record.append("STOPPED with Ctrl+C before the end; the zip has what was done.")
        print()
        print(record[-1])
    record += every_row_checked(folder)
    (folder / "test_run_steps.txt").write_text(f"Tax-Checker test run {stamp}\n" + "\n".join(record) + "\n",
                                               encoding="utf-8")
    (folder / ".last_run").unlink(missing_ok=True)
    out = HERE / f"TaxCheck_Test_Results_{stamp}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                z.write(path, path.relative_to(folder))
    step("Done")
    print("\n".join(record))
    print()
    print(f"Send this file: {out}")
    print("It holds only the test data, plus this PC's name, Windows user name (in folder paths) and versions.")
    if sys.platform == "win32":
        subprocess.Popen(["explorer", "/select,", str(out)])
    return 0


if __name__ == "__main__":
    sys.exit(main())
