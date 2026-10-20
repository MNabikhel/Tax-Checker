"""Workbook profile: the shape of a supplier file, without its contents, for tailoring the tool to it.

    Double-click "Profile Workbook.bat"   (or: python profile_workbook.py [workbook] [--sheet NAME])

Prints (and saves in logs/profile_<date>.txt next to the workbook) counts and patterns only: which columns
the tool found, how Tax IDs, countries, provinces, postal codes and dates are written, which vendor-master
markings appear in names ("(DNU)", "O/A", "*", brackets, cut-off names), and what the tool would do with each
row offline. No supplier name or number is printed: digits show as 9 and letters as A ("999999999AA9999").
Country and province values are printed as written (codes like "CA" or "Ont."); a value with digits or more
than 3 words or 20 characters is shown as its shape. Unless --offline, CRA's public list of simplified
(digital economy) registrants is downloaded once to count vendor names on it (no registry lookups).

The report is meant to be shared with whoever tailors the tool (see TAILORING.md).
"""

import argparse
import datetime as dt
import re
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent


def shape(value, limit=24):
    """'123456789 RT0001' -> '999999999 AA9999'; long values are cut."""
    s = re.sub(r"[A-Za-z]", "A", re.sub(r"\d", "9", str(value).strip()))
    return s if len(s) <= limit else s[:limit] + "..."


def safe_value(value):
    """A country/province value as written, unless it looks like free text (digits, or long): then its shape."""
    from taxcheck.workbook import normalize_country, normalize_province

    v = str(value).strip()
    known = normalize_province(v) or normalize_country(v) == "CA"
    return v if known or (len(v) <= 20 and not re.search(r"\d", v) and len(v.split()) <= 3) else shape(v, 20)


def top(counter, n=25):
    return [(k, v) for k, v in counter.most_common(n)]


# Vendor-master markings, counted (never printed).
NAME_PATTERNS = [
    ("do-not-use marker (DNU, DO NOT USE)", re.compile(r"\bdnu\b|do\s+not\s+use", re.I)),
    ("operating-as (O/A, DBA, C.O.B.)", re.compile(r"\b(?:o/a|o\.a\.|d/?b/?a|c\.o\.b\.?|operating\s+as)\b", re.I)),
    ("brackets ( ... )", re.compile(r"[()\[\]]")),
    ("asterisk *", re.compile(r"\*")),
    ("REMIT / ACCT / account number", re.compile(r"\bremit\b|\bacct\b|#\s*\d{4,}", re.I)),
    ("numbered company (1234567 ONTARIO INC)", re.compile(r"^\s*\d{6,10}\s+(?:ontario|alberta|b\.?\s?c\.?|canada|"
                                                          r"manitoba|saskatchewan|nova scotia|quebec|qu[eé]bec)", re.I)),
    ("Québec numbered (9999-9999 QUEBEC INC)", re.compile(r"^\s*\d{4}-\d{4}\s+qu[eé]bec", re.I)),
    ("ends in Inc/Ltd/Corp/LLC...", re.compile(r"\b(?:inc|ltd|limited|corp|corporation|llc|ulc|lp|llp|ltee)\.?\s*$", re.I)),
    ("foreign legal form (Pty, GmbH, AB, BV, PLC...)", re.compile(r"\b(?:pty|pte|gmbh|ab|bv|b\.v\.|nv|plc|sarl|srl|"
                                                                 r"s\.a\.|ag|oy)\b\.?", re.I)),
    ("trailing spaces + number", re.compile(r"\s{2,}\d+\s*$")),
    ("all upper case", re.compile(r"^[^a-z]*[A-Z][^a-z]*$")),
]


def simplified_counts(rows, located):
    """How many vendors' names match CRA's list of simplified (digital economy) GST/HST registrants.

    Downloads CRA's public list once (cached a week in the tool's cache folder); no CRA registry lookups, so
    these are name matches only (a real run then has CRA confirm each one)."""
    from taxcheck.checks import _card_style_names, _simplified_by_name, clean_vendor_name, vendor_name_candidates
    from taxcheck.gst import SimplifiedList
    from taxcheck.http import new_session

    sl = SimplifiedList(new_session(), cache_dir=HERE / "cache")
    sl._load()
    if not sl._rows:
        return f"ONLINE SELLERS: CRA's simplified registrant list couldn't be loaded ({sl.error}); not counted"
    found = Counter()
    for r, v in rows:
        if not v.get("name"):
            continue
        names = _card_style_names(vendor_name_candidates(clean_vendor_name(v["name"])[0]))
        foreign = located[r][2]
        if _simplified_by_name(sl, names):
            found["foreign, exact name" if foreign else "Canadian, exact name"] += 1
        elif foreign and _simplified_by_name(sl, names, probable=True):
            found["foreign, probable name"] += 1
    total = sum(len(e) for e in sl._rows.values())
    return (f"ONLINE SELLERS (names on CRA's simplified GST/HST list of {total} entries; a run has CRA confirm "
            "each): " + (", ".join(f"{k}: {n}" for k, n in found.most_common()) or "none"))


