"""GST/HST checks: the CRA GST/HST Registry, backed by CRA's published simplified-registrant list."""

import datetime as dt
import html
import logging
import re

from .http import Throttle, new_session
from .result import INVALID, NOT_CONFIRMED, NOT_REGISTERED, REGISTERED, Result

log = logging.getLogger(__name__)

REGISTRY = "https://www.businessregistration-inscriptionentreprise.gc.ca/ebci/brom/registry/pub/"
SIMPLIFIED_LIST = (
    "https://www.canada.ca/en/revenue-agency/services/tax/businesses/topics/gst-hst-businesses/"
    "digital-economy-gsthst/confirming-simplified-gst-hst-account-number.html"
)
SIMPLIFIED_NOTE = (
    "Simplified GST/HST registrant (non-resident digital seller): GST/HST they charge is not "
    "eligible for input tax credits, so give them your GST/HST number so they don't charge it."
)


def _text(fragment):
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment))).strip()


def name_variants(*names):
    """The CRA registry ignores case and punctuation but not extra words, so try a few spellings."""
    seen, out = set(), []
    for name in names:
        if not name:
            continue
        name = str(name).strip()
        for candidate in (name, re.sub(r"^the\s+", "", name, flags=re.I)):
            key = re.sub(r"[^a-z0-9]", "", candidate.lower())
            if key and key not in seen:
                seen.add(key)
                out.append(candidate)
    return out


