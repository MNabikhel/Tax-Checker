"""Parsing and offline validation of Canadian sales-tax registration numbers."""

import re


class BadNumber(ValueError):
    """The value can't be read as the expected kind of number."""


def _compact(value):
    if re.fullmatch(r"\s*\d(\.\d+)?[eE]\+?\d+\s*", str(value or "")):
        raise BadNumber(
            f"'{value}' was turned into scientific notation by Excel, which loses digits. Format the column "
            "as Text in the source file and re-enter the number."
        )
    return re.sub(r"[\s\-./]", "", str(value or "")).upper()


def bn_check_digit_ok(bn9):
    """CRA business numbers carry a Luhn (mod 10) check digit."""
    total = 0
    for i, ch in enumerate(reversed(bn9)):
        d = int(ch)
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def parse_gst(value):
    """Return (bn9, program_account) from e.g. '12345 6789 RT 0001' or '123456789'."""
    s = _compact(value)
    m = re.fullmatch(r"(\d{9})(?:RT(\d{4}))?", s)
    if not m:
        raise BadNumber(f"'{value}' is not a GST/HST number (expected 9 digits + RT + 4 digits)")
    bn9, ref = m.group(1), m.group(2)
    if not bn_check_digit_ok(bn9):
        raise BadNumber(f"'{value}' fails the CRA check digit, likely a typo")
    return bn9, f"RT{ref}" if ref else None


def parse_bn(value):
    """Return the 9-digit business number from a BN, or from a full GST/HST/payroll number."""
    s = _compact(value)
    m = re.fullmatch(r"(\d{9})(?:[A-Z]{2}\d{4})?", s)
    if not m:
        raise BadNumber(f"'{value}' is not a 9-digit business number")
    if not bn_check_digit_ok(m.group(1)):
        raise BadNumber(f"'{value}' fails the CRA check digit, likely a typo")
    return m.group(1)


def parse_qst(value):
    """Return (kind, normalized) where kind is 'TQ' (general), 'NR' (non-resident) or 'FI'.

    'FI' is a 9-digit number used by selected listed financial institutions; the CRA
    administers those, so they are confirmed through the GST/HST registry.
    """
    s = _compact(value)
    if s.startswith("QST"):
        s = s[3:]
    m = re.fullmatch(r"(\d{10})(?:TQ(\d{4}))?", s)
    if m:
        return "TQ", f"{m.group(1)}TQ{m.group(2) or '0001'}"
    m = re.fullmatch(r"NR(\d{8})", s)
    if m:
        return "NR", f"NR{m.group(1)}"
    m = re.fullmatch(r"(\d{9})(?:TQ\d{4}|RT\d{4})?", s)
    if m:
        return "FI", m.group(1)
    raise BadNumber(f"'{value}' is not a QST number (expected 10 digits + TQ + 4 digits, or NR + 8 digits)")


def parse_bc_pst(value):
    """Return a BC PST number as '1234-5678' from e.g. 'PST-1234-5678'."""
    s = _compact(value)
    if s.startswith("PST"):
        s = s[3:]
    if not re.fullmatch(r"\d{8}", s):
        raise BadNumber(f"'{value}' is not a BC PST number (expected PST-1234-5678)")
    return f"{s[:4]}-{s[4:]}"


def parse_mb_rst(value):
    """Return a 7-digit Manitoba RST number. The 15-digit account number on RST returns isn't it."""
    s = _compact(value)
    if s.startswith("RST"):
        s = s[3:]
    if re.fullmatch(r"\d{15}", s):
        raise BadNumber(f"'{value}' looks like the 15-digit RST account number; Manitoba's registry needs the 7-digit RST number")
    if not re.fullmatch(r"\d{7}", s):
        raise BadNumber(f"'{value}' is not a Manitoba RST number (expected 7 digits)")
    return s


# CRA program identifiers that can follow the 9-digit business number. Only RT is GST/HST.
PROGRAMS = {
    "RT": "GST/HST", "RC": "corporate income tax", "RP": "payroll", "RR": "registered charity",
    "RM": "import/export", "RZ": "information returns", "RY": "Ontario/other provincial", "MC": "unknown",
}


