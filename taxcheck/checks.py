"""Decides which checks apply to a supplier row and runs them."""

import copy
import logging
import re

from .numbers import BadNumber, parse_bc_pst, parse_bn, parse_gst, parse_mb_rst, parse_qst
from .result import ERROR, INVALID, MANUAL, MISSING, NOT_REGISTERED, REGISTERED, Result
from .workbook import normalize_province

log = logging.getLogger(__name__)

SK_REGISTRY = "https://www.sets.saskatchewan.ca/rptp/portal/footer/pst-registry"
SK_MANUAL = (
    "Saskatchewan's PST registry has a reCAPTCHA, so it needs a person: re-run with --sk-assist to tick it once "
    f"and let the script do the searches, or search the business name at {SK_REGISTRY}."
)
MB_MANUAL = (
    "Browser checks are turned off. Search at https://taxcess.gov.mb.ca/TAXcess/?Link=RSTLookup with the business "
    "name and RST or business number."
)


def _names_from(result, label):
    """Legal and trade names another check found for this supplier, as (name, label) pairs.

    Revenu Québec's names come as "LEGAL NAME (trade name: TRADE)".
    """
    if not result or not result.registered_name or result.status not in (REGISTERED, NOT_REGISTERED):
        return ()
    m = re.fullmatch(r"(.*?)(?: \(trade name: (.*)\))?", result.registered_name)
    return tuple((n, label) for n in m.groups() if n)


def _blank(v):
    return v is None or str(v).strip() == ""


