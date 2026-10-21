"""Decides which checks apply to a supplier row and runs them."""

import copy
import datetime as dt
import logging
import re
import threading

from .gst import _name_key
from .numbers import BadNumber, _compact_tax_id, normalize_tax_id, parse_bc_pst, parse_bn, parse_gst, parse_mb_rst, parse_qst
from .result import (
    NAME_MATCHES, NAME_UNCONFIRMED, NAME_UPDATE,
    ERROR, INVALID, MANUAL, MISSING, NAME_NOT_MATCHED, NOT_APPLICABLE, NOT_CONFIRMED, NOT_LOOKED_UP, NOT_REGISTERED, REGISTERED, Result,
)
from .workbook import normalize_country, normalize_province, province_from_postal

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


# Vendor-master markers that aren't part of the legal name ("(DNU) ACME LTD", "ACME LTD DO NOT USE",
# "DNU - ACME", "ACME (USE 107 DO NOT USE"). Left in, they make every CRA / Manitoba name match fail.
_DNU = re.compile(
    r"[(\[]\s*(?:dnu|do\s+not\s+use)\b[^)\]]*[)\]]"  # (DNU) / [DO NOT USE] / (DNU-USE 456)
    r"|\*+\s*dnu\s*\*+"                              # *DNU*
    r"|^[\sx*#-]*\bdo\s+not\s+use\b[\sx*#:-]*(?=\w)"  # XXX DO NOT USE XXX ACME / ** DO NOT USE ** ACME
    r"|^\s*dnu\b\s*[-:]?"                          # DNU - ACME / DNU-ACME / DNU ACME
    r"|^\s*do\s+not\s+use\b\s*[-:]?"               # DO NOT USE-ACME / DO NOT USE ACME
    r"|[-(]?\s*\bdnu\s*$"                          # ACME -DNU / ACME (DNU
    r"|\s*[-(][^-()]*?\bdo\s+not\s+use\b.*$"      # ACME -REMIT DO NOT USE / ACME (USE 107 DO NOT USE
    r"|\s*\bdo\s+not\s+use\b.*$",                 # ACME LTD DO NOT USE
    re.I,
)


# Set from tailoring.json (taxcheck/tailoring.py): text to remove from vendor names, and extra do-not-use markers.
EXTRA_NAME_MARKERS = []
EXTRA_DNU_MARKERS = []


def clean_vendor_name(name):
    """(name without do-not-use markers, True if a marker was removed)."""
    if _blank(name):
        return name, False
    extra_dnu = False
    if EXTRA_NAME_MARKERS or EXTRA_DNU_MARKERS:
        text = str(name)
        for rx in EXTRA_NAME_MARKERS:
            text = rx.sub(" ", text)
        for rx in EXTRA_DNU_MARKERS:
            text, n = rx.subn(" ", text)
            extra_dnu = extra_dnu or n > 0
        text = re.sub(r"\s{2,}", " ", text).strip(" -,:;*")
        if text != str(name).strip():
            name = text or name
    if not _DNU.search(str(name)):
        return name, extra_dnu  # no built-in marker: the name is used as given (minus tailoring-file text)
    cleaned = re.sub(r"\s{2,}", " ", _DNU.sub(" ", str(name))).strip(" -,:;*")
    return (cleaned or str(name).strip()), bool(cleaned) or extra_dnu


def _blank(v):
    return v is None or str(v).strip() == ""


# ---- Vendor-master names -> names CRA might accept --------------------------------------------------------------
# CRA's registry only confirms a number with the business's own name (case and punctuation ignored, extra words not).
# Vendor masters pack more into the field: "9999901 ONTARIO INC. O/A MAPLE PRO", "ACME LTD. (BIRCHWOOD)",
# "0999903 B.C LTD DBA THE NORTHSTAR GROUP", "WIDGET SUPPLY OF SPOKANE      8515", "ACME INC -REMIT".
# These are all still the sheet's own name, so a match on one of them is a normal REGISTERED.
_OPERATING_AS = re.compile(
    r"(?:^|\s+|(?<=[A-Za-z]{2}\.))(?:o/a|oa|o\.a\.?|dba|d/b/a|d\.b\.a\.?|operating\s+as|doing\s+business\s+as|"
    r"carrying\s+on\s+business\s+as|c\.o\.b\.?|c/o|itf|a\s+div(?:ision)?\s+of)(?=\s|$)\s*", re.I)
_PAREN = re.compile(r"\s*\(([^()]*)\)?\s*$|\s*\(([^()]*)\)\s*")
# Vendor-master status notes in brackets: dropped, never tried as a trade name.
_STATUS_TAG = re.compile(r"\s*[(\[]\s*(?:inactive|old|closed|duplicate|dup|obsolete|do\s+not\s+pay|hold|on\s+hold)\s*[)\]]",
                         re.I)
_JUNK = [
    re.compile(r"\s{2,}\d+\s*$"),                      # ERP vendor numbers padded on the right: "...     8515"
    re.compile(r"\s*[-,]?\s*\bremit\b.*$", re.I),       # "-REMIT", "REMIT TO ..."
    re.compile(r"\s+acct\s*#.*$", re.I),               # "ACCT #1602-..."
    re.compile(r"\s+#\s*[\d-]{5,}.*$"),                 # account numbers: "ACME MOBILITY  #555000111222"
    re.compile(r"[\s*,;:-]+$"),                        # trailing "*", commas, dashes
    re.compile(r"^[\s*]+"),                            # leading "*****"
]
_SUFFIX_SWAP = [("incorporated", "inc."), ("limited", "ltd."), ("corporation", "corp."), ("company", "co."),
                ("limitée", "ltée"), ("limitee", "ltee")]
_PROVINCE_WORDS = (r"alberta|ontario|b\.?\s?c\.?|british columbia|canada|manitoba|saskatchewan|nova scotia|"
                   r"new brunswick|québec|quebec|n\.?w\.?t\.?|yukon|newfoundland(?: and| &) labrador|newfoundland|"
                   r"p\.?e\.?i\.?|prince edward island")