def normalize_tax_id(value):
    """Read a free-form "Tax ID" (as in the ERP vendor export) for the GST/HST check.

    Returns (kind, number, note):
      kind "gst"   -> number is "<bn9>" or "<bn9>RT<ref>" ready for parse_gst; note explains any clean-up
      kind "qst"   -> the Tax ID is really a Québec QST number (1234567890TQ0001)
      kind "zero"  -> a placeholder such as 000000000RT0001 (passes the check digit, but isn't a real number)
      kind "raw"   -> couldn't be tidied; number is the original value so parse_gst reports why it's invalid

    The CRA registry only uses the 9-digit business number, so a business number written with another
    program account (RC0001 corporate tax, RP0001 payroll...), with a 3- or 5-digit reference, or in the old
    "R123456789" GST style still identifies the business. The note says what was assumed, so the reader
    can fix the vendor master.
    """
    raw = str(value or "").strip()
    s = re.sub(r"[\s\-./]", "", raw).upper()
    if re.fullmatch(r"\d{10}(TQ\d{4})?", s) and "TQ" in s:
        return "qst", s, ""
    if re.fullmatch(r"0{6,}(?:[A-Z]{2}\d*)?", s):  # 000000000RT0001, 00000000RT00001...
        return "zero", raw, ""
    m = re.fullmatch(r"(\d{9})RT(\d{4})", s)
    if m:
        return "gst", s, ""
    if re.fullmatch(r"\d{9}", s):
        return "gst", s, ""
    m = re.fullmatch(r"R(\d{9})(?:RT\d{4})?", s)
    if m:
        return "gst", m.group(1), f"Tax ID '{raw}' is in the old 'R' GST format; checked business number {m.group(1)}."
    m = re.fullmatch(r"(\d{9})([A-Z]{2})(\d{0,5})", s)
    if m:
        bn9, prog, ref = m.groups()
        if prog == "RT":
            return "gst", bn9, f"Tax ID '{raw}' has a non-standard RT reference ('{ref or 'none'}'); checked business number {bn9}."
        what = PROGRAMS.get(prog)
        if what and what != "unknown":
            desc = f"is a {what} account ({prog}), not the GST/HST (RT) account"
        else:
            desc = f"has program code '{prog}', which isn't a GST/HST (RT) account (typo?)"
        return "gst", bn9, f"Tax ID '{raw}' {desc}; checked business number {bn9} for GST/HST."
    return "raw", raw, ""


def tax_id_problem(value):
    """Short, filterable description of what's wrong with a Tax ID as entered ("" when it's a clean GST/HST number).

    Used for the "GST/HST Tax ID Problem" column, so vendor-master clean-up can be filtered by kind of problem.
    """
    raw = str(value or "").strip()
    if not raw:
        return "No Tax ID"
    if re.fullmatch(r"\s*\d(\.\d+)?[eE]\+?\d+\s*", raw):
        return "Excel scientific notation (digits lost)"
    kind, number, _ = normalize_tax_id(raw)
    s = re.sub(r"[\s\-./]", "", raw).upper()
    if kind == "zero":
        return "Placeholder (all zeros)"
    if kind == "qst":
        return "QST number entered as Tax ID"
    if kind == "raw":
        digits = re.match(r"R?(\d+)", s)
        if digits and len(digits.group(1)) != 9:
            return f"Wrong length ({len(digits.group(1))} digits, needs 9)"
        return "Not a GST/HST number format"
    bn9 = number[:9]
    if not bn_check_digit_ok(bn9):
        return "Fails CRA check digit (typo)"
    if re.fullmatch(r"R\d{9}(RT\d{4})?", s):
        return "Old R-format GST number"
    m = re.fullmatch(r"\d{9}([A-Z]{2})(\d{0,5})", s)
    if m:
        prog, ref = m.groups()
        if prog == "RT":
            return "" if len(ref) == 4 else f"RT reference not 4 digits ('{ref or 'none'}')"
        what = PROGRAMS.get(prog)
        if what and what != "unknown":
            return f"{what[0].upper() + what[1:]} account ({prog}), not GST/HST (RT)"
        return f"Program code '{prog}' (typo for RT?)"
    if re.fullmatch(r"\d{9}", s):
        return "Business number only (RT0001 assumed)"
    return ""
