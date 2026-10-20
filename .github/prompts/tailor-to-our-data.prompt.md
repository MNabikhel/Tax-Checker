---
mode: agent
description: Tailor Tax-Checker to our vendor-master export and get it ready for a full run
---
Read AGENTS.md, TAILORING.md and HANDOFF.md section 0, and follow the rules in .github/copilot-instructions.md.

Then:
1. Run the offline tests (`python -m unittest discover tests`) and tell me if anything fails.
2. Ask me for the path of our supplier workbook (it stays outside this repo), then run
   `python profile_workbook.py "<path>"` and `python check_suppliers.py "<path>" --check-columns`.
3. Go through TAILORING.md section 5 with that output: list what you'd change and why (columns, Tax ID formats,
   province/country spellings, name markers, dates, online sellers), and ask me about anything that is a
   business decision before changing it.
4. Make the changes with tests (made-up data only), run the tests, then run
   `python check_suppliers.py "<path>" --offline` and summarize the counts.
5. Tell me exactly what to run next (trial, then full run) and what to send you back.
Don't commit or push until I say so, and never add the workbook or any results/logs to git.
