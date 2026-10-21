"""What the window (gui.py) shows and runs, kept apart from the widgets so it can be tested without a display.

The window never checks anything itself: it runs the same scripts as the old .bat files (check_suppliers.py,
check_setup.py, profile_workbook.py, run_test.py) with the options chosen in it, and reads their output.

- `next_step(tool_dir, workbook, settings)`: what the person should do next, from the notes the scripts leave
  behind (.last_run, cache/saskatchewan_progress.json, cache/saskatchewan_searches.json, the window's settings).
- `command(action, workbook, options, stop_file)`: the script and options for each button (no console menus).
- `parse_line(line)`: what a line of a script's output means for the window (progress, CAPTCHA, results...).
"""

import datetime as dt
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

SETTINGS_FILE = "gui_settings.json"  # in the tool folder (not committed): the window's choices and setup result
SK_DAILY_LIMIT = 100

# Actions (buttons). Each is a script run with options; see command().
FULL, RESUME, SK_BATCH, FINISH, TRIAL, OFFLINE, PROFILE, SETUP, TEST = (
    "full", "resume", "sk_batch", "finish", "trial", "offline", "profile", "setup", "test")
CHECK_RUNS = (FULL, RESUME, SK_BATCH, FINISH, TRIAL, OFFLINE)  # check_suppliers.py: stoppable with the stop file

TAX_KEYS = ("gst", "qst", "bc_pst", "mb_rst", "sk_pst")
TAX_ARGS = {"gst": "gst", "qst": "qst", "bc_pst": "bc", "mb_rst": "mb", "sk_pst": "sk"}
DEFAULT_OPTIONS = {"taxes": list(TAX_KEYS), "sk_now": True, "gst_dates": "end", "paid_since": ""}


def _read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def load_settings(tool_dir):
    data = _read_json(Path(tool_dir) / SETTINGS_FILE, {})
    return data if isinstance(data, dict) else {}


def save_settings(tool_dir, settings):
    path = Path(tool_dir) / SETTINGS_FILE
    try:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(settings, indent=1), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass  # only a convenience


def options_from(settings):
    opts = dict(DEFAULT_OPTIONS)
    opts.update({k: v for k, v in (settings.get("options") or {}).items() if k in DEFAULT_OPTIONS})
    opts["taxes"] = [t for t in TAX_KEYS if t in (opts.get("taxes") or [])] or list(TAX_KEYS)
    if opts.get("gst_dates") not in ("none", "end", "full"):
        opts["gst_dates"] = "end"
    return opts


def same_file(a, b):
    if not a or not b:
        return False
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return str(a).lower() == str(b).lower()


def last_run(tool_dir):
    """The latest full run's note (check_suppliers.remember_run), or None."""
    data = _read_json(Path(tool_dir) / ".last_run", None)
    return data if isinstance(data, dict) and data.get("workbook") else None


def saskatchewan_progress(tool_dir, workbook):
    """This workbook's Saskatchewan counts (check_suppliers.record_saskatchewan_progress), or None."""
    data = _read_json(Path(tool_dir) / "cache" / "saskatchewan_progress.json", {})
    if not isinstance(data, dict):
        return None
    for path, entry in data.items():
        if same_file(path, workbook) and isinstance(entry, dict):
            return entry
    return None


def saskatchewan_searches(tool_dir, now=None, limit=SK_DAILY_LIMIT):
    """(searches left now, when the whole day's worth is back) from the search log the checker keeps (each search
    frees up 24 hours after it was made). The time is None when no search was made in the last 24 hours."""
    now = time.time() if now is None else now
    times = _read_json(Path(tool_dir) / "cache" / "saskatchewan_searches.json", [])
    try:
        recent = sorted(float(t) for t in times if float(t) > now - 24 * 3600)
    except (TypeError, ValueError):
        recent = []
    left = max(limit - len(recent), 0)
    return left, (dt.datetime.fromtimestamp(recent[-1] + 24 * 3600) if recent else None)


@dataclass
class Step:
    """The recommended next action for the window's banner."""
    action: str = ""  # one of the actions above, or "" (nothing to run: choose a workbook / all done / wait)
    button: str = ""  # the big button's label
    title: str = ""  # one line: where things are
    why: str = ""  # one or two lines: what happens when the button is clicked
    sk: dict = field(default_factory=dict)  # Saskatchewan counts, when known
    ready_at: dt.datetime = None  # Saskatchewan: when the next batch is worth running


def _when(moment, now):
    if moment.date() == now.date():
        return f"today at {moment:%H:%M}"
    if moment.date() == (now + dt.timedelta(days=1)).date():
        return f"tomorrow at {moment:%H:%M}"
    return f"{moment:%a %d %b at %H:%M}"


