"""Setup test: checks everything the tax check needs, in about 2 minutes, and writes a report.

    "Check setup" in Tax Checker, or "advanced\Test Setup.bat"   (or: python check_setup.py [workbook.xlsx])

Steps, each reported PASS / WARN / FAIL with a plain-English fix:
  1. Python and the installed packages
  2. Certificates (corporate networks re-sign web traffic)
  3. Can each government site be reached?
  4. Can the browser start and open the Manitoba / BC pages?
  5. The workbook: columns, row counts, what would be checked, can results be saved next to it?
  6. A few real lookups: a known public business (proves the site works) and a few of your suppliers

The report is saved in the "logs" folder next to the workbook (with a detailed log beside it), and copied
to the configured diagnostics folder (see taxcheck/diagnostics.py), so it can be read for debugging.
Nothing in the workbook is changed.
"""

import datetime as dt
import logging
import os
import platform
import sys
import time
import traceback
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
PASS, WARN, FAIL, INFO = "PASS", "WARN", "FAIL", "INFO"

log = logging.getLogger("check_setup")


class Report:
    def __init__(self):
        self.lines = []
        self.results = []  # (step, status, message)

    def say(self, text=""):
        print(text, flush=True)
        self.lines.append(text)
        log.info(text)

    def step(self, title):
        self.say("")
        self.say(f"=== {title} ===")

    def result(self, status, what, detail="", fix=""):
        self.results.append((status, what, detail, fix))
        self.say(f"  [{status}] {what}" + (f": {detail}" if detail else ""))
        if fix and status in (WARN, FAIL):
            self.say(f"         Fix: {fix}")

    def save(self, path):
        path.write_text("\n".join(self.lines) + "\n", encoding="utf-8")


def explain_error(e):
    """(short description, suggested fix) for a failed web request, in plain language."""
    import requests

    text = f"{type(e).__name__}: {e}"
    if isinstance(e, requests.exceptions.SSLError) or "CERTIFICATE_VERIFY_FAILED" in text:
        return ("certificate not trusted (the network re-signs web traffic)",
                "re-run \"advanced\\Test Setup.bat\" (it installs 'truststore'). If it still fails, ask IT whether Python may use the "
                "Windows certificate store, or for the company root certificate.")
    if isinstance(e, requests.exceptions.ProxyError) or "ProxyError" in text:
        return ("the network proxy refused or needs settings", "ask IT for the proxy address and set HTTPS_PROXY.")
    if isinstance(e, (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout)):
        return ("no answer within 20 seconds (site down, or blocked by the network)",
                "open the address in your browser. If it opens there, the network may block Python: ask IT.")
    if "NameResolution" in text or "getaddrinfo" in text:
        return ("the site's address couldn't be looked up (no internet / DNS)", "check you're online (VPN if remote).")
    return (text.splitlines()[0][:200], "see the detailed log next to this report.")


def check_python(rep):
    rep.step("1. Python and packages")
    v = sys.version_info
    ver = f"{v.major}.{v.minor}.{v.micro}"
    if (v.major, v.minor) < (3, 9):
        rep.result(FAIL, "Python version", ver, "install Python 3.12 from python.org.")
    elif (v.major, v.minor) > (3, 13):
        rep.result(WARN, "Python version", f"{ver} (tool tested on 3.11-3.13; usually fine)",
                   "if odd errors appear, install Python 3.12 and re-run \"advanced\\Test Setup.bat\".")
    else:
        rep.result(PASS, "Python version", ver)
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    rep.result(PASS if in_venv else WARN, "Tool's own Python environment (.venv)", "in use" if in_venv else "not in use",
               "start this with \"Tax Checker.bat\" rather than plain python.")
    from importlib import metadata

    ok = True
    for pkg in ("requests", "openpyxl", "playwright", "truststore"):
        try:
            rep.result(PASS, f"Package {pkg}", metadata.version(pkg))
        except metadata.PackageNotFoundError:
            ok = False
            rep.result(FAIL if pkg != "truststore" else WARN, f"Package {pkg}", "not installed",
                       "re-run \"advanced\\Test Setup.bat\" (it installs requirements.txt).")
    return ok


def check_certificates(rep):
    rep.step("2. Certificates")
    from taxcheck.certs import use_system_certificates

    if use_system_certificates():
        rep.result(PASS, "Using the Windows certificate store", "lookups trust the same certificates as your browser")
    else:
        rep.result(WARN, "Using Python's own certificate list",
                   "on networks with TLS inspection this causes CERTIFICATE_VERIFY_FAILED", "re-run \"advanced\\Test Setup.bat\" to install truststore.")


