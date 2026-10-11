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
