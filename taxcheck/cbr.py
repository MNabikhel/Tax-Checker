"""Official legal names by business number from Canada's Business Registries (CBR).

CBR (https://ised-isde.canada.ca/cbr/srch/) is ISED's search across the federal and provincial corporate
registries (Alberta, BC, Manitoba, Ontario, Nova Scotia, Saskatchewan, Québec...). Its public JSON search, the
one the site itself uses, needs no key and can be searched by business number. It names the provincial companies
that the federal corporations data and OrgBook BC can't, and gives the full legal name behind a vendor name cut
off by the vendor master. Checked 2026-10-16: by business number it named a Manitoba company whose vendor name
was cut off at 40 characters ("...INDUSTRIAL PRO" -> "...INDUSTRIAL PRODUCTS LTD."), a numbered Manitoba
company behind a trade name, and Alberta companies.

The search is a keyword search, so it also returns businesses that only share a word with the number's
records; only results whose own business number is the one asked about are used. A name found here is only
ever a candidate: CRA has to accept it for the number before it counts.
"""

import logging
import re

from .http import SiteHealth, Throttle, new_session

log = logging.getLogger(__name__)

SEARCH = "https://ised-isde.canada.ca/cbr/srch/api/v1/search"


def parse_docs(payload, bn9):
    """Names of the registrations in a CBR search result whose business number is `bn9`; active ones first."""
    active, inactive = [], []
    for doc in (payload or {}).get("docs", []):
        if re.sub(r"\D", "", str(doc.get("BN") or ""))[:9] != bn9:
            continue  # a keyword hit on something else
        name = str(doc.get("Company_Name") or "").strip()
        if not name:
            # Some registries only fill "text": [ids..., name]; the name is the entry that isn't a number/ID.
            name = next((t for t in doc.get("text", []) if re.search(r"[A-Za-z]{2}", str(t))
                         and not re.fullmatch(r"[A-Z]{0,3}\d[\dA-Z-]*", str(t))), "")
        target = active if str(doc.get("Status_State", "")).lower() == "active" else inactive
        if name and name not in target:
            target.append(name)
    return active + [n for n in inactive if n not in active]


_PARTNERSHIP = re.compile(r"\b(?:l\.?\s?l?\.?\s?p\.?|limited\s+partnership|partnership|s\.?\s?e\.?\s?n\.?\s?c\.?)(?:\W|$)",
                          re.I)


def complete(payload):
    """Whether a search's results are all on this page. `count` is the number of matching businesses; `docs`
    lists them, each registration in another province as a "child" of its home one."""
    payload = payload or {}
    parents = [d for d in payload.get("docs", []) if d.get("hierarchy") != "child"]
    return int(payload.get("count") or 0) <= len(parents)


def numbers_by_name(payload, name):
    """(business number, registered name) for the registrations in a CBR name search whose name is `name` apart
    from case, punctuation, "&"/"and" and legal suffix, and that are the same kind of entity (a partnership
    "ACME LP" isn't the corporation "ACME INC."). Distinct numbers only."""
    from .checks import core_name, different_entity_kinds

    want = core_name(name)
    found = {}
    for doc in (payload or {}).get("docs", []):
        bn9 = re.sub(r"\D", "", str(doc.get("BN") or ""))[:9]
        registered = str(doc.get("Company_Name") or "").strip()
        if len(bn9) == 9 and registered and core_name(registered) == want and not different_entity_kinds(name, registered):
            found.setdefault(bn9, registered)
    return list(found.items())


class BusinessRegistries:
    label = "Canada's Business Registries"

    def __init__(self, session=None, delay=1.0, cache=None):
        self.session = session or new_session()
        self.throttle = Throttle(delay)
        self.health = SiteHealth("Canada's Business Registries")
        self._cache = {} if cache is None else cache  # shareable between parallel workers
        self.error = None

    # Results per search ("pagesize"). The site refused more than 50 from 2026-10-22 (400 "Page size cannot exceed
    # 50"; it allowed 100 before). If it lowers the limit again, _search adopts the limit it names.
    PAGE = 50

    def _search(self, query):
        for _ in range(2):
            self.throttle.wait()
            r = self.session.get(SEARCH, params={"fq": "keyword:{%s}" % query, "lang": "en",
                                                 "queryaction": "fieldquery", "sortfield": "score", "sortorder": "desc",
                                                 "pagesize": self.PAGE}, timeout=30)
            m = (re.search(r"page size cannot exceed (\d+)", getattr(r, "text", "") or "", re.I)
                 if getattr(r, "status_code", 200) == 400 else None)
            if not m or int(m.group(1)) >= self.PAGE or int(m.group(1)) < 1:
                break
            log.warning("Canada's Business Registries now allows %s results per search; using that", m.group(1))
            self.PAGE = int(m.group(1))
        r.raise_for_status()
        return r.json()

    def numbers_for_name(self, name):
        """Business numbers registered under exactly this name (apart from punctuation and legal suffix), for a
        supplier with no GST/HST number on file. A lead only: CRA must confirm it, and the supplier should too."""
        query = re.sub(r"[^\w\s&-]", " ", str(name or "")).strip()
        if len(re.sub(r"\W", "", query)) < 6 or self.health.down:
            return []
        key = ("name", query.lower())
        if key in self._cache:
            return self._cache[key]
        try:
            payload = self._search(query)
            # "Exactly one business under this name" is only known when every match is on the page.
            found = numbers_by_name(payload, name) if complete(payload) else []
            self.health.record(True)
        except Exception as e:
            self.health.record(False, str(e))
            log.warning("Canada's Business Registries name search for %r failed: %s", name, e)
            return []
        log.debug("Canada's Business Registries name=%r -> %s", name, found)
        self._cache[key] = found
        return found

    def names(self, bn9):
        """Registered names for a business number (empty if none, or if the search is down)."""
        if bn9 in self._cache:
            return self._cache[bn9]
        if self.health.down:
            return []
        try:
            found = parse_docs(self._search(bn9), bn9)
            self.health.record(True)
        except Exception as e:
            self.health.record(False, str(e))
            self.error = (f"Canada's Business Registries lookups failed ({type(e).__name__}: {e}); provincial names "
                          "weren't used for some suppliers")
            log.warning("Canada's Business Registries lookup for %s failed: %s", bn9, e)
            return []
        log.debug("Canada's Business Registries bn=%s -> %s", bn9, found)
        self._cache[bn9] = found
        return found