def check_sites(rep):
    rep.step("3. Government sites (one request each)")
    import requests

    from taxcheck.bc_pst import ETAXBC
    from taxcheck.fedcorp import SOURCES
    from taxcheck.gst import REGISTRY, SIMPLIFIED_LIST
    from taxcheck.http import USER_AGENT
    from taxcheck.mb_rst import TAXCESS_RST
    from taxcheck.orgbook import SEARCH
    from taxcheck.qst import API, NR_LIST
    from taxcheck.sk_pst import SK_REGISTRY

    # (label, url, importance): "required" blocks the run; others reduce what can be checked.
    sites = [
        ("CRA GST/HST Registry", REGISTRY + "reg_01_Ld.action", "required"),
        ("CRA simplified GST/HST list", SIMPLIFIED_LIST, "optional"),
        ("Revenu Québec QST API", API.format("1019288451TQ0004"), "optional"),
        ("Revenu Québec NR list", NR_LIST, "optional"),
        ("OrgBook BC (official names)", f"{SEARCH}?q=108161779", "optional"),
        ("Corporations Canada data (official names)", SOURCES[0], "optional"),
        ("Manitoba TAXcess (RST)", TAXCESS_RST, "optional"),
        ("eTaxBC (BC PST)", ETAXBC, "optional"),
        ("Saskatchewan SETS (SK PST)", SK_REGISTRY, "info"),
    ]
    status = {}
    for label, url, importance in sites:
        t0 = time.monotonic()
        try:
            # stream=True: only the headers are read, so the 110 MB federal file isn't downloaded here.
            r = requests.get(url, timeout=20, stream=True, headers={"User-Agent": USER_AGENT})
            r.close()
            secs = time.monotonic() - t0
            log.debug("%s %s -> HTTP %s in %.1fs", label, url, r.status_code, secs)
            if r.status_code < 400:
                rep.result(PASS, label, f"HTTP {r.status_code} in {secs:.1f}s")
                status[label] = True
                continue
            detail = f"site answered HTTP {r.status_code}" + (" (refused this network)" if r.status_code == 403 else
                                                               " (site error / maintenance)" if r.status_code >= 500 else "")
            fix = "re-run later; if it persists, the site may block this network."
        except Exception as e:
            log.debug("%s %s failed", label, url, exc_info=True)
            detail, fix = explain_error(e)
        status[label] = False
        if importance == "info":
            rep.result(INFO, label, f"{detail}. Expected: Saskatchewan is checked in a visible browser (--sk-assist) instead.")
        elif label == "CRA simplified GST/HST list":
            rep.result(WARN, label, f"{detail}. The tool loads this page through the browser instead (see step 4).",
                       "nothing, if step 4 loads it. It only matters for non-resident digital sellers.")
        elif label == "Revenu Québec NR list":
            rep.result(WARN, label, detail, "only matters for NR-type QST numbers (none in the vendor export).")
        else:
            rep.result(FAIL if importance == "required" else WARN, label, detail, fix)
    return status


