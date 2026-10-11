"""QST checks: Revenu Québec's validation API, plus its published list of non-resident (NR) registrants."""

import html
import logging
import re
import time
from pathlib import Path

import requests

from .http import Throttle, new_session
from .result import ERROR, INVALID, NOT_CONFIRMED, NOT_REGISTERED, REGISTERED, Result

log = logging.getLogger(__name__)

API = "https://svcnab2b.revenuquebec.ca/2019/02/ValidationTVQ/{}"
NR_CACHE_AGE = 7 * 24 * 3600
NR_LIST = (
    "https://www.revenuquebec.ca/en/businesses/consumption-taxes/gsthst-and-qst/special-cases-gsthst-and-qst/"
    "suppliers-outside-quebec/list-of-suppliers-outside-quebec-that-are-registered-for-the-qst/"
)


def _text(fragment):
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment))).strip()


NR_NUMBER = re.compile(r"\bNR\s*-?\s*(\d{4})\s*-?\s*(\d{4})\b", re.I)


def parse_nr_list(page):
    """Map 'NR12345678' -> {'trade_name', 'legal_name'} from the Revenu Québec NR list page.

    The page has never been seen from the build environment (Revenu Québec blocked it), so this reads
    the documented layout (a table: trade name, legal name, number like "NR 0013 0061") and falls back to
    any element containing an NR number (list items, paragraphs) if there's no table. Names are
    best-effort; the number is what matters for the check.
    """
    rows = {}
    for tr in re.findall(r"<tr.*?</tr>", page, re.S | re.I):
        cells = [_text(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S | re.I)]
        for i, cell in enumerate(cells):
            m = NR_NUMBER.search(cell)
            if m and len(cell) <= 20:
                names = [c for j, c in enumerate(cells) if j != i and c and c != "-" and not NR_NUMBER.search(c)]
                rows[f"NR{m.group(1)}{m.group(2)}"] = {
                    "trade_name": names[0] if names else "",
                    "legal_name": names[1] if len(names) > 1 else (names[0] if names else ""),
                }
    if rows:
        return rows
    # No table: take each list item / paragraph / div that holds an NR number.
    for block in re.findall(r"<(li|p|div|dd)[^>]*>(.*?)</\1>", page, re.S | re.I):
        text = _text(block[1])
        for m in NR_NUMBER.finditer(text):
            if len(text) > 300:
                continue  # a container holding many entries; its children are handled separately
            name = NR_NUMBER.sub("", text).strip(" -–|,;:")
            rows.setdefault(f"NR{m.group(1)}{m.group(2)}", {"trade_name": name, "legal_name": name})
    return rows


