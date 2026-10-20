"""Progress reporting for long runs (a 10,000-vendor list takes hours).

Three things, all written as the run goes so nothing is lost if it stops:

* a progress file, ``progress_<timestamp>.csv`` in the logs folder: one line per supplier, flushed
  immediately, with every status and detail, how long the row took and any error. Open it in Excel
  while the run is going to see where it is; it's also what ``--resume`` reads to pick up after a stop.
* a progress line in the log (and on screen) every N suppliers or minutes: done/total, percent, speed,
  time left, and the status counts so far.
* (in check_suppliers.run) a checkpoint save of the results workbook every N suppliers.
"""

import csv
import datetime as dt
import json
import logging
import time
from collections import Counter
from pathlib import Path

from .result import ERROR, MANUAL, Result

log = logging.getLogger(__name__)

TAX_KEYS = ["gst", "qst", "bc_pst", "sk_pst", "mb_rst"]
FIELDS = (
    ["sheet_row", "supplier", "country", "province", "tax_id", "seconds", "finished_at", "source"]
    + [f"{k}_status" for k in TAX_KEYS]
    + [f"{k}_detail" for k in TAX_KEYS]
    + ["names_on_record", "errors", "results_json"]
)


def _fmt_duration(seconds):
    seconds = int(max(seconds, 0))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


class ProgressReport:
    def __init__(self, log_dir, total, every_rows=25, every_seconds=120):
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        self.path = log_dir / f"progress_{dt.datetime.now():%Y%m%d_%H%M%S}.csv"
        self.total = total
        self.every_rows = max(int(every_rows or 0), 0)
        self.every_seconds = every_seconds
        self.done = 0
        self.counts = Counter()
        self.errors = 0
        self.started = time.monotonic()
        self._last_report = self.started
        # utf-8-sig so Excel shows accents in supplier names correctly.
        self._file = open(self.path, "w", newline="", encoding="utf-8-sig")
        self._writer = csv.DictWriter(self._file, fieldnames=FIELDS)
        self._writer.writeheader()
        self._file.flush()
        log.info("Progress file: %s", self.path)

    def record(self, sheet_row, values, results, seconds, location=("", ""), source="checked"):
        """Write one supplier's outcome and, now and then, a progress line."""
        statuses = {k: r.status for k, r in results.items()}
        errors = [f"{k}: {r.detail}" for k, r in results.items() if r.status == ERROR]
        names = []
        for r in results.values():
            if r.registered_name and r.registered_name not in names:
                names.append(r.registered_name)
        line = {
            "sheet_row": sheet_row,
            "supplier": values.get("name") or "",
            "country": location[0],
            "province": location[1],
            "tax_id": values.get("gst") or "",
            "seconds": f"{seconds:.1f}",
            "finished_at": dt.datetime.now().isoformat(timespec="seconds"),
            "source": source,
            "names_on_record": "; ".join(names),
            "errors": " | ".join(errors),
            "results_json": json.dumps({k: [r.status, r.detail, r.registered_name, r.name_check, r.facts] for k, r in results.items()},
                                       ensure_ascii=False, default=str),
        }
        for k in TAX_KEYS:
            if k in results:
                line[f"{k}_status"] = results[k].status
                line[f"{k}_detail"] = results[k].detail
        self._writer.writerow(line)
        self._file.flush()  # so the file is current even if the run is killed

        if source != "checked":
            return  # resumed rows were checked in an earlier run; they don't count toward speed/ETA
        self.done += 1
        self.counts.update(statuses.values())
        self.errors += bool(errors)
        now = time.monotonic()
        if (self.every_rows and self.done % self.every_rows == 0) or now - self._last_report >= self.every_seconds \
                or self.done == self.total:
            self._last_report = now
            log.info("%s", self.summary_line())

    def summary_line(self):
        elapsed = time.monotonic() - self.started
        rate = elapsed / self.done if self.done else 0
        left = rate * (self.total - self.done)
        eta = (dt.datetime.now() + dt.timedelta(seconds=left)).strftime("%H:%M")
        pct = 100 * self.done / self.total if self.total else 100
        counts = ", ".join(f"{k} {v}" for k, v in self.counts.most_common())
        return (
            f"PROGRESS {self.done}/{self.total} ({pct:.1f}%) | elapsed {_fmt_duration(elapsed)} | "
            f"{rate:.1f} s/supplier | about {_fmt_duration(left)} left (done ~{eta}) | "
            f"rows with errors {self.errors} | so far: {counts or '-'}"
        )

    def close(self):
        if not self._file.closed:
            self._file.close()


def load_resume(path):
    """Results from an earlier run's progress file: {sheet_row: {tax: Result}}.

    Rows that had an ERROR are left out, so a resumed run retries them. So are Saskatchewan rows left for a
    manual check because the searches couldn't run (the 100-a-day limit, nobody ticked the CAPTCHA, the window
    was closed, no --sk-assist): "re-run with --resume" is what the run tells the user. A name too short to
    search stays as it was (searching again can't help).
    """
    path = Path(path)
    done, retried = {}, 0
    with open(path, newline="", encoding="utf-8-sig") as f:
        for line in csv.DictReader(f):
            try:
                row = int(line["sheet_row"])
                data = json.loads(line["results_json"])
            except (KeyError, ValueError, TypeError):
                continue
            results = {k: Result(*v) for k, v in data.items()}
            sk = results.get("sk_pst")
            sk_not_searched = (sk is not None and sk.status == MANUAL and "at least 4 letters" not in sk.detail)
            if sk_not_searched or any(r.status == ERROR for r in results.values()):
                retried += 1
                done.pop(row, None)
                continue
            done[row] = results
    log.info("Resuming from %s: %d suppliers already done, %d with errors or Saskatchewan searches still to do will "
             "be checked again", path, len(done), retried)
    return done
