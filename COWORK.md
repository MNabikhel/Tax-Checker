# Tailoring with Microsoft 365 Copilot Cowork

> **Latest (2026-10-21):** read [`COWORK_V3_RERUN.md`](COWORK_V3_RERUN.md) first: what the v1/v2 comparison
> found, what changed in the tool, tonight's re-run and what to check after it.

This is the start page when the tailoring agent is **Copilot Cowork** (Microsoft 365) rather than a coding
agent. Cowork works with files in OneDrive/SharePoint (reading documents, reading and editing Excel files) and
runs in the company's Microsoft 365. It doesn't run programs on the user's PC and doesn't use GitHub. So the
work is split:

| Who | Does |
|---|---|
| **Cowork** | Reads this folder's guides, the user's workbook and every report/log the tool writes (all in OneDrive); decides what the export needs; writes the **tailoring settings** (`tailoring.xlsx`, or `tailoring.json`); explains results and next steps |
| **The user** | Double-clicks `Tax Checker.bat` on their PC and uses its window's buttons (the older `.bat` files are in `advanced\`) (the tool runs there: their network, Edge, the government sites) and puts Cowork's settings file in the tool folder |

Everything the tool writes lands **next to the workbook** (in OneDrive): the profile report and logs in a
`logs` folder, results as `<workbook>_tax_check.xlsx` (`..._TRIAL.xlsx` for a trial). Cowork can read them there
directly; the user doesn't need to upload anything.

## For Cowork: read first

1. `TAILORING.md` sections 1–6: the rules, the tax background (incl. simplified "digital economy" GST/HST
   registrants), what happens to one supplier row, what's known about the user's export, and the playbook.
2. `HANDOFF.md` §0 (state and next jobs), §7 if a site behaves oddly, §10 to read symptoms in a log.
3. `taxcheck/tailoring.py`'s top comment: what the settings file can change.

Rules you must keep (from the user): never edit the source workbook; never guess legal names; never filter by
payment amount; never get around the Saskatchewan CAPTCHA; no supplier names or numbers in anything that leaves
the company. **Don't edit the `.py` files**: you can't run the tests, and an untested change can break a 20,000-row
run. If something needs a code change, write a change request instead (below).

## The tailoring loop

1. **User:** "Check setup" in the Tax Checker window once (expects READY). ("Test run" under More is a one-click test on built-in public test
   data; its `TaxCheck_Test_Results_<date>.zip` goes to the tool's maintainer, not into tailoring.)
2. **User:** "Profile workbook" (under More), with the workbook chosen. **Cowork:** read `logs/profile_<date>.txt` next to the
   workbook, and look at the workbook itself (it's in OneDrive): column headers, how Tax IDs, provinces, countries,
   names and dates are written.
3. **Cowork:** work through TAILORING §5's table. Most findings are settings, not code:

   | Finding | Setting (sheet row: Setting / Field or code / Value) |
   |---|---|
   | A needed column shows "(not used)" | `column` / field (`gst`, `name`, `province`, `country`, `postal`, `date`, `qst`, `mb_rst`, `bc_pst`, `sk_pst`, `pst`, `bn`, `trade_name`) / the exact header |
   | A province spelling isn't recognised | `province` / two-letter code / the spelling |
   | Canada written another way in Country | `canada value` / (blank) / the value |
   | Text dates are month-first (09/30/2026) | `date format first` / (blank) / `%m/%d/%Y` |
   | A vendor-master marking spoils names ("- INACTIVE") | `remove from names` / (blank) / the text |
   | A marking means do-not-use ("DO NOT PAY") | `do not use marker` / (blank) / the text |

   Start from `examples/tailoring_template.xlsx`: keep only the rows needed, add a `note` row saying why each
   is there, and save it in the **tool folder** as `tailoring.xlsx` (or have the user copy it there).
4. **User:** "Profile workbook" again. Its second line says which settings were applied; a mistake in the
   file stops it with a message saying what to fix. **Cowork:** confirm the columns and routing now look right.
5. **User:** "Offline check" (under More; about 2 minutes, no lookups). **Cowork:** read the results workbook's
   "Tax Check Summary" sheet and the log: NO NUMBER, INVALID NUMBER, NOT APPLICABLE counts; anything surprising
   goes back to step 3.
6. **User:** "Trial (25 suppliers)" (under More). **Cowork:** check against TAILORING §5 step 4's pass criteria.
7. **User:** "Start the full check" (all rows; 3–4 hours; overnight is fine). After any stop (restart, network
   drop, Ctrl+C), and the next day for Saskatchewan suppliers past the registry's 100-a-day limit:
   "Continue the stopped check" (same workbook and answers, no typing).
   **Cowork:** walk the tax lead through the summary sheet and the Action List.

## When a change needs code

Some things can't be settings: a new Tax ID format the parser doesn't know, a different routing rule, a new
output layout, a government site that changed. Write a **change request** (a Word document or an email to the
developer/agent who maintains the code) with: what the profile/log shows (counts and shapes, no supplier data),
which file and function TAILORING §8 points to, the behaviour wanted, and an example row made up in the same
shape for the test. Code changes come back as a new version of the tool folder.

## Starter prompt (for the user to paste into Cowork)

> You're taking over our supplier tax-registration checker. The tool is in OneDrive at `<tool folder>` and our
> vendor export is `<workbook path>`. Read COWORK.md in the tool folder first, then TAILORING.md, and follow
> them. Look at our workbook and the profile report in the `logs` folder next to it, tell me what needs
> tailoring and why, and create the tailoring.xlsx settings file. Ask me about anything that's a business
> decision. Then tell me exactly which file to double-click next and what you'll check in its output.