def check_browser(rep, log_dir):
    rep.step("4. Browser (Manitoba and BC lookups; CRA simplified list)")
    from taxcheck.bc_pst import ETAXBC
    from taxcheck.browser import Browser, BrowserUnavailable
    from taxcheck.gst import SIMPLIFIED_LIST, SimplifiedList
    from taxcheck.mb_rst import TAXCESS_RST

    browser = Browser(debug_dir=log_dir)
    try:
        try:
            browser._ensure_started()
        except BrowserUnavailable as e:
            rep.result(FAIL, "Browser", str(e)[:400],
                       "make sure Microsoft Edge is installed and up to date, then re-run \"advanced\\Test Setup.bat\"; send the report if it still fails.")
            return False
        rep.result(PASS, "Browser started", browser.engine)
        pages = (("Manitoba TAXcess page", TAXCESS_RST), ("eTaxBC page", ETAXBC), ("CRA simplified list page (canada.ca)", SIMPLIFIED_LIST))
        for label, url in pages:
            t0 = time.monotonic()
            try:
                with browser.page() as page:
                    resp = page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(2000)
                    title = page.title()
                    code = resp.status if resp else "?"
                    shot = log_dir / f"setup_{label.split()[0].lower()}.png"
                    page.screenshot(path=str(shot))
                    # The page as sent, not the live view (its scripts may still be paging the table).
                    html = (resp.text() if resp is not None else page.content()) if "canada.ca" in label else ""
                secs = time.monotonic() - t0
                ok = resp is None or resp.status < 400
                extra = ""
                if html:
                    n = len(SimplifiedList.parse(html))
                    ok = ok and n >= SimplifiedList.MIN_NUMBERS
                    extra = f", {n} registrants listed" + ("" if ok else " (incomplete: not saved)")
                    if ok:  # save it where the full run looks first, so the run doesn't have to fetch it again
                        cache = HERE / "cache"
                        cache.mkdir(exist_ok=True)
                        (cache / "cra_simplified_list.html").write_text(html, encoding="utf-8")
                rep.result(PASS if ok else WARN, label, f"HTTP {code}, title '{title}'{extra} in {secs:.1f}s",
                           "the site may be in maintenance; re-run later. Send the report if it persists.")
            except Exception as e:
                first = str(e).splitlines()[0][:200] if str(e) else type(e).__name__
                log.debug("Browser check %s failed", label, exc_info=True)
                rep.result(WARN, label, first, "send the report (the detailed log has the full error).")
        return True
    finally:
        browser.close()


def duration(hours):
    """0.04 -> '3 minutes', 2.5 -> '2.5 hours'."""
    minutes = max(round(hours * 60), 1)
    return (f"{minutes} minute" + ("s" if minutes > 1 else "")) if hours < 1 else f"{hours:.1f} hours"


def span(hours):
    """'2.5 hours-3.5 hours' style range, or one value when both ends round the same."""
    low, high = duration(hours), duration(hours * 1.4)
    return low if low == high else f"{low} to {high}"


def check_workbook(rep, path):
    rep.step("5. Workbook")
    from openpyxl.utils import get_column_letter

    from taxcheck.checks import Offline, RowChecker
    from taxcheck.numbers import normalize_tax_id, parse_bn
    from taxcheck.workbook import InputError, SupplierSheet

    rep.say(f"  File: {path}")
    t0 = time.monotonic()
    try:
        sheet = SupplierSheet(path)
    except InputError as e:
        rep.result(FAIL, "Reading the workbook", str(e), "send the report; the column names may need adding.")
        return None
    except PermissionError:
        rep.result(FAIL, "Reading the workbook", "file is locked", "close it in Excel and try again.")
        return None
    rows = list(sheet.rows())
    rep.result(PASS, "Read", f"{len(rows)} supplier rows in {time.monotonic() - t0:.0f}s")
    mapped = ", ".join(f"{k} <- '{sheet.ws.cell(sheet.header_row, c).value}' ({get_column_letter(c)})" for k, c in sheet.cols.items())
    rep.say(f"  Columns: {mapped}")
    for need in ("name", "gst"):
        if need not in sheet.cols:
            rep.result(FAIL, f"Column for {need}", "not found", "send the report so the header can be added.")
            return None

    off = Offline()
    checker = RowChecker(off, off, bc=off, mb=off, sk=off, available=sheet.cols)
    overall, gst_status, provinces = Counter(), Counter(), Counter()
    to_lookup, mb_rows, samples = set(), 0, []
    for r, values in rows:
        res = checker.check(values, dt.date.today())
        country, prov, foreign, _ = RowChecker.locate(values)
        statuses = [x.status for x in res.values()]
        gst_status[res["gst"].status] += 1
        if all(s == "NOT APPLICABLE" for s in statuses):
            overall["not applicable (foreign)"] += 1
        elif all(s == "NOT LOOKED UP" for s in statuses):
            overall["ready to look up"] += 1
        else:
            overall["needs review already (bad/missing number)"] += 1
        if not foreign:
            provinces[prov or "?"] += 1
        if "mb_rst" in res and res["mb_rst"].status == "NOT LOOKED UP":
            mb_rows += 1
        if res["gst"].status == "NOT LOOKED UP":
            kind, number, _ = normalize_tax_id(values.get("gst"))
            try:
                bn = parse_bn(number) if kind == "gst" else None
            except Exception:
                bn = None
            if bn:
                to_lookup.add(bn)
                if len(samples) < 3 or (prov == "MB" and not any(x[3] == "MB" for x in samples)):
                    samples.append((r, values, bn, prov))
    rep.say("  Rows: " + ", ".join(f"{k} {v}" for k, v in overall.most_common()))
    rep.say("  GST/HST: " + ", ".join(f"{k} {v}" for k, v in gst_status.most_common()))
    rep.say("  Canadian rows by province: " + ", ".join(f"{k} {v}" for k, v in provinces.most_common()))
    # ~3 s per GST/HST supplier on average (name mismatches need extra lookups), 3 in parallel by default;
    # Manitoba (~6 s each) runs on the main thread at the same time, so the longer of the two decides.
    # 3.0 s per supplier includes the extra name forms tried; +1.5 s for the quick registration-date check.
    hours = max(len(to_lookup) * 4.5 / 3, mb_rows * 6) / 3600
    rep.result(INFO, "Full run estimate", f"about {len(to_lookup)} GST/HST lookups + {mb_rows} Manitoba lookups, "
                                         f"roughly {span(hours)} with 3 parallel lookups and quick registration dates "
                                         "(the defaults; GST/HST only with no dates is about 40% faster)")

    # Can results be written next to the workbook?
    out = Path(path).with_name(f"{Path(path).stem}_tax_check.xlsx")
    try:
        probe = Path(path).parent / "logs" / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        if out.exists():
            with open(out, "a+b"):
                pass
        rep.result(PASS, "Results can be saved next to the workbook", out.name)
    except PermissionError:
        rep.result(FAIL, "Saving results", f"{out.name} is open or the folder is read-only", "close it in Excel.")
    return samples


