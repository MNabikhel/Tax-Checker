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

INSTALL_HINT = ("Run Test Setup.bat (it installs Playwright), or make sure Microsoft Edge is installed; "
                "'playwright install chromium' also works where downloads aren't blocked.")


class BrowserUnavailable(RuntimeError):
    pass


# Playwright's sync API can be started only once per thread: a second sync_playwright().start() fails with "using
# Playwright Sync API inside the asyncio loop". The hidden browser (Manitoba, BC) and the visible one (the
# Saskatchewan CAPTCHA) share one driver; it stops when the last browser closes.
_driver = None
_driver_users = 0


def _acquire_driver():
    global _driver, _driver_users
    if _driver is None:
        from playwright.sync_api import sync_playwright

        _driver = sync_playwright().start()
    _driver_users += 1
    return _driver


def _release_driver():
    global _driver, _driver_users
    _driver_users -= 1
    if _driver_users <= 0:
        if _driver is not None:
            try:
                _driver.stop()
            except Exception as e:  # already gone (e.g. Ctrl+C also stopped the driver process)
                log.debug("Playwright driver stop: %s", e)
        _driver, _driver_users = None, 0


class Browser:
    """Use as `with Browser() as browser:`. Each lookup should take a fresh `browser.page()` context."""

    def __init__(self, headless=True, debug_dir=None):
        self.headless = headless
        self.debug_dir = Path(debug_dir) if debug_dir else None
        self._pw = self._browser = None
        self._start_error = None
        self.engine = None  # which browser actually started, e.g. "Microsoft Edge 141.0..."

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        """Never raises: on Ctrl+C the browser's driver process gets the interrupt too and may be gone already,
        and a failure here used to skip saving the results workbook (seen in a stress test)."""
        if self._browser:
            try:
                self._browser.close()
            except Exception as e:
                log.debug("Browser close: %s", e)
            self._browser = None
        if self._pw:
            self._pw = None
            _release_driver()

    def _ensure_started(self):
        if self._browser:
            return
        if self._start_error:
            raise BrowserUnavailable(self._start_error)
        try:
            self._pw = _acquire_driver()
        except Exception as e:
            self.close()
            self._start_error = f"browser couldn't start ({str(e).splitlines()[0]}). {INSTALL_HINT}"
            log.exception("Playwright start failed")
            raise BrowserUnavailable(self._start_error) from e
        base = {"headless": self.headless}
        if os.environ.get("HTTPS_PROXY"):
            base["proxy"] = {"server": os.environ["HTTPS_PROXY"]}
        # Try, in order: a browser named in TAXCHECK_CHROMIUM, Playwright's own Chromium, then the Microsoft Edge
        # or Google Chrome already installed on the PC. Edge is on every Windows PC and needs no download, which
        # matters on corporate networks where 'playwright install chromium' can't download (seen 2026-10-13).
        attempts = []
        if os.environ.get("TAXCHECK_CHROMIUM"):
            attempts.append(("TAXCHECK_CHROMIUM", {"executable_path": os.environ["TAXCHECK_CHROMIUM"]}))
        attempts += [("Playwright Chromium", {}), ("Microsoft Edge", {"channel": "msedge"}), ("Google Chrome", {"channel": "chrome"})]
        errors = []
        for label, extra in attempts:
            try:
                self._browser = self._pw.chromium.launch(**base, **extra)
                self.engine = f"{label} {self._browser.version}"
                log.debug("Started %s (headless=%s)", self.engine, self.headless)
                if errors:
                    log.info("Using %s for browser lookups (%s)", self.engine, "; ".join(errors))
                return
            except Exception as e:
                first = str(e).splitlines()[0] if str(e) else type(e).__name__
                errors.append(f"{label}: {first[:150]}")
                log.debug("Couldn't start %s: %s", label, first)
        self.close()
        self._start_error = f"browser couldn't start ({' | '.join(errors)}). {INSTALL_HINT}"
        log.error(self._start_error)
        raise BrowserUnavailable(self._start_error)

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
