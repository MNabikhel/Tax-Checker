"""Tailoring without code changes: an optional `tailoring.json` in the tool folder.

The agent tailoring the tool may not be able to edit or run code (e.g. Microsoft 365 Copilot Cowork works with
files, not programs). Everything the tailoring guide expects to need for a new export can go in this one file:

    {
      "columns": {"gst": ["Vendor Tax No"], "name": ["Supplier Legal Name"]},
      "provinces": {"ON": ["Ontario Canada"], "QC": ["Qc."]},
      "canada_values": ["CANADÁ", "CA - Canada"],
      "date_formats_first": ["%m/%d/%Y"],
      "remove_from_names": ["- INACTIVE", "ZZ-"],
      "do_not_use_markers": ["DO NOT PAY", "OBSOLETE"],
      "notes": "Why each entry is here (free text, not used by the tool)."
    }

Every key is optional. Columns, provinces and Canada values are *added* to the built-in ones; date formats
listed are tried *before* the built-in ones (e.g. "%m/%d/%Y" for month-first text dates); `remove_from_names`
is text removed from vendor names before lookups (case-insensitive, anywhere in the name);
`do_not_use_markers` is removed the same way and marks the vendor do-not-use. A typo (unknown key, unknown
field or province code, invalid date format) stops the run with a message saying what to fix, rather than
being silently ignored.
"""

import copy
import datetime as dt
import json
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

FILE_NAME = "tailoring.json"
XLSX_NAME = "tailoring.xlsx"  # the same settings as a sheet, for agents that work in Excel rather than text files
# Sheet rows: Setting | Field or code | Value. Setting names map to the JSON keys.
XLSX_SETTINGS = {"column": "columns", "province": "provinces", "canada value": "canada_values",
                 "date format first": "date_formats_first", "remove from names": "remove_from_names",
                 "do not use marker": "do_not_use_markers", "note": "notes"}
KEYS = {"columns", "provinces", "canada_values", "date_formats_first", "remove_from_names", "do_not_use_markers",
        "notes"}


class TailoringError(ValueError):
    """The tailoring file can't be used as written."""


_ORIGINAL = {}


def _remember():
    from . import checks, workbook

    if not _ORIGINAL:
        _ORIGINAL.update(columns=copy.deepcopy(workbook.COLUMNS), provinces=copy.deepcopy(workbook.PROVINCES),
                         canada=set(workbook.COUNTRIES_CA), dates=list(workbook.DATE_FORMATS),
                         remove=list(checks.EXTRA_NAME_MARKERS), dnu=list(checks.EXTRA_DNU_MARKERS))


def reset():
    """Back to the built-in settings (for tests, and before applying a file a second time)."""
    from . import checks, workbook

    if not _ORIGINAL:
        return
    workbook.COLUMNS.clear()
    workbook.COLUMNS.update(copy.deepcopy(_ORIGINAL["columns"]))
    workbook.PROVINCES.clear()
    workbook.PROVINCES.update(copy.deepcopy(_ORIGINAL["provinces"]))
    workbook.COUNTRIES_CA.clear()
    workbook.COUNTRIES_CA.update(_ORIGINAL["canada"])
    workbook.DATE_FORMATS[:] = _ORIGINAL["dates"]
    checks.EXTRA_NAME_MARKERS[:] = _ORIGINAL["remove"]
    checks.EXTRA_DNU_MARKERS[:] = _ORIGINAL["dnu"]
    workbook.rebuild_aliases()


def _strings(value, where):
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise TailoringError(f"{where} must be a list of text values, e.g. [\"Vendor Tax No\"]")
    return [v.strip() for v in value]


def _read_xlsx(path):
    """tailoring.xlsx -> the same dict as tailoring.json."""
    import openpyxl

    data = {}
    ws = openpyxl.load_workbook(path, read_only=True, data_only=True).worksheets[0]
    for i, row in enumerate(ws.iter_rows(values_only=True), 1):
        cells = [str(c).strip() if c is not None else "" for c in (list(row) + [None] * 3)[:3]]
        setting, key, value = cells[0].lower(), cells[1], cells[2]
        if not setting or setting in ("setting", "settings") or setting.startswith("#"):
            continue  # header row, blank row or comment
        if setting not in XLSX_SETTINGS:
            raise TailoringError(f"{path.name} row {i}: unknown setting '{cells[0]}'. Settings: "
                                 f"{', '.join(XLSX_SETTINGS)}")
        name = XLSX_SETTINGS[setting]
        if name == "notes":
            continue
        if not value:
            raise TailoringError(f"{path.name} row {i}: no value in the third column")
        if name in ("columns", "provinces"):
            if not key:
                raise TailoringError(f"{path.name} row {i}: '{cells[0]}' needs the field or province code in the "
                                     "second column")
            data.setdefault(name, {}).setdefault(key if name == "columns" else key.upper(), []).append(value)
        else:
            data.setdefault(name, []).append(value)
    return data


