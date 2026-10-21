"""Saskatchewan PST checks through the SETS 'PST On-Line Registry', with a person in the loop.

The registry's terms page has a Google reCAPTCHA, so it can't be fully automated. In assisted mode
(`--sk-assist`) a visible browser window opens; the person ticks the terms box, completes the
"I'm not a robot" check and clicks Next. After that, the search page (a business-name box) is an
ordinary form, and the script runs every Saskatchewan search in that same session.

The registry searches by business name only and confirms whether the business holds a vendor's
licence or a registered consumer number, without showing the number.

The results page (seen on a real run, 2026-10-16) has a "Business Name | Type" table (#pstLookUp), Type being a
code such as PSTVEND (vendor's licence), with "A total of 3 companies were found." / "Only 1 company found." above
it, or "No results found, please try a different search." instead. The search is a full page load (a form post),
so the results are read only once the new page has replaced the old one. REGISTERED only when a row's name is the
supplier's (apart from punctuation, spacing, case and legal ending); anything else is NOT CONFIRMED with a
screenshot saved next to the log.
"""

import datetime as dt
import json
import logging
import os
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
TOO_MANY = re.compile(r"more than \d+ companies", re.I)  # "The search resulted in more than 25 companies. ..."
FOUND = re.compile(r"total of\s+(\d+)\s+compan|only\s+(1|one)\s+company\s+found", re.I)


def found_count(page_text):
    """How many results the page says it found ("A total of 3 companies were found." / "Only 1 company found.")."""
    m = FOUND.search(page_text or "")
    return 0 if not m else (int(m.group(1)) if m.group(1) else 1)


FAILED_KEY = "__failed_batches__"  # in the answers file: search term -> batches in which the registry didn't answer


def _is_answer(rows, page_text):
    """A results page worth keeping: not the registry's limit page, and its results could be read."""
    return not SEARCH_LIMIT.search(page_text or "") and not unreadable(rows, page_text)


def unreadable(rows, page_text):
    """The page says it found businesses, but no result rows could be read."""
    return found_count(page_text) > 0 and not any(any(c for c in r) for r in rows or [])