def check_lookups(rep, samples, site_ok, browser_ok, log_dir):
    rep.step("6. Real lookups")
    from taxcheck.checks import clean_vendor_name
    from taxcheck.gst import GstChecker, cra_today
    from taxcheck.mb_rst import MbRstChecker
    from taxcheck.browser import Browser

    if not site_ok.get("CRA GST/HST Registry"):
        rep.result(FAIL, "GST/HST lookups", "skipped: the CRA registry couldn't be reached (step 3)", "fix step 3 first.")
        return
    today = cra_today()
    # Same set-up as a real run: the browser is the fallback for canada.ca, and the list is cached in the tool's folder.
    browser = Browser(debug_dir=log_dir) if browser_ok else None
    gst = GstChecker(delay=1.5, cache_dir=HERE / "cache")  # no official-name sources: no 110 MB download here
    gst.simplified.browser = browser
    # A run waits for the CRA registry when it's down (up to hours); this check reports it and finishes instead.
    gst.health.give_up_after = 0
    try:
        try:
            gst.registry_lookup("857305932", "Amazon.com.ca ULC", today)
        except Exception as e:
            rep.result(FAIL, "CRA GST/HST registry", f"isn't answering right now ({(gst.health.last_error or str(e))[:160]})",
                       "CRA's site is down (not your PC). A run started now waits for it, retrying every 2 minutes, "
                       "and carries on when it's back; or run this setup test again later.")
            return
        t0 = time.monotonic()
        res = gst.check("857305932", ["Amazon.com.ca ULC"], today)
        good = res.status == "REGISTERED"
        rep.result(PASS if good else FAIL, "Known business (Amazon.com.ca ULC, 857305932)",
                   f"{res.status} in {time.monotonic() - t0:.1f}s", "send the report: the CRA page may have changed.")
        if not good:
            rep.say(f"         Details: {res.detail}")
            return
        if gst.simplified.error:
            rep.result(WARN, "CRA simplified list", gst.simplified.error[:300],
                       "only affects non-resident digital sellers; the main GST/HST check works without it.")
        else:
            rep.result(PASS, "CRA simplified list loaded", f"{len(gst.simplified._rows)} business numbers")
        for r, values, bn, prov in samples[:3]:
            t0 = time.monotonic()
            try:
                res = gst.check(bn, [clean_vendor_name(values.get("name"))[0]], today)
                rep.result(PASS if res.status != "ERROR" else WARN, f"Row {r} {values.get('name')} ({bn}, {prov})",
                           f"{res.status} in {time.monotonic() - t0:.1f}s. {res.detail[:160]}")
            except Exception as e:
                rep.result(WARN, f"Row {r} {values.get('name')}", f"{type(e).__name__}: {e}")
                log.debug("lookup failed", exc_info=True)

        if not browser:
            rep.result(WARN, "Manitoba lookup", "skipped: the browser didn't work (step 4)")
            return
        mb = MbRstChecker(browser)
        t0 = time.monotonic()
        res = mb.check(["Amazon"], bn9="857305932")
        good = res.status == "REGISTERED"
        rep.result(PASS if good else WARN, "Manitoba known business (Amazon, 857305932)",
                   f"{res.status} in {time.monotonic() - t0:.1f}s. {res.detail[:200]}",
                   "send the report (a screenshot of the failed page is saved in the logs folder).")
        mb_sample = next(((r, v, bn) for r, v, bn, prov in samples if prov == "MB"), None)
        if good and mb_sample:
            r, values, bn = mb_sample
            res = mb.check([clean_vendor_name(values.get("name"))[0]], bn9=bn)
            rep.result(PASS if res.status != "ERROR" else WARN, f"Manitoba row {r} {values.get('name')}",
                       f"{res.status}. {res.detail[:160]}")
    except Exception as e:
        rep.result(WARN, "Lookups", f"{type(e).__name__}: {str(e).splitlines()[0][:200] if str(e) else ''}")
        log.debug("Lookup step failed", exc_info=True)
    finally:
        if browser:
            browser.close()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    print("Tax-Checker setup test." + ("" if argv else " Choose the supplier workbook in the window that opens..."),
          flush=True)
    sys.path.insert(0, str(HERE))
    from check_suppliers import choose_workbook, default_log_dir
    from taxcheck.diagnostics import copy_to_diagnostics
    from taxcheck.logsetup import setup_logging

    workbook = argv[0] if argv else choose_workbook()
    if workbook:
        from check_suppliers import remember_workbook

        remember_workbook(workbook)
    log_dir = default_log_dir(workbook) if workbook else HERE / "logs"
    log_path = setup_logging(log_dir)
    # The detailed log keeps everything; the console shows only the report lines printed below.
    for h in logging.getLogger().handlers:
        if not isinstance(h, logging.FileHandler):
            h.setLevel(logging.CRITICAL)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = log_dir / f"setup_test_{stamp}.txt"

    rep = Report()
    rep.say(f"Tax-Checker setup test, {dt.datetime.now():%Y-%m-%d %H:%M}")
    rep.say(f"Computer: {platform.node()} | Windows/OS: {platform.platform()} | Tool folder: {HERE}")
    steps = []
    try:
        steps.append(check_python(rep))
        check_certificates(rep)
        site_ok = check_sites(rep)
        browser_ok = check_browser(rep, log_dir)
        samples = None
        if workbook:
            samples = check_workbook(rep, workbook)
        else:
            rep.step("5. Workbook")
            rep.result(WARN, "No workbook chosen", "workbook checks skipped")
        check_lookups(rep, samples or [], site_ok, browser_ok, log_dir)
    except KeyboardInterrupt:
        rep.say("")
        rep.say("Stopped with Ctrl+C.")
    except Exception as e:
        rep.result(FAIL, "Unexpected error in the setup test", f"{type(e).__name__}: {e}", "send the report.")
        log.debug("Unexpected error", exc_info=True)
        rep.lines.append(traceback.format_exc())

    fails = [r for r in rep.results if r[0] == FAIL]
    warns = [r for r in rep.results if r[0] == WARN]
    rep.step("Summary")
    if fails:
        rep.say(f"  NOT READY: {len(fails)} problem(s) to fix:")
        for _, what, detail, fix in fails:
            rep.say(f"   - {what}: {detail}" + (f"  ->  {fix}" if fix else ""))
    else:
        rep.say("  READY: GST/HST lookups work." + (f" {len(warns)} warning(s) above (other checks may be limited)." if warns else ""))
        if not os.environ.get("TAXCHECK_TEST_RUN"):
            rep.say("  This test doesn't create a results workbook. The trial does (\"Trial (25 suppliers)\" in Tax Checker, or next here): 25 real lookups, about 2 minutes.")
    rep.say(f"  Report: {report_path}")
    rep.say(f"  Detailed log: {log_path}")
    rep.save(report_path)
    for h in logging.getLogger().handlers:
        h.flush()
    shots = sorted(Path(log_dir).glob("setup_*.png")) + sorted(Path(log_dir).glob("*fail*.png"))
    folder = copy_to_diagnostics(report_path, log_path, *shots[-6:])
    if folder:
        print(f"  Copied to diagnostics folder: {folder}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
