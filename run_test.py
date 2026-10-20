"""One-click test of the whole tool on public test data (what "Run Test.bat" runs).

No vendor data is used: it copies examples/Test_Suppliers.xlsx (public businesses and made-up suppliers) to a
new folder under test_run/, then runs, as the user would:

  1. the setup check (check_setup.py) on that file,
  2. the workbook profile (profile_workbook.py),
  3. a full run of every tax (Saskatchewan included: the person ticks its CAPTCHA once), stopped part way as
     if Ctrl+C was pressed (--stop-after),
  4. the resume, the way "Resume Last Run.bat" does it (--resume last),

and puts every log, report and results workbook in one zip next to this file, to send to whoever maintains
the tool. The user's own "last run" note is put back afterwards, so Resume Last Run.bat still means their run.
"""

import datetime as dt
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEST_FILE = HERE / "examples" / "Test_Suppliers.xlsx"
STOP_AFTER = 8  # suppliers checked before the simulated Ctrl+C


def step(title):
    print()
    print("=" * 78)
    print(title)
    print("=" * 78, flush=True)


def run(args, record):
    """Run one step with this Python, showing its output; note how it ended."""
    started = dt.datetime.now()
    code = subprocess.call([sys.executable, *args], cwd=HERE, stdin=subprocess.DEVNULL)
    record.append(f"{started:%H:%M:%S} exit {code} ({(dt.datetime.now() - started).seconds} s): {' '.join(args)}")
    return code


def every_row_checked(workbook):
    """After the resume, every supplier in the test file should have a result."""
    import openpyxl

    results = workbook.with_name(f"{workbook.stem}_tax_check.xlsx")
    try:
        ws = openpyxl.load_workbook(results, read_only=True).worksheets[0]
        rows = list(ws.iter_rows(values_only=True))
        overall = rows[0].index("Overall")
    except Exception as e:
        return f"RESULT: couldn't read {results.name} ({type(e).__name__}: {e})"
    suppliers = [r for r in rows[1:] if r and r[0]]
    missing = [i for i, r in enumerate(rows[1:], 2) if r and r[0] and not r[overall]]
    errors = sum(1 for r in suppliers if any(v == "ERROR" for v in r))
    if missing:
        return f"RESULT: {len(missing)} of {len(suppliers)} suppliers have no result (rows {missing[:10]})"
    return f"RESULT: all {len(suppliers)} suppliers checked; {errors} with an ERROR status"


def main():
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    folder = HERE / "test_run" / stamp
    folder.mkdir(parents=True)
    workbook = folder / TEST_FILE.name
    shutil.copy2(TEST_FILE, workbook)
    kept = {p: p.read_bytes() for p in (HERE / ".last_run", HERE / ".last_workbook") if p.exists()}
    record = [f"Tax-Checker test run {stamp}", f"Test workbook: {workbook}"]

    print("Tax-Checker test run: about 10 minutes, public test data only (no vendor data).")
    print("You only need to do one thing: when the Saskatchewan window opens (step 3), tick 'I agree' and")
    print("'I'm not a robot', then click Next. Everything else runs by itself.")
    try:
        step("Step 1 of 4: setup check")
        run(["check_setup.py", str(workbook)], record)
        step("Step 2 of 4: workbook profile")
        run(["profile_workbook.py", str(workbook)], record)
        step(f"Step 3 of 4: full run, stopped on purpose after {STOP_AFTER} suppliers (as if Ctrl+C was pressed), "
             "to test resuming")
        print(">>> Watch for the Saskatchewan window: tick the boxes and click Next. <<<", flush=True)
        run(["check_suppliers.py", str(workbook), "--sk-assist", "--gst-dates", "end", "--stop-after", str(STOP_AFTER)],
            record)
        step("Step 4 of 4: resuming the stopped run (as Resume Last Run.bat does): checks every remaining supplier")
        run(["check_suppliers.py", "--resume", "last"], record)
    finally:
        for path in (HERE / ".last_run", HERE / ".last_workbook"):  # the user's own notes, as they were
            if path in kept:
                path.write_bytes(kept[path])
            elif path.exists():
                path.unlink()

    record.append(every_row_checked(workbook))
    (folder / "test_run_steps.txt").write_text("\n".join(record) + "\n", encoding="utf-8")
    out = HERE / f"TaxCheck_Test_Results_{stamp}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                z.write(path, path.relative_to(folder))
    step("Done")
    print("\n".join(record[2:]))
    print()
    print(f"Send this file: {out}")
    if sys.platform == "win32":
        subprocess.Popen(["explorer", "/select,", str(out)])
    return 0


if __name__ == "__main__":
    sys.exit(main())