class SimplifiedList:
    """CRA's list of simplified GST/HST registrants, downloaded once per run."""

    def __init__(self, session):
        self.session = session
        self._rows = None
        self.error = None

    def lookup(self, bn9, date):
        """The entry for this business number that applies on `date`, else its most recent one."""
        if self._rows is None:
            self._rows = {}
            try:
                r = self.session.get(SIMPLIFIED_LIST, timeout=60)
                r.raise_for_status()
                self._rows = self.parse(r.text)
                log.info("Loaded CRA simplified GST/HST list: %d business numbers", len(self._rows))
                if not self._rows:
                    self.error = "CRA simplified registrant list was empty; its page layout may have changed"
            except Exception as e:  # the main registry check still works without it
                self.error = f"couldn't download CRA simplified registrant list: {e}"
            if self.error:
                log.warning(self.error)
        entries = self._rows.get(bn9)
        if not entries:
            return None
        for e in entries:
            if (not e["registered"] or e["registered"] <= date) and (not e["deregistered"] or e["deregistered"] > date):
                return e
        return max(entries, key=lambda e: e["registered"] or dt.date.min)

    @staticmethod
    def parse(page):
        """Map business number -> list of accounts (a business can be listed more than once)."""
        rows = {}
        for tr in re.findall(r"<tr.*?</tr>", page, re.S):
            cells = [_text(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
            if len(cells) < 5:
                continue
            m = re.match(r"(\d{9})", cells[2].replace(" ", ""))
            if m:
                rows.setdefault(m.group(1), []).append({
                    "legal_name": cells[0],
                    "trade_name": "" if cells[1] == "-" else cells[1],
                    "number": cells[2],
                    "registered": _parse_date(cells[3]),
                    "deregistered": _parse_date(cells[4]),
                })
        return rows


REGISTRY_RESULTS = {
    "GST/HST number registered on this transaction date": "registered",
    "GST/HST number was not registered on this transaction date": "not_registered",
    "Insufficient information entered": "no_match",
}


def parse_registry_result(page):
    """Read a CRA registry results page. Returns (outcome, CRA's message); raises on anything unexpected."""
    m = re.search(r"<strong>Result</strong>\s*</div>\s*<div[^>]*>(.*?)</div>", page, re.S)
    if m:
        msg = _text(m.group(1))
        for prefix, outcome in REGISTRY_RESULTS.items():
            if msg.startswith(prefix):
                return outcome, msg
        raise RuntimeError(f"unexpected CRA registry result: {msg}")
    errors = [_text(e) for e in re.findall(r'<span class="[^"]*label-error[^"]*">(.*?)</span>', page, re.S)]
    if "GST/HST number is not valid." in errors:
        return "invalid", "GST/HST number is not valid."
    raise RuntimeError("CRA registry rejected the search: " + ("; ".join(errors) or "no result on page"))


def _parse_date(s):
    try:
        return dt.datetime.strptime(s.strip(), "%B %d, %Y").date()
    except ValueError:
        return None


class GstChecker:
    def __init__(self, delay=1.5):
        self.session = new_session()
        self.throttle = Throttle(delay)
        self.simplified = SimplifiedList(self.session)

    def registry_lookup(self, bn9, name, date):
        """One CRA registry search. Returns 'registered', 'not_registered', 'no_match' or 'invalid'."""
        self.throttle.wait()
        form = self.session.get(REGISTRY + "reg_01_Ld.action", timeout=30)
        form.raise_for_status()
        token = re.search(r'name="token" value="([^"]+)"', form.text).group(1)
        r = self.session.post(
            REGISTRY + "reg_01_Sbmt.action",
            data={
                "struts.token.name": "token",
                "token": token,
                "businessNumber": bn9,
                "businessName": name[:175],
                "requestDate": date.isoformat(),
                "reg.label.submit": "Search",
            },
            timeout=30,
        )
        r.raise_for_status()
        outcome, message = parse_registry_result(r.text)
        log.debug("CRA registry bn=%s name=%r date=%s -> %s (%s)", bn9, name, date, outcome, message)
        return outcome

    def check(self, bn9, names, date):
        today = dt.date.today()
        future_note = ""
        if date > today:
            # The registry refuses future dates; confirm as of today instead.
            future_note = f"Transaction date {date} is in the future, so registration was checked as of today."
            date = today
        listed = self.simplified.lookup(bn9, date)
        if listed:
            log.debug("bn=%s is on the simplified list: %s", bn9, listed)
        result = self._check(bn9, names, date, listed)
        if future_note:
            result.detail = f"{future_note} {result.detail}"
        return result

    def _check(self, bn9, names, date, listed):
        candidates = name_variants(*names, *(listed and (listed["legal_name"], listed["trade_name"]) or ()))
        note = SIMPLIFIED_NOTE if listed else ""

        for name in candidates:
            outcome = self.registry_lookup(bn9, name, date)
            if outcome == "registered":
                detail = f"CRA GST/HST Registry confirms registration on {date} (matched name '{name}')."
                return Result(REGISTERED, " ".join(filter(None, [detail, note])), listed and listed["legal_name"] or name)
            if outcome == "invalid":
                return Result(INVALID, "CRA says this GST/HST number is not valid.")
            if outcome == "not_registered":
                # CRA gives this answer about the number and date whatever name is entered,
                # so it's definite about the number but says nothing about the name.
                return Result(NOT_REGISTERED, f"CRA: this number was not registered for GST/HST on {date}.")

        if listed:
            dereg = listed["deregistered"]
            if dereg and dereg <= date:
                return Result(NOT_REGISTERED, f"Simplified GST/HST registration ended {dereg}.", listed["legal_name"])
            if listed["registered"] and listed["registered"] > date:
                return Result(NOT_REGISTERED, f"Simplified GST/HST registration only starts {listed['registered']}.", listed["legal_name"])
            return Result(REGISTERED, f"Found on CRA's simplified registrant list. {note}", listed["legal_name"])

        tried = "; ".join(f"'{n}'" for n in candidates) or "(no name given)"
        return Result(
            NOT_CONFIRMED,
            f"CRA could not match this number with name {tried} on {date}. Either the number isn't "
            "registered or the legal name differs from CRA's records (CRA doesn't say which). "
            "Check the supplier's exact legal name and re-run.",
        )
