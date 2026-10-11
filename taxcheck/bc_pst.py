"""BC PST checks through the eTaxBC 'PST Number Verification Service' (needs the shared browser)."""

import logging
import re

from .browser import BrowserUnavailable
from .http import SiteHealth, Throttle
from .result import ERROR, MANUAL, NOT_CONFIRMED, REGISTERED, Result

log = logging.getLogger(__name__)

ETAXBC = "https://www.etax.gov.bc.ca/btp/eservices/_/"


class BcPstChecker:
    def __init__(self, browser, delay=2.0):
        self.browser = browser
        self.throttle = Throttle(delay)
        self.health = SiteHealth("eTaxBC")

    def _lookup(self, bn9, pst):
        # eTaxBC keeps search state per session, so each lookup gets a clean one.
        with self.browser.page() as page:
            try:
                page.goto(ETAXBC, wait_until="networkidle")
                page.get_by_text("Provincial Sales Tax (PST) verification service").click()
                page.wait_for_load_state("networkidle")
                page.get_by_label("Business Number").fill(bn9)
                page.get_by_label(re.compile("PST Number")).fill(pst)
                # Each field change triggers a server round trip; let them finish before submitting.
                page.wait_for_load_state("networkidle")
                # The terms checkbox is a styled label that sometimes ignores a click, so confirm it took.
                terms = page.get_by_role("checkbox", name=re.compile("I have read and agree"))
                for _ in range(5):
                    if terms.is_checked():
                        break
                    page.get_by_text("I have read and agree").click()
                    page.wait_for_load_state("networkidle")
                else:
                    raise RuntimeError("couldn't tick the terms-of-use checkbox")
                page.get_by_role("button", name="Next").click()
                page.wait_for_function("document.body.innerText.includes('Search Date')", timeout=20000)
                m = re.search(r"Result:\s*(.+)", page.inner_text("body"))
                return m.group(1).strip() if m else ""
            except Exception:
                self.browser.record_failure(page, f"etaxbc_{bn9}_{pst}")
                raise

    def check(self, bn9, pst, attempts=3):
        if self.health.down:
            return Result(ERROR, self.health.down_message())
        for attempt in range(1, attempts + 1):
            self.throttle.wait()
            try:
                outcome = self._lookup(bn9, pst)
                log.debug("eTaxBC bn=%s pst=%s -> %r (attempt %d)", bn9, pst, outcome, attempt)
                break
            except BrowserUnavailable as e:
                return Result(MANUAL, f"Couldn't check automatically: {e} Or verify PST-{pst} by hand at {ETAXBC}")
            except Exception as e:
                error = str(e).splitlines()[0]
                log.warning("eTaxBC lookup bn=%s pst=%s attempt %d/%d failed: %s", bn9, pst, attempt, attempts, error)
        else:
            self.health.record(False, error)
            return Result(ERROR, f"eTaxBC lookup failed after {attempts} tries: {error} (see log file)")
        self.health.record(True)
        if outcome == "PST number is valid":
            return Result(REGISTERED, f"eTaxBC: PST-{pst} is valid for BN {bn9}.")
        if outcome == "No Match Found":
            return Result(
                NOT_CONFIRMED,
                f"eTaxBC: no match for PST-{pst} with BN {bn9}. The PST number is wrong, inactive, "
                "or belongs to a different business number.",
            )
        return Result(ERROR, f"eTaxBC returned an unexpected result: '{outcome}'")
