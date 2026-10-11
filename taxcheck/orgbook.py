"""Official legal names by business number for BC-registered organizations, from OrgBook BC.

OrgBook BC (https://orgbook.gov.bc.ca) is the BC government's public directory of organizations
registered in BC: companies incorporated in BC, extraprovincial registrations of companies from
elsewhere, societies, partnerships... Its public API needs no key and can be searched by business
number. It complements the federal corporations data (`fedcorp.py`), which only covers federally
incorporated companies. Checked 2026-10-11: for 20 BC-incorporated companies missing from the federal
data, CRA accepted OrgBook's name for all 19 that were GST-registered.
"""

import logging
import re

from .http import SiteHealth, Throttle, new_session

log = logging.getLogger(__name__)

SEARCH = "https://orgbook.gov.bc.ca/api/v4/search/topic"


def parse_topics(payload, bn9):
    """Names of the organizations in an OrgBook search result whose business number is `bn9`.

    Active organizations come first. Each topic lists names as {"type": ..., "text": ...}, where type is
    "entity_name", "business_number" or (for some) an assumed/DBA name type.
    """
    active, inactive = [], []
    for topic in (payload or {}).get("results", []):
        names = topic.get("names", [])
        bns = [n.get("text", "") for n in names if n.get("type") == "business_number"]
        if not any(re.sub(r"\D", "", b)[:9] == bn9 for b in bns):
            continue
        status = next((a.get("value") for a in topic.get("attributes", []) if a.get("type") == "entity_status"), "")
        target = active if status == "ACT" else inactive
        for n in names:
            if n.get("type") != "business_number" and n.get("text") and n["text"] not in target:
                target.append(n["text"])
    return active + [n for n in inactive if n not in active]


class OrgBookBC:
    label = "the BC registry (OrgBook BC)"

    def __init__(self, session=None, delay=0.5):
        self.session = session or new_session()
        self.throttle = Throttle(delay)
        self.health = SiteHealth("OrgBook BC")
        self._cache = {}
        self.error = None

    def names(self, bn9):
        """Official names for a business number registered in BC (empty if none, or if OrgBook is down)."""
        if bn9 in self._cache:
            return self._cache[bn9]
        if self.health.down:
            return []
        self.throttle.wait()
        try:
            r = self.session.get(SEARCH, params={"q": bn9}, timeout=30)
            r.raise_for_status()
            found = parse_topics(r.json(), bn9)
            self.health.record(True)
        except Exception as e:
            self.health.record(False, str(e))
            self.error = f"OrgBook BC lookups failed ({type(e).__name__}: {e}); BC names weren't used for some suppliers"
            log.warning("OrgBook BC lookup for %s failed: %s", bn9, e)
            return []
        log.debug("OrgBook BC bn=%s -> %s", bn9, found)
        self._cache[bn9] = found
        return found
