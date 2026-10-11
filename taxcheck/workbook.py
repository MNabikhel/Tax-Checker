"""Reading the supplier workbook and writing results back into a copy of it."""

import csv
import datetime as dt
import logging
import re
from collections import Counter
from pathlib import Path

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .result import ERROR, INVALID, MANUAL, MISSING, NOT_CONFIRMED, NOT_REGISTERED, REGISTERED

log = logging.getLogger(__name__)

# Accepted header spellings (compared lowercase with spaces/punctuation removed).
COLUMNS = {
    "name": ["supplier name", "legal name", "legal entity", "legal entity name", "business name", "vendor name",
             "company name", "supplier", "vendor", "company", "name"],
    "trade_name": ["trade name", "operating name", "operating as", "dba", "dba name", "doing business as"],
    "province": ["province", "prov", "province/territory", "province / territory", "province/state", "state/prov",
                 "state/province", "region"],
    "gst": ["gst/hst number", "gst/hst no", "gst/hst #", "gst/hst", "gst number", "gst no", "gst #", "gst", "hst number", "hst #", "hst"],
    "bn": ["business number", "bn", "cra business number"],
    "qst": ["qst number", "qst no", "qst #", "qst", "tvq", "qst/tvq"],
    "bc_pst": ["bc pst number", "bc pst no", "bc pst #", "bc pst"],
    "sk_pst": ["sk pst number", "sk pst #", "sk pst", "saskatchewan pst"],
    "mb_rst": ["mb rst number", "mb rst #", "mb rst", "rst number", "rst #", "rst", "manitoba rst", "mb pst"],
    "pst": ["pst number", "pst no", "pst #", "pst", "provincial sales tax", "provincial sales tax number", "pst/rst"],
    "date": ["transaction date", "invoice date", "date"],
}

PROVINCES = {
    "AB": ["alberta", "alta"], "BC": ["british columbia", "b.c."], "MB": ["manitoba", "man"],
    "NB": ["new brunswick"], "NL": ["newfoundland", "newfoundland and labrador", "nfld"],
    "NS": ["nova scotia"], "NT": ["northwest territories", "nwt"], "NU": ["nunavut"],
    "ON": ["ontario", "ont"], "PE": ["prince edward island", "pei"], "QC": ["quebec", "québec", "que", "pq"],
    "SK": ["saskatchewan", "sask"], "YT": ["yukon"],
}

STATUS_FILL = {
    REGISTERED: "C6EFCE",
    NOT_REGISTERED: "FFC7CE",
    INVALID: "FFC7CE",
    NOT_CONFIRMED: "FFEB9C",
    MISSING: "FFEB9C",
    MANUAL: "DDEBF7",
    ERROR: "F8CBAD",
}

TAXES = [("gst", "GST/HST"), ("qst", "QST"), ("bc_pst", "BC PST"), ("sk_pst", "SK PST"), ("mb_rst", "MB RST")]


def _key(s):
    return re.sub(r"[^a-z0-9#]", "", str(s or "").lower())


_ALIASES = {_key(a): col for col, names in COLUMNS.items() for a in names}
TAX_FIELDS = {"gst", "bn", "qst", "pst", "bc_pst", "sk_pst", "mb_rst"}


def suggest_field(header):
    """For an unrecognized header, the closest known alias and its field, or None (used by --check-columns)."""
    import difflib

    key = _key(header)
    if not key:
        return None
    if re.search(r"\b(id|code|key|ref|no\.?)$", str(header).strip().lower()) and not re.search(
        r"tax|gst|hst|qst|tvq|pst|rst|bn|business", str(header).lower()
    ):
        return None  # "Vendor ID", "Supplier Code": identifiers, not names
    candidates = list(_ALIASES)
    if re.search(r"tax|gst|hst|qst|tvq|pst|rst", key):
        # A tax-ish header should only be suggested as a tax-number field, never as province or name.
        candidates = [a for a in candidates if _ALIASES[a] in TAX_FIELDS]
    match = difflib.get_close_matches(key, candidates, n=1, cutoff=0.6)
    if not match:
        # Partial words: "Legal Entity" contains "legal"...
        match = [a for a in candidates if len(a) >= 3 and (a in key or key in a)][:1]
    return (match[0], _ALIASES[match[0]]) if match else None


def normalize_province(value):
    s = str(value or "").strip()
    if s.upper() in PROVINCES:
        return s.upper()
    low = s.lower()
    for code, names in PROVINCES.items():
        if low in names:
            return code
    return ""