def profile(path, sheet_name=None, with_lists=False):
    sys.path.insert(0, str(HERE))
    from taxcheck.checks import DryRun, RowChecker
    from taxcheck.gst import cra_today
    from taxcheck.numbers import normalize_tax_id, tax_id_problem
    from taxcheck.workbook import SupplierSheet, parse_date

    sheet = SupplierSheet(path, sheet_name)
    lines = []
    out = lines.append
    rows = list(sheet.rows())
    out(f"Workbook profile, {dt.date.today()}: {Path(path).name}, sheet '{sheet.ws.title}', header on row "
        f"{sheet.header_row}, {len(rows)} data rows")
    out("")
    out("COLUMNS")
    headers = [sheet.ws.cell(sheet.header_row, c).value for c in range(1, sheet.ws.max_column + 1)]
    used = {c: field for field, c in sheet.cols.items()}
    for i, h in enumerate(headers, 1):
        if h is not None:
            out(f"  {i:>3} {str(h)[:40]:40} -> {used.get(i, '(not used)')}")
    missing = [f for f in ("name", "trade_name", "gst", "bn", "qst", "pst", "bc_pst", "mb_rst", "sk_pst", "province",
                           "country", "postal", "date") if f not in sheet.cols]
    out(f"  Fields with no column: {', '.join(missing) or 'none'}")

    def field(name):
        return [v.get(name) for _, v in rows]

    def blank(v):
        return v is None or str(v).strip() == ""

    located = {r: RowChecker.locate(v) for r, v in rows}
    foreign_rows = sum(1 for loc in located.values() if loc[2])
    out("")
    out(f"CANADIAN / FOREIGN (as the tool decides from Country, State and Postal Code): {len(rows) - foreign_rows} "
        f"Canadian, {foreign_rows} foreign")
    ca_states = Counter(str(v.get("province")).strip().upper() for r, v in rows if located[r][2]
                        and RowChecker.locate({"province": v.get("province")})[1])
    if ca_states:
        out(f"  Foreign rows whose State is a Canadian province code: {sum(ca_states.values())} "
            f"({', '.join(f'{k}: {n}' for k, n in ca_states.most_common(5))}): check the Country column on those")

    for name in ("gst", "qst", "pst", "bc_pst", "mb_rst", "sk_pst", "bn"):
        if name not in sheet.cols:
            continue
        for group, wanted in (("Canadian rows", False), ("foreign rows", True)):
            values = [v.get(name) for r, v in rows if located[r][2] == wanted]
            if not values:
                continue
            out("")
            out(f"{name.upper()} COLUMN, {group}: {sum(blank(v) for v in values)} blank of {len(values)}")
            out("  Shapes (9 = digit, A = letter), most common first:")
            for k, n in top(Counter(shape(v) for v in values if not blank(v)), 15 if wanted else 25):
                out(f"    {n:>7}  {k}")
            if name == "gst" and not wanted:  # foreign Tax IDs are mostly EINs: no Canadian parsing for them
                kinds, problems = Counter(), Counter()
                for v in values:
                    if blank(v):
                        continue
                    try:
                        kinds[normalize_tax_id(v)[0]] += 1
                    except Exception as e:  # noqa: BLE001 - counting what the parser makes of it
                        kinds[f"unreadable ({type(e).__name__})"] += 1
                    p = tax_id_problem(v)
                    if p:
                        problems[re.sub(r"'[^']*'", "'…'", p)[:90]] += 1
                out("  Read by the tool as: " + ", ".join(f"{k}: {n}" for k, n in kinds.most_common()))
                if problems:
                    out("  Tax ID problems the tool reports (value hidden):")
                    for k, n in top(problems, 12):
                        out(f"    {n:>7}  {k}")

    for name, label in (("country", "COUNTRY"), ("province", "PROVINCE / STATE")):
        if name in sheet.cols:
            out("")
            out(f"{label} values as written (top 40):")
            for k, n in top(Counter(safe_value(v) if not blank(v) else "(blank)" for v in field(name)), 40):
                out(f"    {n:>7}  {k}")
    if "postal" in sheet.cols:
        out("")
        out("POSTAL CODE shapes:")
        for k, n in top(Counter(shape(v, 12) if not blank(v) else "(blank)" for v in field("postal")), 12):
            out(f"    {n:>7}  {k}")

    names = [str(v).strip() for v in field("name") if not blank(v)]
    out("")
    out(f"NAMES: {len(names)} filled, {len(set(n.upper() for n in names))} distinct (ignoring case), "
        f"{len(names) - len(set(n.upper() for n in names))} repeats")
    lengths = Counter(len(n) for n in names)
    if lengths:
        longest = max(lengths)
        out(f"  Longest name: {longest} characters, reached by {lengths[longest]} names "
            f"({'probably the vendor master field limit: names this long are likely cut off' if lengths[longest] >= 3 else 'no sign of a field limit'})")
        out("  Lengths near the top: " + ", ".join(f"{k} chars: {lengths[k]}" for k in sorted(lengths)[-6:]))
    for label, rx in NAME_PATTERNS:
        n = sum(1 for x in names if rx.search(x))
        if n:
            out(f"    {n:>7}  {label}")

    if "date" in sheet.cols:
        dates = field("date")
        kinds, parsed = Counter(), []
        for v in dates:
            if blank(v):
                kinds["(blank)"] += 1
            elif isinstance(v, (dt.date, dt.datetime)):
                kinds["Excel date cell"] += 1
                parsed.append(v.date() if isinstance(v, dt.datetime) else v)
            else:
                kinds["text " + shape(v, 16)] += 1
                d = parse_date(v)
                if d:
                    parsed.append(d)
                else:
                    kinds["  (unreadable text)"] += 1
        out("")
        out("DATES: " + ", ".join(f"{k}: {n}" for k, n in kinds.most_common(10)))
        if parsed:
            years = Counter(d.year for d in parsed)
            out(f"  Range {min(parsed)} to {max(parsed)}; by year: " + ", ".join(f"{y}: {years[y]}" for y in sorted(years)))

    # Same GST/HST number on differently named vendors (counted only).
    if "gst" in sheet.cols:
        by_number = {}
        for _, v in rows:
            digits = re.sub(r"\D", "", str(v.get("gst") or ""))[:9]
            if len(digits) == 9 and not blank(v.get("name")):
                by_number.setdefault(digits, set()).add(re.sub(r"[^a-z0-9]", "", str(v["name"]).lower()))
        shared = sum(1 for s in by_number.values() if len(s) > 1)
        counts = Counter(re.sub(r"\D", "", str(v.get("gst") or ""))[:9] for _, v in rows)
        repeated = sum(n - 1 for k, n in counts.items() if len(k) == 9 and n > 1)
        out("")
        out(f"SHARED NUMBERS: {shared} business numbers appear on vendors with different names; {repeated} rows "
            "repeat a number already on another row (looked up once)")

    if with_lists:
        out("")
        out(simplified_counts(rows, located))

    # What the tool would do with each row, offline (same routing as a real run).
    dry = DryRun()
    checker = RowChecker(dry, dry, bc=dry, mb=dry, sk=dry, available=sheet.cols)
    routes = Counter()
    for _, v in rows:
        try:
            result = checker.check(v, parse_date(v.get("date")) or cra_today())
        except Exception as e:  # noqa: BLE001
            routes[f"error: {type(e).__name__}"] += 1
            continue
        for tax, res in result.items():
            routes[f"{tax:7} {res.status}"] += 1
    out("")
    out("WHAT THE TOOL WOULD DO (offline routing; WOULD CHECK = looked up online in a real run; online sellers on")
    out("CRA's simplified list show here as gst NOT APPLICABLE: a real run finds them by name, see ONLINE SELLERS):")
    for k, n in sorted(routes.items()):
        out(f"    {n:>7}  {k}")
    return lines


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("workbook", nargs="?")
    ap.add_argument("--sheet")
    ap.add_argument("--offline", action="store_true", help="don't download CRA's simplified registrant list "
                                                           "(skips the online-seller count)")
    args = ap.parse_args(argv)
    sys.path.insert(0, str(HERE))
    from check_suppliers import choose_workbook, default_log_dir

    path = args.workbook or choose_workbook()
    if not path:
        print("No workbook chosen.")
        return 1
    import logging

    logging.disable(logging.INFO)  # the routing pass logs every row; the profile is the output here
    if not args.offline:
        from taxcheck.certs import use_system_certificates

        use_system_certificates()
    lines = profile(path, args.sheet, with_lists=not args.offline)
    text = "\n".join(lines)
    print(text)
    out = default_log_dir(path) / f"profile_{dt.datetime.now():%Y%m%d_%H%M%S}.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text + "\n", encoding="utf-8")
    print(f"\nSaved in {out}\nIt has no supplier names or numbers: you can send it to whoever tailors the tool.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
