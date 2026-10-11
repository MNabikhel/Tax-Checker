"""Official legal names by business number, from Corporations Canada's open data (federal corporations).

The CRA GST/HST registry needs the business name to match its records. When the name in the supplier
sheet doesn't match, the federal corporate name for that business number usually does. The data is
published on open.canada.ca ("Federal Corporations", updated weekly); it covers federally incorporated
companies only, not provincial ones. The CSVs (~110 MB) are cached locally and refreshed weekly.
"""

import csv
import logging
import re
import time
from pathlib import Path

from .http import new_session

log = logging.getLogger(__name__)

SOURCES = [
    "https://d4bf66bykfyaf.cloudfront.net/corporations-active-cbca-en.csv",
    "https://d4bf66bykfyaf.cloudfront.net/corporations-active-non-cbca-en.csv",
]
MAX_AGE = 7 * 24 * 3600


class FederalCorporations:
    label = "the federal corporations registry"

    def __init__(self, cache_dir="cache", session=None):
        self.cache_dir = Path(cache_dir)
        self.session = session or new_session()
        self._names = None
        self.error = None

    def _fetch(self, url):
        path = self.cache_dir / url.rsplit("/", 1)[-1]
        if path.exists() and time.time() - path.stat().st_mtime < MAX_AGE:
            log.debug("Using cached %s", path)
            return path
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        log.info("Downloading federal corporations data: %s", url)
        tmp = path.with_suffix(".part")
        with self.session.get(url, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
        tmp.replace(path)
        return path

    def _load(self):
        self._names = {}
        try:
            for url in SOURCES:
                with open(self._fetch(url), encoding="utf-8-sig", newline="") as f:
                    for row in csv.DictReader(f):
                        bn = (row.get("Business number (BN)") or "").strip()
                        if not re.fullmatch(r"\d{9}", bn):
                            continue
                        names = self._names.setdefault(bn, [])
                        for key in ("Corporate name - form 1", "Corporate name - form 2"):
                            name = (row.get(key) or "").strip()
                            if name and name not in names:
                                names.append(name)
            log.info("Loaded federal corporate names for %d business numbers", len(self._names))
        except Exception as e:
            self.error = f"couldn't load federal corporations data: {e}"
            log.warning(self.error)

    def names(self, bn9):
        """Official corporate names for a business number (empty list if it's not a federal corporation)."""
        if self._names is None:
            self._load()
        return self._names.get(bn9, [])