class QstChecker:
    def __init__(self, nr_list_file=None, delay=0.5, browser=None, cache_dir=None):
        self.session = new_session()
        self.throttle = Throttle(delay)
        self.nr_list_file = nr_list_file
        self.browser = browser  # fallback for the NR list when Revenu Québec blocks plain downloads
        self.cache_file = Path(cache_dir) / "qst_nr_list.html" if cache_dir else None
        self._nr_rows = None
        self.nr_error = None
        self.nr_refusals = []

    def check(self, number, suffix_assumed=False):
        """Validate a TQ number. `suffix_assumed` means the sheet had no TQ suffix and TQ0001 was filled in."""
        result = self._check(number)
        if suffix_assumed and result.status != REGISTERED:
            result.detail = (
                f"No TQ suffix was given, so {number} was checked. A business can have several QST "
                f"accounts (TQ0001, TQ0002, ...); get the full number from the supplier. {result.detail}"
            )
        return result

    def _check(self, number):
        self.throttle.wait()
        r = self.session.get(API.format(number), timeout=30)
        try:
            body = r.json()
            log.debug("QST API %s -> HTTP %s %s", number, r.status_code, body)
        except ValueError:
            log.warning("QST API %s -> HTTP %s, non-JSON body: %.300s", number, r.status_code, r.text)
            return Result(ERROR, f"Revenu Québec API answered HTTP {r.status_code} without data.")
        codes = {m.get("CodeMessage") for m in body.get("MessagesFonctionnels") or []}
        if body.get("OperationReussie") and body.get("Resultat"):
            res = body["Resultat"]
            name = res.get("NomEntreprise") or ""
            if res.get("RaisonSociale"):
                name += f" (trade name: {res['RaisonSociale']})"
            since = (res.get("DateStatut") or "")[:10]
            status = res.get("StatutSousDossierUsager")
            if status == "R":  # Régulier
                return Result(REGISTERED, f"Revenu Québec: registration valid since {since}.", name)
            if status == "A":  # Annulé
                return Result(NOT_REGISTERED, f"Revenu Québec: registration cancelled since {since}.", name)
            return Result(
                NOT_CONFIRMED,
                f"Revenu Québec status '{res.get('DescriptionStatut')}' since {since}; review before relying on it.",
                name,
            )
        if "GX.AucuneDonneeTrouvee" in codes:
            return Result(NOT_REGISTERED, "Revenu Québec has no QST registration under this number.")
        if "GX.IdentifiantEntreeInvalide" in codes:
            return Result(INVALID, "Revenu Québec says this is not a valid QST number (likely a typo).")
        return Result(ERROR, f"Revenu Québec API: HTTP {r.status_code} {', '.join(sorted(filter(None, codes)))}")

    def _nr_list_sources(self):
        """Yield (description, page HTML) from each way of getting the NR list, best first."""
        if self.nr_list_file:
            yield f"saved file {self.nr_list_file}", Path(self.nr_list_file).read_text(encoding="utf-8", errors="replace")
            return
        if self.cache_file and self.cache_file.exists() and time.time() - self.cache_file.stat().st_mtime < NR_CACHE_AGE:
            yield f"cached copy {self.cache_file}", self.cache_file.read_text(encoding="utf-8")
        try:
            r = self.session.get(NR_LIST, timeout=60)
            if r.ok:
                yield "download", r.text
            else:
                self.nr_refusals.append(f"download: HTTP {r.status_code}")
                log.info("Plain download of the NR list got HTTP %s; trying a real browser", r.status_code)
        except requests.RequestException as e:
            log.info("Plain download of the NR list failed (%s); trying a real browser", e)
        if self.browser:
            with self.browser.page() as page:
                resp = page.goto(NR_LIST, wait_until="networkidle", timeout=60000)
                log.debug("Browser load of NR list -> HTTP %s", resp and resp.status)
                if resp is not None and resp.status >= 400:
                    self.nr_refusals.append(f"browser: HTTP {resp.status}")
                else:
                    yield "browser download", page.content()

    def _load_nr_list(self):
        if self._nr_rows is not None:
            return self._nr_rows
        self._nr_rows = {}
        self.nr_refusals = []  # sources that answered with an HTTP error (blocked), as opposed to a page we couldn't read
        errors = []
        try:
            for source, page in self._nr_list_sources():
                rows = parse_nr_list(page)
                if rows:
                    self._nr_rows = rows
                    log.info("Loaded Revenu Québec NR list from %s: %d registrants", source, len(rows))
                    if self.cache_file and source.endswith("download"):
                        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
                        self.cache_file.write_text(page, encoding="utf-8")
                    break
                errors.append(f"{source} had no NR numbers (blocked, or the page layout changed)")
        except Exception as e:
            errors.append(f"{type(e).__name__}: {str(e).splitlines()[0]}")
        if not self._nr_rows:
            self.nr_error = "couldn't load Revenu Québec NR list (" + "; ".join(errors + self.nr_refusals or ["no source available"]) + ")"
            log.warning(self.nr_error)
        return self._nr_rows

    def check_nr(self, number):
        rows = self._load_nr_list()
        if self.nr_error:
            return Result(
                ERROR,
                f"{self.nr_error}. Save the page {NR_LIST} from your browser and re-run with --nr-list <saved file>.",
            )
        row = rows.get(number)
        if row:
            name = row["legal_name"] + (f" (trade name: {row['trade_name']})" if row["trade_name"] != row["legal_name"] else "")
            return Result(REGISTERED, "Listed by Revenu Québec as registered under the specified (NR) system.", name)
        return Result(NOT_REGISTERED, "Not on Revenu Québec's list of NR (non-resident) QST registrants.")
