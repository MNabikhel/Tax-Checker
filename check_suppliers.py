"""Check suppliers' Canadian sales-tax registrations from an Excel workbook.

    python check_suppliers.py suppliers.xlsx
    python check_suppliers.py suppliers.xlsx -o results.xlsx --date 2026-09-30

See README.md for the expected columns and what each check does.
"""

import argparse
import contextlib
import datetime as dt
import sys
from pathlib import Path

from taxcheck.checks import RowChecker
from taxcheck.gst import GstChecker
from taxcheck.qst import QstChecker
from taxcheck.workbook import SupplierSheet, normalize_province, parse_date


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("workbook", help="supplier list (.xlsx)")
    ap.add_argument("-o", "--output", help="where to write results (default: <workbook>_tax_check.xlsx)")
    ap.add_argument("--sheet", help="sheet name (default: first sheet)")
    ap.add_argument("--date", help="date to confirm registration on, YYYY-MM-DD (default: each row's date column, else today)")
    ap.add_argument("--no-browser", action="store_true", help="skip BC PST lookups, which need a headless browser")
    ap.add_argument("--nr-list", help="saved copy of Revenu Québec's NR registrant list page, if it can't be downloaded")
    args = ap.parse_args(argv)

    src = Path(args.workbook)
    out = Path(args.output) if args.output else src.with_name(f"{src.stem}_tax_check.xlsx")
    default_date = parse_date(args.date) if args.date else dt.date.today()
    if args.date and not default_date:
        ap.error("--date must look like 2026-09-30")

    sheet = SupplierSheet(src, args.sheet)
    rows = list(sheet.rows())
    print(f"{len(rows)} suppliers found in '{sheet.ws.title}' (columns: {', '.join(sheet.cols)})")

    gst, qst = GstChecker(), QstChecker(nr_list_file=args.nr_list)
    needs_browser = not args.no_browser and any(
        v.get("bc_pst") or (v.get("pst") and normalize_province(v.get("province")) == "BC")
        for _, v in rows
    )

    results = {}
    with contextlib.ExitStack() as stack:
        bc = None
        if needs_browser:
            from taxcheck.bc_pst import BcPstChecker

            bc = stack.enter_context(BcPstChecker())
        checker = RowChecker(gst, qst, bc)
        for i, (r, values) in enumerate(rows, 1):
            date = parse_date(values.get("date")) or default_date
            results[r] = checker.check(values, date)
            summary = ", ".join(f"{k}={v.status}" for k, v in results[r].items())
            print(f"[{i}/{len(rows)}] {values.get('name')}: {summary}")

    notes = [
        f"Run on {dt.date.today()} from {src.name}.",
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
    sheet.write_results(results, out, notes)
    print(f"\nResults written to {out}")


if __name__ == "__main__":
    sys.exit(main())