def apply(path):
    """Apply a tailoring file if it exists: `tailoring.json`, or `tailoring.xlsx` beside it. Returns a one-line
    description of what was applied ("" if there's no file)."""
    from . import checks, workbook

    path = Path(path)
    xlsx = path.with_name(XLSX_NAME)
    if not path.exists() and xlsx.exists():
        path = xlsx
    if not path.exists():
        return ""
    _remember()
    reset()
    if path.suffix.lower() == ".xlsx":
        try:
            data = _read_xlsx(path)
        except TailoringError:
            raise
        except Exception as e:
            raise TailoringError(f"{path.name} couldn't be read ({type(e).__name__}: {e}); is it open in Excel?") from e
    else:
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except ValueError as e:
            raise TailoringError(f"{path.name} isn't valid JSON ({e}). Check commas and quotes.") from e
    if not isinstance(data, dict):
        raise TailoringError(f"{path.name} must be a JSON object: {{ \"columns\": ..., ... }}")
    unknown = set(data) - KEYS
    if unknown:
        raise TailoringError(f"{path.name}: unknown setting(s) {', '.join(sorted(unknown))}. "
                             f"Known: {', '.join(sorted(KEYS))}")
    done = []
    for field, names in (data.get("columns") or {}).items():
        if field not in workbook.COLUMNS:
            raise TailoringError(f"{path.name}: column field '{field}' isn't one the tool reads. "
                                 f"Fields: {', '.join(workbook.COLUMNS)}")
        added = [n.lower() for n in _strings(names, f"columns.{field}")]
        keys = {workbook._key(n) for n in added}
        for other, spellings in workbook.COLUMNS.items():  # a header named for this field belongs to it only
            if other != field:
                workbook.COLUMNS[other] = [n for n in spellings if workbook._key(n) not in keys]
        # Placed first: a header the user named explicitly beats the built-in spellings for that field.
        workbook.COLUMNS[field] = added + [n for n in workbook.COLUMNS[field] if n not in added]
        done.append(f"columns {field}: {', '.join(added)}")
    for code, names in (data.get("provinces") or {}).items():
        if code not in workbook.PROVINCES:
            raise TailoringError(f"{path.name}: province code '{code}' unknown. Codes: {', '.join(workbook.PROVINCES)}")
        workbook.PROVINCES[code] += [n.lower().replace(".", "") for n in _strings(names, f"provinces.{code}")]
        done.append(f"province {code} spellings")
    if "canada_values" in data:
        workbook.COUNTRIES_CA.update(v.upper().replace(".", "") for v in _strings(data["canada_values"], "canada_values"))
        done.append("country values for Canada")
    if "date_formats_first" in data:
        formats = _strings(data["date_formats_first"], "date_formats_first")
        for f in formats:
            try:
                dt.datetime.now().strftime(f)
                if not re.search(r"%[dmyYbB]", f):
                    raise ValueError
            except ValueError:
                raise TailoringError(f"{path.name}: '{f}' isn't a date format (e.g. \"%m/%d/%Y\")") from None
        workbook.DATE_FORMATS[:0] = formats
        done.append(f"date formats first: {', '.join(formats)}")
    for key, target, label in (("remove_from_names", checks.EXTRA_NAME_MARKERS, "text removed from names"),
                               ("do_not_use_markers", checks.EXTRA_DNU_MARKERS, "do-not-use markers")):
        if key in data:
            target += [re.compile(re.escape(t), re.I) for t in _strings(data[key], key)]
            done.append(label)
    workbook.rebuild_aliases()
    summary = f"Tailoring file {path.name} applied: " + ("; ".join(done) or "nothing to apply")
    log.info(summary)
    return summary
