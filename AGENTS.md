# Agent instructions (Tax-Checker)

You are taking over a Python tool that checks Canadian suppliers' sales-tax registrations (GST/HST, QST,
BC PST, Manitoba RST, Saskatchewan PST) against government sites and writes the results into a copy of the
supplier workbook. It is built, verified end to end, and ready; your job is to **tailor it to the user's
vendor-master export** and support their full run.

1. Read `TAILORING.md` (the playbook: tax background, one row end to end, what's known about the user's file,
   what to change for what you see). Then `HANDOFF.md` §0 and the parts it points you to.
2. Follow the rules in `.github/copilot-instructions.md` (same as TAILORING §1): never edit the source
   workbook, never guess legal names, never filter by payment amount, never bypass the Saskatchewan CAPTCHA or
   loosen TLS/throttling, never commit supplier files, names or numbers, keep the `.bat` flow working.
3. Start by establishing the baseline: `python -m unittest discover tests` must pass.
4. Then get the shape of the user's file: run `python profile_workbook.py "<path to their workbook>"` (or ask
   them to double-click `Profile Workbook.bat`) and work through TAILORING §5 with its output.
5. Every change gets a test with made-up data; record changes in HANDOFF §12.