def parse_date(value):
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    s = str(value or "").strip()
    # Order matters for ambiguous dates: 09/10/2026 is read day-first (9 October), as in Canadian usage.
    for fmt in (
        "%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y%m%d",
        "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y",
        "%d-%b-%Y", "%d %b %Y", "%d-%B-%Y", "%d %B %Y",
        "%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y",
    ):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def as_text(value):
    """Text from outside sources is written as text: a leading '=' would make Excel treat it as a formula."""
    value = str(value or "")
    return "'" + value if value[:1] in ("=", "+", "@") else value


def header_hints(path, sheet=None):
    """When no header row is recognized: the most header-like row and a suggested field per cell.

    Used by --check-columns so it can still help when almost nothing matches.
    """
    path = Path(path)
    wb = _read_csv(path) if path.suffix.lower() == ".csv" else openpyxl.load_workbook(path, data_only=True)
    ws = wb[sheet] if sheet else wb.worksheets[0]
    best_row, best = None, []
    for r, row in enumerate(ws.iter_rows(min_row=1, max_row=min(ws.max_row, 15), values_only=True), 1):
        cells = [(c, v) for c, v in enumerate(row, 1) if isinstance(v, str) and v.strip()]
        if len(cells) > len(best):
            best_row, best = r, cells
    hints = []
    for c, header in best:
        field = _ALIASES.get(_key(header))
        hint = (header, field) if field else suggest_field(header)
        hints.append((get_column_letter(c), header, field, hint))
    return best_row, hints


