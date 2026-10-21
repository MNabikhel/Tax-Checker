"""GST/HST checks: the CRA GST/HST Registry, backed by CRA's published simplified-registrant list."""

import contextlib
import datetime as dt
import html
import logging
import re
import time
import unicodedata
from pathlib import Path

import requests

from .http import SiteHealth, Throttle, new_session
from .result import INVALID, NAME_NOT_MATCHED, NOT_CONFIRMED, NOT_REGISTERED, REGISTERED, Result

log = logging.getLogger(__name__)

REGISTRY = "https://www.businessregistration-inscriptionentreprise.gc.ca/ebci/brom/registry/pub/"
# Number-only check (see GstChecker._number_only): a name CRA will never match, and a date before GST existed.
PLACEHOLDER_NAME = "Name Not Supplied"
BEFORE_GST = dt.date(1980, 1, 1)
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


def _name_key(name):
    """A name the way the CRA registry compares it, so two spellings with the same key are one search.

    Measured live on 2026-10-16 (public registrations, e.g. "Amazon.com.ca ULC", "University of British
    Columbia"): case and extra spaces don't matter; "." and "&" separate words like a space
    ("Amazon.com.ca" = "Amazon com ca"; "U.L.C." = "U L C", not "ULC"); every other punctuation mark is
    dropped and joins what's on either side ("British-Columbia" = "BritishColumbia", which fails;
    "Columbia's" = "Columbias"; "Amazon,com,ca" = "Amazoncomca"). Missing spaces always fail.
    """
    # Accented letters are compared as their plain letter ("LTÉE" = "LTEE"), not dropped.
    text = unicodedata.normalize("NFKD", str(name).lower()).encode("ascii", "ignore").decode()
    text = re.sub(r"[.&]", " ", text)
    return " ".join(re.sub(r"[^a-z0-9\s]", "", text).split())


def name_variants(*names):
    """The CRA registry ignores case and punctuation but not extra words, so try a few spellings.

    Vendor-master names are broken into likely legal/operating names before they get here
    (checks.vendor_name_candidates); official names from registries are used as given.
    """
    seen, out = set(), []
    for name in names:
        if not name:
            continue
        name = str(name).strip()
        for candidate in (name, re.sub(r"^the\s+", "", name, flags=re.I)):
            key = _name_key(candidate)
            if key and key not in seen:
                seen.add(key)
                out.append(candidate)
    return out


class SimplifiedList:
    """CRA's list of simplified GST/HST registrants, loaded once per run.

    canada.ca doesn't answer Python's requests on some corporate networks (seen 2026-10-13 on a corporate network: read timeouts
    while a browser loads it fine), so: a cached copy under a week old, else one quick plain download,
    else the page through the shared browser. Successful downloads are cached.
    """

    CACHE_AGE = 7 * 24 * 3600
    # CRA's list had 1,781 business numbers in October 2026 and only grows. Far fewer means a page read before
    # it finished building its table (seen: 7, from a browser's half-built view), so it's not used or cached.
    MIN_NUMBERS = 500

    def __init__(self, session, browser=None, cache_dir=None):
        self.session = session
        self.browser = browser  # set by check_suppliers.run once the shared browser exists
        self.cache_file = Path(cache_dir) / "cra_simplified_list.html" if cache_dir else None
        self._rows = None
        self.error = None

    def _sources(self):
        """Yield (description, page HTML), best first."""
        if self.cache_file and self.cache_file.exists() and time.time() - self.cache_file.stat().st_mtime < self.CACHE_AGE:
            yield "cached copy", self.cache_file.read_text(encoding="utf-8")
        try:
            # One quick try without retries: when canada.ca stalls, retrying only wastes minutes.
            r = requests.get(SIMPLIFIED_LIST, timeout=(10, 25), headers={"User-Agent": self.session.headers.get("User-Agent", "")})
            r.raise_for_status()
            yield "download", r.text
        except Exception as e:
            log.info("Plain download of the CRA simplified list failed (%s: %s); trying the browser",
                     type(e).__name__, str(e).splitlines()[0][:150] if str(e) else "")
        if self.browser:
            with self.browser.page() as page:
                resp = page.goto(SIMPLIFIED_LIST, wait_until="domcontentloaded", timeout=60000)
                log.debug("Browser load of the CRA simplified list -> HTTP %s", resp and resp.status)
                if resp is None or resp.status < 400:
                    # The page as CRA sent it: page.content() is the browser's live view, which the page's
                    # scripts may still be rearranging (paging the table).
                    yield "browser", resp.text() if resp is not None else page.content()

    def _load(self):
        self._rows = {}
        errors = []
        try:
            for source, page in self._sources():
                rows = self.parse(page)
                if 0 < len(rows) < self.MIN_NUMBERS:
                    log.info("CRA simplified list from %s has only %d business numbers (incomplete page); trying "
                             "the next source", source, len(rows))
                    errors.append(f"{source}: only {len(rows)} business numbers (incomplete page)")
                    continue
                if rows:
                    self._rows = rows
                    log.info("Loaded CRA simplified GST/HST list from %s: %d business numbers", source, len(rows))
                    if self.cache_file and source != "cached copy":
                        with contextlib.suppress(OSError):
                            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
                            self.cache_file.write_text(page, encoding="utf-8")
                    return
                errors.append(f"{source}: no business numbers found (page layout changed?)")
        except Exception as e:  # the main registry check still works without it
            errors.append(f"{type(e).__name__}: {str(e).splitlines()[0][:200] if str(e) else ''}")
        self.error = "couldn't load CRA simplified registrant list (" + ("; ".join(errors) or "plain download failed, no browser") + ")"
        log.warning(self.error)

    def lookup(self, bn9, date):
        """The entry for this business number that applies on `date`, else its most recent one."""
        if self._rows is None:
            self._load()
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