def next_step(tool_dir, workbook, settings=None, now=None):
    """What to do next for this workbook. Order: set up, full run, resume a stopped run, Saskatchewan batches,
    copy the Saskatchewan answers into the full run's results, done."""
    settings = load_settings(tool_dir) if settings is None else settings
    now_ts = time.time() if now is None else now
    now_dt = dt.datetime.fromtimestamp(now_ts)
    if not workbook:
        return Step(title="Choose the supplier workbook to start.",
                    why="Click \"Choose...\" above. The workbook itself is never changed: results go in a copy next to it.")
    if not Path(workbook).exists():
        return Step(title="The chosen workbook isn't there any more.",
                    why=f"Choose it again (it was {workbook}).")
    setup = settings.get("setup_ok") or {}
    run = last_run(tool_dir)
    this_run = run if run and same_file(run.get("workbook"), workbook) else None
    if not this_run and not same_file(setup.get("workbook"), workbook):  # (a run under way is never held up)
        return Step(SETUP, "Check setup", "First: a quick check that everything works (about 2 minutes).",
                    "It checks the government sites, the browser and a few real lookups on this workbook, and creates "
                    "no results. Then the full check is the next step.")
    run = this_run
    if not run:
        return Step(FULL, "Start the full check", "Ready for the full check of this workbook.",
                    "First you'll see the options (what's checked and how, each explained), then click Start. It runs "
                    "by itself for a few hours and keeps the PC awake (leave it on and on the network).")
    if not run.get("finished"):
        return Step(RESUME, "Continue the stopped check", "The last full check stopped before the end.",
                    "It continues where it stopped, with the same choices; finished suppliers aren't checked again.")
    with_sk = run.get("taxes") is None or "sk_pst" in (run.get("taxes") or [])
    prog = saskatchewan_progress(tool_dir, workbook) if with_sk else None
    if prog and prog.get("total"):
        total, answered = prog["total"], prog.get("answered", 0)
        by_hand, waiting = prog.get("by_hand", 0), prog.get("waiting", 0)
        counts = dict(total=total, answered=answered, by_hand=by_hand, waiting=waiting)
        hand = f" {by_hand} need a search by hand." if by_hand else ""
        if waiting:
            left, full = saskatchewan_searches(tool_dir, now_ts)
            title = f"Saskatchewan: {answered} of {total} suppliers have an answer, {waiting} to go.{hand}"
            if left >= min(waiting, 20):  # worth a batch now (even a part one)
                part = "" if left >= min(waiting, SK_DAILY_LIMIT) else f" ({left} searches free now; the full day's " \
                                                                       f"{SK_DAILY_LIMIT} from {_when(full, now_dt)})"
                return Step(SK_BATCH, "Run today's Saskatchewan batch", title,
                            "A browser window opens: tick 'I agree' and 'I'm not a robot', then Next (you have 5 "
                            f"minutes). Up to {min(left, waiting)} searches then run by themselves (about "
                            f"{max(round(min(left, waiting) * 12 / 60 / 5) * 5, 5)} minutes)."
                            + part, counts)
            used = "Today's searches are used up." if not left else f"Only {left} of today's searches are free now."
            return Step("", "", title, f"{used} Come back {_when(full, now_dt)} for the next batch (open this "
                        "window and click the button that appears). Answers so far are kept.", counts, ready_at=full)
        if prog.get("in_results_waiting", 0):
            return Step(FINISH, "Add the Saskatchewan answers to the results",
                        f"Saskatchewan: all {total} suppliers are done ({answered} have an answer).{hand}",
                        "Last step: puts every Saskatchewan answer into the full check's results workbook. No CAPTCHA "
                        "and no searches; it re-checks those suppliers' other taxes too (about half an hour).", counts)
        return Step("", "", "All done: the results workbook is complete." + hand,
                    "Click \"Open results\". To check again later (e.g. next quarter), click \"Start a new full "
                    "check\" under More.", counts)
    return Step("", "", "All done: the results workbook is complete.",
                "Click \"Open results\". To check again later (e.g. next quarter), click \"Start a new full check\" "
                "under More.")


def command(action, workbook, options, stop_file=None, last=None):
    """[script, options...] for an action (run with the tool's Python, in the tool folder). `last` is the last full
    run's note (.last_run), for continuing it with its own choices."""
    opts = options_from({"options": options or {}})
    taxes = opts["taxes"]
    run_opts = []  # (no --show-result: the window has "Open results", and Explorer popping up after each batch is noise)
    if stop_file and action in CHECK_RUNS:
        run_opts += ["--stop-file", str(stop_file)]
    if action == SETUP:
        return ["check_setup.py", str(workbook)]
    if action == PROFILE:
        return ["profile_workbook.py", str(workbook)]
    if action == TEST:
        return ["run_test.py"]
    if action == SK_BATCH:
        return ["check_suppliers.py", "--sk-only", *run_opts]  # the last full run's workbook and cut-off
    if action in (RESUME, FINISH):
        # Same workbook and choices as the stopped run (its own Saskatchewan now-or-later, not today's Options).
        # Finishing uses the saved Saskatchewan answers (--sk-assist; the browser opens only if a search is still
        # needed, and none is). --sk-later: no question about it (there's no console to answer in).
        extra = ["--sk-assist"] if action == FINISH or (last or {}).get("sk_assist") else ["--sk-later"]
        return ["check_suppliers.py", "--resume", "last", *extra, *run_opts]
    argv = ["check_suppliers.py", str(workbook)]
    if set(taxes) != set(TAX_KEYS):
        argv += ["--taxes", ",".join(TAX_ARGS[t] for t in taxes)]
    if action == OFFLINE:
        argv += ["--paid-since", str(opts["paid_since"]).strip()] if opts.get("paid_since") else []
        return argv + ["--offline", *run_opts]
    if opts["sk_now"] and "sk_pst" in taxes:
        argv.append("--sk-assist")
    argv += ["--gst-dates", opts["gst_dates"]]
    if opts.get("paid_since"):
        argv += ["--paid-since", str(opts["paid_since"]).strip()]
    if action == TRIAL:
        argv += ["--limit", "25"]
    return argv + run_opts


