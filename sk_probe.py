"""Saskatchewan probe: capture what the PST registry's results pages look like, so the tool can read them.

    Double-click "Saskatchewan Probe.bat"   (or: python sk_probe.py ["Business name" ...])

A browser window opens on the registry. Tick "I agree" and "I'm not a robot" and click Next; the probe then
searches a few well-known Saskatchewan businesses (or the names you give). For each search it saves, in a
"logs/sk_probe_<date>" folder: a screenshot, the page itself (.html), and what the tool read from it. Send that
folder (or zip it) to whoever maintains the tool. It uses a few of the registry's 100 searches per day.
"""

import datetime as dt
import json
import logging
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Well-known Saskatchewan businesses that certainly hold a vendor's licence, plus one that can't exist.
DEFAULT_NAMES = ["Federated Co-operatives Limited", "Saskatchewan Telecommunications", "Zzqx Imaginary Widgets"]


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    sys.path.insert(0, str(HERE))
    from taxcheck.browser import Browser
    from taxcheck.certs import use_system_certificates
    from taxcheck.diagnostics import copy_to_diagnostics
    from taxcheck.logsetup import setup_logging
    from taxcheck.sk_pst import SkPstAssistant, classify, search_terms

    out = HERE / "logs" / f"sk_probe_{dt.datetime.now():%Y%m%d_%H%M%S}"
    out.mkdir(parents=True, exist_ok=True)
    log_path = setup_logging(out)
    log = logging.getLogger("sk_probe")
    use_system_certificates()
    names = argv or DEFAULT_NAMES
    print("A browser window opens: tick 'I agree' and 'I'm not a robot', click Next, then leave it alone.")
    report = []
    with Browser(headless=False, debug_dir=out) as browser:
        sk = SkPstAssistant(browser, counter_file=HERE / "cache" / "saskatchewan_searches.json")
        for name in names:
            entry = {"name": name, "terms": search_terms(name)}
            try:
                sk._ready()
                term = entry["terms"][0]
                rows, text = sk._search(term)
                sk.searches_since_captcha += 1
                stem = out / f"{len(report) + 1}_{''.join(c if c.isalnum() else '_' for c in term)[:30]}"
                sk.page.screenshot(path=f"{stem}.png", full_page=True)
                Path(f"{stem}.html").write_text(sk.page.content(), encoding="utf-8")
                status, detail, row = classify(rows, text, [name])
                entry.update(searched=term, rows=rows, page_text=text[:4000], tool_reads=status, detail=detail,
                             url=sk.page.url, files=[f"{stem.name}.png", f"{stem.name}.html"])
                print(f"  {name}: {status}. {detail}")
            except Exception as e:
                entry["error"] = f"{type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}"
                log.exception("Probe search for %s failed", name)
                print(f"  {name}: failed ({entry['error']})")
            report.append(entry)
    (out / "probe_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    for h in logging.getLogger().handlers:
        h.flush()
    print(f"\nSaved in {out}\nSend that folder (zip it) to whoever maintains the tool.")
    copy_to_diagnostics(out / "probe_report.json", log_path, *sorted(out.glob("*.png")), *sorted(out.glob("*.html")))
    return 0


def run_and_record():
    """Run the probe; if it fails, say why on screen and in logs/sk_probe_error.txt (the window may close)."""
    import traceback

    try:
        return main()
    except Exception:
        text = traceback.format_exc()
        print("\nThe probe stopped with an error:\n" + text)
        try:
            (HERE / "logs").mkdir(exist_ok=True)
            (HERE / "logs" / "sk_probe_error.txt").write_text(text, encoding="utf-8")
            print(f"Also saved in {HERE / 'logs' / 'sk_probe_error.txt'}")
        except OSError:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(run_and_record())
