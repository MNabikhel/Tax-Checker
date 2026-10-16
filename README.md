# Tax-Checker

Checks whether suppliers are registered for Canadian sales taxes (GST/HST, QST, BC PST, Manitoba RST,
Saskatchewan PST) using the governments' own lookup services. It reads a supplier workbook or CSV,
runs the checks, and writes a copy of the workbook with results added.

Taking over development or adapting it to a new dataset? Start with [HANDOFF.md](HANDOFF.md).

## Setup

Requires Python 3.9+ ([python.org](https://www.python.org/downloads/); on Windows tick "Add python.exe to
PATH" when installing).

**Windows:** double-click `Setup.bat` once. It creates a private Python environment in the tool's
folder, installs everything, downloads the browser used for BC/Manitoba/Saskatchewan, and runs the tests.

**Any system:**
```
python -m venv .venv
.venv\Scripts\activate            (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
playwright install chromium      # one-time; needed for BC, Manitoba and Saskatchewan lookups
```

## Quick start (Windows)

1. Double-click **`Test Setup.bat`**: installs/repairs what's needed, checks every government site, the
   browser and your workbook, does a few real lookups, and ends with READY / NOT READY and the fixes. Its
   report (and every run's log) is also copied to a diagnostics folder if you set one up (below).
   It doesn't create a results workbook; when READY it offers the trial.
2. **`Run Trial (25 suppliers).bat`** (or answer Y at the end of the setup test): 25 real lookups, about 2 minutes;
   saves `<workbook>_tax_check_TRIAL.xlsx` next to the workbook and opens that folder.
3. **`Run Tax Check.bat`**: the full run; saves `<workbook>_tax_check.xlsx` next to the workbook and opens that folder.

On corporate networks that inspect and re-sign web traffic, Python's own certificate list makes every lookup fail
with `CERTIFICATE_VERIFY_FAILED`. The tool uses the operating system's certificate store through the `truststore`
package (in `requirements.txt`); certificate checking stays on. Where `playwright install chromium` can't download,
the browser lookups use the installed Microsoft Edge (or Chrome) instead.

**Diagnostics folder (optional).** To have setup reports, run logs and failure screenshots copied to a folder
someone helping you can read (e.g. a cloud-synced folder), copy `diagnostics_dir.example.txt` to
`diagnostics_dir.txt` and put the folder on its first line (environment variables like `%OneDriveCommercial%`
work), or set `TAXCHECK_DIAGNOSTICS_DIR`. Off by default; `diagnostics_dir.txt` is git-ignored.

## Usage

**Windows:** double-click `Run Tax Check.bat`. A window opens to choose the supplier workbook; the check
runs and the window stays open at the end so you can read the summary.

**Command line:**
```
python check_suppliers.py                 # a window opens to choose the workbook
python check_suppliers.py suppliers.xlsx  # or give the path
```
Without a screen (e.g. on a server) it asks you to type or paste the path instead. The picker opens in
the folder you used last time.

Everything a run produces is saved **next to the chosen workbook**: the results as
`<workbook>_tax_check.xlsx`, and the log (plus any failure screenshots) in a `logs` folder beside it.
Downloaded reference data is kept in the tool's own `cache` folder and shared between runs. Options:

| Option | What it does |
|---|---|
| `-o results.xlsx` | Choose the output file. |
| `--sheet "Vendors"` | Read a sheet other than the first one. |
| `--date 2026-09-30` | Confirm registration on this date (default: each row's date column, else today). |
| `--no-browser` | Skip BC and Manitoba lookups (they need the headless browser). |
| `--sk-assist` | Check Saskatchewan PST. A browser window opens; tick its CAPTCHA once and the script does the searches. |
| `--trust-gst-number` | Count "REGISTERED - NAME NOT MATCHED" (GST number registered, vendor name not confirmed) as OK. |
| `--no-name-lookup` | Don't look up official names for GST/HST name mismatches (skips the ~110 MB federal download and OrgBook BC). |
| `--cache-dir folder` | Where downloaded reference data is kept (default: `cache`, refreshed weekly). |
| `--nr-list saved.html` | Use a saved copy of Revenu Québec's NR registrant list if the download is blocked. |
| `--log-dir folder` | Where run logs go (default: `logs`). |
| `-v` | Also show the detailed log on screen. |
| `--check-columns` | Check nothing: show how your columns are read and what the first rows would check (offline). |
| `--offline` | Look nothing up: check every row's number format, CRA check digit and routing, and write the results file. Rows that pass show `NOT LOOKED UP` (Overall `PENDING`). |
| `--limit 25` | Trial run: only check the first 25 suppliers. |
| `--resume logs\progress_<date>.csv` | Continue a run that stopped: suppliers finished in that run are kept, ones with errors are re-checked. |
| `--checkpoint-every 2000` | Save the results workbook every N suppliers during the run (default 2000; `0` = only at the end). |
| `--progress-every 25` | Write a progress line (done, speed, time left, counts) every N suppliers (default 25). |
| `--gst-dates end` | GST/HST registration dates: `none`, `end` (default: still registered today; cancellation date) or `full` (also every start date; slow). The .bat launchers ask. |
| `--taxes gst` | Which taxes to check: `gst`, `qst`, `bc`, `mb`, `sk` (comma-separated) or `all` (default). GST/HST is always checked. The .bat launchers ask with a menu (`--ask`). |
| `--workers 3` | Parallel lookups (default 3, max 6; `1` = one at a time). Each keeps its own pacing; Manitoba/BC browser checks run alongside on the main thread. |
| `--as-of-today` | Ignore the date column and confirm every registration as of today (or `--date`). |

To fix a name and re-check, edit the results file and run the script on it. It overwrites its own
result columns rather than adding new ones.

Press Ctrl+C to stop a long run early: the results so far are saved, and the summary sheet says the
run stopped early. If a government site is down (they have maintenance windows), the script notices
after two failed suppliers, stops trying that site for the rest of the run, and marks those rows
ERROR so you can re-run later.

To try it out, run it on `examples/sample_suppliers.xlsx`, which uses publicly published numbers.

## ERP vendor-master exports (Company Name / Tax ID / Country layout)

The tool reads a typical ERP vendor-master export as it comes, with these headers (any other columns are left
alone):

| Export column | Used as |
|---|---|
| Company Name | Supplier name (legal name) |
| Tax ID | GST/HST number. Tidied first: `RC0001`/`RP0001`/`RR0001` and other CRA program accounts, old `R123456789` numbers and 3- or 5-digit references are checked by their 9-digit business number, with a note in the details. `000000000RT0001` placeholders are NO NUMBER. A QST number in this column is checked as QST. |
| Country | `CA` rows get the Canadian checks. Other countries are **NOT APPLICABLE**, unless the Tax ID is an explicit Canadian GST/HST number (9 digits + `RT`), which is checked. A bare 9-digit Tax ID on a foreign vendor is usually a US EIN and is not sent to CRA. |
| State | Province. When blank (or `CA`) on a Canadian vendor, it's taken from the postal code. |
| Postal Code | Fills in the province (see above); a blank country with a Canadian postal code counts as Canada. |
| Check/ Itm Date | Last payment date: GST/HST is confirmed as of that date (future dates use today). Use `--as-of-today` to confirm as of today instead. |

The export has no QST or PST columns, so Québec and BC vendors aren't flagged NO NUMBER for those taxes.
Manitoba RST is still checked (Manitoba's registry searches by name plus the business number from the Tax
ID), and Saskatchewan PST is a manual check unless you run with `--sk-assist`.

Suggested order for a large list: `--check-columns`, then `--offline` (2 minutes, fix any
INVALID NUMBER / NO NUMBER rows you can), then `--limit 25` for a live trial, then the full run.

## Long runs: progress, checkpoints and resuming

A full run of a 20,000-row export (about half Canadian) took about 2 h 45 min with the default 3 parallel lookups
and all taxes (registration dates off); expect roughly 3x that one at a time. Foreign suppliers and rows with a missing or invalid number take no time: they're never sent online. While it runs:

- **Progress file** `logs\progress_<date>_<time>.csv`: one line per supplier, written as each one finishes:
  every status and detail, the province used, seconds taken and any error. Open it in Excel at any time.
- **Progress lines** in the log and on screen every 25 suppliers (or 2 minutes): done/total, percent, seconds
  per supplier, time left, and status counts so far.
- **Checkpoint saves** of the results workbook every 2,000 suppliers, marked "IN PROGRESS" on the summary sheet.

If the run stops (Ctrl+C, PC restart, network drop), start it again with
`--resume logs\progress_<date>_<time>.csv`. Finished suppliers are kept, suppliers with errors are re-checked,
and the new progress file includes both, so the latest one is always complete.

## Input columns

Start from `examples/suppliers_template.xlsx`, or use your own workbook (.xlsx, .xlsm) or CSV. The
results are always saved as .xlsx. Header names are matched loosely, so `GST #`, `GST/HST Number` and
`HST` all work. The header row can be anywhere in the first 15 rows. Old `.xls` files need to be saved
as `.xlsx` first.

If a CSV was opened and saved in Excel, long numbers may have become scientific notation
(`8.57306E+08`). Those digits are lost, so the script flags them as INVALID NUMBER; format the column as
Text and re-enter them.

| Column | Required | Notes |
|---|---|---|
| Supplier Name | yes | **Legal** name. The CRA check fails if this doesn't match CRA's records. |
| Trade Name | no | Also tried against the CRA registry if the legal name doesn't match. |
| Province | no | Used to decide which provincial taxes apply, and to route a generic `PST Number` column. |
| GST/HST Number | | `123456789RT0001` (spaces fine). Its first 9 digits are also the business number used for BC and Manitoba. |
| Business Number | | Only needed for BC PST when there's no GST/HST number. |
| QST Number | | `1234567890TQ0001` or `NR00001234`. |
| PST Number | | Treated as BC, SK or MB based on Province. Or use `BC PST Number`, `SK PST Number`, `MB RST Number`. Manitoba needs the 7-digit RST number (or just the GST/HST number). |
| Transaction Date | no | Date to confirm the GST/HST registration on. Future dates are checked as of today. |

Numbers typed as numbers in Excel are handled. Formula cells are read as their last calculated value,
so save the workbook in Excel before running. A QST number without its `TQ` suffix is checked as
`TQ0001`; if that account isn't active, the result says so and asks for the full number.

## What gets checked

| Tax | Source | Automated? |
|---|---|---|
| GST/HST | [CRA GST/HST Registry](https://www.businessregistration-inscriptionentreprise.gc.ca/ebci/brom/registry/pub/reg_01_Ld.action) | Yes |
| Simplified GST/HST (non-resident digital sellers) | CRA registry, plus [CRA's published list](https://www.canada.ca/en/revenue-agency/services/tax/businesses/topics/gst-hst-businesses/digital-economy-gsthst/confirming-simplified-gst-hst-account-number.html) | Yes |
| QST (`TQ` numbers) | [Revenu Québec validation API](https://www.revenuquebec.ca/en/online-services/tools/application-programming-interface-api-used-to-validate-a-qst-registration-number/) | Yes |
| QST (`NR` numbers) | [Revenu Québec's NR registrant list](https://www.revenuquebec.ca/en/businesses/consumption-taxes/gsthst-and-qst/special-cases-gsthst-and-qst/suppliers-outside-quebec/list-of-suppliers-outside-quebec-that-are-registered-for-the-qst/) | Yes |
| BC PST | eTaxBC PST Number Verification Service (headless browser) | Yes |
| MB RST | [TAXcess RST Registration Registry](https://taxcess.gov.mb.ca/TAXcess/?Link=RSTLookup) (headless browser) | Yes: business name plus RST or business number |
| SK PST | [SETS PST registry](https://www.sets.saskatchewan.ca/rptp/portal/footer/pst-registry) | Assisted (`--sk-assist`): you tick its reCAPTCHA once per run. Without it, flagged for a manual check. |

Before going online, numbers are checked for format and for the CRA check digit, so typos are caught
without a lookup.

**GST/HST name mismatches.** CRA only confirms a number when the name matches its records exactly
(the legal name or a trade name CRA has on file; case and punctuation don't matter, but every word
does, so "ACME" won't match "ACME LTD"). The script tries the sheet's name, the same name cleaned of
vendor-master shorthand (`(DNU)`, `*`, and either side of `O/A`, `DBA`, `C.O.B.`), without "The", and
the trade name. If none match, it looks up the business number in
[Corporations Canada's open data](https://open.canada.ca/data/en/dataset/0032ce54-c5dd-4b66-99a0-320a7b5e99f2)
(all federal corporations, updated weekly), [OrgBook BC](https://orgbook.gov.bc.ca) (companies and other
organizations registered in BC, via its public API), and, when the row has a QST number, the legal name
Revenu Québec returns. It retries with each official name it finds.

If no name matches, the number alone still gets a definite answer. CRA checks the number against the date
before it looks at the name, so one extra lookup shows whether the number is registered, not registered,
or unknown to CRA. A number registered under a name other than the one in your sheet is reported as
**REGISTERED - NAME NOT MATCHED**, with CRA's name for the business when it's known. The number is valid,
but it isn't confirmed to be this supplier's (it could be another business's number), so it shows as REVIEW.
If your vendor names are unreliable and you only need the number checked, `--trust-gst-number` counts it
as OK.

**Québec NR list.** Revenu Québec's website blocks some networks, such as cloud servers. The script tries
a plain download, then a real browser, and keeps a copy for a week. If both are blocked, save the page
from your browser and pass it with `--nr-list`.

**Saskatchewan (`--sk-assist`).** The registry's terms page has a reCAPTCHA that a person must complete.
After that, the search page is an ordinary form, so the script runs every Saskatchewan search in that
session. Results are read cautiously: REGISTERED only when a result row has the supplier's name and a
licence type, otherwise NOT CONFIRMED. A screenshot of each search is saved next to the log. The results
page couldn't be seen while building this, so check the first run's screenshots against the results
column. If nobody completes the CAPTCHA within 5 minutes, the Saskatchewan rows are marked for a manual check.

## Reading the results

Results are added to the right of your columns (your workbook itself is never changed), with filter buttons on
every header. Two extra sheets: **Action List** (only the suppliers that need something done, worst first) and
**Tax Check Summary** (counts per issue with what to do, and notes from the run).

**Per supplier**

| Column | What it tells you |
|---|---|
| Overall | OK / REVIEW / NOT APPLICABLE (foreign supplier). |
| Main Issue | The most important problem for this supplier, e.g. "GST/HST: Registered under another name". Filter on it. |
| Action Needed | What to do about the Main Issue. |
| Other Issues | Any other problems (other taxes). |
| Vendor Name Check | MATCHES / UPDATE NAME / NOT CONFIRMED: is the vendor name the name CRA has? |
| Suggested Vendor Name | The name CRA accepted, when the vendor name should change (check it's the same business). |
| Do-Not-Use Vendor | "Yes" when the vendor name carries a DNU / DO NOT USE marker. |
| Province Used | The province the checks were based on ("(from postal code)" when the State column was blank or wrong). |

**Per tax** (GST/HST, QST, BC PST, MB RST, SK PST; only the taxes chosen for the run)

| Column | What it tells you |
|---|---|
| Status | The government source's answer (REGISTERED, NOT REGISTERED...). |
| Issue | What that means, as a short fixed phrase you can filter on (list below). |
| Number Checked | The number actually looked up (after tidying the Tax ID). |
| Registered Since / Registration End | Registration dates where known (see below). "Active on <date>" = still registered that day. |
| Name on Record (provincial) | The name the provincial registry has. |
| Details | The full explanation. |

GST/HST also has: **Tax ID Problem** (e.g. "Corporate income tax account (RC), not GST/HST (RT)", "Old R-format",
"Wrong length", "Placeholder"), **Date Checked** (the payment date used), **Status Today**, **Name Accepted by
CRA**, **Name Source** (vendor name as entered / reworded / BC or federal registry) and **Registry Name**.

GST/HST Issue values: Registered - name matches · Registered - vendor name differs from CRA · Registered - Tax ID
needs correcting · Registered under another name · Registered - legal name not confirmed · Registered after
payment date · Registration cancelled since payment date · Not registered · Registration cancelled · Number not
known to CRA · No GST/HST number · No GST/HST number (placeholder Tax ID) · Tax ID not a GST/HST number · Tax ID
typo (fails check digit) · Foreign supplier - not applicable · Lookup failed.

**Registration dates.** Revenu Québec gives QST start/cancellation dates, and CRA's simplified list gives them for
non-resident digital sellers. The CRA GST/HST registry gives no dates: it only answers "registered on this day?",
so dates are found by asking about specific days. The run asks how far to go: *Quick* (default, about +1 hour on
the full vendor list) confirms each supplier is still registered today and finds the cancellation date if not;
the start date is found for suppliers that registered after the payment date. *Full* also finds every supplier's
start date (about 14 lookups each: hours more, best for smaller lists). *None* skips it. Registrations don't
expire; "Registration End" is a cancellation. Manitoba, BC and Saskatchewan's registries give no dates.


**Vendor Name Check** (GST/HST, right after Overall) answers a separate question: is the vendor name in your list
the name CRA has? The tool never invents names and never edits your workbook; it only tries forms of your own
vendor name (e.g. the legal part before "O/A") and official registry names, and CRA must accept one.

| Vendor Name Check | Meaning |
|---|---|
| MATCHES | CRA accepted the vendor name exactly as entered. |
| UPDATE NAME | Registration confirmed, but CRA accepted a different form of the name (shown in the GST/HST details and Name on Government Record). Correct the vendor record. |
| NOT CONFIRMED | The number is registered, but CRA accepted no name we have. Get the legal name from the supplier (e.g. their invoice). |

Each tax gets a **Status** and a **Details** column, and each row gets an **Overall** column (`OK`
when every applicable check came back `REGISTERED`). A **Tax Check Summary** sheet has counts per
tax and notes from the run.

| Status | Meaning |
|---|---|
| REGISTERED | The government source confirms the registration (for GST/HST: number and the sheet's name). |
| REGISTERED - NAME NOT MATCHED | GST/HST only: the number is registered, but not under the name in your sheet. The details give CRA's name when known. OK with `--trust-gst-number`. |
| NOT REGISTERED | The source says the number isn't (or is no longer) registered. |
| NOT CONFIRMED | The source couldn't match it (BC PST/business-number mismatch, no Manitoba or Saskatchewan match, an unusual QST status). |
| INVALID NUMBER | The number is malformed or fails its check digit. |
| NO NUMBER | The tax looks applicable, but no number was provided. |
| MANUAL CHECK | Wasn't checked automatically (SK without `--sk-assist`, or no browser available). The details say how to check it. |
| ERROR | A lookup failed (site down, network). Re-run later. |
| NOT APPLICABLE | Non-Canadian supplier with no Canadian tax number; no Canadian check applies. Overall `NOT APPLICABLE`. |
| NOT LOOKED UP | `--offline` only: passed the offline checks, still needs the online run. Overall `PENDING`. |

Lookups are spaced out (about 1.5 s per CRA lookup and 5 s per BC lookup), so a few hundred suppliers
take several minutes. A number that appears on several rows is only looked up once.

## Logs

Every run writes `logs/tax_check_<date>_<time>.log` in a `logs` folder next to the workbook (`--log-dir`
changes it). The **Tax Check Summary** sheet names the log
for that run. The log records:

- the arguments, Python version, and which spreadsheet column was matched to each field;
- each row's input values and every result with its details;
- every request to a government site (URL, HTTP status, time taken), plus the raw QST API answers
  and CRA result messages;
- warnings (an unreadable date, a list that didn't download) and full error tracebacks.

When a BC or Manitoba lookup fails, a screenshot of the page is saved next to the log. Logs contain
supplier names and tax numbers, so treat them like the workbook itself. `logs/` is git-ignored.

## Tests

```
python -m unittest discover tests                            # offline, under a second
TAXCHECK_LIVE=1 python -m unittest tests.test_live -v        # against the real services, ~1 min
TAXCHECK_LIVE=1 TAXCHECK_SK_ASSIST=1 python -m unittest tests.test_live.LiveSaskatchewanAssisted -v   # tick the CAPTCHA when the window opens
```

The offline tests use real responses saved from CRA and Revenu Québec (`tests/fixtures`), so a
parsing change gets checked against what the sites actually send. Run the live tests when results
look wrong: a failure there usually means a government site changed.

## Adapting to your workbook

Start with `python check_suppliers.py your_file.xlsx --check-columns`. It doesn't go online: it shows
which column feeds which field, suggests aliases for headers it doesn't recognize, and previews what
each row would check.

Column names are matched in `COLUMNS` at the top of `taxcheck/workbook.py`. To support a header
the script doesn't recognize, add its spelling to the right list. The log shows which column each
field was matched to, so it's easy to confirm.
