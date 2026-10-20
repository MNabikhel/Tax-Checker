import logging
import threading
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
    # 429 = "too many requests": wait (the site's Retry-After when it sends one) and try again, like a server error.
    retry = Retry(total=3, backoff_factor=2, status_forcelist=(429, 500, 502, 503, 504))
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
    """Pauses lookups to a site that isn't responding, instead of failing every row while it's down.

    After `limit` lookups in a row fail outright, `down` waits until the next retry is due (every
    `retry_every` seconds) and then lets one lookup through as the retry. Government sites have maintenance
    windows and brief outages, and a long run shouldn't turn thousands of rows into ERROR because of a
    two-minute blip. Only when the site has failed continuously for `give_up_after` seconds does `down` stay
    True for the rest of the run, so the remaining lookups fail at once with an explanation (and --resume
    retries them later). One instance can be shared by parallel workers: they all pause together.
    """

    def __init__(self, site, limit=2, retry_every=120, give_up_after=30 * 60, sleep=time.sleep, clock=time.monotonic):
        self.site = site
        self.limit = limit
        self.retry_every = retry_every
        self.give_up_after = give_up_after
        self.sleep, self.clock = sleep, clock
        self.failures = 0
        self.last_error = ""
        self.first_failure_at = self.last_failure_at = None
        self.gave_up = False
        self._lock = threading.Lock()

    @property
    def down(self):
        """True only once the site has been given up on. Otherwise, while it's failing, waits until a retry is due."""
        if self.gave_up:
            return True
        if self.failures < self.limit:
            return False
        with self._lock:  # parallel workers wait here together
            if self.failures < self.limit:  # another worker's retry worked meanwhile
                return False
            if self.clock() - self.first_failure_at >= self.give_up_after:
                if not self.gave_up:
                    log.warning("%s hasn't responded for %.0f minutes; giving up on it for the rest of this run "
                                "(double-click \"Resume Last Run.bat\" later)", self.site, (self.clock() - self.first_failure_at) / 60)
                self.gave_up = True
                return True
            wait = self.retry_every - (self.clock() - self.last_failure_at)
            if wait > 0:
                log.warning("%s isn't responding (%d lookups in a row failed: %s); pausing, next try in %.0f s",
                            self.site, self.failures, self.last_error[:150], wait)
                self.sleep(wait)
        return False

    def down_message(self):
        minutes = self.give_up_after / 60
        return (
            f"{self.site} wasn't responding for {minutes:.0f} minutes ({self.failures} lookups in a row failed, last "
            f"error: {self.last_error}), so it wasn't tried again in this run. It may be down for maintenance; "
            "double-click \"Resume Last Run.bat\" later."
        )

    def record(self, ok, error=""):
        if ok:
            if self.failures >= self.limit:
                log.info("%s is responding again", self.site)
            self.failures, self.first_failure_at = 0, None
            return
        now = self.clock()
        self.failures += 1
        self.last_error = error
        self.last_failure_at = now
        if self.first_failure_at is None:
            self.first_failure_at = now
