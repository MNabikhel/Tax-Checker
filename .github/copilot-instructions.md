# Copilot instructions for Tax-Checker

This repo checks Canadian suppliers' sales-tax registrations (GST/HST, QST, BC PST, Manitoba RST,
Saskatchewan PST) against official government lookup services and writes the results into a copy of the
supplier workbook.

**Tailoring the tool to the user's data? Read `TAILORING.md` first** (tax background, one row end to end, the
user's dataset, a step-by-step playbook driven by `Profile Workbook.bat`). Then **read `HANDOFF.md` before changing anything** (start with section 0, "Current state and what's next"). It
explains the architecture, how each government site behaves, what has been verified, and how to adapt the
tool to a new dataset. Status: verified end to end and ready for the user's full run (19,911 suppliers).

Key rules:
- Adapting to a new supplier file means mapping its columns: run
  `python check_suppliers.py <file> --check-columns` (offline, makes no lookups) and add header spellings
  to `COLUMNS` in `taxcheck/workbook.py`. Don't rewrite the checkers for this.
- Don't change the government-site code (`taxcheck/gst.py`, `qst.py`, `bc_pst.py`, `mb_rst.py`, `sk_pst.py`,
  `orgbook.py`, `cbr.py`, `fedcorp.py`) unless a live test or a real run shows a site changed. Its odd-looking details
  are deliberate (see HANDOFF.md section 7).
- Ask the user about business decisions (ambiguous columns, foreign suppliers, which date to check,
  `--trust-gst-number`) instead of guessing.
- Every change needs a test; `python -m unittest discover tests` must pass. Live tests:
  `TAXCHECK_LIVE=1 python -m unittest tests.test_live -v` (PowerShell: `$env:TAXCHECK_LIVE=1`).
- Never commit real supplier data, results (`*_tax_check*.xlsx`), `logs/` or `cache/`. Test names and
  numbers must be made up (or the public test data in `tests/test_live.py`), never copied from the user's file.
- The user's standing rules: never edit the source workbook, never guess legal names (only forms of the
  vendor's own name and official registry names), never filter or prioritize by payment amount, keep the
  double-click `.bat` flow working. Open decisions for the user are in HANDOFF.md section 2.
- Never bypass CAPTCHAs, disable TLS verification, or remove request throttling. Saskatchewan allows 100
  searches a day: don't add searches per supplier without a reason.
- A name or number from a registry is only a candidate until CRA accepts it for that business number; leads
  (e.g. "possible GST/HST number") go in Action Needed and never change a status.
- When reading a user's run, start with the log and the "Tax Check Summary" sheet; HANDOFF.md section 10
  maps symptoms to causes. Ask the user for the log rather than guessing.
- Record what you changed in HANDOFF.md section 12 (change log).
- Code style: small functions, docstrings explaining *why*, plain-English messages in results and logs.
