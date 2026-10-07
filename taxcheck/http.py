import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
TIMEOUT = 30


def new_session():
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    retry = Retry(total=3, backoff_factor=2, status_forcelist=(500, 502, 503, 504))
    s.mount("https://", HTTPAdapter(max_retries=retry))
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
