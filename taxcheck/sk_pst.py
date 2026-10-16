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
import json
import logging
import re
import time
from pathlib import Path

from .browser import BrowserUnavailable
from .result import ERROR, MANUAL, NOT_CONFIRMED, REGISTERED, Result

log = logging.getLogger(__name__)

SK_REGISTRY = "https://www.sets.saskatchewan.ca/rptp/portal/footer/pst-registry"
SEARCH_BOX = "#pstSearch"
SEARCH_LIMIT = re.compile(r"(?:search|daily)\s+limit|limit\s+(?:has\s+been\s+)?(?:reached|exceeded)|"
                          r"exceeded\s+the\s+(?:maximum|number)|too\s+many\s+searches", re.I)
DAILY_LIMIT = 100  # "the number of searches in a 24-hour period is restricted to 100" (the registry's own page)
NO_RESULTS = re.compile(r"no (records|results|matches|businesses)|not found|could not find|0 results", re.I)


def _key(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


_LEGAL_ENDING = re.compile(r"[\s,.]+(?:inc|incorporated|ltd|limited|corp|corporation|co|company|ulc|llc|lp|llp|"
                           r"ltee|ltée|limitee|limitée)\.?$", re.I)
MAX_TERMS = 2  # the registry allows 100 searches a day, so at most 2 per supplier


def _cleaned(name):
    """The vendor name without vendor-master markings ("*", bracketed notes)."""
    term = re.sub(r"\([^)]*\)?|\[[^\]]*\]?|[*#]+", " ", str(name or ""))
    return re.sub(r"\s+", " ", term).strip(" -,;:")


def search_term(name):
    """The portion of a vendor name to search for: no vendor-master markings ("*", bracketed notes), no leading
    "The", no legal ending. The registry matches any portion of a name and ignores punctuation, spacing and case
    (its own instructions), so "1 STOP VACUUM CENTRE" also finds "1 STOP VACUUM CENTRE LTD."."""
    term = re.sub(r"^the\s+", "", _cleaned(name), flags=re.I)
    while _LEGAL_ENDING.search(term):
        term = _LEGAL_ENDING.sub("", term).strip(" -,;:")
    return term


def search_terms(*names):
    """Names to search, best first, at most MAX_TERMS. The registry needs at least 4 characters."""
    out, seen = [], set()
    for n in names:
        # Trimmed to a portion, unless that leaves under 4 characters ("The ABC Ltd" -> "ABC" is too short to
        # search, so "The ABC" is searched instead).
        term = next((t for t in (search_term(n), re.sub(r"^the\s+", "", _cleaned(n), flags=re.I), _cleaned(n))
                     if len(_key(t)) >= 4), "")
        if term and _key(term) not in seen:
            seen.add(_key(term))
            out.append(term)
    return out[:MAX_TERMS]


def _core(text):
    """A name without punctuation, spacing, case or legal ending, for comparing a result with the vendor."""
    return _key(search_term(text))


def classify(rows, page_text, names):
    """Decide what a results page says about this supplier.

    rows: table rows as lists of cell text. Returns (status, detail, matched_row_text).
    """
    # A portion search returns similar names too ("ACME" finds "ACME SUPPLY"): only a result whose name is the
    # vendor's (apart from punctuation, spacing, case and legal ending) is this supplier.
    wanted = {_core(n) for n in names if n and len(_core(n)) >= 4}
    matching = [" | ".join(c for c in r if c) for r in rows if any(_core(c) in wanted for c in r if c)]
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
    if SEARCH_LIMIT.search(page_text):
        raise DailyLimitReached("Saskatchewan's registry says its daily search limit has been reached")
    if NO_RESULTS.search(page_text):
        return NOT_CONFIRMED, "Saskatchewan PST registry found no business under this name.", ""
    other = [" | ".join(c for c in r if c) for r in rows if any(c for c in r)][:5]
    if other:
        return NOT_CONFIRMED, "No result matched the supplier's name. Registry returned: " + "; ".join(other), ""
    return NOT_CONFIRMED, "Couldn't read the registry's answer.", ""


class DailyLimitReached(RuntimeError):
    """The registry's 100 searches per 24 hours are used up."""


class CaptchaEverySearch(RuntimeError):
    """The registry asked for the CAPTCHA again almost straight away: bulk checking isn't possible."""


class SkPstAssistant:
    """Saskatchewan searches in a visible browser, after a person passes the registry's CAPTCHA once.

    The CAPTCHA guards only the terms page: in the registry's own pstLookup.js, the terms form's Next button
    requires grecaptcha.getResponse(), while the search form's submit only checks that the name has at least
    4 characters. So one CAPTCHA covers the whole browser session. check_suppliers runs every Saskatchewan
    search straight after the CAPTCHA (prefetch), so the session doesn't sit idle and time out during a long run.

    If the registry shows the terms page again, the person is asked again (the session may have expired). If
    that happens after fewer than REASK_MIN searches, the CAPTCHA is evidently needed per search, so bulk
    checking stops and the remaining Saskatchewan rows are left for a manual check.
    """

    REASK_MIN = 3

    def __init__(self, browser, wait_minutes=5, counter_file=None, daily_limit=DAILY_LIMIT):
        self.browser = browser  # must be headed (visible) so a person can complete the CAPTCHA
        # Times of searches in the last 24 hours, kept between runs, because the registry allows only 100 a day.
        self.counter_file = Path(counter_file) if counter_file else None
        self.daily_limit = daily_limit
        self._searched_at = self._load_counter()
        self.wait_ms = int(wait_minutes * 60 * 1000)
        self.page = None
        self.unavailable = None
        self.captchas = 0  # times the person was asked to complete it
        self.searches_since_captcha = 0

    def _search_box_visible(self):
        box = self.page.locator(SEARCH_BOX)
        return box.count() > 0 and box.first.is_visible()

    def _back_to_search(self):
        """From a results page back to the search box, without reloading the terms page if possible."""
        if self._search_box_visible():
            return True
        again = self.page.get_by_role("link", name=re.compile(r"new search|search again|back", re.I)).or_(
            self.page.get_by_role("button", name=re.compile(r"new search|search again|back", re.I)))
        if again.count():
            again.first.click()
            self.page.wait_for_load_state("domcontentloaded")
            if self._search_box_visible():
                return True
        self.page.goto(SK_REGISTRY, wait_until="domcontentloaded")
        return self._search_box_visible()

    def _ready(self):
        """Make sure the search box is on screen, asking the person to pass the CAPTCHA if needed."""
        if self.page is None:
            self.page = self.browser.new_persistent_page()
            self.page.goto(SK_REGISTRY, wait_until="domcontentloaded")
            if self._search_box_visible():
                return
        elif self._back_to_search():
            return
        if self.captchas and self.searches_since_captcha < self.REASK_MIN:
            raise CaptchaEverySearch(
                f"Saskatchewan's registry asked for the CAPTCHA again after {self.searches_since_captcha} "
                "search(es), so it can't be checked in bulk")
        self.captchas += 1
        log.warning(
            "\n>>> Saskatchewan PST registry%s: in the browser window, tick 'I agree', complete 'I'm not a robot', "
            "then click Next. Waiting up to %s; the Saskatchewan searches then run by themselves.",
            " (asking again: the registry's session expired)" if self.captchas > 1 else "",
            f"{self.wait_ms // 60000} minutes" if self.wait_ms >= 60000 else f"{self.wait_ms // 1000} seconds",
        )
        self.page.bring_to_front()
        self.page.locator(SEARCH_BOX).wait_for(state="visible", timeout=self.wait_ms)
        self.searches_since_captcha = 0
        log.info("Saskatchewan search page is open; continuing automatically.")

    def _load_counter(self):
        try:
            return [float(t) for t in json.loads(self.counter_file.read_text())] if self.counter_file else []
        except (OSError, ValueError, TypeError):
            return []

    def searches_left(self):
        cutoff = time.time() - 24 * 3600
        self._searched_at = [t for t in self._searched_at if t > cutoff]
        return max(self.daily_limit - len(self._searched_at), 0)

    def _count_search(self):
        self._searched_at.append(time.time())
        if self.counter_file:
            try:
                self.counter_file.parent.mkdir(parents=True, exist_ok=True)
                self.counter_file.write_text(json.dumps(self._searched_at))
            except OSError as e:
                log.debug("Couldn't save the Saskatchewan search count: %s", e)

    def _limit_message(self):
        again = dt.datetime.fromtimestamp(min(self._searched_at) + 24 * 3600) if self._searched_at else None
        when = f" More searches are possible from {again:%Y-%m-%d %H:%M}." if again else ""
        return (f"Saskatchewan's registry allows {self.daily_limit} searches per 24 hours and they're used up.{when} "
                f"Re-run later with --resume, or search by hand at {SK_REGISTRY}")

    def _search(self, term):
        if not self.searches_left():
            raise DailyLimitReached("no searches left")
        self._count_search()
        page = self.page
        page.fill(SEARCH_BOX, term)
        # Only the PST search form's own button: every SETS page also has a site-wide "Search SETS" button.
        form = page.locator(f"form:has({SEARCH_BOX})")
        button = form.get_by_role("button", name=re.compile("search", re.I)).or_(
            form.get_by_role("link", name=re.compile(r"^\s*search\s*$", re.I))
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
        """A screenshot and the page itself (HTML) next to the log, so the results layout can be checked."""
        if not self.browser.debug_dir:
            return ""
        stem = self.browser.debug_dir / f"sk_{re.sub(r'[^A-Za-z0-9]+', '_', label)[:40]}_{dt.datetime.now():%H%M%S}"
        try:
            self.page.screenshot(path=f"{stem}.png", full_page=True)
            Path(f"{stem}.html").write_text(self.page.content(), encoding="utf-8")
            return f" Screenshot: {Path(stem).name}.png"
        except Exception:
            return ""

    def check(self, names):
        if self.unavailable:
            return Result(MANUAL, self.unavailable)
        terms = search_terms(*names)
        if not terms:
            return Result(MANUAL, "Saskatchewan's registry needs at least 4 letters of the business name (apart from "
                                  f"Ltd/Inc); the vendor name is too short. Search by hand at {SK_REGISTRY}")
        try:
            last = None
            for term in terms:
                self._ready()
                rows, text = self._search(term)
                self.searches_since_captcha += 1
                status, detail, row = classify(rows, text, names)
                last = (status, detail, term)
                if status == REGISTERED:
                    return Result(status, detail + self._evidence(term), row)
            status, detail, term = last
            return Result(status, f"{detail} (searched: {', '.join(terms)}).{self._evidence(term)}")
        except DailyLimitReached:
            self.unavailable = self._limit_message()
            log.warning("Saskatchewan: %s", self.unavailable)
            return Result(MANUAL, self.unavailable)
        except CaptchaEverySearch as e:
            self.unavailable = f"{e}. Search by hand at {SK_REGISTRY}"
            log.warning(self.unavailable)
            return Result(MANUAL, self.unavailable)
        except BrowserUnavailable as e:
            self.unavailable = f"Couldn't open a browser for the assisted check: {e} Search by hand at {SK_REGISTRY}"
            return Result(MANUAL, self.unavailable)
        except Exception as e:
            if "Timeout" in type(e).__name__ and self.page is not None and not self.page.locator(SEARCH_BOX).count():
                # Nobody completed the CAPTCHA in time; don't make every remaining row wait again.
                self.unavailable = f"The CAPTCHA wasn't completed in time. Search by hand at {SK_REGISTRY}"
                log.warning("Saskatchewan: %s", self.unavailable)
                return Result(MANUAL, self.unavailable)
            log.exception("Saskatchewan assisted check failed")
            return Result(ERROR, f"Saskatchewan assisted check failed: {str(e).splitlines()[0]} (see log file)")
