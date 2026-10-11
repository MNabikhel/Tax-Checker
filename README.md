# Tax-Checker

Checks whether suppliers are registered for Canadian sales taxes (GST/HST, QST, BC PST, Manitoba RST,
Saskatchewan PST) using the governments' own lookup services. It reads a supplier workbook or CSV,
runs the checks, and writes a copy of the workbook with results added.

Taking over development or adapting it to a new dataset? Start with [HANDOFF.md](HANDOFF.md).

## Setup

Requires Python 3.9+.

```
pip install -r requirements.txt
playwright install chromium      # one-time; needed for BC, Manitoba and Saskatchewan lookups
```

## Usage

```
python check_suppliers.py suppliers.xlsx
```

This writes `suppliers_tax_check.xlsx` next to the input. Options:

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

To fix a name and re-check, edit the results file and run the script on it. It overwrites its own
result columns rather than adding new ones.

Press Ctrl+C to stop a long run early: the results so far are saved, and the summary sheet says the
run stopped early. If a government site is down (they have maintenance windows), the script notices
after two failed suppliers, stops trying that site for the rest of the run, and marks those rows
ERROR so you can re-run later.

To try it out, run it on `examples/sample_suppliers.xlsx`, which uses publicly published numbers.

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

**GST/HST name mismatches.** CRA only confirms a number when the name matches its records, and it gives
the same answer for a wrong name as for an unregistered number. The script tries the sheet's name,
without "The", and the trade name. If none match, it looks up the business number in
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

Lookups are spaced out (about 1.5 s per CRA lookup and 5 s per BC lookup), so a few hundred suppliers
take several minutes. A number that appears on several rows is only looked up once.

## Logs

Every run writes `logs/tax_check_<date>_<time>.log`. The **Tax Check Summary** sheet names the log
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