_NUMBERED = re.compile(rf"^\d{{5,10}}\s+(?:{_PROVINCE_WORDS})\.?$", re.I)
# Short forms vendor masters use for numbered companies' provinces: "9999907 AB", "9999908 ON LTD", "999909 NB INC."
_PROVINCE_SHORT = {
    "ab": "ALBERTA", "alta": "ALBERTA", "alb": "ALBERTA", "on": "ONTARIO", "ont": "ONTARIO", "bc": "B.C.",
    "sk": "SASKATCHEWAN", "sask": "SASKATCHEWAN", "mb": "MANITOBA", "man": "MANITOBA", "ns": "NOVA SCOTIA",
    "nb": "NEW BRUNSWICK", "nl": "NEWFOUNDLAND AND LABRADOR", "nf": "NEWFOUNDLAND AND LABRADOR",
    "nfld": "NEWFOUNDLAND AND LABRADOR", "nf and labrador": "NEWFOUNDLAND AND LABRADOR", "pe": "P.E.I.",
    "pei": "P.E.I.", "qc": "QUÉBEC", "que": "QUÉBEC", "nwt": "N.W.T.", "nt": "N.W.T.", "yt": "YUKON",
}
# A numbered company at the start of the name, in short or full form, with or without its suffix, whatever follows:
# "9999907 AB O/A ...", "9999906 ALBERTA PRAIRIE PAINTING", "9999908 ON LTD".
_NUMBERED_SHORT = re.compile(
    r"^(\d{5,10})\s+(nf and labrador|nfld|alta|alb|ont|sask|man|pei|que|nwt|ab|on|bc|sk|mb|ns|nb|nl|nf|pe|qc|nt|yt|"
    rf"{_PROVINCE_WORDS})\b\.?(?:\s+(ltd|limited|inc|incorporated|corp|corporation)\b\.?)?", re.I)
# Legal suffix followed by more words: "ACME SUPPLY INC-EDM", "QUICK DELIVERY LTD. - SK JOB",
# "9999905 ALBERTA LTD. WINDOW MAGIC". The part up to the suffix is the legal name. ("Co"/"Company" left out:
# they appear inside names like "CO-OP".)
_UP_TO_SUFFIX = re.compile(
    r"^(.*?\b(?:ltd|limited|inc|incorporated|corp|corporation|ulc|llc|ltée|ltee|limitée|limitee)\b\.?)(?:\s*[-,/(]\s*|\s+)\S", re.I)
_LEGAL_SUFFIXES = {"inc", "incorporated", "ltd", "limited", "corp", "corporation", "co", "company", "llc", "ulc",
                   "lp", "llp", "ltee", "limitee", "the", "cie"}
MAX_NAME_TRIES = 8


def _tidy(name):
    for rx in _JUNK:
        name = rx.sub("", name)
    return re.sub(r"\s{2,}", " ", name).strip()


def _swap_suffix(name):
    low = name.lower()
    for long, short in _SUFFIX_SWAP:
        if re.search(rf"\b{re.escape(long)}$", low):
            return name[: -len(long)] + short.upper()
        if re.search(rf"\b{re.escape(short.rstrip('.'))}\.?$", low):
            return re.sub(rf"\b{re.escape(short.rstrip('.'))}\.?$", long.upper(), name, flags=re.I)
    return None


def _legal_and_operating(name):
    """("ACME LTD", ["WIDGETS"]) for "ACME LTD O/A WIDGETS" or "ACME LTD (WIDGETS)": the legal part and the
    operating (trade) names, untidied."""
    # Status notes and asterisks are vendor-master markings, never part of a registered name.
    # (Runs of spaces are kept here: _tidy uses them to spot padded vendor numbers.)
    plain = re.sub(r"(?<=\w)\*+(?=\s|$)", "", _STATUS_TAG.sub("", str(name).strip())).strip()
    legal, trades = plain, []
    parts = [p.strip(" -,;:") for p in _OPERATING_AS.split(plain)]
    if len(parts) > 1:
        named = [p for p in parts if re.search(r"[a-z0-9]", p, re.I)]
        if named and not core_name(named[0]) and len(named) > 1 and not parts[0].strip():
            named = named[1:]  # "C.O.B. WIDGETS": the operating name is all there is
        if named and core_name(named[0]):  # "THE OA GROUP": a lone "The" before it means it's not a marker
            legal = named[0]
            trades += named[1:]
    m = _PAREN.search(legal)
    if m:
        inner = re.sub(r"^(?:o/a|oa|dba|d/b/a|operating\s+as)\s+", "", (m.group(1) or m.group(2) or "").strip(), flags=re.I)
        legal = _PAREN.sub(" ", legal).strip()
        if inner:
            trades.append(inner)
    return legal, trades


def operating_names(name):
    """The operating (trade) names in a vendor-master name: after "O/A"/"DBA", or in brackets."""
    return [] if _blank(name) else [_tidy(t) for t in _legal_and_operating(name)[1]]


def vendor_name_candidates(name):
    """Names to try with CRA for one vendor-master name, most likely first (at most MAX_NAME_TRIES).

    Order: the name as given; its legal part (before "O/A"/"DBA"/brackets, or up to "LTD"/"INC" when a trade
    name or location follows); a tidied version (no padded vendor number, "REMIT", "*"); the legal suffix
    spelled the other way (Ltd/Limited...); numbered companies written in full ("9999907 AB" ->
    "9999907 ALBERTA LTD."); and last the operating name (after "O/A", in brackets) or the parent company
    ("..., A DIV OF BIGBUILD"). Every candidate is made from the vendor's own name; nothing is invented.
    """
    if _blank(name):
        return []
    name = str(name).strip()
    legal, trades = _legal_and_operating(name)
    legal = _tidy(legal)
    cut = _UP_TO_SUFFIX.match(legal)
    if cut and core_name(cut.group(1)):  # not "INC RESEARCH LTD" -> "INC"
        legal = cut.group(1).strip()
    out = [name, legal]
    swapped = _swap_suffix(legal)
    if swapped:
        out.append(swapped)
    trades = [_tidy(t) for t in trades]
    for part in [legal, *trades]:
        short = _NUMBERED_SHORT.match(part)
        # A two-letter province short form only when nothing but a legal suffix follows: "1234567 ON TIME
        # DELIVERY LTD" is a company named "On Time Delivery", not a numbered Ontario company.
        rest = part[short.end():].strip(" .,-") if short else ""
        if short and short.group(2).lower() in _PROVINCE_SHORT and len(short.group(2)) <= 3 and rest:
            short = None
        if short:
            number, prov, suffix = short.group(1), _PROVINCE_SHORT.get(short.group(2).lower(), short.group(2).upper()), short.group(3)
            base = f"{number} {prov}"
            if suffix:
                full = f"{base} {suffix.upper()}"
                out += [full, _swap_suffix(full) or full]
            else:
                out += [f"{base} LTD.", f"{base} INC."]
        elif _NUMBERED.match(part):  # (kept for forms the pattern above doesn't cover)
            # A numbered company ("9999904 ALBERTA") is the legal name even when written after "O/A";
            # without its suffix CRA won't match it, so try the usual ones.
            out += [part, f"{part} LTD.", f"{part} INC."]
    out += trades
    seen, unique = set(), []
    for n in out:
        key = _name_key(n)  # spellings CRA treats as the same name are one search
        if key and key not in seen:
            seen.add(key)
            unique.append(n)
    return unique[:MAX_NAME_TRIES]


