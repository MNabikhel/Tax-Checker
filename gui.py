"""Tax Checker's window: one place to set up, run, resume, and do the daily Saskatchewan batches.

Started by "Tax Checker.bat". It runs the same scripts as the .bat files in the "advanced" folder, with the
choices made here (taxes, dates, cut-off), shows their progress, and says what to do next. Everything it
needs to work out the next step is in the notes the scripts leave (see taxcheck/gui_logic.py).
"""

import contextlib
import datetime as dt
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from taxcheck import gui_logic as logic

HERE = Path(__file__).resolve().parent
STOP_FILE = HERE / "cache" / "stop_requested"
MAX_LOG_LINES = 3000
WINDOWS = sys.platform == "win32"

TAX_NAMES = {"gst": "GST/HST", "qst": "QST", "bc_pst": "BC PST", "mb_rst": "Manitoba RST", "sk_pst": "Saskatchewan PST"}
ACTION_NAMES = {logic.FULL: "Full check", logic.RESUME: "Continuing the check", logic.SK_BATCH: "Saskatchewan batch",
                logic.FINISH: "Adding the Saskatchewan answers", logic.TRIAL: "Trial (25 suppliers)",
                logic.OFFLINE: "Offline check", logic.PROFILE: "Workbook profile", logic.SETUP: "Setup check",
                logic.TEST: "Test run"}


