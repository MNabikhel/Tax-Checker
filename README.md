# Tax-Checker

Checks whether suppliers are registered for Canadian sales taxes (GST/HST, QST, BC PST) using the
governments' own lookup services. It reads a supplier workbook, runs the checks, and writes a copy
of the workbook with results added.

## Setup

Requires Python 3.9+.

```
pip install -r requirements.txt
playwright install chromium      # one-time; only needed for BC PST lookups
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
| `--no-browser` | Skip BC PST lookups (they need the headless browser). |
| `--nr-list saved.html` | Use a saved copy of Revenu Québec's NR registrant list if the download is blocked. |
| `--log-dir folder` | Where run logs go (default: `logs`). |
| `-v` | Also show the detailed log on screen. |

To fix a name and re-check, edit the results file and run the script on it. It overwrites its own
result columns rather than adding new ones.

To try it out, run it on `examples/sample_suppliers.xlsx`, which uses publicly published numbers.

## Input columns

Start from `examples/suppliers_template.xlsx`, or use your own workbook. Header names are matched
loosely, so `GST #`, `GST/HST Number` and `HST` all work. The header row can be anywhere in the
first 15 rows.

| Column | Required | Notes |
|---|---|---|
| Supplier Name | yes | **Legal** name. The CRA check fails if this doesn't match CRA's records. |
| Trade Name | no | Also tried against the CRA registry if the legal name doesn't match. |
| Province | no | Used to decide which provincial taxes apply, and to route a generic `PST Number` column. |
| GST/HST Number | | `123456789RT0001` (spaces fine). Its first 9 digits are also used as the BC business number. |
| Business Number | | Only needed for BC PST when there's no GST/HST number. |
| QST Number | | `1234567890TQ0001` or `NR00001234`. |
| PST Number | | Treated as BC, SK or MB based on Province. Or use `BC PST Number`, `SK PST Number`, `MB RST Number`. |
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
| SK PST | [SETS PST registry](https://www.sets.saskatchewan.ca/rptp/portal/footer/pst-registry) | No: protected by reCAPTCHA. Flagged for a manual check. |
| MB RST | None (Manitoba has no public lookup) | No: flagged for a manual check, with Manitoba Finance contact details. |

Before going online, numbers are checked for format and for the CRA check digit, so typos are caught
without a lookup.

## Reading the results

Each tax gets a **Status** and a **Details** column, and each row gets an **Overall** column (`OK`
when every applicable check came back `REGISTERED`). A **Tax Check Summary** sheet has counts per
tax and notes from the run.

| Status | Meaning |
|---|---|
| REGISTERED | The government source confirms the registration. |
| NOT REGISTERED | The source says the number isn't (or is no longer) registered. |
| NOT CONFIRMED | The source couldn't match it. For GST/HST this usually means the name doesn't match CRA's records: CRA gives the same answer for a wrong name and an unregistered number. |
| INVALID NUMBER | The number is malformed or fails its check digit. |
| NO NUMBER | The tax looks applicable, but no number was provided. |
| MANUAL CHECK | Can't be automated (SK, MB). The details say how to check it. |
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

When a BC lookup fails, a screenshot of the eTaxBC page is saved next to the log. Logs contain
supplier names and tax numbers, so treat them like the workbook itself. `logs/` is git-ignored.

## Tests

```
python -m unittest discover tests                            # offline, under a second
TAXCHECK_LIVE=1 python -m unittest tests.test_live -v        # against the real services, ~30 s
```

The offline tests use real responses saved from CRA and Revenu Québec (`tests/fixtures`), so a
parsing change gets checked against what the sites actually send. Run the live tests when results
look wrong: a failure there usually means a government site changed.

## Adapting to your workbook

Column names are matched in `COLUMNS` at the top of `taxcheck/workbook.py`. To support a header
the script doesn't recognize, add its spelling to the right list. The log shows which column each
field was matched to, so it's easy to confirm.
