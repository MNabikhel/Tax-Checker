# Copilot instructions for Tax-Checker

This repo checks Canadian suppliers' sales-tax registrations (GST/HST, QST, BC PST, Manitoba RST,
Saskatchewan PST) against official government lookup services and writes the results into a copy of the
supplier workbook.

**Read `HANDOFF.md` before changing anything** (start with section 0). It explains the architecture, how
each government site behaves, what has been verified, and how to adapt the tool to a new dataset.

Key rules:
- Adapting to a new supplier file means mapping its columns: run
  `python check_suppliers.py <file> --check-columns` (offline, makes no lookups) and add header spellings
  to `COLUMNS` in `taxcheck/workbook.py`. Don't rewrite the checkers for this.
- Don't change the government-site code (`taxcheck/gst.py`, `qst.py`, `bc_pst.py`, `mb_rst.py`, `sk_pst.py`,
  `orgbook.py`, `fedcorp.py`) unless a live test or a real run shows a site changed. Its odd-looking details
  are deliberate (see HANDOFF.md section 7).
- Ask the user about business decisions (ambiguous columns, foreign suppliers, which date to check,
  `--trust-gst-number`) instead of guessing.
- Every change needs a test; `python -m unittest discover tests` must pass. Live tests:
  `TAXCHECK_LIVE=1 python -m unittest tests.test_live -v` (PowerShell: `$env:TAXCHECK_LIVE=1`).
- Never commit real supplier data, results (`*_tax_check.xlsx`), `logs/` or `cache/`.
- Never bypass CAPTCHAs, disable TLS verification, or remove request throttling.
- Record what you changed in HANDOFF.md section 12 (change log).
- Code style: small functions, docstrings explaining *why*, plain-English messages in results and logs.