def core_name(name):
    """Name for comparing with an official name: letters and digits only, "&"/"+" as "and", without legal
    suffixes or "the"."""
    # Dots dropped first, so dotted abbreviations read as one word ("L.P." as LP, "B.C." as BC).
    text = str(name or "").lower().replace(".", "").replace("é", "e").replace("&", " and ").replace("+", " and ")
    return "".join(w for w in re.findall(r"[a-z0-9]+", text) if w not in _LEGAL_SUFFIXES)


_ENDS_WITH_SUFFIX = re.compile(r"\b(?:inc|incorporated|ltd|limited|corp|corporation|ulc|llc|lp|llp|ltee|ltée|"
                               r"limitee|limitée)\.?$", re.I)


_CORPORATE_END = re.compile(r"\b(?:inc|incorporated|ltd|limited|corp|corporation|ulc|llc|ltee|ltée|limitee|limitée)"
                            r"\.?\s*$", re.I)


def entity_kind(name):
    """'partnership' (LP, LLP, S.E.N.C.), 'corporation' (ends in Inc/Ltd/Corp...), or None when the name says
    neither (vendor masters often leave the legal form off)."""
    from .cbr import _PARTNERSHIP

    if _PARTNERSHIP.search(str(name or "")):
        return "partnership"
    return "corporation" if _CORPORATE_END.search(str(name or "").strip()) else None


def different_entity_kinds(a, b):
    """True when one name is a partnership and the other a corporation ("ACME LP" vs "ACME INC."): different
    legal entities even when the names agree. A name with no legal form ("ACME") is compatible with either."""
    ka, kb = entity_kind(a), entity_kind(b)
    return bool(ka and kb and ka != kb)


def foreign_form_name(name):
    """core_name with trailing foreign legal forms dropped too ("SPOTIFY AB" -> "spotify"). Only legal forms:
    not regions or words like "International", which name sister companies ("X INTERNATIONAL LTD" isn't "X")."""
    text = str(name or "").lower().replace(".", "").replace("é", "e").replace("&", " and ").replace("+", " and ")
    words = re.findall(r"[a-z0-9]+", text)
    while words and words[-1] in (_LEGAL_SUFFIXES | _FOREIGN_FORMS) - {"com"}:
        words.pop()
    return "".join(w for w in words if w not in _LEGAL_SUFFIXES)


def same_business_name(sheet_names, official, raw_name="", truncated_at=34):
    """True when an official name is the vendor's name apart from spacing, punctuation, "&"/"and" or legal
    suffix ("ACME WIDGET CANADA INC." vs "AcmeWidget Canada Inc."); one half of a bilingual official name
    ("NORTHWIND PEST CONTROL INC./COMPAGNIE D'EXTERMINATION NORTHWIND INC."); the same name with a successor year
    ("STRAIGHTEDGE LOCATING" vs "STRAIGHTEDGE LOCATING (2011) LTD."); or a vendor name cut off by the vendor master's
    field length that the official name continues."""
    halves = [official] + ([p for p in re.split(r"\s*/\s*", str(official or "")) if p] if "/" in str(official or "") else [])
    for half in halves:
        off = core_name(half)
        if not off:
            continue
        for n in sheet_names:
            mine = core_name(n)
            if not mine:
                continue
            if different_entity_kinds(n, half):
                continue  # "ACME LP" (a partnership) isn't "ACME INC." (a corporation), though core names agree
            if mine == off:
                return True
            if len(mine) >= MIN_LIST_NAME and foreign_form_name(n) == foreign_form_name(half) == mine:
                return True  # a foreign legal form the vendor master leaves out: "SPOTIFY" / "SPOTIFY AB"
            if off.startswith(mine) and off[len(mine):].isdigit() and len(off) - len(mine) == 4:
                return True  # "(2011)" successor-company year
            # The ERP's name field holds 40 characters: a name that (nearly) filled it was probably cut off. Only a
            # name that runs to the end of a long cell counts (not the legal part of "X LTD O/A Y" or of
            # "X LTD (DO NOT USE)"), and not one that ends in a legal suffix: that one is complete.
            raw = str(raw_name or n).strip().upper()
            if (len(raw) >= truncated_at and raw.endswith(str(n).strip().upper()) and len(mine) >= 15
                    and not _ENDS_WITH_SUFFIX.search(str(n).strip()) and off.startswith(mine)):
                return True
    return False


# Shortest normalized name compared with the simplified list: shorter names ("ABC", "ZOOM", "MIRO") collide too
# easily. Five letters still covers listed sellers such as Canva, Figma, Udemy, Asana and Slack.
MIN_LIST_NAME = 5

# Words a non-resident seller's listed name often ends with that a vendor master leaves out ("Canva Pty Ltd",
# "Spotify AB", "Squarespace Ireland Limited", "GoDaddy.com LLC", "Slack Technologies, Inc."). Only trailing
# words are dropped, for the "probable" match below.
_FOREIGN_FORMS = {"pty", "pte", "ab", "ag", "bv", "nv", "gmbh", "sarl", "srl", "sa", "spa", "plc", "oy", "oyj",
                  "as", "asa", "aps", "kk", "uc", "dac", "pbc", "sl", "sas", "sasu", "ltda", "sro", "kft", "bhd",
                  "sdn", "pvt", "private", "unlimited", "public", "com"}
_REGIONS = {"ireland", "international", "intl", "emea", "europe", "uk", "usa", "us", "global", "asia", "pacific",
            "apac", "worldwide", "operations"}
_DESCRIPTORS = {"technologies", "technology", "labs", "software", "systems", "solutions", "services", "media",
                "communications", "networks", "platforms", "online", "digital", "studios", "apps"}
_TRAILING = _LEGAL_SUFFIXES | _FOREIGN_FORMS | _REGIONS | _DESCRIPTORS


def loose_name(name, descriptors=False):
    """core_name without trailing legal forms (also foreign ones), ".com" and regions, and with `descriptors`
    also trailing words like "Technologies" or "Labs". Descriptors are only dropped from a listed name: dropped
    from both, "CLERK TECHNOLOGIES INC" would match a listed "CLERK, INC." (seen when measuring false matches)."""
    text = str(name or "").lower().replace(".", " ").replace("é", "e").replace("&", " and ").replace("+", " and ")
    words = re.findall(r"[a-z0-9]+", text)
    trailing = _TRAILING if descriptors else _TRAILING - _DESCRIPTORS
    while words and words[-1] in trailing:
        words.pop()
    return "".join(w for w in words if w not in _LEGAL_SUFFIXES)


def _simplified_index(simplified):
    """({core name: [bn9]}, {loose name: [bn9]}) over the list's legal and trade names, built once per list and
    shared by parallel workers."""
    index = getattr(simplified, "_name_index", None)
    if index is None:
        if simplified._rows is None:
            simplified._load()
        exact, loose = {}, {}
        for bn9, entries in (simplified._rows or {}).items():
            for e in entries:
                for listed in (e["legal_name"], e["trade_name"]):
                    for table, key in ((exact, core_name(listed)), (loose, loose_name(listed)),
                                       (loose, loose_name(listed, descriptors=True))):
                        if len(key) >= MIN_LIST_NAME and bn9 not in table.setdefault(key, []):
                            table[key].append(bn9)
        index = simplified._name_index = (exact, loose)
    return index


