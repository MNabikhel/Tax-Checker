"""Saskatchewan PST checks through the SETS 'PST On-Line Registry', with a person in the loop.

The registry's terms page has a Google reCAPTCHA, so it can't be fully automated. In assisted mode
(`--sk-assist`) a visible browser window opens; the person ticks the terms box, completes the
"I'm not a robot" check and clicks Next. After that, the search page (a business-name box) is an
ordinary form, and the script runs every Saskatchewan search in that same session.

The registry searches by business name only and confirms whether the business holds a vendor's
licence or a registered consumer number, without showing the number.

NOTE: the results page couldn't be inspected while building this (it sits behind the CAPTCHA), so
results are read conservatively: REGISTERED only when a result row contains the supplier's name and
names a licence type. Anything else is NOT CONFIRMED with a screenshot saved next to the log. The
full page text is logged, so the reading can be tightened after the first real run.
"""

import datetime as dt
import logging
import re

from .browser import BrowserUnavailable
from .result import ERROR, MANUAL, NOT_CONFIRMED, REGISTERED, Result

log = logging.getLogger(__name__)

SK_REGISTRY = "https://www.sets.saskatchewan.ca/rptp/portal/footer/pst-registry"
SEARCH_BOX = "#pstSearch"
NO_RESULTS = re.compile(r"no (records|results|matches|businesses)|not found|could not find|0 results", re.I)


def _key(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def search_terms(*names):
    """Names to search, best first. The registry needs at least 4 characters."""
    out, seen = [], set()
    for n in names:
        if not n:
            continue
        for candidate in (str(n).strip(), re.sub(r"^the\s+", "", str(n).strip(), flags=re.I)):
            if len(candidate.replace(" ", "")) >= 4 and _key(candidate) not in seen:
                seen.add(_key(candidate))
                out.append(candidate)
    return out


def classify(rows, page_text, names):
    """Decide what a results page says about this supplier.

    rows: table rows as lists of cell text. Returns (status, detail, matched_row_text).
    """
    wanted = [_key(n) for n in names if n and len(_key(n)) >= 4]
    matching = [" | ".join(c for c in r if c) for r in rows if any(w in _key(" ".join(r)) for w in wanted)]
    for row in matching:
        low = row.lower()
        if "vendor" in low:
            return REGISTERED, "Saskatchewan PST registry: holds a vendor's licence.", row
        if "consumer" in low:
            return (
                REGISTERED,
                "Saskatchewan PST registry: holds a registered consumer number (it buys taxable goods for its own "
                "use; this number can't be used for tax-exempt purchases for resale).",
                row,
            )
    if matching:
        return NOT_CONFIRMED, "Found a matching name but couldn't read its licence type: " + "; ".join(matching[:3]), ""
    if NO_RESULTS.search(page_text):
        return NOT_CONFIRMED, "Saskatchewan PST registry found no business under this name.", ""
    other = [" | ".join(c for c in r if c) for r in rows if any(c for c in r)][:5]
    if other:
        return NOT_CONFIRMED, "No result matched the supplier's name. Registry returned: " + "; ".join(other), ""
    return NOT_CONFIRMED, "Couldn't read the registry's answer.", ""


class SkPstAssistant:
    def __init__(self, browser, wait_minutes=5):
        self.browser = browser  # must be headed (visible) so a person can complete the CAPTCHA
        self.wait_ms = int(wait_minutes * 60 * 1000)
        self.page = None
        self.unavailable = None

    def _ready(self):
        """Make sure the search box is on screen, asking the person to pass the CAPTCHA if needed."""
        if self.page is None:
            self.page = self.browser.new_persistent_page()
        if self.page.locator(SEARCH_BOX).count() and self.page.locator(SEARCH_BOX).is_visible():
            return
        self.page.goto(SK_REGISTRY, wait_until="domcontentloaded")
        if self.page.locator(SEARCH_BOX).count():
            return
        log.warning(
            "\n>>> Saskatchewan PST registry: in the browser window that just opened, tick 'I agree', "
            "complete 'I'm not a robot', then click Next. Waiting up to %s...",
            f"{self.wait_ms // 60000} minutes" if self.wait_ms >= 60000 else f"{self.wait_ms // 1000} seconds",
        )
        self.page.locator(SEARCH_BOX).wait_for(state="visible", timeout=self.wait_ms)
        log.info("Saskatchewan search page is open; continuing automatically.")

    def _search(self, term):
        page = self.page
        page.fill(SEARCH_BOX, term)
        button = page.get_by_role("button", name=re.compile("search", re.I)).or_(
            page.get_by_role("link", name=re.compile(r"^\s*search\s*$", re.I))
        )
        if button.count():
            button.first.click()
        else:
            page.locator(SEARCH_BOX).press("Enter")
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_load_state("networkidle")
        rows = page.eval_on_selector_all(
            "table tr", "trs => trs.map(tr => [...tr.querySelectorAll('th,td')].map(c => c.innerText.trim()))"
        )
        text = page.inner_text("body")
        log.debug("SK registry search %r -> rows %s | page text: %s", term, rows, re.sub(r"\s+", " ", text)[:2000])
        return rows, text

    def _evidence(self, label):
        if not self.browser.debug_dir:
            return ""
        shot = self.browser.debug_dir / f"sk_{re.sub(r'[^A-Za-z0-9]+', '_', label)[:40]}_{dt.datetime.now():%H%M%S}.png"
        try:
            self.page.screenshot(path=str(shot), full_page=True)
            return f" Screenshot: {shot.name}"
        except Exception:
            return ""

    def check(self, names):
        if self.unavailable:
            return Result(MANUAL, self.unavailable)
        terms = search_terms(*names)
        if not terms:
            return Result(ERROR, "Saskatchewan's registry needs a business name of at least 4 characters.")
        try:
            self._ready()
            last = None
            for term in terms:
                rows, text = self._search(term)
                status, detail, row = classify(rows, text, names)
                last = (status, detail, term)
                if status == REGISTERED:
                    return Result(status, detail + self._evidence(term), row)
                self._ready()  # back to the search box for the next term
            status, detail, term = last
            return Result(status, f"{detail} (searched: {', '.join(terms)}).{self._evidence(term)}")
        except BrowserUnavailable as e:
            self.unavailable = f"Couldn't open a browser for the assisted check: {e} Search by hand at {SK_REGISTRY}"
            return Result(MANUAL, self.unavailable)
        except Exception as e:
            log.exception("Saskatchewan assisted check failed")
            if "Timeout" in type(e).__name__ and self.page is not None and not self.page.locator(SEARCH_BOX).count():
                # Nobody completed the CAPTCHA in time; don't make every remaining row wait again.
                self.unavailable = f"The CAPTCHA wasn't completed in time. Search by hand at {SK_REGISTRY}"
                return Result(MANUAL, self.unavailable)
            return Result(ERROR, f"Saskatchewan assisted check failed: {str(e).splitlines()[0]} (see log file)")
