"""The window's logic (taxcheck/gui_logic.py): next step, the commands it runs, and how it reads their output.
Made-up workbooks and notes only; no display needed."""

import datetime as dt
import json
import tempfile
import time
import unittest
from pathlib import Path

from taxcheck import gui_logic as g


class NextStep(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tool = Path(self.tmp.name) / "tool"
        (self.tool / "cache").mkdir(parents=True)
        self.wb = Path(self.tmp.name) / "vendors.xlsx"
        self.wb.write_bytes(b"x")
        self.settings = {"setup_ok": {"workbook": str(self.wb)}}
        self.now = dt.datetime(2026, 10, 21, 20, 0).timestamp()

    def step(self, settings=None):
        return g.next_step(self.tool, str(self.wb), self.settings if settings is None else settings, now=self.now)

    def last_run(self, finished=True, taxes=None):
        (self.tool / ".last_run").write_text(json.dumps({"workbook": str(self.wb), "progress": "p.csv",
                                                         "finished": finished, "taxes": taxes}))

    def sk(self, **counts):
        entry = dict(total=946, answered=0, by_hand=0, waiting=946, in_results_waiting=846)
        entry.update(counts)
        (self.tool / "cache" / "saskatchewan_progress.json").write_text(json.dumps({str(self.wb.resolve()): entry}))

    def searches(self, n, hours_ago=1.0):
        t = self.now - hours_ago * 3600
        (self.tool / "cache" / "saskatchewan_searches.json").write_text(json.dumps([t + i for i in range(n)]))

    def test_no_workbook_or_missing_one(self):
        self.assertEqual(g.next_step(self.tool, "", {}).action, "")
        self.assertIn("isn't there", g.next_step(self.tool, str(self.wb) + ".gone", {}).title)

    def test_setup_first_then_full_check(self):
        self.assertEqual(self.step({}).action, g.SETUP)
        self.assertEqual(self.step({"setup_ok": {"workbook": "other.xlsx"}}).action, g.SETUP)
        self.assertEqual(self.step().action, g.FULL)

    def test_a_different_workbooks_run_means_a_full_check_for_this_one(self):
        (self.tool / ".last_run").write_text(json.dumps({"workbook": "elsewhere.xlsx", "progress": "p", "finished": False}))
        self.assertEqual(self.step().action, g.FULL)

    def test_stopped_run_is_continued(self):
        self.last_run(finished=False)
        self.assertEqual(self.step().action, g.RESUME)

    def test_saskatchewan_batch_when_searches_are_free(self):
        self.last_run()
        self.sk(answered=100, waiting=846)
        step = self.step()
        self.assertEqual(step.action, g.SK_BATCH)
        self.assertIn("100 of 946", step.title)
        self.assertIn("846 to go", step.title)
        self.assertIn("I'm not a robot", step.why)

    def test_saskatchewan_used_up_says_when_to_come_back(self):
        self.last_run()
        self.sk(answered=100, waiting=846)
        self.searches(100, hours_ago=1)
        step = self.step()
        self.assertEqual(step.action, "")
        self.assertIn("used up", step.why)
        self.assertIn("tomorrow at 19:", step.why)
        self.assertEqual(step.ready_at.date(), dt.date(2026, 10, 22))

    def test_a_part_batch_is_offered_when_enough_searches_are_back(self):
        self.last_run()
        self.sk(answered=100, waiting=846)
        self.searches(70, hours_ago=1)  # 30 free now
        step = self.step()
        self.assertEqual(step.action, g.SK_BATCH)
        self.assertIn("30 searches free now", step.why)

    def test_too_few_free_searches_means_come_back_later(self):
        self.last_run()
        self.sk(answered=100, waiting=846)
        self.searches(90, hours_ago=1)  # 10 free: not worth a CAPTCHA
        step = self.step()
        self.assertEqual(step.action, "")
        self.assertIn("Only 10 of today's searches are free now", step.why)

    def test_searches_older_than_a_day_are_free_again(self):
        self.searches(100, hours_ago=25)
        self.assertEqual(g.saskatchewan_searches(self.tool, self.now)[0], 100)

    def test_all_answered_then_finish_then_done(self):
        self.last_run()
        self.sk(answered=940, by_hand=6, waiting=0, in_results_waiting=846)
        step = self.step()
        self.assertEqual(step.action, g.FINISH)
        self.assertIn("6 need a search by hand", step.title)
        self.sk(answered=940, by_hand=6, waiting=0, in_results_waiting=0)  # after the finishing resume
        self.assertIn("All done", self.step().title)

    def test_run_without_saskatchewan_is_done_when_finished(self):
        self.last_run(taxes=["gst"])
        self.sk(waiting=946)
        self.assertIn("All done", self.step().title)

    def test_unreadable_notes_dont_break_it(self):
        (self.tool / ".last_run").write_text("{not json")
        (self.tool / "cache" / "saskatchewan_progress.json").write_text("[]")
        (self.tool / "cache" / "saskatchewan_searches.json").write_text('"x"')
        self.assertEqual(self.step().action, g.FULL)
        self.assertEqual(g.saskatchewan_searches(self.tool, self.now)[0], 100)


class Commands(unittest.TestCase):
    WB = "C:/Users/me/OneDrive/My Vendors.xlsx"

    def test_full_check_defaults(self):
        argv = g.command(g.FULL, self.WB, {}, "stop")
        self.assertEqual(argv[:2], ["check_suppliers.py", self.WB])
        self.assertIn("--sk-assist", argv)
        self.assertEqual(argv[argv.index("--gst-dates") + 1], "end")
        self.assertEqual(argv[argv.index("--stop-file") + 1], "stop")
        self.assertNotIn("--taxes", argv)  # everything
        self.assertNotIn("--ask", argv)  # never a console question

    def test_chosen_taxes_dates_and_cut_off(self):
        argv = g.command(g.FULL, self.WB, {"taxes": ["gst", "mb_rst"], "sk_now": True, "gst_dates": "full",
                                           "paid_since": " 2022-01-01 "}, None)
        self.assertEqual(argv[argv.index("--taxes") + 1], "gst,mb")
        self.assertNotIn("--sk-assist", argv)  # Saskatchewan isn't being checked
        self.assertEqual(argv[argv.index("--paid-since") + 1], "2022-01-01")
        self.assertEqual(argv[argv.index("--gst-dates") + 1], "full")
        self.assertNotIn("--stop-file", argv)

    def test_saskatchewan_later(self):
        self.assertNotIn("--sk-assist", g.command(g.FULL, self.WB, {"sk_now": False}))

    def test_other_actions(self):
        self.assertEqual(g.command(g.SK_BATCH, self.WB, {}, "s")[:2], ["check_suppliers.py", "--sk-only"])
        self.assertEqual(g.command(g.RESUME, self.WB, {})[:4], ["check_suppliers.py", "--resume", "last", "--sk-assist"])
        # Finishing uses the saved answers (no search, so no CAPTCHA), even if Saskatchewan-now is unticked.
        self.assertIn("--sk-assist", g.command(g.FINISH, self.WB, {"sk_now": False}))
        self.assertNotIn("--sk-assist", g.command(g.RESUME, self.WB, {"sk_now": False}))
        self.assertIn("--limit", g.command(g.TRIAL, self.WB, {}))
        offline = g.command(g.OFFLINE, self.WB, {})
        self.assertIn("--offline", offline)
        self.assertNotIn("--sk-assist", offline)
        self.assertEqual(g.command(g.SETUP, self.WB, {}), ["check_setup.py", self.WB])
        self.assertEqual(g.command(g.PROFILE, self.WB, {}), ["profile_workbook.py", self.WB])
        self.assertEqual(g.command(g.TEST, self.WB, {}, "s"), ["run_test.py"])

    def test_paid_since_check(self):
        self.assertEqual(g.check_paid_since(""), "")
        self.assertEqual(g.check_paid_since("2022-01-01"), "")
        self.assertIn("like 2022-01-01", g.check_paid_since("Jan 2022"))
        self.assertIn("future", g.check_paid_since((dt.date.today() + dt.timedelta(days=3)).isoformat()))

    def test_options_are_cleaned(self):
        opts = g.options_from({"options": {"taxes": ["sk_pst", "nonsense", "gst"], "gst_dates": "weird", "x": 1}})
        self.assertEqual(opts["taxes"], ["gst", "sk_pst"])
        self.assertEqual(opts["gst_dates"], "end")
        self.assertNotIn("x", opts)


class Output(unittest.TestCase):
    """Lines exactly as the scripts print them."""

    def test_progress(self):
        ev = g.parse_line("PROGRESS 1234/19911 (6.2%) | elapsed 0:41:10 | 2.0 s/supplier | about 10:22:31 left "
                          "(done ~06:40) | rows with errors 0 | so far: REGISTERED 1100")
        self.assertEqual((ev.kind, ev.done, ev.total), ("progress", 1234, 19911))
        self.assertIn("1,234 of 19,911", ev.text)
        self.assertIn("done ~06:40", ev.text)
        ev = g.parse_line("[17/946] row 233 PRAIRIE WIDGETS LTD: sk_pst=REGISTERED")
        self.assertEqual((ev.kind, ev.done, ev.total), ("progress", 17, 946))
        ev = g.parse_line("[Saskatchewan 3/100] PRAIRIE WIDGETS LTD: REGISTERED")
        self.assertEqual((ev.kind, ev.done, ev.total), ("sk_progress", 3, 100))

    def test_saskatchewan_captcha(self):
        for line in (">>> Saskatchewan: up to 100 searches now. A browser window opens: tick 'I agree' and 'I'm not "
                     "a robot' once and click Next. The searches then run by themselves.",
                     ">>> Saskatchewan PST registry: in the browser window, tick 'I agree', complete 'I'm not a robot', "
                     "then click Next. Waiting up to 5 minutes; the Saskatchewan searches then run by themselves."):
            self.assertEqual(g.parse_line(line).kind, "captcha")
        self.assertEqual(g.parse_line("Saskatchewan search page is open; continuing automatically.").kind, "captcha_done")

    def test_site_pause_and_recovery(self):
        ev = g.parse_line("The CRA GST/HST Registry isn't responding (1 lookups in a row failed: Error 500); pausing, "
                          "next try in 120 s")
        self.assertEqual(ev.kind, "paused")
        self.assertIn("every 2 minute(s)", ev.text)
        self.assertEqual(g.parse_line("The CRA GST/HST Registry is responding again").kind, "resumed")

    def test_results_batch_stop_and_setup(self):
        self.assertEqual(g.parse_line("RESULTS WORKBOOK: My Vendors_tax_check.xlsx").text, "My Vendors_tax_check.xlsx")
        self.assertEqual(g.parse_line("SAVED IN:         C:\\Users\\me\\OneDrive").text, "C:\\Users\\me\\OneDrive")
        ev = g.parse_line("SASKATCHEWAN BATCH: 212 of 946 Saskatchewan suppliers have an answer; 734 still to search.")
        self.assertEqual(ev.kind, "batch")
        self.assertTrue(ev.text.startswith("212 of 946"))
        ev = g.parse_line("Stopped early after 8 of 30 suppliers; saving the results so far.")
        self.assertEqual((ev.kind, ev.text), ("stopped", "Stopped early after 8 of 30 suppliers"))
        self.assertEqual(g.parse_line("  READY: GST/HST lookups work.").done, 1)
        ev = g.parse_line("  NOT READY: 2 problem(s) to fix:")
        self.assertEqual((ev.kind, ev.done), ("ready", 0))
        self.assertIsNone(g.parse_line("Checking: GST/HST, QST"))


class Files(unittest.TestCase):
    def test_settings_round_trip_and_results_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            tool = Path(tmp)
            self.assertEqual(g.load_settings(tool), {})
            g.save_settings(tool, {"workbook": "a.xlsx"})
            self.assertEqual(g.load_settings(tool), {"workbook": "a.xlsx"})
            wb = tool / "My [Vendors].xlsx"
            self.assertIsNone(g.results_file(wb))
            first = tool / "My [Vendors]_tax_check.xlsx"
            first.write_bytes(b"x")
            batch = tool / "My [Vendors]_saskatchewan.xlsx"
            batch.write_bytes(b"x")
            past = time.time() - 60
            import os

            os.utime(first, (past, past))
            self.assertEqual(g.results_file(wb), batch)  # the newest
            self.assertEqual(g.results_file(wb, tool, first.name), first)  # the one the last run named


if __name__ == "__main__":
    unittest.main()
