"""One shared Chromium (via Playwright) for the lookups that need a real browser.

The browser only starts when a lookup first asks for a page, so runs that don't need it never pay
for it. If it can't start (Playwright or Chromium not installed), `BrowserUnavailable` is raised
for every page request and the checks report that instead of failing one by one.
"""

import contextlib
import datetime as dt
import logging
import os
import re
from pathlib import Path

log = logging.getLogger(__name__)

INSTALL_HINT = "Run 'pip install playwright' and 'playwright install chromium' to enable browser-based checks."


class BrowserUnavailable(RuntimeError):
    pass


class Browser:
    """Use as `with Browser() as browser:`. Each lookup should take a fresh `browser.page()` context."""

    def __init__(self, headless=True, debug_dir=None):
        self.headless = headless
        self.debug_dir = Path(debug_dir) if debug_dir else None
        self._pw = self._browser = None
        self._start_error = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        if self._browser:
            self._browser.close()
            self._browser = None
        if self._pw:
            self._pw.stop()
            self._pw = None

    def _ensure_started(self):
        if self._browser:
            return
        if self._start_error:
            raise BrowserUnavailable(self._start_error)
        try:
            from playwright.sync_api import sync_playwright

            self._pw = sync_playwright().start()
            kwargs = {"headless": self.headless}
            if os.environ.get("TAXCHECK_CHROMIUM"):
                kwargs["executable_path"] = os.environ["TAXCHECK_CHROMIUM"]
            if os.environ.get("HTTPS_PROXY"):
                kwargs["proxy"] = {"server": os.environ["HTTPS_PROXY"]}
            self._browser = self._pw.chromium.launch(**kwargs)
            log.debug("Started Chromium %s (headless=%s)", self._browser.version, self.headless)
        except Exception as e:
            self.close()
            self._start_error = f"browser couldn't start ({str(e).splitlines()[0]}). {INSTALL_HINT}"
            log.exception("Browser start failed")
            log.error(self._start_error)
            raise BrowserUnavailable(self._start_error) from e

    @contextlib.contextmanager
    def page(self):
        """A page in a brand-new browser context (clean cookies/session), closed afterwards."""
        self._ensure_started()
        ctx = self._browser.new_context()
        try:
            page = ctx.new_page()
            page.set_default_timeout(30000)
            yield page
        finally:
            ctx.close()

    def new_persistent_page(self):
        """A page the caller keeps open across several lookups (closed with the browser)."""
        self._ensure_started()
        page = self._browser.new_context().new_page()
        page.set_default_timeout(30000)
        return page

    @staticmethod
    def check_response(response, site):
        """Fail fast when a site answers with an error page (e.g. 502/503 during maintenance)."""
        if response is not None and response.status >= 400:
            raise RuntimeError(f"{site} answered HTTP {response.status} (down or under maintenance?)")

    def record_failure(self, page, label):
        """Log the page text and save a screenshot next to the log, for debugging a failed lookup."""
        with contextlib.suppress(Exception):
            log.debug("%s page text at failure: %s", label, re.sub(r"\s+", " ", page.inner_text("body"))[:1500])
        if self.debug_dir:
            shot = self.debug_dir / f"{label}_fail_{dt.datetime.now():%H%M%S_%f}.png"
            with contextlib.suppress(Exception):
                page.screenshot(path=str(shot), full_page=True)
                log.debug("Saved failure screenshot: %s", shot)
