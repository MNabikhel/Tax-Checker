"""Decides which checks apply to a supplier row and runs them."""

import copy
import logging

from .numbers import BadNumber, parse_bc_pst, parse_bn, parse_gst, parse_qst
from .result import ERROR, INVALID, MANUAL, MISSING, Result
from .workbook import normalize_province

log = logging.getLogger(__name__)

SK_REGISTRY = "https://www.sets.saskatchewan.ca/rptp/portal/footer/pst-registry"
SK_MANUAL = (
    "Saskatchewan's PST registry requires a person to tick a reCAPTCHA, so it can't be automated. "
    f"Search the business name at {SK_REGISTRY}; it confirms an active vendor licence but doesn't show the number."
)
MB_MANUAL = (
    "Manitoba has no public RST lookup. Get the supplier's 7-digit RST number in writing and confirm it with "
    "Manitoba Finance, Taxation Division: 204-945-5603 / 1-800-782-0318 / MBTax@gov.mb.ca."
)


def _blank(v):
    return v is None or str(v).strip() == ""


class RowChecker:
    def __init__(self, gst, qst, bc=None):
        self.gst = gst
        self.qst = qst
        self.bc = bc  # None means BC lookups are skipped (no browser)
        self._cache = {}  # the same number often appears on several rows; look it up once

    def check(self, row, date):
        prov = normalize_province(row.get("province"))
        names = [row.get("name"), row.get("trade_name")]
        pst = row.get("pst")
        bc_pst = row.get("bc_pst") if not _blank(row.get("bc_pst")) else (pst if prov == "BC" else None)
        sk_pst = row.get("sk_pst") if not _blank(row.get("sk_pst")) else (pst if prov == "SK" else None)
        mb_rst = row.get("mb_rst") if not _blank(row.get("mb_rst")) else (pst if prov == "MB" else None)

        log.debug("Row input: %s | province=%r date=%s", row, prov, date)
        out = {}
        out["gst"] = self._guard(self._gst, row.get("gst"), names, date)
        if not _blank(row.get("qst")):
            out["qst"] = self._guard(self._qst, row.get("qst"), names, date)
        elif prov == "QC":
            out["qst"] = Result(MISSING, "Québec supplier but no QST number provided.")
        if not _blank(bc_pst):
            out["bc_pst"] = self._guard(self._bc, bc_pst, row.get("bn") or row.get("gst"))
        elif prov == "BC":
            out["bc_pst"] = Result(MISSING, "BC supplier but no PST number provided.")
        if not _blank(sk_pst) or prov == "SK":
            out["sk_pst"] = Result(MANUAL, SK_MANUAL)
        if not _blank(mb_rst) or prov == "MB":
            out["mb_rst"] = Result(MANUAL, MB_MANUAL)
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
            log.exception("%s%s failed", fn.__name__, args)
            return Result(ERROR, f"{type(e).__name__}: {e} (see log file)")
        self._cache[key] = result
        return copy.copy(result)

    def _gst(self, value, names, date):
        if _blank(value):
            return Result(MISSING, "No GST/HST number provided.")
        bn9, _ = parse_gst(value)
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
            return Result(ERROR, "BC's lookup needs the supplier's business number; add a GST/HST or Business Number column.")
        if self.bc is None:
            return Result(MANUAL, f"Browser checks turned off. Verify PST-{pst} at https://www.etax.gov.bc.ca/btp/eservices/_/")
        return self.bc.check(parse_bn(bn_source), pst)
