"""Manitoba RST checks through the TAXcess 'RST Registration Registry' (needs the shared browser).

The registry takes a business name plus either the 7-digit RST number or the 9-digit business number.
It confirms whether the business has an active RST registration but never shows the RST number.
The name is matched loosely (case-insensitive, partial words), but every word entered must appear in
the registered name, so shorter variants are tried if the full name doesn't match.
"""

import logging
import re

from .browser import BrowserUnavailable
from .http import SiteHealth, Throttle
from .result import ERROR, MANUAL, NOT_CONFIRMED, NOT_REGISTERED, REGISTERED, Result

log = logging.getLogger(__name__)

TAXCESS_RST = "https://taxcess.gov.mb.ca/TAXcess/?Link=RSTLookup"
HEADER = ["Status", "Legal Name(s)", "Operating As Name(s)"]


def name_variants(*names):
    """Full names first, then 'The'-less versions, then each name's first distinctive word."""
    out, seen = [], set()

    def add(candidate):
        key = re.sub(r"[^a-z0-9]", "", candidate.lower())
        if len(key) >= 2 and key not in seen:
            seen.add(key)
            out.append(candidate)

    names = [str(n).strip() for n in names if n and str(n).strip()]
    for n in names:
        add(n)
        add(re.sub(r"^the\s+", "", n, flags=re.I))
    for n in names:
        words = [w for w in re.findall(r"[\w.&'-]+", n) if w.lower() != "the"]
        if words:
            add(words[0])
    return out


def parse_results(rows):
    """rows: table rows as lists of cell text. Returns [(status, legal_name, operating_as)]."""
    found = []
    for cells in rows:
        if len(cells) == 3 and cells != HEADER and cells[0]:
            found.append(tuple(cells))
    return found


class MbRstChecker:
    def __init__(self, browser, delay=2.0):
        self.browser = browser
        self.throttle = Throttle(delay)
        self.health = SiteHealth("Manitoba TAXcess")

    def _lookup(self, name, bn9=None, rst=None):
        """One registry search. Returns a list of (status, legal name, operating name); empty if none found."""
        with self.browser.page() as page:
            try:
                page.goto(TAXCESS_RST, wait_until="networkidle")
                if rst:
                    page.get_by_label("RST Number").fill(rst)
                if bn9:
                    page.get_by_label("Business Number").fill(bn9)
                page.get_by_label("Business Name").fill(name)
                page.wait_for_load_state("networkidle")
                page.get_by_role("button", name="Next").click()
                # TAXcess's content security policy blocks wait_for_function, so wait on elements instead.
                no_results = page.get_by_text("No results found", exact=True)
                page.get_by_role("heading", name="Search Results").or_(no_results).first.wait_for(timeout=20000)
                if no_results.count():
                    return []
                rows = page.eval_on_selector_all(
                    "table tr", "trs => trs.map(tr => [...tr.querySelectorAll('th,td')].map(c => c.innerText.trim()))"
                )
                return parse_results(rows)
            except Exception:
                self.browser.record_failure(page, f"taxcess_{bn9 or rst}")
                raise

    def _lookup_with_retry(self, name, bn9, rst, attempts=3):
        for attempt in range(1, attempts + 1):
            self.throttle.wait()
            try:
                found = self._lookup(name, bn9, rst)
                log.debug("TAXcess RST name=%r bn=%s rst=%s -> %s", name, bn9, rst, found)
                return found
            except BrowserUnavailable:
                raise
            except Exception as e:
                error = str(e).splitlines()[0]
                log.warning("TAXcess lookup name=%r attempt %d/%d failed: %s", name, attempt, attempts, error)
        raise RuntimeError(f"TAXcess lookup failed after {attempts} tries: {error}")

    def check(self, names, bn9=None, rst=None):
        if not (bn9 or rst):
            return Result(ERROR, "Manitoba's registry needs the RST number or the business number (GST/HST number).")
        key = f"RST {rst}" if rst else f"BN {bn9}"
        tried = []
        for name in name_variants(*names):
            tried.append(name)
            if self.health.down:
                return Result(ERROR, self.health.down_message())
            try:
                found = self._lookup_with_retry(name, bn9, rst)
                self.health.record(True)
            except BrowserUnavailable as e:
                return Result(MANUAL, f"Couldn't check automatically: {e} Or search by hand at {TAXCESS_RST}")
            except RuntimeError as e:
                self.health.record(False, str(e))
                return Result(ERROR, f"{e} (see log file)")
            if found:
                status, legal, operating = found[0]
                registered_name = legal + (f" (operating as: {operating})" if operating else "")
                if status.lower() == "active":
                    return Result(REGISTERED, f"Manitoba RST registry: active registration for {key}.", registered_name)
                return Result(NOT_REGISTERED, f"Manitoba RST registry: status '{status}' for {key}.", registered_name)
        return Result(
            NOT_CONFIRMED,
            f"Manitoba RST registry found nothing for {key} with name "
            + "; ".join(f"'{n}'" for n in tried)
            + ". The business isn't registered for Manitoba RST, or the number/name doesn't match its records.",
        )