class RegistryServerError(requests.ConnectionError):
    """CRA's registry is having a server problem: treated like a dropped connection (retried; the run pauses while
    the site is down) rather than as an answer about the supplier."""


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


def cra_today(now=None):
    """Today's date in Ottawa, which is what the CRA registry treats as "today".

    The registry rejects later dates as "in the future", so on a PC whose clock is ahead of Ottawa
    (UTC, Atlantic, Newfoundland, overseas) the local date can be refused shortly after midnight.
    UTC-5 is Ottawa's winter offset; in summer it lags Ottawa by an hour, so this date is never ahead
    of Ottawa's (at worst it's a day behind for one hour). No time-zone database needed (Windows
    Python doesn't ship one).
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    return (now.astimezone(dt.timezone.utc) - dt.timedelta(hours=5)).date()


def _parse_date(s):
    try:
        return dt.datetime.strptime(s.strip(), "%B %d, %Y").date()
    except ValueError:
        return None


class GstChecker:
    def __init__(self, delay=1.5, corporations=None, name_sources=None, cache_dir=None):
        """Name sources are tried, in order, when the sheet's names don't match CRA's records.

        Each has `.label` and `.names(bn9) -> [official names]` (FederalCorporations, OrgBookBC).
        `corporations` is kept as a shorthand for the first one.
        """
        self.session = new_session()
        self.throttle = Throttle(delay)
        self.simplified = SimplifiedList(self.session, cache_dir=cache_dir)
        self.corporations = corporations
        self.name_sources = [s for s in [corporations, *(name_sources or [])] if s]
        # Waits up to 6 hours for the registry to come back (an overnight run has nobody watching; seen: CRA's own
        # Error 500 page for every search at 1:30 am on 2026-10-21), retrying every 2 minutes, all workers together.
        self.health = SiteHealth("The CRA GST/HST Registry", limit=1, give_up_after=6 * 3600)
        # Every answer, by (business number, name as CRA compares it, date): the same question comes up again for
        # duplicate rows, the "registered today?" and 1980 checks, and date searches. Shared by parallel workers.
        self.answers = {}

    def registry_lookup(self, bn9, name, date, attempts=3):
        """One CRA registry search. Returns 'registered', 'not_registered', 'no_match' or 'invalid'.

        Searches are read-only and each attempt fetches a fresh form token, so a dropped connection
        is simply retried.
        """
        key = (bn9, _name_key(name), date)
        if key in self.answers:
            return self.answers[key]
        attempt = 0
        while True:
            if self.health.down:  # while the site is failing this waits for the next retry; True once given up on
                raise RuntimeError(self.health.down_message())
            attempt += 1
            try:
                outcome = self._registry_lookup(bn9, name, date)
                self.health.record(True)
                self.answers[key] = outcome
                return outcome
            except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as e:
                if attempt < attempts:
                    log.warning("CRA registry problem (attempt %d/%d), retrying: %s", attempt, attempts, e)
                    time.sleep(2 * attempt)
                    continue
                self.health.record(False, f"{type(e).__name__}: {e}")
                if self.health.failures >= self.health.limit:
                    attempt = 0  # the site is down: this supplier waits with the others and is tried again
                    continue
                raise

    def _registry_lookup(self, bn9, name, date):
        self.throttle.wait()
        form = self.session.get(REGISTRY + "reg_01_Ld.action", timeout=30)
        form.raise_for_status()
        m = re.search(r'name="token" value="([^"]+)"', form.text)
        if not m:
            raise RuntimeError("the CRA registry search page has changed (no form token found); see the log")
        token = m.group(1)
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
        if "500.jsp" in r.url or "(Error 500)" in r.text:
            raise RegistryServerError("CRA's registry answered with its own Error 500 page (a problem with CRA's server)")
        outcome, message = parse_registry_result(r.text)
        log.debug("CRA registry bn=%s name=%r date=%s -> %s (%s)", bn9, name, date, outcome, message)
        return outcome

    def check(self, bn9, names, date, extra_names=()):
        """`extra_names`: (name, source label) pairs already known from other checks, e.g. Revenu Québec's."""
        today = cra_today()
        future_note = ""
        if date > today:
            # The registry refuses future dates; confirm as of today instead.
            future_note = f"Transaction date {date} is in the future, so registration was checked as of {today}."
            date = today
        listed = self.simplified.lookup(bn9, date)
        if listed:
            log.debug("bn=%s is on the simplified list: %s", bn9, listed)
        result = self._check(bn9, names, date, listed, extra_names)
        if future_note:
            result.detail = f"{future_note} {result.detail}"
        return result

    def _candidates(self, bn9, names, listed, extra_names=()):
        """Names to try, best first: the sheet's, CRA's simplified list, names from other checks, then
        each name source (federal registry, OrgBook BC...).

        Yields (name, source). A source is only consulted if every earlier name failed.
        """
        tried = set()

        def fresh(group):
            for name in name_variants(*(group or ())):
                if _name_key(name) not in tried:
                    tried.add(_name_key(name))
                    yield name

        for name in fresh(names):
            yield name, "sheet"
        if listed:
            for name in fresh((listed["legal_name"], listed["trade_name"])):
                yield name, "CRA's simplified registrant list"
        for name, label in extra_names:
            for variant in fresh((name,)):
                yield variant, label
        for source in self.name_sources:
            for name in fresh(source.names(bn9)):
                yield name, source.label

    def _official_name(self, bn9, extra_names=()):
        """(name, source label, tied to this business number?) for reports when nothing matched.

        Registries looked up by business number (federal corporations, OrgBook BC) come first: their name
        belongs to this number. A name from another check (Revenu Québec's, found by the QST number) only
        says what the supplier is called there; the GST/HST number in the sheet could be another business's.
        """
        for source in self.name_sources:
            found = source.names(bn9)
            if found:
                return found[0], source.label, True
        for name, label in extra_names:
            return name, label, False
        return "", "", False

    def _check(self, bn9, names, date, listed, extra_names=()):
        res = self._decide(bn9, names, date, listed, extra_names)
        res.facts.setdefault("date_checked", str(date))
        if listed:
            # CRA's published list gives the dates for simplified (non-resident digital seller) registrations.
            res.facts["simplified_registrant"] = True
            res.facts["listed_number"] = re.sub(r"\s", "", listed["number"])
            if listed.get("registered"):
                res.facts["registered_since"] = str(listed["registered"])
            if listed.get("deregistered"):
                res.facts["registration_end"] = str(listed["deregistered"])
            res.facts["dates_source"] = "CRA simplified registrant list"
        return res

    def _decide(self, bn9, names, date, listed, extra_names=()):
        note = SIMPLIFIED_NOTE if listed else ""
        tried, sheet_tried = [], []
        for name, source in self._candidates(bn9, names, listed, extra_names):
            tried.append(name)
            if source == "sheet":
                sheet_tried.append(name)
            outcome = self.registry_lookup(bn9, name, date)
            if outcome == "registered":
                if source == "sheet":
                    detail = f"CRA GST/HST Registry confirms registration on {date} (matched name '{name}')."
                    return Result(REGISTERED, " ".join(filter(None, [detail, note])), listed and listed["legal_name"] or name,
                                  facts={"accepted_name": name, "name_source": "sheet", "cra_answer": "registered"})
                # Registered, but under a name from CRA's list or the federal registry, not the sheet's:
                # the number is fine; whether it's this supplier's number is for a person to judge.
                return Result(NAME_NOT_MATCHED, " ".join(filter(None, [self._other_name_detail(name, source, sheet_tried, date), note])), name,
                              facts={"accepted_name": name, "name_source": source, "official_name": name,
                                     "cra_answer": "registered"})
            if outcome == "invalid":
                return Result(INVALID, "CRA says this GST/HST number is not valid.", facts={"cra_answer": "invalid"})
            if outcome == "not_registered":
                # CRA gives this answer about the number and date whatever name is entered,
                # so it's definite about the number but says nothing about the name.
                return Result(NOT_REGISTERED, f"CRA: this number was not registered for GST/HST on {date}.",
                              facts={"cra_answer": "not_registered_on_date"})

        if listed:
            dereg = listed["deregistered"]
            if dereg and dereg <= date:
                return Result(NOT_REGISTERED, f"Simplified GST/HST registration ended {dereg}.", listed["legal_name"],
                              facts={"cra_answer": "deregistered", "official_name": listed["legal_name"],
                                     "name_source": "CRA's simplified registrant list"})
            if listed["registered"] and listed["registered"] > date:
                return Result(NOT_REGISTERED, f"Simplified GST/HST registration only starts {listed['registered']}.", listed["legal_name"],
                              facts={"cra_answer": "not_registered_on_date", "official_name": listed["legal_name"],
                                     "name_source": "CRA's simplified registrant list"})
            # On CRA's list, but CRA accepted none of the names, the listed legal name included: don't treat the
            # list as CRA's confirmation. Decide from the number (as for any unmatched name), and show the listed
            # name as the official one for this number.
            res = self._number_only(bn9, date, tried, extra_names)
            if res.status == NAME_NOT_MATCHED:
                res.registered_name = listed["legal_name"]
                res.facts.update(official_name=listed["legal_name"], official_source="CRA's simplified registrant list",
                                 official_name_tied_to_number=True)
                res.detail += f" CRA's simplified registrant list has this number as '{listed['legal_name']}'."
            res.detail = " ".join(filter(None, [res.detail, note]))
            return res

        return self._number_only(bn9, date, tried, extra_names)

    @staticmethod
    def _other_name_detail(name, source, sheet_tried, date):
        sheet = "; ".join(f"'{n}'" for n in sheet_tried) or "(no name in the sheet)"
        return (
            f"CRA confirms this number is registered for GST/HST on {date} under '{name}' (name from {source}), "
            f"not under the name in your sheet: {sheet}. If '{name}' is this supplier, update your vendor record; "
            "if not, the number may belong to another business."
        )

    def _number_only(self, bn9, date, tried, extra_names=()):
        """Decide registration from the number alone, when no name matched CRA's records.

        CRA's registry checks the number against the date before it looks at the name:
          - not registered on that date  -> "was not registered on this transaction date", whatever the name;
          - registered on that date      -> "Insufficient information" unless the name matches;
          - no GST/HST account at all    -> "Insufficient information" for every date.
        So with names that didn't match today: asking about a date before GST existed (1980) answers
        "not registered" for any number that has a GST/HST account, and "Insufficient information" for a
        number CRA doesn't know. Together: registered today, but under a name we don't have. Verified on
        2026-10-11 against 27 registered and 11 unregistered companies, de-registered accounts, and made-up
        numbers, with no disagreement. It proves the number is registered, not that it belongs to this
        supplier, hence a separate status.
        """
        if not tried:  # no name at all in the sheet: ask about the date itself first
            outcome = self.registry_lookup(bn9, PLACEHOLDER_NAME, date)
            if outcome == "not_registered":
                return Result(NOT_REGISTERED, f"CRA: this number was not registered for GST/HST on {date}.",
                              facts={"cra_answer": "not_registered_on_date"})
            if outcome == "invalid":
                return Result(INVALID, "CRA says this GST/HST number is not valid.", facts={"cra_answer": "invalid"})
        official_name, official_source, tied = self._official_name(bn9, extra_names)
        official = [official_name] if official_name else []
        tried_text = "; ".join(f"'{n}'" for n in tried) or "(no name in the sheet)"
        if date <= BEFORE_GST:
            return Result(NOT_CONFIRMED, f"CRA could not match this number with {tried_text} on {date}.")
        account = self.registry_lookup(bn9, PLACEHOLDER_NAME, BEFORE_GST)
        if account == "not_registered":
            known = ""
            if official and tied:
                known = f" The official name on record for this number is '{official_name}' ({official_source})."
            elif official:
                known = (f" The supplier's name at {official_source} is '{official_name}', but CRA doesn't have it "
                         "under this number.")
            return Result(
                NAME_NOT_MATCHED,
                f"CRA: this number is registered for GST/HST on {date}, but not under {tried_text}.{known} "
                "Confirm the number belongs to this supplier (e.g. the legal name on their invoice) before "
                "relying on it for input tax credits.",
                official[0] if official else "",
                facts={"official_name": official_name, "official_source": official_source,
                       "official_name_tied_to_number": tied, "cra_answer": "registered_number_only",
                       "names_tried": tried},
            )
        return Result(
            NOT_REGISTERED,
            f"CRA has no GST/HST account under this number (it isn't recognized for any date). It's most likely "
            "a typo or not a GST/HST number; ask the supplier for it again.",
            facts={"cra_answer": "unknown_number"},
        )
