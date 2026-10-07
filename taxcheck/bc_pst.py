"""BC PST checks through the eTaxBC 'PST Number Verification Service' (needs a headless browser)."""

import contextlib
import os
import re

from .http import Throttle
from .result import ERROR, NOT_CONFIRMED, REGISTERED, Result

ETAXBC = "https://www.etax.gov.bc.ca/btp/eservices/_/"


class BcPstChecker:
    """Drives eTaxBC with Playwright. Start it once with `with BcPstChecker() as bc:`."""

    def __init__(self, delay=2.0, headless=True):
        self.throttle = Throttle(delay)
        self.headless = headless
        self._pw = self._browser = None

    def __enter__(self):
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        kwargs = {"headless": self.headless}
        if os.environ.get("TAXCHECK_CHROMIUM"):
            kwargs["executable_path"] = os.environ["TAXCHECK_CHROMIUM"]
        if os.environ.get("HTTPS_PROXY"):
            kwargs["proxy"] = {"server": os.environ["HTTPS_PROXY"]}
        self._browser = self._pw.chromium.launch(**kwargs)
        return self

    def __exit__(self, *exc):
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()

    def _lookup(self, bn9, pst):
        # eTaxBC keeps search state per session, so each lookup gets a clean one.
        ctx = self._browser.new_context()
        try:
            page = ctx.new_page()
            page.set_default_timeout(30000)
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
        except Exception as e:
            body = ""
            with contextlib.suppress(Exception):
                body = re.sub(r"\s+", " ", page.inner_text("body"))[:300]
            raise RuntimeError(f"{str(e).splitlines()[0]} | page: {body}") from e
        finally:
            ctx.close()

    def check(self, bn9, pst, attempts=3):
        for _ in range(attempts):
            self.throttle.wait()
            try:
                outcome = self._lookup(bn9, pst)
                break
            except Exception as e:
                error = str(e)
        else:
            return Result(ERROR, f"eTaxBC lookup failed after {attempts} tries: {error}")
        if outcome == "PST number is valid":
            return Result(REGISTERED, f"eTaxBC: PST-{pst} is valid for BN {bn9}.")
        if outcome == "No Match Found":
            return Result(
                NOT_CONFIRMED,
                f"eTaxBC: no match for PST-{pst} with BN {bn9}. The PST number is wrong, inactive, "
                "or belongs to a different business number.",
            )
        return Result(ERROR, f"eTaxBC returned an unexpected result: '{outcome}'")