def rows_from_html(html):
    """Result rows straight from the page's HTML (#pstLookUp's tbody), for when the table can't be read live."""
    m = re.search(r'<table[^>]*id="pstLookUp".*?<tbody[^>]*>(.*?)</tbody>', html or "", re.S | re.I)
    if not m:
        return []
    cell = lambda c: re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c)).replace("&amp;", "&").strip()
    rows = [[cell(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S | re.I)]
            for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", m.group(1), re.S | re.I)]
    return [r for r in rows if any(r)]
LOAD_TIMEOUT_S = 60


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


def _licence(row):
    """What a result row's Type says: 'vendor', 'consumer' or None. The registry shows codes ("PSTVEND")."""
    kind = " ".join(row[1:]).upper() if len(row) > 1 else ""
    words = " ".join(row).lower()
    if "VEND" in kind or "vendor" in words:
        return "vendor"
    if "CONS" in kind or "consumer" in words:
        return "consumer"
    return None


def classify(rows, page_text, names):
    """Decide what a results page says about this supplier.

    rows: result rows as lists of cell text (business name first, then type). Returns (status, detail, matched_row).
    """
    # A portion search returns similar names too ("ACME" finds "ACME SUPPLY"): only a result whose name is the
    # vendor's (apart from punctuation, spacing, case and legal ending) is this supplier.
    wanted = {_core(n) for n in names if n and len(_core(n)) >= 4}
    rows = [[re.sub(r"\s+", " ", c).strip() for c in r] for r in rows]
    matching = [r for r in rows if r and r[0] and _core(r[0]) in wanted]
    text = lambda r: " | ".join(c for c in r if c)
    kinds = {}
    for r in matching:
        kinds.setdefault(_licence(r), r)
    if "vendor" in kinds:
        return REGISTERED, "Saskatchewan PST registry: holds a vendor's licence.", text(kinds["vendor"])
    if "consumer" in kinds:
        return (
            REGISTERED,
            "Saskatchewan PST registry: holds a registered consumer number (it buys taxable goods for its own "
            "use; this number can't be used for tax-exempt purchases for resale).",
            text(kinds["consumer"]),
        )
    if matching:
        return NOT_CONFIRMED, ("Found the name but couldn't read its licence type: "
                               + "; ".join(text(r) for r in matching[:3])), ""
    if SEARCH_LIMIT.search(page_text):
        raise DailyLimitReached("Saskatchewan's registry says its daily search limit has been reached")
    if TOO_MANY.search(page_text):
        return NOT_CONFIRMED, ("More than 25 businesses in Saskatchewan's registry contain this name, so it shows "
                               "none of them."), ""
    if NO_RESULTS.search(page_text) and not rows:
        return NOT_CONFIRMED, "Saskatchewan PST registry found no business under this name.", ""
    other = [text(r) for r in rows if any(r)][:5]
    if other:
        return NOT_CONFIRMED, ("No result is exactly the supplier's name; similar names in the registry: "
                               + "; ".join(other)), ""
    if unreadable(rows, page_text):  # results shown but not read: a failure to retry, not an answer
        return ERROR, f"The registry found {found_count(page_text)} result(s), but they couldn't be read.", ""
    return NOT_CONFIRMED, "Couldn't read the registry's answer.", ""


class DailyLimitReached(RuntimeError):
    """The registry's 100 searches per 24 hours are used up."""


class ResultsNotLoaded(RuntimeError):
    """A search was submitted but no results page came back."""


class CaptchaEverySearch(RuntimeError):
    """The registry asked for the CAPTCHA again almost straight away, twice running: bulk checking isn't possible."""


class CaptchaNotCompleted(RuntimeError):
    """Nobody completed the CAPTCHA within the wait."""


class WindowClosed(RuntimeError):
    """The Saskatchewan browser window was closed during the run."""


# Problems that end the Saskatchewan searches for the whole run. Anything else affects only the one supplier.
_STOPS_THE_RUN = (DailyLimitReached, CaptchaEverySearch, CaptchaNotCompleted, WindowClosed, BrowserUnavailable)


class SkPstAssistant:
    """Saskatchewan searches in a visible browser, after a person passes the registry's CAPTCHA once.

    The CAPTCHA guards only the terms page: in the registry's own pstLookup.js, the terms form's Next button
    requires grecaptcha.getResponse(), while the search form's submit only checks that the name has at least
    4 characters. So one CAPTCHA covers the whole browser session. check_suppliers runs every Saskatchewan
    search straight after the CAPTCHA (prefetch), so the session doesn't sit idle and time out during a long run.

    If the registry shows the terms page again, the person is asked again (the session may have expired, or a
    search hit an error page). Only if that happens twice running after fewer than REASK_MIN searches each is
    the CAPTCHA evidently needed per search; then bulk checking stops and the remaining Saskatchewan rows are left
    for a manual check. A search that fails (error page, no answer) is tried once more, then that supplier is
    marked ERROR and the searches carry on with the next one.
    """

    REASK_MIN = 3
    # Answers are kept 60 days: Saskatchewan's 100-a-day limit means a big list takes days of batches ("Run
    # Saskatchewan Batch.bat"), and every later run reuses what's been searched (no CAPTCHA, no daily searches).
    ANSWER_AGE = 60 * 24 * 3600  # covers 10-20 batch days with weekends and missed days
    MAX_FAILS = 2  # a search the registry failed to answer in this many batches is left for a search by hand
    STOP_AFTER_FAILURES = 3  # this many suppliers failing in a row: the site is down, stop searching for this run

    def __init__(self, browser, wait_minutes=5, counter_file=None, daily_limit=DAILY_LIMIT, answers_file=None):
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
        self._per_captcha = []  # searches answered after each earlier CAPTCHA
        # The registry's answer to each search term, kept a day between runs (see ANSWER_AGE).
        self.answers_file = Path(answers_file) if answers_file else None
        self._answers, self._failed = self._load_answers()
        self._failed_now = set()  # terms whose failure this run has counted (a supplier can be asked about twice)
        self._submitted = None  # the term last actually sent to the registry
        self._failures_in_a_row = 0
        self.reused_at = None  # when the answer just returned was searched, if it came from an earlier run

    def _load_answers(self):
        """({search term: saved answer}, {search term: failed batches}). Answers that aren't answers (a limit page,
        results that couldn't be read) are never used, even if an earlier version saved them."""
        try:
            data = json.loads(self.answers_file.read_text(encoding="utf-8")) if self.answers_file else {}
            failed = {t: int(n) for t, n in (data.pop(FAILED_KEY, None) or {}).items()}
            now = time.time()
            answers = {t: a for t, a in data.items() if isinstance(a, dict) and now - float(a["at"]) < self.ANSWER_AGE
                       and _is_answer(a["rows"], a["text"])}
            return answers, failed
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return {}, {}

    def _save(self):
        if not self.answers_file:
            return
        try:
            self.answers_file.parent.mkdir(parents=True, exist_ok=True)
            # Written whole, then swapped in: a stop mid-write mustn't lose weeks of batches' answers.
            tmp = self.answers_file.with_name(self.answers_file.name + ".tmp")
            tmp.write_text(json.dumps({**self._answers, FAILED_KEY: self._failed}, default=str), encoding="utf-8")
            os.replace(tmp, self.answers_file)
        except (OSError, TypeError, ValueError):
            pass  # only saves searches in later batches

    def gave_up(self, names):
        """The registry failed to answer this supplier's first search in MAX_FAILS batches: search it by hand."""
        return any(self._failed.get(t, 0) >= self.MAX_FAILS for t in search_terms(*names))

    def answered(self, names):
        """True when this supplier's answer can be given from saved answers alone (no browser, no search): every
        term it would search is saved, or an earlier saved term already finds it registered."""
        terms = search_terms(*names)
        if not terms or self.gave_up(names):
            return True  # name too short, or left for a search by hand: nothing to search
        try:
            for term in terms:
                saved = self._answers.get(term)
                if not saved:
                    return False
                if classify(saved["rows"], saved["text"], names)[0] == REGISTERED:
                    return True
        except Exception:
            return False
        return True

    def _save_answer(self, term, rows, text):
        if not self.answers_file or not _is_answer(rows, text):
            return
        self._answers[term] = {"at": time.time(), "rows": rows, "text": text[:50000]}
        self._failed.pop(term, None)
        self._save()

    def _note_failure(self, term):
        """The registry didn't answer a search that was actually sent: counted once per batch per term."""
        if term in self._failed_now:
            return
        self._failed_now.add(term)
        self._failed[term] = self._failed.get(term, 0) + 1
        self._save()

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
        if self.captchas:
            self._per_captcha.append(self.searches_since_captcha)
            if len(self._per_captcha) >= 2 and max(self._per_captcha[-2:]) < self.REASK_MIN:
                raise CaptchaEverySearch(
                    "Saskatchewan's registry asked for the CAPTCHA again after only "
                    f"{' and then '.join(map(str, self._per_captcha[-2:]))} search(es), so it can't be checked in bulk")
        self.captchas += 1
        log.warning(
            "\n>>> Saskatchewan PST registry%s: in the browser window, tick 'I agree', complete 'I'm not a robot', "
            "then click Next. Waiting up to %s; the Saskatchewan searches then run by themselves.",
            " (asking again: the registry's session expired)" if self.captchas > 1 else "",
            f"{self.wait_ms // 60000} minutes" if self.wait_ms >= 60000 else f"{self.wait_ms // 1000} seconds",
        )
        self.page.bring_to_front()
        try:
            self.page.locator(SEARCH_BOX).wait_for(state="visible", timeout=self.wait_ms)
        except Exception as e:
            if self.page.is_closed():
                raise WindowClosed("the Saskatchewan browser window was closed") from e
            if "Timeout" in type(e).__name__:
                raise CaptchaNotCompleted("the CAPTCHA wasn't completed in time") from e
            raise
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
        # Searches free up one at a time, 24 hours after each was made: a few from 24 hours after the first, the
        # whole day's worth from 24 hours after the last.
        when = ""
        if self._searched_at:
            first, full = (dt.datetime.fromtimestamp(t + 24 * 3600) for t in (min(self._searched_at), max(self._searched_at)))
            when = f" The next full batch is possible from {full:%Y-%m-%d %H:%M} (a few searches from {first:%H:%M})."
        return (f"Saskatchewan's registry allows {self.daily_limit} searches per 24 hours and they're used up.{when} "
                f"Double-click \"Run Saskatchewan Batch.bat\" then for the next batch, or search by hand at {SK_REGISTRY}")

    # Result rows: from DataTables' own data when it has taken over the table (it keeps only one page of rows on
    # screen), otherwise from the page. Header rows (no <td>) are left out.
    _READ_ROWS = """() => {
        const clean = s => s.replace(/\\s+/g, ' ').trim();
        const table = document.querySelector('#pstLookUp');
        const $ = window.jQuery;
        // Both ways, keeping whichever has more rows: DataTables holds every page of a paged table, but on the
        // user's PC (Edge) it once reported no rows while the page showed three.
        let fromTable = [];
        try {
            if (table && $ && $.fn && $.fn.dataTable && $.fn.dataTable.isDataTable(table)) {
                const cell = h => { const d = document.createElement('div'); d.innerHTML = String(h); return clean(d.textContent); };
                fromTable = $(table).DataTable().rows().data().toArray()
                    .map(r => (Array.isArray(r) ? r : Object.values(r || {})).map(cell)).filter(r => r.some(c => c));
            }
        } catch (e) { fromTable = []; }
        const trs = table ? table.querySelectorAll('tbody tr') : document.querySelectorAll('table tr');
        const fromPage = [...trs].map(tr => [...tr.querySelectorAll('td')].map(c => clean(c.textContent)))
            .filter(r => r.some(c => c));
        return fromTable.length >= fromPage.length ? fromTable : fromPage;
    }"""

    _NEW_PAGE_FOR = """term => {
        if (window.__taxcheckOldPage || document.readyState === 'loading') return false;
        const box = document.querySelector('#pstSearch');
        return !box || box.value.trim() === term;  // results echo the name searched; no box: terms or error page
    }"""

    def _submit_and_wait(self, submit, term):
        """Submit the search and wait for this search's results page to replace the current one.

        The search is a form post that reloads the page; reading straight after the click reads the previous
        search's results (seen on a real run). So the current page is marked, and reading waits until a page
        without the mark has loaded whose search box holds this search's name (a late answer to an earlier,
        abandoned search isn't taken for this one)."""
        page = self.page
        page.evaluate("() => { window.__taxcheckOldPage = true; }")
        submit()
        deadline = time.monotonic() + LOAD_TIMEOUT_S
        while True:
            try:
                if page.evaluate(self._NEW_PAGE_FOR, term):
                    return
            except Exception:  # the old page is being torn down mid-check
                pass
            if page.is_closed():
                raise WindowClosed("the Saskatchewan browser window was closed")
            if time.monotonic() > deadline:
                try:
                    page.evaluate("() => window.stop()")  # so the late answer can't land on the next search
                except Exception:
                    pass
                raise ResultsNotLoaded(f"Saskatchewan's registry didn't answer within {LOAD_TIMEOUT_S} seconds")
            page.wait_for_timeout(150)

    def _search(self, term):
        if not self.searches_left():
            raise DailyLimitReached("no searches left")
        self._count_search()
        self._submitted = term
        page = self.page
        page.fill(SEARCH_BOX, term)
        # Only the PST search form's own button: every SETS page also has a site-wide "Search SETS" button.
        form = page.locator(f"form:has({SEARCH_BOX})")
        button = form.get_by_role("button", name=re.compile("search", re.I)).or_(
            form.get_by_role("link", name=re.compile(r"^\s*search\s*$", re.I))
        )
        if button.count():
            self._submit_and_wait(lambda: button.first.click(), term)
        else:
            self._submit_and_wait(lambda: page.locator(SEARCH_BOX).press("Enter"), term)
        page.wait_for_load_state("domcontentloaded")
        rows = page.evaluate(self._READ_ROWS)
        text = page.inner_text("body")
        for _ in range(3):  # the page says it found results but none were read yet: give it a moment
            if not unreadable(rows, text):
                break
            page.wait_for_timeout(1000)
            rows, text = page.evaluate(self._READ_ROWS), page.inner_text("body")
        if unreadable(rows, text):
            rows = rows_from_html(page.content())
            log.info("Saskatchewan: results for %r read from the page's HTML (%d rows)", term, len(rows))
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

    def _answered_search(self, term):
        """One search, tried a second time if it fails or lands on a page without the search box (an error page,
        or the terms page when the session ended). Raises if the second try fails too. An answer from an earlier
        run within a day is reused without the browser."""
        self.reused_at = None
        saved = self._answers.get(term)
        if saved:
            self.reused_at = dt.datetime.fromtimestamp(float(saved["at"]))
            return saved["rows"], saved["text"]
        if not self.searches_left():  # before the window and its CAPTCHA: nothing could be searched anyway
            raise DailyLimitReached("no searches left")
        for attempt in (1, 2):
            try:
                self._ready()
                rows, text = self._search(term)
                if self._search_box_visible():
                    self.searches_since_captcha += 1
                    self._save_answer(term, rows, text)
                    return rows, text
                problem = "the registry showed a page without its search box: " + re.sub(r"\s+", " ", text)[:150]
            except _STOPS_THE_RUN:
                raise
            except Exception as e:
                if self.page is not None and self.page.is_closed():
                    raise WindowClosed("the Saskatchewan browser window was closed") from e
                problem = f"{type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}"
            if attempt == 1:
                log.warning("Saskatchewan search for %r didn't work (%s); trying it once more.", term, problem)
        raise ResultsNotLoaded(problem)

    def check(self, names):
        terms = search_terms(*names)
        if not terms:
            return Result(MANUAL, "Saskatchewan's registry needs at least 4 letters of the business name (apart from "
                                  f"Ltd/Inc); the vendor name is too short. Search by hand at {SK_REGISTRY}")
        if self.gave_up(names):
            return Result(MANUAL, f"Saskatchewan's registry didn't answer this search in {self.MAX_FAILS} batches; "
                                  f"search the name by hand at {SK_REGISTRY}")
        if self.unavailable and not self.answered(names):  # searching has stopped; saved answers still count
            return Result(MANUAL, self.unavailable)
        term = terms[0]
        try:
            last = None
            for term in terms:
                self._submitted = None
                rows, text = self._answered_search(term)
                status, detail, row = classify(rows, text, names)
                if status == ERROR and not self.reused_at:  # results shown but unreadable: a failed search
                    self._note_failure(term)
                self._failures_in_a_row = 0
                note = (f" (Registry answer from this tool's search on {self.reused_at:%Y-%m-%d %H:%M}.)"
                        if self.reused_at else self._evidence(term))
                last = (status, detail, term, note)
                if status == REGISTERED:
                    return Result(status, detail + note, row)
            status, detail, term, note = last
            return Result(status, f"{detail} (searched: {', '.join(terms)}).{note}")
        except DailyLimitReached:
            self.unavailable = self._limit_message()
        except (CaptchaEverySearch, CaptchaNotCompleted, WindowClosed) as e:
            self.unavailable = f"{str(e)[0].upper()}{str(e)[1:]}. Search by hand at {SK_REGISTRY}"
        except BrowserUnavailable as e:
            self.unavailable = f"Couldn't open a browser for the assisted check: {e} Search by hand at {SK_REGISTRY}"
        except Exception as e:
            # Only this supplier: the next one is searched as usual (and this one again in the next batch). It counts
            # towards leaving it for a search by hand only if the search was really sent (not the site being down).
            log.warning("Saskatchewan search failed for %s: %s", term, e, exc_info=not isinstance(e, ResultsNotLoaded))
            if self._submitted == term:
                self._note_failure(term)
            self._failures_in_a_row += 1
            if self._failures_in_a_row >= self.STOP_AFTER_FAILURES:
                self.unavailable = (f"Saskatchewan's registry stopped answering ({self._failures_in_a_row} searches in a "
                                    f"row failed); the rest wait for the next batch. Search by hand at {SK_REGISTRY}")
                log.warning("Saskatchewan: %s", self.unavailable)
            return Result(ERROR, f"Saskatchewan search failed twice ({str(e).splitlines()[0]}). Search by hand at "
                                 f"{SK_REGISTRY}{self._evidence(terms[0]) if self.page and not self.page.is_closed() else ''}")
        log.warning("Saskatchewan: %s", self.unavailable)
        return Result(MANUAL, self.unavailable)
