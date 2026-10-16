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


class BusinessRegistries:
    label = "Canada's Business Registries"

    def __init__(self, session=None, delay=1.0, cache=None):
        self.session = session or new_session()
        self.throttle = Throttle(delay)
        self.health = SiteHealth("Canada's Business Registries")
        self._cache = {} if cache is None else cache  # shareable between parallel workers
        self.error = None

    def names(self, bn9):
        """Registered names for a business number (empty if none, or if the search is down)."""
        if bn9 in self._cache:
            return self._cache[bn9]
        if self.health.down:
            return []
        self.throttle.wait()
        try:
            r = self.session.get(SEARCH, params={"fq": "keyword:{%s}" % bn9, "lang": "en", "queryaction": "fieldquery",
                                                 "sortfield": "score", "sortorder": "desc"}, timeout=30)
            r.raise_for_status()
            found = parse_docs(r.json(), bn9)
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
