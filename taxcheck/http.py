import logging
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)

# Note: adding an Accept-Language header makes canada.ca hang, so only the User-Agent is set.
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
TIMEOUT = 30


def _log_response(response, *args, **kwargs):
    log.debug(
        "%s %s -> HTTP %s (%.2fs, %d bytes)",
        response.request.method,
        response.url,
        response.status_code,
        response.elapsed.total_seconds(),
        len(response.content),
    )


def new_session():
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    retry = Retry(total=3, backoff_factor=2, status_forcelist=(500, 502, 503, 504))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.hooks["response"].append(_log_response)
    return s


class Throttle:
    """Keeps a minimum gap between requests so we stay polite to government sites."""

    def __init__(self, seconds):
        self.seconds = seconds
        self._last = 0.0

    def wait(self):
        gap = time.monotonic() - self._last
        if gap < self.seconds:
            time.sleep(self.seconds - gap)
        self._last = time.monotonic()


class SiteHealth:
    """Stops hammering a site that's down: after `limit` lookups in a row fail outright, give up on it.

    Lookups for the rest of the run then fail immediately with an explanation, instead of each one
    waiting through its own retries (which for a site under maintenance can take minutes per row).
    """

    def __init__(self, site, limit=2):
        self.site = site
        self.limit = limit
        self.failures = 0
        self.last_error = ""

    @property
    def down(self):
        return self.failures >= self.limit

    def down_message(self):
        return (
            f"{self.site} wasn't responding ({self.failures} lookups in a row failed, last error: {self.last_error}), "
            "so it wasn't tried again in this run. It may be down for maintenance; re-run later."
        )

    def record(self, ok, error=""):
        if ok:
            self.failures = 0
            return
        self.failures += 1
        self.last_error = error
        if self.failures == self.limit:
            log.warning("%s looks down after %d failed lookups in a row; skipping it for the rest of this run", self.site, self.failures)