def _simplified_by_name(simplified, names, probable=False):
    """Business numbers on CRA's simplified-registrant list whose legal or trade name is one of `names`.

    Exact: equal apart from case, punctuation and legal suffix (core_name). With `probable`, a looser match
    (loose_name: trailing foreign legal forms, ".com", regions and descriptor words dropped on both sides) that
    points at exactly one business; it's reported as probable, for a person to confirm."""
    exact, loose = _simplified_index(simplified)
    table, keyfn = (loose, loose_name) if probable else (exact, core_name)
    found = []
    for n in names:
        key = keyfn(n) if n else ""
        for bn9 in table.get(key, []) if len(key) >= MIN_LIST_NAME else []:
            if bn9 not in found:
                found.append(bn9)
    if probable and len(found) > 1:
        return []  # several listed businesses: too vague to suggest one
    return found


def _card_style_names(names):
    """'GOOGLE *GSUITE' (card-statement style) -> 'GOOGLE' as an extra name to compare with the list."""
    extra = [re.split(r"\s*\*", str(n))[0].strip() for n in names if n and "*" in str(n).strip("* ")]
    return [n for n in names if n] + [e for e in extra if e and e not in names]


class RowChecker:
    def __init__(self, gst, qst, bc=None, mb=None, sk=None, available=None, cache=None, defer_browser=False, taxes=None,
                 gst_dates="none"):
        # available: the fields the input file has a column for (SupplierSheet.cols). A tax whose number
        # column doesn't exist at all (e.g. the ERP vendor export has no QST or PST columns) isn't reported
        # as NO NUMBER: the file simply doesn't carry it. None keeps the original behaviour (always report).
        self.available = None if available is None else set(available)
        self.gst = gst
        self.qst = qst
        self.bc = bc  # None means BC lookups are skipped (no browser)
        self.mb = mb  # None means Manitoba lookups are skipped (no browser)
        self.sk = sk  # None means Saskatchewan is left for a manual check (no --sk-assist)
        self.number_search = True  # --no-number-search: no "possible GST/HST number" lead for suppliers with none
        # The same number often appears on several rows; look it up once. Parallel workers share one dict.
        self._cache = {} if cache is None else cache
        # defer_browser: worker threads can't use the (single-threaded) browser, so BC/SK/MB checks are returned
        # as DEFERRED placeholders and run afterwards on the main thread with run_deferred().
        self.defer_browser = defer_browser
        # taxes: which provincial checks to run, e.g. {"mb_rst"}; None = all. GST/HST is always checked: it decides
        # foreign / not-applicable rows and is the check the rest of the report hangs on.
        self.taxes = None if taxes is None else set(taxes) | {"gst"}
        # GST/HST registration dates from the CRA registry (gst_dates.py): "none"; "end" = is it still registered
        # today, and if not, since when (1 extra lookup per registered supplier, ~9 more if cancelled); "full" = also
        # the start date (~14 more lookups per supplier).
        self.gst_dates = gst_dates

    def _on(self, tax):
        return self.taxes is None or tax in self.taxes

    def _has(self, *fields):
        return self.available is None or any(f in self.available for f in fields)

    @staticmethod
    def locate(row):
        """(country, province, foreign, how) for a row.

        Country comes from the Country column; when it's blank, a Canadian province code or Canadian postal
        code makes the row Canadian, and a state/province that isn't Canadian makes it foreign. A blank or
        non-Canadian province on a Canadian row (e.g. State "CA") is filled in from the postal code.
        Files without Country/Postal columns behave as before: every row is treated as Canadian.
        """
        country = normalize_country(row.get("country"))
        prov = normalize_province(row.get("province"))
        postal_prov = province_from_postal(row.get("postal"))
        how = ""
        if not country:
            if prov or postal_prov:
                country = "CA"
            elif not _blank(row.get("province")):
                return "", "", True, f"no country, state/province '{row.get('province')}' isn't Canadian"
        foreign = country not in ("", "CA")
        if not foreign and not prov and postal_prov:
            prov, how = postal_prov, f"province {postal_prov} taken from postal code {row.get('postal')}"
        return country, prov, foreign, how

    def check(self, row, date):
        out, ctx = self._check_row(row, date)
        self._annotate(out, row, date, ctx)
        return out

    def _annotate(self, out, row, date, ctx):
        """Structured facts for the report columns (see report.py). Also, for a supplier that wasn't registered on
        the payment date, one more lookup as of today: "registered since" and "not registered at all" call for
        different actions."""
        from .gst import cra_today
        from .numbers import tax_id_problem

        g = out.get("gst")
        if g is not None:
            f = g.facts
            f.update({
                "country": ctx["country"], "province_used": ctx["province_used"], "dnu": ctx["dnu"],
                "vendor_name": row.get("name") or "", "name_used": ctx["name"] or "",
                "tax_id_entered": "" if _blank(row.get("gst")) else str(row.get("gst")),
                "tax_id_problem": "" if ctx["foreign"] else tax_id_problem(row.get("gst")),
                "number_checked": (g.facts.get("number_checked") if g.facts.get("digital_economy_match")
                                   else "" if g.status == NOT_APPLICABLE else ctx["gst_value"] or ""),
            })
            listed = f.get("listed_number", "")
            entered = _compact_tax_id(str(row.get("gst") or ""))
            if (listed.endswith("RT9999") and re.fullmatch(r"\d{9}RT\d{4}", entered or "") and entered != listed
                    and not f["tax_id_problem"]):
                # Simplified registrants moved from RT0001 to RT9999 on 2023-07-01; the old account is closed.
                f["tax_id_problem"] = (f"CRA lists this simplified registrant's current number as {listed}; "
                                       f"'{row.get('gst')}' is an older or wrong account. Update the Tax ID.")
            elif listed.endswith("RT9999") and re.fullmatch(r"\d{9}", entered or "") and not f["tax_id_problem"]:
                f["tax_id_problem"] = f"Only the 9-digit business number is on file; the full number is {listed}."
            if g.status in (REGISTERED, NAME_NOT_MATCHED, NOT_REGISTERED, NOT_CONFIRMED) or "date_checked" in f:
                f.setdefault("date_checked", str(date))
            today = cra_today()
            can_search = (self.gst_dates != "none" and hasattr(self.gst, "registry_lookup")
                          and not _blank(ctx["gst_value"]) and not f.get("simplified_registrant"))
            if g.status in (REGISTERED, NAME_NOT_MATCHED) and can_search:
                self._gst_dates(f, str(ctx["gst_value"])[:9], date, today)
            elif (g.status == NOT_REGISTERED and f.get("cra_answer") == "not_registered_on_date" and date < today
                    and not _blank(ctx["gst_value"])):
                self._registered_since_payment(g, ctx, date, today, can_search)
        numbers = {"qst": ctx.get("qst_value"), "bc_pst": ctx.get("bc_pst"), "mb_rst": ctx.get("mb_rst") or ctx["gst_value"]}
        for key, res in out.items():
            if key != "gst" and isinstance(res, Result) and not _blank(numbers.get(key)):
                res.facts.setdefault("number_checked", str(numbers[key]))

    def _registered_since_payment(self, g, ctx, date, today, can_search):
        """Not registered on the payment date: is it registered today, and if so since when?"""
        from .gst_dates import first_change, registered_on

        f = g.facts
        bn9 = str(ctx["gst_value"])[:9]
        try:
            if can_search:
                now = registered_on(self.gst, bn9, today)
                f["status_today"] = REGISTERED if now else NOT_REGISTERED
            else:
                f["status_today"] = self._guard(self._gst, ctx["gst_value"], ctx["gst_names"], today).status
                now = f["status_today"] in (REGISTERED, NAME_NOT_MATCHED)
            f["today"] = str(today)
            if now:
                g.detail += f" It is registered as of today ({today}): it registered after {date}."
                f["registration_end"] = f"Active on {today}"
                if can_search:
                    since = self._per_number("registered since", bn9, lambda s: s > date,
                                             lambda: first_change(self.gst, bn9, date, today, False))
                    f["registered_since"] = str(since)
                    f["dates_source"] = "CRA registry (date search)"
                    g.detail += f" Registered since {since}."
            else:
                g.detail += f" Still not registered today ({today})."
                if can_search:
                    self._cancelled_before_payment(g, bn9, date)
                self._whose_number(g, bn9, ctx, date)
        except Exception as e:
            f["dates_error"] = f"{type(e).__name__}: {e}"
            log.warning("Registration date search for %s failed: %s", bn9, e)

    LOOKBACK_DAYS = 730  # how far before the payment date a recent cancellation is looked for

    def _cancelled_before_payment(self, g, bn9, date):
        """Not registered on the payment date or today: was it registered shortly before (seen: CRA recorded a
        cancellation weeks late, back-dated)? If so, find the cancellation date. One lookup when it wasn't."""
        from .gst_dates import cancellation_date, registered_on

        earlier = date - dt.timedelta(days=self.LOOKBACK_DAYS)
        before = self._per_number("registered earlier", bn9, lambda v: True, lambda: registered_on(self.gst, bn9, earlier))
        if not before:
            return
        end = self._per_number("cancelled before", bn9, lambda e: e < date,
                               lambda: cancellation_date(self.gst, bn9, earlier, date))
        g.facts.update(registration_end=str(end), cancelled_before_payment=True,
                       dates_source="CRA registry (date search)")
        g.detail += (f" It was registered until {end}: the registration was cancelled before {date}. (Found by asking "
                     f"CRA's registry about this number on other days with a placeholder name: it answers 'insufficient "
                     f"information' on days the number was registered, up to {end}, and 'not registered' from "
                     f"{end + dt.timedelta(days=1)}.)")

    def _whose_number(self, g, bn9, ctx, date):
        """A number not registered on the payment date or today: whose business number it is (the registries
        looked up by number), and a possible current number: another business number registered under exactly
        that name, which CRA confirms is registered for GST/HST (a lead for a person, never a status change)."""
        from .gst import cra_today

        official = getattr(self.gst, "_official_name", None)
        if official is None:
            return
        try:
            name, label, _ = official(bn9)
            if name:
                g.facts.update(official_name=name, official_name_source=label)
                g.detail += f" Business number {bn9} belongs to '{name}' ({label})."
            names = [n for n in (name, *ctx["gst_names"][1:2]) if n]
            if names:
                self._possible_number(g, names, cra_today(), exclude=bn9)
                if g.facts.get("possible_number"):
                    g.detail = g.detail.replace(" Possible number:", " Possible current number:")
        except Exception as e:  # extra information only
            log.debug("Couldn't look up whose number %s is: %s", bn9, e)

    def _per_number(self, what, bn9, still_true, search):
        """A registration date for a business number, searched once and reused for its other rows while it
        still fits their payment date (e.g. a cancellation after one payment date is after earlier ones too)."""
        key = ("date search", what, bn9)
        if key in self._cache and still_true(self._cache[key]):
            return self._cache[key]
        self._cache[key] = found = search()
        return found

    def _gst_dates(self, f, bn9, date, today):
        """Registration end (and, in "full" mode, start) for a number registered on `date`. Cached per number."""
        from .gst_dates import GST_START, cancellation_date, registered_on, registration_start

        def gst_dates_start():
            return GST_START.isoformat()

        date = min(date, today)  # CRA refuses future dates
        key = ("gst_dates", bn9, str(date), self.gst_dates)
        if key in self._cache:
            f.update(self._cache[key])
            return
        found = {"dates_source": "CRA registry (date search)"}
        try:
            if date < today:
                now = registered_on(self.gst, bn9, today)
                if now:
                    found["registration_end"] = f"Active on {today}"
                elif now is False:
                    end = self._per_number("cancelled", bn9, lambda e: e >= date,
                                           lambda: cancellation_date(self.gst, bn9, date, today))
                    found.update(registration_end=str(end), cancelled_after_payment=True, status_today=NOT_REGISTERED,
                                 today=str(today))
            else:
                found["registration_end"] = f"Active on {today}"
            if self.gst_dates == "full":
                start = self._per_number("registration start", bn9, lambda st: st is None or st <= date,
                                         lambda: registration_start(self.gst, bn9, date))
                found["registered_since"] = str(start) if start else f"{gst_dates_start()} or earlier"
        except Exception as e:  # dates are extra information: never let them break the row
            found["dates_error"] = f"{type(e).__name__}: {e}"
            log.warning("Registration date search for %s failed: %s", bn9, e)
        if "dates_error" not in found:
            self._cache[key] = found
        f.update(found)

    @staticmethod
    def _search_names(name, trade_name, candidates=None):
        """Names for registries that search by words (Manitoba, Saskatchewan): as given, then the legal part."""
        candidates = vendor_name_candidates(name) if candidates is None else candidates
        return [*candidates[:2], trade_name] if candidates else [name, trade_name]

    def saskatchewan_searches(self, rows):
        """The Saskatchewan searches checking `rows` will need, as (names, sheet_had_number), each once.

        Saskatchewan's registry asks for a CAPTCHA once per browser session (its terms page), so check_suppliers
        runs all of these right after the person passes it, instead of one at a time through a long run. The
        arguments are exactly what _check_row passes to _sk_noted, so the row checks later find the results in
        the cache.
        """
        if not self._on("sk_pst"):
            return []
        jobs, seen = [], set()
        for row in rows:
            _, prov, foreign, _ = self.locate(row)
            if foreign:
                continue
            sk = row.get("sk_pst") if not _blank(row.get("sk_pst")) else (row.get("pst") if prov == "SK" else None)
            if _blank(sk) and prov != "SK":
                continue
            name, _ = clean_vendor_name(row.get("name"))
            job = (self._search_names(name, row.get("trade_name")), not _blank(sk))
            if repr(job) not in seen:
                seen.add(repr(job))
                jobs.append(job)
        return jobs

    def check_saskatchewan(self, row, date=None):
        """Only the Saskatchewan PST check, for "Run Saskatchewan Batch.bat": {} if the row has nothing to search."""
        jobs = self.saskatchewan_searches([row])
        if not jobs:
            return {}
        names, had_number = jobs[0]
        return {"sk_pst": self._browser_check("_sk_noted", names, had_number)}

    def _check_row(self, row, date):
        country, prov, foreign, how = self.locate(row)
        if how:
            log.debug("%s: %s", row.get("name"), how)
        name, dnu = clean_vendor_name(row.get("name"))
        if dnu:
            log.debug("%s: looked up as %r (do-not-use marker removed)", row.get("name"), name)
        # CRA gets every likely form of the vendor-master name (legal part before "O/A", without brackets...);
        # Manitoba/Saskatchewan, which search by words, get the name as given and its legal part.
        candidates = vendor_name_candidates(name)
        gst_names = [*candidates, row.get("trade_name")]
        names = self._search_names(name, row.get("trade_name"), candidates)
        raw_name = str(row.get("name") or "")

        # The GST/HST column may be a free-form "Tax ID": tidy it (other CRA program accounts, old "R" format,
        # placeholders, a QST number in the wrong column). The note goes into the result details.
        gst_value, qst_value, gst_note, kind = row.get("gst"), row.get("qst"), "", None
        if not _blank(gst_value):
            kind, number, gst_note = normalize_tax_id(gst_value)
            if kind == "gst":
                gst_value = number
            elif kind == "zero":
                gst_value, gst_note = None, f"Tax ID '{row.get('gst')}' is a placeholder, not a real GST/HST number."
            elif kind == "qst" and _blank(qst_value):
                gst_value, qst_value = None, number
                gst_note = f"Tax ID '{row.get('gst')}' is a Québec QST number; it was checked in the QST column."

        ctx = {"country": country, "foreign": foreign, "dnu": dnu, "name": name, "gst_names": gst_names,
               "gst_value": gst_value, "qst_value": qst_value,
               "province_used": f"{prov} (from postal code)" if prov and how else prov}
        if foreign:
            # A QST number is checked for foreign suppliers too: non-residents selling into Québec register for
            # QST, often under the specified (NR) system, which only non-residents use.
            extra = {}
            if not _blank(qst_value) and self._on("qst"):
                extra["qst"] = self._guard(self._qst, qst_value, names, date)
                log.debug("  qst: %s | %s", extra["qst"].status, extra["qst"].detail)
            # Foreign suppliers only get a Canadian check when they gave an explicit GST/HST (RT) number:
            # a bare 9-digit Tax ID is usually a US EIN, which can pass the CRA check digit by chance.
            # Explicit: 9 digits followed by a program code (123456789RT0001, ...RC0001), or the old R123456789.
            explicit = kind == "gst" and bool(re.search(r"^R\d{9}|\d{9}[A-Z]{2}", _compact_tax_id(str(row.get("gst")))))
            if kind == "gst" and not explicit and self._on_simplified_list(gst_value):
                explicit = True  # a bare 9-digit number that is a simplified registrant's: not a US EIN
            where = f"country {country}" if country else how or "country unknown"
            if not explicit:
                if _blank(row.get("gst")):
                    why = "no Canadian tax number on file"
                elif gst_note and _blank(gst_value):
                    why = gst_note[:-1]
                else:
                    why = f"Tax ID '{row.get('gst')}' isn't a Canadian GST/HST number"
                res = self._digital_economy(gst_names, date, f"Non-Canadian supplier ({where}); {why}.",
                                            operating=[*operating_names(name), row.get("trade_name")])
                if res is None:
                    res = Result(NOT_APPLICABLE, f"Non-Canadian supplier ({where}); {why}, so no Canadian sales-tax check applies.")
                else:
                    res = self._name_check(res, name, dnu)
                log.debug("  gst: %s | %s", res.status, res.detail)
                return {"gst": res, **extra}, ctx
            res = self._name_check(self._confirm_official(self._guard(self._gst, gst_value, gst_names, date), gst_names,
                                                          raw_name), name, dnu)
            res.detail = f"Non-Canadian supplier ({where}) with a Canadian GST/HST number. " + (gst_note + " " if gst_note else "") + res.detail
            log.debug("  gst: %s | %s | %s", res.status, res.detail, res.registered_name)
            return {"gst": res, **extra}, ctx
        row = dict(row, qst=qst_value)
        pst = row.get("pst")
        bc_pst = row.get("bc_pst") if not _blank(row.get("bc_pst")) else (pst if prov == "BC" else None)
        sk_pst = row.get("sk_pst") if not _blank(row.get("sk_pst")) else (pst if prov == "SK" else None)
        mb_rst = row.get("mb_rst") if not _blank(row.get("mb_rst")) else (pst if prov == "MB" else None)
        ctx.update(bc_pst=bc_pst, mb_rst=mb_rst)

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
        if not _blank(row.get("qst")) and self._on("qst"):
            qst = self._guard(self._qst, row.get("qst"), names, date)
        out["gst"] = self._name_check(self._confirm_official(
            self._guard(self._gst, gst_value, gst_names, date, _names_from(qst, "Revenu Québec (QST)")), gst_names,
            raw_name), name, dnu)
        if gst_note:
            out["gst"].detail = f"{gst_note} {out['gst'].detail}".strip()
        if out["gst"].status in (MISSING, INVALID):
            found = self._digital_economy(gst_names, date, out["gst"].detail, foreign=False)
            if found is not None:
                out["gst"] = self._name_check(found, name, dnu)
        if out["gst"].status == MISSING:
            self._possible_number(out["gst"], gst_names, date)
        if qst:
            out["qst"] = qst
        elif prov == "QC" and self._has("qst") and self._on("qst"):
            out["qst"] = Result(MISSING, "Québec supplier but no QST number provided.")
        if not self._on("bc_pst"):
            pass
        elif not _blank(bc_pst):
            out["bc_pst"] = self._browser_check("_bc", bc_pst, row.get("bn") or gst_value)
        elif prov == "BC" and self._has("bc_pst", "pst"):
            out["bc_pst"] = Result(MISSING, "BC supplier but no PST number provided.")
        if self._on("sk_pst") and (not _blank(sk_pst) or prov == "SK"):
            out["sk_pst"] = self._browser_check("_sk_noted", names, not _blank(sk_pst))
        if self._on("mb_rst") and (not _blank(mb_rst) or prov == "MB"):
            # Manitoba's registry has the legal name: the name CRA accepted for the number is worth a search too.
            gst = out.get("gst")
            cra_names = [gst.facts.get("accepted_name"), gst.facts.get("official_name"),
                         gst.registered_name if gst.status == REGISTERED else None] if gst else []
            mb_names = list(dict.fromkeys(n for n in [*names, *cra_names] if n))
            out["mb_rst"] = self._browser_check("_mb", mb_rst, mb_names, row.get("bn") or gst_value)
        if dnu and "gst" in out:
            out["gst"].detail = f"Vendor is marked do-not-use in the vendor master; looked up as '{name}'. " + out["gst"].detail
        for tax, res in out.items():
            log.debug("  %s: %s | %s | %s", tax, res.status, res.detail, res.registered_name)
        return out, ctx

    def _possible_number(self, res, names, date, exclude=None):
        """For a Canadian supplier with no GST/HST number: a business number registered under exactly the vendor's
        name (Canada's Business Registries, by name) that CRA confirms is registered for GST/HST under that name on
        the date. Noted on `res` as a lead for a person to confirm with the supplier; the status stays NO NUMBER.
        Measured on suppliers whose number was known: 5 of 9 found, none wrong (2026-10-16)."""
        if not self.number_search:
            return
        sources = [s for s in getattr(self.gst, "name_sources", []) if hasattr(s, "numbers_for_name")]
        if not sources or not hasattr(self.gst, "registry_lookup"):
            return
        for source in sources:
            candidates = {}
            for n in names[:2]:
                for bn9, registered in source.numbers_for_name(n):
                    if bn9 != exclude:  # (a cancelled number: its business's other numbers)
                        candidates.setdefault(bn9, registered)
            if len(candidates) != 1:
                continue  # none, or several businesses with this name: no lead
            (bn9, registered), = candidates.items()
            try:
                if self.gst.registry_lookup(bn9, registered, date) != "registered":
                    continue
            except Exception as e:
                log.debug("Couldn't confirm possible GST/HST number %s: %s", bn9, e)
                continue
            res.facts.update(possible_number=bn9, possible_number_name=registered, possible_number_source=source.label)
            res.detail += (f" Possible number: {source.label} lists '{registered}' with business number {bn9}, and CRA "
                           f"confirms it's registered for GST/HST under that name on {date}.")
            return

    def _on_simplified_list(self, number):
        simplified = getattr(self.gst, "simplified", None)
        if simplified is None or not hasattr(simplified, "parse") or _blank(number):
            return False
        if simplified._rows is None:
            simplified._load()
        rows = simplified._rows
        return isinstance(rows, dict) and str(number)[:9] in rows

    def _digital_economy(self, names, date, why, foreign=True, operating=()):
        """A supplier with no usable Canadian number whose name is on CRA's list of simplified GST/HST
        (digital economy) registrants, confirmed with the CRA registry; None if not.

        Many non-resident digital sellers are set up as foreign vendors with a US EIN or no tax number on file.
        The vendor's name must equal a listed legal or trade name apart from case, punctuation, "&"/"and" and
        legal suffixes (core_name); matching on parts of names gave many false positives in a manual pass.
        CRA must then confirm the listed number under the listed legal name on the date.
        """
        simplified = getattr(self.gst, "simplified", None)
        if simplified is None or not hasattr(simplified, "parse") or not hasattr(self.gst, "registry_lookup"):
            return None  # --offline / --check-columns stand-ins
        names = _card_style_names(names)
        # A probable (looser) match only for foreign vendors: a Canadian business with no number on file isn't
        # likely to be a non-resident digital seller, so only an exact name match counts for it. And only from
        # the vendor's own legal name: a short operating name ("... DBA SLATE") matched an unrelated registrant
        # ("Slate Digital LLC") once "Digital" was dropped. Operating names still count for exact matches.
        skip = {_name_key(n) for n in operating if n}
        own = [n for n in names if n and _name_key(n) not in skip]
        for probable in (False, True) if foreign else (False,):
            for bn9 in _simplified_by_name(simplified, own if probable else names, probable=probable):
                entry = simplified.lookup(bn9, date)
                key = ("digital_economy", bn9, str(date))
                if key not in self._cache:
                    try:
                        self._cache[key] = self.gst.registry_lookup(bn9, entry["legal_name"], date)
                    except Exception as e:  # report it as an error, so --resume tries the row again
                        log.warning("Simplified-registrant confirmation for %s failed: %s", bn9, e)
                        return Result(ERROR, f"{why} The name matches a simplified (digital economy) registrant on "
                                             f"CRA's list ({entry['number']}), but confirming it with CRA failed: "
                                             f"{type(e).__name__}: {e} (see log file)")
                if self._cache[key] != "registered":
                    log.debug("%s matches simplified registrant %s, but CRA answered %r for %s", names[0], bn9,
                              self._cache[key], date)
                    continue
                listed = entry["legal_name"] + (f" (trade name: {entry['trade_name']})" if entry["trade_name"] else "")
                how = (f"The vendor name probably is '{listed}' on CRA's list of simplified GST/HST (digital economy) "
                       "registrants (the names differ only in legal form, region or a word like 'Technologies': "
                       "check it's the same business)" if probable else
                       f"The vendor name matches '{listed}' on CRA's list of simplified GST/HST (digital economy) "
                       "registrants")
                return Result(
                    REGISTERED,
                    f"{why} {how}, and CRA confirms {entry['number']} under that name on {date}. Simplified "
                    "registrants can't give input tax credits for GST/HST they charge.",
                    entry["legal_name"],
                    facts={"digital_economy_match": "probable" if probable else True, "simplified_registrant": True,
                           "cra_answer": "registered", "number_checked": entry["number"].replace(" ", ""),
                           "accepted_name": entry["legal_name"], "official_name": entry["legal_name"],
                           "name_source": "CRA's simplified registrant list",
                           "registered_since": str(entry["registered"] or ""),
                           "registration_end": str(entry["deregistered"] or ""),
                           "dates_source": "CRA simplified registrant list"},
                )
        return None

    @staticmethod
    def _name_check(res, entered, dnu):
        """Separate from the tax result: is the vendor name in the sheet the name CRA has on record?

        The tax result can be REGISTERED after CRA accepted the legal part of "ACME INC. O/A ACE" or an official
        registry spelling; the vendor record still needs fixing, so this says which name to use.
        """
        if res.status == REGISTERED:
            accepted = res.facts.get("accepted_name") or res.registered_name
            if not accepted or _name_key(accepted) == _name_key(str(entered or "")):
                res.name_check = NAME_MATCHES
                if dnu:
                    res.detail = "Vendor name matches CRA apart from the do-not-use marker. " + res.detail
            else:
                res.name_check = NAME_UPDATE
                said = "CRA accepted" if res.facts.get("accepted_name") else "the official name for this number is"
                res.detail = (f"Vendor name differs from CRA's records: {said} '{accepted}', not "
                              f"'{entered}'. Update the vendor name. " + res.detail)
        elif res.status == NAME_NOT_MATCHED:
            res.name_check = NAME_UNCONFIRMED
        return res

    @staticmethod
    def _confirm_official(res, sheet_names, raw_name):
        """REGISTERED - NAME NOT MATCHED becomes REGISTERED when a name tied to this business number is the
        vendor's own name apart from spacing, punctuation or legal suffix, or the vendor name is that name cut off
        by the vendor master's field length.

        "Tied to this business number" means CRA accepted that name for the number, or it came from a registry
        looked up by the number (federal corporations, OrgBook BC, CRA's simplified list). A name found another
        way (Revenu Québec's, by the QST number) proves nothing about the GST/HST number, which could be another
        business's, so it never upgrades the result.
        """
        if res.status != NAME_NOT_MATCHED or not res.registered_name:
            return res
        f = res.facts
        accepted = f.get("cra_answer") == "registered"
        if not (accepted or f.get("official_name_tied_to_number")):
            return res
        if not same_business_name([n for n in sheet_names if n], res.registered_name, raw_name):
            return res
        how = ("CRA accepted it for this number" if accepted
               else f"{f.get('official_source') or 'an official registry'} gives it for this number")
        return Result(
            REGISTERED,
            f"Registered for GST/HST. The name '{res.registered_name}' ({how}) is this vendor's name apart from "
            f"spacing, punctuation, legal suffix or a cut-off name in the vendor master. Consider updating the vendor "
            f"name to match. CRA detail: {res.detail}",
            res.registered_name,
            facts=dict(f),
        )

    def _browser_check(self, method, *args):
        if self.defer_browser:
            return Deferred(method, args)
        return self._guard(getattr(self, method), *args)

    def run_deferred(self, out):
        """Run the browser checks a worker deferred (main thread only). Changes `out` in place."""
        for tax, res in list(out.items()):
            if isinstance(res, Deferred):
                done = self._guard(getattr(self, res.method), *res.args)
                done.facts = {**res.facts, **done.facts}  # keep facts noted on the placeholder (number checked)
                out[tax] = done
                log.debug("  %s: %s | %s | %s", tax, out[tax].status, out[tax].detail, out[tax].registered_name)
        return out

    def _sk_noted(self, names, sheet_had_number):
        res = self._sk(names)
        if sheet_had_number and res.status != MANUAL:
            res.detail += " (Saskatchewan's registry searches by name only; the PST number in the sheet wasn't used.)"
        return res

    def _guard(self, fn, *args):
        key = (fn.__name__, repr(args))
        busy_key = ("in progress",) + key
        while True:
            if key in self._cache:
                log.debug("Reusing earlier result for %s%s", fn.__name__, args)
                return self._cache[key].copy()  # own facts dict: later notes on this row mustn't leak into others
            # Parallel workers share the cache: if another one is already looking this up (duplicate rows are
            # often next to each other), wait for its answer instead of asking the site again.
            mine = threading.Event()
            theirs = self._cache.setdefault(busy_key, mine)  # atomic: exactly one thread gets to look it up
            if theirs is mine:
                break
            theirs.wait(timeout=900)
            # Its answer is in the cache now, unless it failed (errors aren't cached): then try ourselves.
        try:
            result = fn(*args)
        except BadNumber as e:
            result = Result(INVALID, str(e))
        except Exception as e:  # network trouble etc. shouldn't stop the whole run
            # One line on screen; the full traceback goes to the log file only.
            log.error("%s%s failed: %s: %s", fn.__name__, args, type(e).__name__, str(e).splitlines()[0] if str(e) else "")
            log.debug("Traceback for the failure above", exc_info=True)
            result = Result(ERROR, f"{type(e).__name__}: {e} (see log file)")
        finally:
            # A failed lookup is worth retrying later; except Saskatchewan's, which has already been tried twice and
            # whose searches are limited to 100 a day.
            if "result" in locals() and (result.status != ERROR or fn.__name__ == "_sk_noted"):
                self._cache[key] = result
            self._cache.pop(busy_key, None)
            mine.set()
        return result.copy()

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
            return self.qst.check(number, suffix_assumed="TQ" not in str(value).upper(),
                                  judge=lambda res: self._qst_on_date(res, date))
        if kind == "NR":
            return self.qst.check_nr(number)
        # 9-digit QST numbers belong to listed financial institutions; the CRA administers their QST.
        res = self.gst.check(number, names, date)
        res.detail = "9-digit QST number (listed financial institution, administered by the CRA). " + res.detail
        return res

    @staticmethod
    def _qst_on_date(res, date):
        """Revenu Québec gives the current status and the date it took effect (DateStatut). Read against the
        payment date: registered since a later date means not registered then; cancelled since a later date
        means it was still registered then (a cancelled account was registered before)."""
        import datetime as dt

        def day(key):
            try:
                return dt.date.fromisoformat(str(res.facts.get(key) or "")[:10])
            except ValueError:
                return None

        since, ended = day("registered_since"), day("registration_end")
        if res.status == REGISTERED and since and date < since:
            return Result(NOT_REGISTERED, f"Revenu Québec: registered for QST only since {since}, after {date}.",
                          res.registered_name, facts={**res.facts, "registered_after_payment": True,
                                                      "date_checked": str(date)})
        if res.status == NOT_REGISTERED and ended and date < ended:
            return Result(REGISTERED, f"Revenu Québec: registered on {date}; the registration was cancelled "
                                      f"effective {ended}.", res.registered_name,
                          facts={**res.facts, "cancelled_after_payment": True, "date_checked": str(date)})
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


class Deferred(Result):
    """Placeholder for a browser check a worker thread couldn't run (see RowChecker.run_deferred)."""

    def __init__(self, method, args):
        super().__init__("DEFERRED", "browser check still to run")
        self.method, self.args = method, args


class DryRun:
    """Stand-in for every checker, for --check-columns: shows what would be looked up, without going online."""

    WOULD = "WOULD CHECK"

    def check(self, *args, **kwargs):
        shown = ", ".join([*(repr(a) for a in args), *(f"{k}={v!r}" for k, v in kwargs.items() if v)])
        return Result(self.WOULD, shown)

    def check_nr(self, number):
        return Result(self.WOULD, f"NR list lookup for {number}")


class Offline:
    """Stand-in for every checker for --offline runs: rows that pass every offline check (number format,
    CRA check digit, routing) are marked NOT LOOKED UP instead of being sent to a government site."""

    DETAIL = "Passed the offline checks (format and check digit); not looked up because this was an offline run."

    def check(self, *args, **kwargs):
        return Result(NOT_LOOKED_UP, self.DETAIL)

    def check_nr(self, number):
        return Result(NOT_LOOKED_UP, self.DETAIL)