def clean_value(value):
    """Excel stores typed numbers as floats (857305932 -> 857305932.0); turn those back into whole numbers."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


class InputError(ValueError):
    """A problem with the input file that the person running the script can fix."""


def _read_csv(path):
    """Load a CSV into a one-sheet workbook. Excel on Windows saves CSVs as cp1252, others as UTF-8."""
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            with open(path, newline="", encoding=encoding) as f:
                rows = list(csv.reader(f))
            break
        except UnicodeDecodeError:
            continue
    log.debug("Read CSV %s as %s: %d rows", path, encoding, len(rows))
    wb = openpyxl.Workbook()
    wb.active.title = re.sub(r"[\\/*?:\[\]]", "_", Path(path).stem)[:31] or "Sheet1"
    for row in rows:
        wb.active.append([v if v != "" else None for v in row])
    return wb


class SupplierSheet:
    def __init__(self, path, sheet=None):
        path = Path(path)
        if not path.exists():
            raise InputError(f"Can't find the input file: {path}")
        suffix = path.suffix.lower()
        if suffix == ".csv":
            self.wb = values_wb = _read_csv(path)
        elif suffix in (".xlsx", ".xlsm", ".xltx", ".xltm"):
            # Two copies: `wb` keeps formulas so the output is a faithful copy of the input;
            # `values_wb` has the calculated values (as last saved by Excel) that the checks read.
            self.wb = openpyxl.load_workbook(path)
            values_wb = openpyxl.load_workbook(path, data_only=True)
        elif suffix == ".xls":
            raise InputError(f"{path.name} is an old-style .xls file. Open it in Excel and use File > Save As > Excel Workbook (.xlsx).")
        else:
            raise InputError(f"{path.name}: expected an Excel workbook (.xlsx) or a .csv file.")
        name = sheet or self.wb.sheetnames[0]
        if name not in self.wb.sheetnames:
            raise InputError(f"There's no sheet called '{name}'. Sheets in this file: {', '.join(self.wb.sheetnames)}")
        self.ws, self.values_ws = self.wb[name], values_wb[name]
        self.header_row, self.cols = self._find_header()
        log.info("Reading sheet '%s': header on row %d", name, self.header_row)
        for col, c in self.cols.items():
            log.debug("  column %-10s <- '%s' (column %s)", col, self.ws.cell(self.header_row, c).value, get_column_letter(c))

    def _find_header(self):
        for r in range(1, min(self.values_ws.max_row, 15) + 1):
            cols = {}
            for c in range(1, self.values_ws.max_column + 1):
                col = _ALIASES.get(_key(self.values_ws.cell(r, c).value))
                if col and col not in cols:
                    cols[col] = c
            if "name" in cols and len(cols) >= 2:
                return r, cols
        seen = [
            str(v).strip()
            for row in self.values_ws.iter_rows(min_row=1, max_row=min(self.values_ws.max_row, 5), values_only=True)
            for v in row
            if v not in (None, "")
        ]
        raise InputError(
            "Couldn't find a header row: it needs a supplier-name column plus at least one other recognized "
            f"column. Text in the first rows: {', '.join(repr(v) for v in seen[:20]) or '(none)'}. "
            "Run with --check-columns for suggestions, and add your header names to COLUMNS in taxcheck/workbook.py."
        )

    def describe_columns(self):
        """Lines explaining the column mapping, for --check-columns."""
        lines = [f"Header row: {self.header_row} on sheet '{self.ws.title}'", "Recognized columns:"]
        for col, c in self.cols.items():
            lines.append(f"  {get_column_letter(c):>3} '{self.values_ws.cell(self.header_row, c).value}' -> {col}")
        used = set(self.cols.values())
        others = []
        for c in range(1, self.values_ws.max_column + 1):
            header = self.values_ws.cell(self.header_row, c).value
            if c in used or header in (None, "") or header == "Overall":
                continue
            hint = suggest_field(header)
            known = _ALIASES.get(_key(header))
            if known:
                note = f"also matches '{known}', but column {get_column_letter(self.cols[known])} is used (leftmost wins)"
            elif hint:
                note = f"not recognized; closest alias '{hint[0]}' ({hint[1]}). If it's that field, add '{header}' to COLUMNS['{hint[1]}']"
            else:
                note = "not used"
            others.append(f"  {get_column_letter(c):>3} '{header}': {note}")
        if others:
            lines.append("Other columns:")
            lines.extend(others)
        missing = [col for col in ("gst", "province", "qst", "pst", "trade_name", "date") if col not in self.cols]
        if missing:
            lines.append(f"Fields with no column: {', '.join(missing)}")
        return lines

    def rows(self):
        """Yield (row_number, {column: value}) for every non-empty supplier row."""
        for r in range(self.header_row + 1, self.values_ws.max_row + 1):
            values = {col: clean_value(self.values_ws.cell(r, c).value) for col, c in self.cols.items()}
            if any(v not in (None, "") for v in values.values()):
                yield r, values

    def write_results(self, results, out_path, run_notes):
        """results: {row_number: {tax_key: Result}}"""
        ws = self.ws
        # Re-running on an earlier results file overwrites its result columns instead of adding more.
        start = next(
            (c for c in range(1, ws.max_column + 1) if ws.cell(self.header_row, c).value == "Overall"),
            ws.max_column + 1,
        )
        if "Tax Check Summary" in self.wb.sheetnames:
            del self.wb["Tax Check Summary"]
        headers = ["Overall"]
        for key, label in TAXES:
            headers += [f"{label} Status", f"{label} Details"]
        headers += ["Name on Government Record", "Checked On"]
        bold = Font(bold=True)
        for i, h in enumerate(headers):
            cell = ws.cell(self.header_row, start + i, h)
            cell.font = bold
        today = dt.date.today().isoformat()

        counts = {key: Counter() for key, _ in TAXES}
        for r, by_tax in results.items():
            for c in range(start, start + len(headers)):  # clear anything left from an earlier run
                cell = ws.cell(r, c)
                cell.value, cell.fill = None, PatternFill()
            col = start + 1
            names = []
            statuses = []
            for key, _ in TAXES:
                res = by_tax.get(key)
                if res:
                    ws.cell(r, col, res.status).fill = PatternFill("solid", fgColor=STATUS_FILL.get(res.status, "FFFFFF"))
                    ws.cell(r, col + 1, as_text(res.detail)).alignment = Alignment(wrap_text=True, vertical="top")
                    counts[key][res.status] += 1
                    statuses.append(res.status)
                    if res.registered_name and res.registered_name not in names:
                        names.append(res.registered_name)
                col += 2
            overall = "OK" if statuses and all(s == REGISTERED for s in statuses) else "REVIEW"
            ws.cell(r, start, overall).fill = PatternFill("solid", fgColor="C6EFCE" if overall == "OK" else "FFEB9C")
            ws.cell(r, col, as_text("; ".join(names)))
            ws.cell(r, col + 1, today)

        for i, h in enumerate(headers):
            ws.column_dimensions[get_column_letter(start + i)].width = 55 if h.endswith("Details") else 18
        ws.freeze_panes = ws.cell(self.header_row + 1, 1)

        summary = self.wb.create_sheet("Tax Check Summary")
        summary.append(["Supplier tax registration check", today])
        summary.append([])
        statuses = [REGISTERED, NOT_REGISTERED, NOT_CONFIRMED, INVALID, MISSING, MANUAL, ERROR]
        summary.append(["Tax"] + statuses)
        for key, label in TAXES:
            summary.append([label] + [counts[key][s] for s in statuses])
        summary.append([])
        summary.append(["Notes"])
        for note in run_notes:
            summary.append([note])
        for cell in summary[3]:
            cell.font = bold
        summary["A1"].font = Font(bold=True, size=14)
        summary.column_dimensions["A"].width = 30
        self.wb.save(out_path)