def python_for_scripts():
    """python.exe next to pythonw.exe: the scripts' output has to be read, and pythonw has none."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").exists():
        return str(exe.with_name("python.exe"))
    return str(exe)


def open_path(path, select=False):
    path = str(path)
    if WINDOWS:
        if select:
            subprocess.Popen(["explorer", "/select,", path])
        else:
            os.startfile(path)  # noqa: S606 (the person's own file, opened in its usual program)
    else:
        subprocess.Popen(["xdg-open", path if not select else str(Path(path).parent)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class App:
    def __init__(self, root, tool_dir=HERE):
        self.root, self.tool_dir = root, Path(tool_dir)
        self.settings = logic.load_settings(self.tool_dir)
        self.proc, self.action, self.lines = None, None, queue.Queue()
        self.results_name = self.results_folder = None
        self.stop_pressed_at = None
        self.saw_results = None  # when a stopped run said where it saved the results
        self.close_when_done = False
        self.step = logic.Step()
        self.eta, self.ready = "", None

        root.title("Tax Checker")
        root.minsize(760, 600)
        self._build()
        self.workbook.set(self._initial_workbook())
        self._show_options_summary()
        self.refresh()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self._poll)

    # ---- layout ------------------------------------------------------------------------------------------------
    def _build(self):
        root = self.root
        style = ttk.Style(root)
        style.configure("Big.TButton", font=("Segoe UI", 12, "bold"), padding=(16, 8))
        style.configure("Title.TLabel", font=("Segoe UI", 12, "bold"))
        style.configure("Alert.TLabel", font=("Segoe UI", 12, "bold"), foreground="#b00020")
        style.configure("Calm.TLabel", font=("Segoe UI", 10))
        style.configure("Head.TLabel", font=("Segoe UI", 10, "bold"))
        style.configure("Help.TLabel", font=("Segoe UI", 9), foreground="#444444")
        pad = {"padx": 12, "pady": 4}

        top = ttk.Frame(root)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="Supplier workbook:").pack(side="left")
        self.workbook = tk.StringVar()
        ttk.Entry(top, textvariable=self.workbook, state="readonly").pack(side="left", fill="x", expand=True, padx=6)
        self.choose_btn = ttk.Button(top, text="Choose...", command=self.choose)
        self.choose_btn.pack(side="left")

        nxt = ttk.LabelFrame(root, text="Next step")
        nxt.pack(fill="x", **pad)
        self.step_title = ttk.Label(nxt, style="Title.TLabel", wraplength=700, justify="left")
        self.step_title.pack(anchor="w", padx=10, pady=(8, 2))
        self.step_why = ttk.Label(nxt, style="Calm.TLabel", wraplength=700, justify="left")
        self.step_why.pack(anchor="w", padx=10)
        self.step_btn = ttk.Button(nxt, style="Big.TButton", command=self.do_step)
        self.step_btn.pack(anchor="w", padx=10, pady=8)

        # The full check's options (the old menus' questions): shown, explained, in the Options window.
        opts = logic.options_from(self.settings)
        self.tax_vars = {k: tk.BooleanVar(value=k in opts["taxes"] or k == "gst") for k in logic.TAX_KEYS}
        self.sk_now = tk.BooleanVar(value=opts["sk_now"])
        self.gst_dates = tk.StringVar(value=opts["gst_dates"])
        self.paid_since = tk.StringVar(value=opts.get("paid_since") or "")
        self.paid_since.trace_add("write", lambda *_: self._options_changed())
        self.opts_summary = ttk.Label(root, style="Calm.TLabel", wraplength=700, justify="left")
        self.opts_summary.pack(anchor="w", padx=12)

        more = ttk.LabelFrame(root, text="More")
        more.pack(fill="x", **pad)
        self.more_btns = []
        for i, (label, action) in enumerate((
                ("Start a new full check", logic.FULL), ("Continue the last check", logic.RESUME),
                ("Saskatchewan batch", logic.SK_BATCH), ("Trial (25 suppliers)", logic.TRIAL),
                ("Check setup", logic.SETUP), ("Profile workbook", logic.PROFILE),
                ("Offline check", logic.OFFLINE), ("Test run (test data)", logic.TEST))):
            b = ttk.Button(more, text=label, command=lambda a=action: self.start(a))
            b.grid(row=i // 4, column=i % 4, sticky="ew", padx=4, pady=3)
            self.more_btns.append(b)
        self.open_btn = ttk.Button(more, text="Open results", command=self.open_results)
        self.open_btn.grid(row=2, column=0, sticky="ew", padx=4, pady=3)
        ttk.Button(more, text="Open logs folder", command=self.open_logs).grid(row=2, column=1, sticky="ew", padx=4, pady=3)
        self.options_btn = ttk.Button(more, text="Options...", command=self.open_options)
        self.options_btn.grid(row=2, column=2, sticky="ew", padx=4, pady=3)
        for c in range(4):
            more.columnconfigure(c, weight=1)

        run = ttk.LabelFrame(root, text="Progress")
        run.pack(fill="both", expand=True, **pad)
        self.status = ttk.Label(run, text="Nothing running.", style="Calm.TLabel", wraplength=700, justify="left")
        self.status.pack(anchor="w", padx=10, pady=(6, 2))
        bar = ttk.Frame(run)
        bar.pack(fill="x", padx=10)
        self.bar = ttk.Progressbar(bar, mode="determinate", maximum=100)
        self.bar.pack(side="left", fill="x", expand=True)
        self.stop_btn = ttk.Button(bar, text="Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(8, 0))
        self.progress_text = ttk.Label(run, text="", style="Calm.TLabel")
        self.progress_text.pack(anchor="w", padx=10)
        logf = ttk.Frame(run)
        logf.pack(fill="both", expand=True, padx=10, pady=6)
        self.log = tk.Text(logf, height=10, wrap="word", font=("Consolas", 9), state="disabled")
        scroll = ttk.Scrollbar(logf, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="left", fill="y")

    def open_options(self, action=None):
        """The full check's options, each explained. With an action (full check or trial), its Start button runs it."""
        win = tk.Toplevel(self.root)
        win.title("Tax Checker: options" if not action else f"Tax Checker: {ACTION_NAMES[action]}")
        win.transient(self.root)
        frame = ttk.Frame(win, padding=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=("Check these choices, then click Start." if action else "Options for the full "
                               "check.") + " They're set to the recommended answers and remembered for next time.",
                  style="Title.TLabel", wraplength=660, justify="left").pack(anchor="w", pady=(0, 8))

        def explain(text, indent=22):
            ttk.Label(frame, text=text, style="Help.TLabel", wraplength=640, justify="left").pack(
                anchor="w", padx=(indent, 4), pady=(0, 6))

        def head(text):
            ttk.Label(frame, text=text, style="Head.TLabel").pack(anchor="w", pady=(4, 0))

        head("1. Which taxes to check")
        row = ttk.Frame(frame)
        row.pack(anchor="w", padx=12)
        for key in logic.TAX_KEYS:
            box = ttk.Checkbutton(row, text=TAX_NAMES[key], variable=self.tax_vars[key], command=self._options_changed)
            box.pack(side="left", padx=4)
            if key == "gst":
                box.state(["disabled"])  # always checked (the other taxes' checks build on it)
        explain("GST/HST is always checked (CRA's registry, every Canadian supplier). QST: Québec suppliers and QST "
                "numbers. BC PST, Manitoba RST and Saskatchewan PST: suppliers in those provinces, looked up on each "
                "province's site. Untick a tax only if you don't need it: the check is a bit faster without it, and "
                "its columns stay empty.")

        head("2. Saskatchewan: now or later")
        ttk.Checkbutton(frame, variable=self.sk_now, command=self._options_changed,
                        text="Start the Saskatchewan searches at the beginning of the full check (recommended)"
                        ).pack(anchor="w", padx=12)
        explain("Saskatchewan's registry needs a person to tick \"I'm not a robot\" and allows 100 searches a day. "
                "Ticked: a browser window opens when the check starts; tick the boxes and click Next (you have 5 "
                "minutes), then up to 100 searches run by themselves (about 20 minutes) before everything else. "
                "Unticked: the check needs nobody at the start, and all Saskatchewan suppliers wait for the daily "
                "batches. Either way, the rest are done by \"Run today's Saskatchewan batch\" on the next days.")

        head("3. GST/HST registration dates")
        explain("Every choice answers the main question: was each supplier registered on its payment date? CRA's "
                "registry shows no dates, so dates are found by asking it about particular days. The choices "
                "differ in how much of that they do:", indent=12)
        for value, label, text in (
                ("end", "Quick (recommended)",
                 "Also checks that each supplier is still registered TODAY, and when a registration was cancelled, "
                 "finds the exact cancellation date (also when it was cancelled shortly before the payment). Adds "
                 "about 1 hour to a full check."),
                ("full", "Full",
                 "Everything in Quick, plus the date each supplier's GST/HST registration STARTED (the "
                 "\"Registration Start\" column). Many more questions to CRA: several hours more on a big list. "
                 "Only if start dates are needed."),
                ("none", "None (fastest)",
                 "The payment-date answer only: no check of whether registered suppliers are still registered "
                 "today, and cancellation dates aren't searched for. (A supplier not registered on its payment "
                 "date is still checked for a later registration.)")):
            ttk.Radiobutton(frame, text=label, value=value, variable=self.gst_dates,
                            command=self._options_changed).pack(anchor="w", padx=12)
            explain(text, indent=36)

        head("4. Paid-since cut-off (optional)")
        row = ttk.Frame(frame)
        row.pack(anchor="w", padx=12)
        ttk.Label(row, text="Only check vendors last paid on or after:").pack(side="left")
        ttk.Entry(row, textvariable=self.paid_since, width=12).pack(side="left", padx=4)
        ttk.Label(row, text="(YYYY-MM-DD, e.g. 2022-01-01)").pack(side="left")
        explain("Blank (recommended unless the tax lead says otherwise) = check every vendor. With a date, vendors "
                "last paid before it aren't looked up (listed as NOT CHECKED), which makes the check faster. Vendors "
                "with no or an unreadable payment date are always checked.")

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(10, 0))
        chosen = {"go": False}

        def go():
            problem = logic.check_paid_since(self.paid_since.get())
            if problem:
                messagebox.showinfo("Tax Checker", problem, parent=win)
                return
            chosen["go"] = True
            win.destroy()

        if action:
            ttk.Button(buttons, text=f"Start: {ACTION_NAMES[action]}", style="Big.TButton", command=go).pack(side="left")
            ttk.Button(buttons, text="Cancel", command=win.destroy).pack(side="left", padx=8)
        else:
            ttk.Button(buttons, text="OK", style="Big.TButton", command=go).pack(side="left")
        win.bind("<Escape>", lambda _: win.destroy())
        win.grab_set()
        self.root.wait_window(win)
        self._show_options_summary()
        return chosen["go"]

    def _show_options_summary(self):
        o = self.options()
        taxes = ", ".join(TAX_NAMES[k] for k in logic.TAX_KEYS if k in o["taxes"])
        dates = {"end": "Quick", "full": "Full", "none": "None"}[o["gst_dates"]]
        sk = ("" if "sk_pst" not in o["taxes"] else "; Saskatchewan at the start" if o["sk_now"]
              else "; Saskatchewan in the daily batches only")
        cut = f"; vendors paid since {o['paid_since']}" if o["paid_since"] else "; every vendor"
        self.opts_summary.configure(text=f"Full check options: {taxes}{sk}; registration dates {dates}{cut}. "
                                         "(Change them with \"Options...\" under More.)")

    def options(self):
        return {"taxes": [k for k, v in self.tax_vars.items() if v.get()], "sk_now": self.sk_now.get(),
                "gst_dates": self.gst_dates.get(), "paid_since": self.paid_since.get().strip()}

    def _options_changed(self):
        self.settings["options"] = self.options()
        logic.save_settings(self.tool_dir, self.settings)

    # ---- workbook ----------------------------------------------------------------------------------------------
    def _initial_workbook(self):
        last = logic.last_run(self.tool_dir) or {}
        for candidate in (self.settings.get("workbook"), last.get("workbook")):
            if candidate and Path(candidate).exists():
                return candidate
        return self.settings.get("workbook") or ""

    def choose(self):
        start = Path(self.workbook.get()).parent if self.workbook.get() else Path.home()
        try:
            start = Path((self.tool_dir / ".last_folder").read_text(encoding="utf-8").strip()) if not \
                self.workbook.get() else start
        except OSError:
            pass
        path = filedialog.askopenfilename(
            parent=self.root, title="Choose the supplier workbook", initialdir=str(start) if start.is_dir() else None,
            filetypes=[("Excel or CSV", "*.xlsx *.xlsm *.csv"), ("All files", "*.*")])
        if not path:
            return
        if Path(path).stem.endswith(("_tax_check", "_saskatchewan", "_tax_check_TRIAL")):
            if not messagebox.askyesno("Tax Checker", "That looks like a results workbook, not the supplier list. "
                                       "Use it anyway?", parent=self.root):
                return
        self.workbook.set(str(Path(path)))
        self.settings["workbook"] = str(Path(path))
        logic.save_settings(self.tool_dir, self.settings)
        self.results_name = self.results_folder = None
        self.refresh()

    # ---- next step ---------------------------------------------------------------------------------------------
    def refresh(self):
        self.step = logic.next_step(self.tool_dir, self.workbook.get(), self.settings)
        self.step_title.configure(text=self.step.title)
        self.step_why.configure(text=self.step.why)
        if self.step.action:
            self.step_btn.configure(text=self.step.button)
            self.step_btn.pack(anchor="w", padx=10, pady=8)
        elif not self.workbook.get() or not Path(self.workbook.get()).exists():
            self.step_btn.configure(text="Choose the workbook...")
            self.step_btn.pack(anchor="w", padx=10, pady=8)
        else:
            self.step_btn.pack_forget()
        self._set_running(self.proc is not None)
        if self.step.ready_at and not self.proc:  # come back to life when the next batch is possible
            wait_ms = (self.step.ready_at - dt.datetime.now()).total_seconds() * 1000
            if 0 < wait_ms < 26 * 3600 * 1000:
                self.root.after(int(wait_ms) + 60_000, self.refresh)

    def do_step(self):
        if self.step.action:
            self.start(self.step.action)
        else:
            self.choose()

    # ---- running -----------------------------------------------------------------------------------------------
    def _set_running(self, running):
        state = "disabled" if running else "normal"
        for b in (*self.more_btns, self.step_btn, self.choose_btn, self.options_btn):
            b.configure(state=state)
        self.stop_btn.configure(state="normal" if running else "disabled")
        have_results = logic.results_file(self.workbook.get(), self.results_folder, self.results_name)
        self.open_btn.configure(state="normal" if have_results else "disabled")

    def _confirm(self, action):
        wb = self.workbook.get()
        needs_wb = action not in (logic.TEST, logic.RESUME, logic.FINISH, logic.SK_BATCH)
        if needs_wb and not (wb and Path(wb).exists()):
            messagebox.showinfo("Tax Checker", "Choose the supplier workbook first.", parent=self.root)
            return False
        problem = logic.check_paid_since(self.paid_since.get())
        if action == logic.OFFLINE and problem:
            messagebox.showinfo("Tax Checker", problem + " (Options... under More)", parent=self.root)
            return False
        last = logic.last_run(self.tool_dir)
        if action in (logic.RESUME, logic.FINISH, logic.SK_BATCH) and not last:
            messagebox.showinfo("Tax Checker", "There's no full check to continue yet: start the full check first.",
                                parent=self.root)
            return False
        if action in (logic.RESUME, logic.FINISH, logic.SK_BATCH) and last and wb and \
                not logic.same_file(last["workbook"], wb):
            return messagebox.askyesno(
                "Tax Checker", f"This continues the last full check, which was of:\n{last['workbook']}\n\n(not the "
                               "workbook chosen above). Go ahead?", parent=self.root)
        if action == logic.FULL and last and logic.same_file(last["workbook"], wb) and self.step.action != logic.FULL:
            if not messagebox.askyesno(
                "Tax Checker", "Start the full check again from the beginning?\n\nThe last check's results workbook "
                               "will be replaced (copy it first if you need it). Saskatchewan answers found so far are "
                               "kept and reused.", parent=self.root):
                return False
        if action in (logic.FULL, logic.TRIAL):
            return self.open_options(action)  # the choices, explained, with the Start button
        return True

    def start(self, action):
        if self.proc or not self._confirm(action):
            return
        argv = logic.command(action, self.workbook.get(), self.options(), STOP_FILE)
        STOP_FILE.parent.mkdir(parents=True, exist_ok=True)
        STOP_FILE.unlink(missing_ok=True)
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        flags = subprocess.CREATE_NO_WINDOW if WINDOWS else 0
        self._clear_log()
        self._append(f"=== {ACTION_NAMES[action]} started {dt.datetime.now():%Y-%m-%d %H:%M} ===")
        try:
            self.proc = subprocess.Popen([python_for_scripts(), "-u", *argv], cwd=self.tool_dir, env=env,
                                         stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         creationflags=flags)
        except OSError as e:
            self.proc = None
            messagebox.showerror("Tax Checker", f"Couldn't start it: {e}", parent=self.root)
            return
        self.action, self.stop_pressed_at, self.saw_results = action, None, None
        self.results_name = self.results_folder = None
        self.ready = None
        threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()
        self.bar.configure(mode="indeterminate", value=0)
        self.bar.start(15)
        self.progress_text.configure(text="")
        self.eta = ""
        self._status(f"{ACTION_NAMES[action]}: starting...")
        self.stop_btn.configure(text="Stop")
        self._set_running(True)

    def _read(self, proc):
        for raw in iter(proc.stdout.readline, b""):
            self.lines.put(raw.decode("utf-8", errors="replace"))
        proc.stdout.close()
        self.lines.put(None)

    def _poll(self):
        try:
            for _ in range(500):
                line = self.lines.get_nowait()
                if line is None:
                    self._finished()
                    break
                self._append(line.rstrip("\n"))
                self._event(logic.parse_line(line))
        except queue.Empty:
            pass
        if self.proc and self.saw_results and time.monotonic() - self.saw_results > 30:
            self._kill()  # a stopped run saved its results but a lookup is still waiting on a site: no need to wait
        self.root.after(150, self._poll)

    def _event(self, ev):
        if ev is None:
            return
        if ev.kind == "progress" and ev.total:
            self.bar.stop()
            self.bar.configure(mode="determinate", maximum=ev.total, value=ev.done)
            if ev.text:  # the periodic PROGRESS line: keep its time left for the per-supplier lines
                self.eta = ev.text.split(" · ", 1)[1] if " · " in ev.text else ""
            self.progress_text.configure(text=f"{ev.done:,} of {ev.total:,} suppliers" + (f" · {self.eta}" if self.eta else ""))
            if str(self.status.cget("style")) == "Alert.TLabel" or str(self.status.cget("text")).endswith("starting..."):
                self._status(f"{ACTION_NAMES[self.action]}: running by itself. You can leave it.")
        elif ev.kind == "sk_progress":
            self.bar.stop()
            self.bar.configure(mode="determinate", maximum=ev.total, value=ev.done)
            self.progress_text.configure(text=f"Saskatchewan: {ev.done} of {ev.total} suppliers")
        elif ev.kind == "captcha":
            self._status(">>> " + ev.text, alert=True)
            self._attention()
        elif ev.kind in ("captcha_done", "resumed"):
            self._status(ev.text or f"{ACTION_NAMES[self.action]}: running by itself.")
        elif ev.kind == "paused":
            self._status(ev.text)
        elif ev.kind == "results":
            self.results_name = ev.text
            if self.stop_pressed_at:
                self.saw_results = time.monotonic()
        elif ev.kind == "folder":
            self.results_folder = ev.text
        elif ev.kind == "batch":
            self._status("Saskatchewan: " + ev.text)
        elif ev.kind == "stopped":
            self._status(ev.text + ". Saving the results so far...")
        elif ev.kind == "ready":
            self.ready = ev

    def _finished(self):
        code = self.proc.wait() if self.proc else None
        action, self.proc = self.action, None
        self.bar.stop()
        STOP_FILE.unlink(missing_ok=True)
        ok = code == 0
        if ok and not self.stop_pressed_at:
            self.bar.configure(mode="determinate", maximum=1, value=1)
        else:
            self.bar.configure(mode="determinate")
        if action == logic.SETUP and ok and self.ready and self.ready.done:
            self.settings["setup_ok"] = {"workbook": self.workbook.get(), "at": dt.datetime.now().isoformat("minutes")}
            logic.save_settings(self.tool_dir, self.settings)
        text = (f"{ACTION_NAMES[action]}: stopped. The results so far are saved; click the button above to continue."
                if self.stop_pressed_at else f"{ACTION_NAMES[action]}: finished." if ok
                else f"{ACTION_NAMES[action]}: ended with a problem (see the messages below and the logs folder).")
        if action == logic.SETUP and self.ready:
            text = f"Setup check: {self.ready.text}"
        self._status(text, alert=not ok and not self.stop_pressed_at)
        self._append(f"=== {text} ===")
        self.refresh()
        self._attention()
        if self.close_when_done:
            self.root.destroy()

    def stop(self):
        if not self.proc:
            return
        if self.action not in logic.CHECK_RUNS or (self.stop_pressed_at and time.monotonic() - self.stop_pressed_at > 5):
            if self.action in logic.CHECK_RUNS and not messagebox.askyesno(
                    "Tax Checker", "Stop right away? What was checked is in the progress file, so \"Continue\" still "
                                   "picks up from there, but the results workbook may not be updated.",
                    parent=self.root):
                return
            self._kill()
            return
        STOP_FILE.parent.mkdir(parents=True, exist_ok=True)
        STOP_FILE.write_text("stop", encoding="utf-8")
        self.stop_pressed_at = time.monotonic()
        self.stop_btn.configure(text="Stop now")
        self._status("Stopping after the current supplier and saving the results so far (can take a minute; if "
                     "it's waiting for the Saskatchewan CAPTCHA or a site, click \"Stop now\").")

    def _kill(self):
        proc = self.proc
        if not proc or proc.poll() is not None:
            return
        self.stop_pressed_at = self.stop_pressed_at or time.monotonic()
        if WINDOWS:  # the whole tree: the run's browser windows too
            subprocess.call(["taskkill", "/F", "/T", "/PID", str(proc.pid)], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            proc.kill()

    def on_close(self):
        if not self.proc:
            self.root.destroy()
            return
        if not messagebox.askyesno("Tax Checker", f"{ACTION_NAMES[self.action]} is still running. Stop it and close? "
                                   "The results so far are saved, and it can be continued later.", parent=self.root):
            return
        self.close_when_done = True
        self.stop()

    # ---- small helpers -----------------------------------------------------------------------------------------
    def _status(self, text, alert=False):
        self.status.configure(text=text, style="Alert.TLabel" if alert else "Calm.TLabel")

    def _attention(self):
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.bell()
        except tk.TclError:
            pass

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _append(self, text):
        at_end = self.log.yview()[1] > 0.98
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        lines = int(self.log.index("end-1c").split(".")[0])
        if lines > MAX_LOG_LINES:
            self.log.delete("1.0", f"{lines - MAX_LOG_LINES}.0")
        self.log.configure(state="disabled")
        if at_end:
            self.log.see("end")

    def open_results(self):
        path = logic.results_file(self.workbook.get(), self.results_folder, self.results_name)
        if path:
            open_path(path)
        else:
            messagebox.showinfo("Tax Checker", "No results workbook yet for this workbook.", parent=self.root)

    def open_logs(self):
        wb = self.workbook.get()
        folder = Path(wb).parent / "logs" if wb else self.tool_dir / "logs"
        if not folder.is_dir():
            folder = self.tool_dir / "logs" if (self.tool_dir / "logs").is_dir() else Path(wb).parent if wb else self.tool_dir
        open_path(folder)


def main():
    if WINDOWS:
        try:  # sharp text on high-DPI screens
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()

    def report(exc, val, tb):  # pythonw has no console: show the problem and keep it in a file
        import traceback

        text = "".join(traceback.format_exception(exc, val, tb))
        with contextlib.suppress(OSError):
            (HERE / "logs").mkdir(exist_ok=True)
            with open(HERE / "logs" / "window_errors.txt", "a", encoding="utf-8") as f:
                f.write(f"--- {dt.datetime.now():%Y-%m-%d %H:%M:%S}\n{text}")
        messagebox.showerror("Tax Checker", f"Something went wrong in the window: {val}\n\nDetails are in "
                             f"{HERE / 'logs' / 'window_errors.txt'}", parent=root)

    root.report_callback_exception = report
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
