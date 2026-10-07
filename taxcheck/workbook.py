"""Reading the supplier workbook and writing results back into a copy of it."""

import datetime as dt
import re
from collections import Counter

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .result import ERROR, INVALID, MANUAL, MISSING, NOT_CONFIRMED, NOT_REGISTERED, REGISTERED

# Accepted header spellings (compared lowercase with spaces/punctuation removed).
COLUMNS = {
    "name": ["supplier name", "legal name", "business name", "vendor name", "company name", "supplier", "vendor", "company", "name"],
    "trade_name": ["trade name", "operating name", "dba", "doing business as"],
    "province": ["province", "prov", "province/territory", "province / territory", "region"],
    "gst": ["gst/hst number", "gst/hst no", "gst/hst #", "gst/hst", "gst number", "gst no", "gst #", "gst", "hst number", "hst #", "hst"],
    "bn": ["business number", "bn", "cra business number"],
    "qst": ["qst number", "qst no", "qst #", "qst", "tvq", "qst/tvq"],
    "bc_pst": ["bc pst number", "bc pst no", "bc pst #", "bc pst"],
    "sk_pst": ["sk pst number", "sk pst #", "sk pst", "saskatchewan pst"],
    "mb_rst": ["mb rst number", "mb rst #", "mb rst", "rst number", "rst #", "rst", "manitoba rst", "mb pst"],
    "pst": ["pst number", "pst no", "pst #", "pst"],
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
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%m/%d/%Y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


class SupplierSheet:
    def __init__(self, path, sheet=None):
        self.wb = openpyxl.load_workbook(path)
        self.ws = self.wb[sheet] if sheet else self.wb.worksheets[0]
        self.header_row, self.cols = self._find_header()

    def _find_header(self):
        for r in range(1, min(self.ws.max_row, 15) + 1):
            cols = {}
            for c in range(1, self.ws.max_column + 1):
                col = _ALIASES.get(_key(self.ws.cell(r, c).value))
                if col and col not in cols:
                    cols[col] = c
            if "name" in cols and len(cols) >= 2:
                return r, cols
        raise ValueError(
            "Couldn't find a header row with a supplier name column plus at least one tax-number column. "
            "See the README for accepted column names."
        )

    def rows(self):
        """Yield (row_number, {column: value}) for every non-empty supplier row."""
        for r in range(self.header_row + 1, self.ws.max_row + 1):
            values = {col: self.ws.cell(r, c).value for col, c in self.cols.items()}
            if any(v not in (None, "") for v in values.values()):
                yield r, values

    def write_results(self, results, out_path, run_notes):
        """results: {row_number: {tax_key: Result}}"""
        ws = self.ws
        start = ws.max_column + 1
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
            col = start + 1
            names = []
            statuses = []
            for key, _ in TAXES:
                res = by_tax.get(key)
                if res:
                    ws.cell(r, col, res.status).fill = PatternFill("solid", fgColor=STATUS_FILL.get(res.status, "FFFFFF"))
                    ws.cell(r, col + 1, res.detail).alignment = Alignment(wrap_text=True, vertical="top")
                    counts[key][res.status] += 1
                    statuses.append(res.status)
                    if res.registered_name and res.registered_name not in names:
                        names.append(res.registered_name)
                col += 2
            overall = "OK" if statuses and all(s == REGISTERED for s in statuses) else "REVIEW"
            ws.cell(r, start, overall).fill = PatternFill("solid", fgColor="C6EFCE" if overall == "OK" else "FFEB9C")
            ws.cell(r, col, "; ".join(names))
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