class RowChecker:
    def __init__(self, gst, qst, bc=None, mb=None, sk=None):
        self.gst = gst
        self.qst = qst
        self.bc = bc  # None means BC lookups are skipped (no browser)
        self.mb = mb  # None means Manitoba lookups are skipped (no browser)
        self.sk = sk  # None means Saskatchewan is left for a manual check (no --sk-assist)
        self._cache = {}  # the same number often appears on several rows; look it up once

    def check(self, row, date):
        prov = normalize_province(row.get("province"))
        names = [row.get("name"), row.get("trade_name")]
        pst = row.get("pst")
        bc_pst = row.get("bc_pst") if not _blank(row.get("bc_pst")) else (pst if prov == "BC" else None)
        sk_pst = row.get("sk_pst") if not _blank(row.get("sk_pst")) else (pst if prov == "SK" else None)
        mb_rst = row.get("mb_rst") if not _blank(row.get("mb_rst")) else (pst if prov == "MB" else None)

        if not _blank(pst) and prov not in ("BC", "SK", "MB") and _blank(row.get("bc_pst")) and _blank(row.get("sk_pst")) and _blank(row.get("mb_rst")):
            log.warning(
                "%s: PST number %r not checked: province %r isn't BC, SK or MB (only those provinces have PST/RST "
                "lookups). Fix the province, or use a BC/SK/MB-specific PST column.",
                row.get("name"), pst, row.get("province"),
            )
        log.debug("Row input: %s | province=%r date=%s", row, prov, date)
        out = {}
        # QST first: Revenu Québec returns the supplier's legal name, which the GST/HST check can use
        # when the sheet's name doesn't match CRA's records.
        qst = None
        if not _blank(row.get("qst")):
            qst = self._guard(self._qst, row.get("qst"), names, date)
        out["gst"] = self._guard(self._gst, row.get("gst"), names, date, _names_from(qst, "Revenu Québec (QST)"))
        if qst:
            out["qst"] = qst
        elif prov == "QC":
            out["qst"] = Result(MISSING, "Québec supplier but no QST number provided.")
        if not _blank(bc_pst):
            out["bc_pst"] = self._guard(self._bc, bc_pst, row.get("bn") or row.get("gst"))
        elif prov == "BC":
            out["bc_pst"] = Result(MISSING, "BC supplier but no PST number provided.")
        if not _blank(sk_pst) or prov == "SK":
            out["sk_pst"] = self._guard(self._sk, names)
            if not _blank(sk_pst) and out["sk_pst"].status not in (MANUAL,):
                out["sk_pst"].detail += " (Saskatchewan's registry searches by name only; the PST number in the sheet wasn't used.)"
        if not _blank(mb_rst) or prov == "MB":
            out["mb_rst"] = self._guard(self._mb, mb_rst, names, row.get("bn") or row.get("gst"))
        for tax, res in out.items():
            log.debug("  %s: %s | %s | %s", tax, res.status, res.detail, res.registered_name)
        return out

    def _guard(self, fn, *args):
        key = (fn.__name__, repr(args))
        if key in self._cache:
            log.debug("Reusing earlier result for %s%s", fn.__name__, args)
            return copy.copy(self._cache[key])
        try:
            result = fn(*args)
        except BadNumber as e:
            result = Result(INVALID, str(e))
        except Exception as e:  # network trouble etc. shouldn't stop the whole run
            # One line on screen; the full traceback goes to the log file only.
            log.error("%s%s failed: %s: %s", fn.__name__, args, type(e).__name__, str(e).splitlines()[0] if str(e) else "")
            log.debug("Traceback for the failure above", exc_info=True)
            return Result(ERROR, f"{type(e).__name__}: {e} (see log file)")
        if result.status != ERROR:  # a failed lookup is worth retrying on a later duplicate row
            self._cache[key] = result
        return copy.copy(result)

    def _gst(self, value, names, date, extra_names=()):
        if _blank(value):
            return Result(MISSING, "No GST/HST number provided.")
        bn9, _ = parse_gst(value)
        if extra_names:
            return self.gst.check(bn9, names, date, extra_names)
        return self.gst.check(bn9, names, date)

    def _qst(self, value, names, date):
        kind, number = parse_qst(value)
        if kind == "TQ":
            return self.qst.check(number, suffix_assumed="TQ" not in str(value).upper())
        if kind == "NR":
            return self.qst.check_nr(number)
        # 9-digit QST numbers belong to listed financial institutions; the CRA administers their QST.
        res = self.gst.check(number, names, date)
        res.detail = "9-digit QST number (listed financial institution, administered by the CRA). " + res.detail
        return res

    def _bc(self, value, bn_source):
        pst = parse_bc_pst(value)
        if _blank(bn_source):
            return Result(MISSING, "BC's lookup needs the supplier's business number; add a GST/HST or Business Number.")
        if self.bc is None:
            return Result(MANUAL, f"Browser checks turned off. Verify PST-{pst} at https://www.etax.gov.bc.ca/btp/eservices/_/")
        return self.bc.check(parse_bn(bn_source), pst)

    def _mb(self, rst_value, names, bn_source):
        rst = None if _blank(rst_value) else parse_mb_rst(rst_value)
        try:
            bn9 = None if _blank(bn_source) else parse_bn(bn_source)
        except BadNumber:
            if not rst:
                raise
            bn9 = None  # the RST number is enough to search with; the bad GST number is flagged in its own column
        if not (rst or bn9):
            return Result(MISSING, "Manitoba supplier but no RST number or GST/HST number to search with.")
        if self.mb is None:
            return Result(MANUAL, MB_MANUAL)
        return self.mb.check(names, bn9=bn9, rst=rst)

    def _sk(self, names):
        if self.sk is None:
            return Result(MANUAL, SK_MANUAL)
        return self.sk.check(names)


class DryRun:
    """Stand-in for every checker, for --check-columns: shows what would be looked up, without going online."""

    WOULD = "WOULD CHECK"

    def check(self, *args, **kwargs):
        shown = ", ".join([*(repr(a) for a in args), *(f"{k}={v!r}" for k, v in kwargs.items() if v)])
        return Result(self.WOULD, shown)

    def check_nr(self, number):
        return Result(self.WOULD, f"NR list lookup for {number}")