def check_paid_since(text):
    """'' if fine (blank = everyone), else what's wrong."""
    text = (text or "").strip()
    if not text:
        return ""
    try:
        day = dt.date.fromisoformat(text)
    except ValueError:
        return "Paid-since must be a date like 2022-01-01 (or blank to check everyone)."
    return "Paid-since can't be in the future." if day > dt.date.today() else ""


# --- reading the scripts' output -------------------------------------------------------------------------------

_PROGRESS = re.compile(r"PROGRESS (\d+)/(\d+) \(([\d.]+)%\).*?about (.+?) left \(done ~(\d\d:\d\d)\)")
_ROW = re.compile(r"\[(\d+)/(\d+)\] row \d+")
_SK_ROW = re.compile(r"\[Saskatchewan (\d+)/(\d+)\]")
_PAUSE = re.compile(r"(.+?) isn't responding .*?pausing, next try in (\d+) s")
_READY = re.compile(r"\b(NOT READY|READY): (.+)")
CAPTCHA_TEXT = ("In the Saskatchewan browser window: tick 'I agree' and 'I'm not a robot', then click Next "
                "(you have 5 minutes).")


@dataclass
class Event:
    # progress (done/total, text) | sk_progress | captcha | captcha_done | paused | resumed | results | folder |
    # batch | stopped | ready (done=1 READY, 0 NOT READY) | zip
    kind: str
    done: int = 0
    total: int = 0
    text: str = ""


def parse_line(line):
    """The meaning of one line of output for the window, or None for an ordinary log line."""
    line = line.rstrip()
    m = _PROGRESS.search(line)
    if m:
        done, total = int(m.group(1)), int(m.group(2))
        return Event("progress", done, total, f"{done:,} of {total:,} suppliers · about {m.group(4)} left "
                                              f"(done ~{m.group(5)})")
    m = _ROW.search(line)
    if m:
        return Event("progress", int(m.group(1)), int(m.group(2)))
    m = _SK_ROW.search(line)
    if m:
        return Event("sk_progress", int(m.group(1)), int(m.group(2)))
    if ">>> Saskatchewan" in line:  # the run's warning before the window opens, and the checker's CAPTCHA prompt
        return Event("captcha", text=CAPTCHA_TEXT)
    if "Saskatchewan search page is open" in line:
        return Event("captcha_done", text="CAPTCHA done: the Saskatchewan searches are running by themselves.")
    m = _PAUSE.search(line)
    if m:
        return Event("paused", text=f"{m.group(1)} isn't answering. The tool waits and retries by itself every "
                                    f"{max(int(m.group(2)) // 60, 1)} minute(s): nothing to do, leave it running.")
    if " is responding again" in line:
        return Event("resumed")
    if "RESULTS WORKBOOK:" in line:
        return Event("results", text=line.split("RESULTS WORKBOOK:", 1)[1].strip())
    if "SAVED IN:" in line:
        return Event("folder", text=line.split("SAVED IN:", 1)[1].strip())
    if "SASKATCHEWAN BATCH:" in line:
        return Event("batch", text=line.split("SASKATCHEWAN BATCH:", 1)[1].strip())
    if "Saving the results workbook" in line:
        return Event("saving")
    if "Stopped early after" in line:
        return Event("stopped", text="Stopped early after" + line.split("Stopped early after", 1)[1].split(";")[0])
    m = _READY.search(line)
    if m:
        return Event("ready", int(m.group(1) == "READY"), text=f"{m.group(1)}: {m.group(2)}")
    if line.startswith("Send this file:"):
        return Event("zip", text=line.split(":", 1)[1].strip())
    return None


def results_file(workbook, folder=None, name=None):
    """The results workbook to open: the one the last run named, else the newest results next to the workbook."""
    if name and folder and (Path(folder) / name).exists():
        return Path(folder) / name
    if not workbook:
        return None
    wb = Path(workbook)
    found = [p for suffix in ("_tax_check", "_saskatchewan") for p in wb.parent.glob(f"{glob_escape(wb.stem)}{suffix}*.xlsx")
             if not p.name.startswith("~$")]
    return max(found, key=lambda p: p.stat().st_mtime) if found else None


def glob_escape(text):
    return re.sub(r"([*?\[])", r"[\1]", text)
