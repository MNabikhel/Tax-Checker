# Handoff: Tax-Checker

For the agent (or person) taking this over. Read this first; it explains what the tool does, what
has been verified, how the code is laid out, and how to adapt it to a new supplier dataset.
`README.md` is the user-facing guide; this file is the engineering one.

**Last updated:** 2026-10-11 · **Branch:** `main` (work happens on `claude/magical-cerf-3mm20q` and
is merged to `main`) · **Tests:** 79 offline + 8 live

---

## 1. What it does

`check_suppliers.py` reads a supplier list (Excel or CSV), checks each supplier's Canadian sales-tax
registrations against the governments' own lookup services, and writes a copy of the workbook with
a status and an explanation per tax, plus a summary sheet and a detailed log file.

| Tax | Source | How | State |
|---|---|---|---|
| GST/HST | CRA GST/HST Registry (web form) | `requests` | Automated, verified live |
| GST/HST (simplified, non-resident digital sellers) | CRA registry + CRA's published registrant list | `requests` | Automated, verified live |
| GST/HST name recovery | Corporations Canada open data (federal corporations CSV, ~110 MB) | download, cached 7 days | Automated, verified live |
| QST (`TQ` numbers) | Revenu Québec validation API (JSON) | `requests` | Automated, verified live |
| QST (`NR` numbers) | Revenu Québec's published NR list | `requests` → browser → saved file | **Untested live** (see §2) |
| BC PST | eTaxBC "PST Number Verification Service" | Playwright | Automated, verified live with Chromium 141 |
| MB RST | Manitoba TAXcess "RST Registration Registry" | Playwright | Automated, verified live with Chromium 153 |
| SK PST | SETS "PST On-Line Registry" | Playwright, **person completes a reCAPTCHA** | Assisted (`--sk-assist`); results page **unseen** (see §2) |

Alberta, the territories and the HST provinces (ON, NB, NS, PE, NL) only need the GST/HST check.

## 2. Open items (check these first on the new PC)

1. **BC with the current Chromium.** BC was verified with Chromium 141. On 2026-10-11 eTaxBC was
   down for scheduled maintenance (Oct 9, 4 PM – Oct 13, 7 AM Pacific), so it couldn't be re-verified
   with Chromium 153, which is what `playwright install chromium` now gives. Manitoba runs on the same
   platform (FAST/GenTax) and works with 153, so BC most likely does too. Verify with
   `TAXCHECK_LIVE=1 python -m unittest tests.test_live -v` (the BC test skips itself if eTaxBC is unreachable).
2. **Québec NR list.** Revenu Québec's website (Cloudflare) refused the cloud server this was built
   on: plain download, headless browser, and the Wayback Machine all got a 403 naming the server's IP.
   It should work from a normal Canadian connection. The parser (`qst.parse_nr_list`) was written from
   the page's documented layout (trade name, legal name, `NR 0013 0061`) and is unit-tested on a
   synthetic table, but **has never seen the real page**. On the first run with an NR supplier, check
   the log for `Loaded Revenu Québec NR list from ...: N registrants` (N should be ~2,000).
3. **Saskatchewan results page.** The reCAPTCHA only guards the terms page (confirmed by reading the
   site's `pstLookup.js`). The search page has a `#pstSearch` box (min. 4 characters). What the
   results look like is unknown, so `sk_pst.classify()` is conservative: REGISTERED only when a table
   row contains the supplier's name and the word "vendor" or "consumer". Every search saves a
   screenshot next to the log and logs the full page text. **After the first real `--sk-assist` run,
   compare those screenshots with the results column and tighten `classify()`.**

## 3. Setup on a new PC

Python 3.9+ (tested on 3.11, 3.12, 3.13; code parses as 3.9).

```
git clone https://github.com/MNabikhel/Tax-Checker.git
cd Tax-Checker
python -m venv .venv
# Windows: .venv\Scripts\activate      macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium            # ~115 MB; needed for BC, Manitoba, Saskatchewan
python -m unittest discover tests      # offline, < 1 s, should say OK
python check_suppliers.py examples/sample_suppliers.xlsx
```

The first run downloads the federal corporations data (~110 MB) into `cache/`, only if some GST
name doesn't match; `--no-name-lookup` skips it. `logs/` and `cache/` are git-ignored.

`TAXCHECK_CHROMIUM=/path/to/chrome` points Playwright at a specific browser binary (used in the
build environment; normally not needed). `HTTPS_PROXY`, if set, is passed to the browser.

## 4. How a run works

```
check_suppliers.main()
  ├─ logsetup.setup_logging()            logs/tax_check_<ts>.log (DEBUG) + console (INFO)
  └─ run()
      ├─ workbook.SupplierSheet(path)    find header row, map columns via COLUMNS aliases
      ├─ build checkers                  GstChecker(+FederalCorporations), QstChecker,
      │                                  Browser (lazy) → BcPstChecker, MbRstChecker, SkPstAssistant
      ├─ for each row: checks.RowChecker.check(values, date)
      │     decides which taxes apply (numbers present / province), calls checkers,
      │     catches every exception → Result(ERROR), caches duplicate lookups
      └─ SupplierSheet.write_results()   result columns + "Tax Check Summary" sheet → <input>_tax_check.xlsx
```

Every check returns a `result.Result(status, detail, registered_name)`. Nothing raises out of a row:
`RowChecker._guard` turns `BadNumber` into INVALID NUMBER and anything else into ERROR (traceback in
the log only).

## 5. Code map

| File | Responsibility | Key names |
|---|---|---|
| `check_suppliers.py` | CLI, run orchestration, notes on the summary sheet, Ctrl+C keeps partial results | `main`, `run`, `save_workbook` |
| `taxcheck/workbook.py` | Read .xlsx/.xlsm/.csv, header detection, column aliases, provinces, dates, write results | `COLUMNS`, `PROVINCES`, `SupplierSheet`, `InputError`, `as_text` |
| `taxcheck/checks.py` | Per-row routing: which checks apply, number parsing, caching, error containment | `RowChecker` |
| `taxcheck/numbers.py` | Offline parsing/validation of every number format; CRA Luhn check digit | `parse_gst`, `parse_bn`, `parse_qst`, `parse_bc_pst`, `parse_mb_rst`, `BadNumber` |
| `taxcheck/gst.py` | CRA registry form, result parsing, name variants, simplified list, federal-name fallback, Ottawa date | `GstChecker`, `parse_registry_result`, `SimplifiedList`, `cra_today`, `name_variants` |
| `taxcheck/fedcorp.py` | Corporations Canada CSVs → `{bn9: [official names]}`, weekly cache | `FederalCorporations` |
| `taxcheck/qst.py` | Revenu Québec API; NR list with download → browser → file fallbacks and cache | `QstChecker`, `parse_nr_list` |
| `taxcheck/browser.py` | Shared lazily-started Chromium, fresh context per lookup, failure screenshots, fail-fast on HTTP errors | `Browser`, `BrowserUnavailable` |
| `taxcheck/bc_pst.py` | eTaxBC form automation | `BcPstChecker` |
| `taxcheck/mb_rst.py` | TAXcess form automation, name variants down to the first word | `MbRstChecker`, `parse_results` |
| `taxcheck/sk_pst.py` | Assisted SETS search after a human passes the reCAPTCHA | `SkPstAssistant`, `classify`, `search_terms` |
| `taxcheck/http.py` | `requests` session (UA, GET retries, request logging), throttling, site-down detection | `new_session`, `Throttle`, `SiteHealth` |
| `taxcheck/logsetup.py` | Log file + console handlers | `setup_logging` |
| `taxcheck/result.py` | Status constants and `Result` | |

## 6. Statuses

| Status | Meaning | Typical cause |
|---|---|---|
| REGISTERED | Source confirms an active registration | |
| NOT REGISTERED | Source definitively says no (or cancelled / deregistered) | |
| NOT CONFIRMED | Source couldn't match; not definitive | GST name mismatch; BC PST/BN mismatch; no MB/SK match |
| INVALID NUMBER | Malformed, fails check digit, or Excel scientific notation | Typos |
| NO NUMBER | Tax applies (by province) but no number given | Missing data |
| MANUAL CHECK | Not checked automatically | SK without `--sk-assist`; no browser; CAPTCHA not completed |
| ERROR | Lookup failed (network, site down, page changed) | Re-run later; see log |

`Overall` is `OK` only if every status in the row is REGISTERED, otherwise `REVIEW`.

Which taxes are checked for a row: GST/HST always (NO NUMBER if blank). QST if a QST number is given
or province is QC. BC/SK/MB if their column has a value, or a generic `PST` column has a value and the
province matches, or the province is BC/SK/MB.

## 7. Source-by-source details and gotchas

These were all learned by testing; don't "simplify" them away.

**CRA GST/HST Registry** (`gst.py`)
- Form: GET `reg_01_Ld.action` for a Struts token, POST `reg_01_Sbmt.action` with `businessNumber`
  (9 digits), `businessName`, `requestDate` (YYYY-MM-DD). No CAPTCHA. ~15 lookups in a row are fine.
- Four answers: *registered on this date* / *was not registered on this date* (given **regardless of
  name**, so it's definitive about the number only) / *Insufficient information* (wrong name **or**
  unregistered; CRA deliberately doesn't say which) / field error *GST/HST number is not valid*.
- Name matching ignores case and punctuation but **not extra words**: "The University of British
  Columbia" fails, "University of British Columbia" passes. Hence `name_variants` (drop "The", trade
  name) and the federal-name fallback.
- Rejects dates after **Ottawa's** today. `cra_today()` uses UTC-5 (never ahead of Ottawa; tested
  every hour of a year). Using the local date broke lookups on a UTC machine after 8 PM Eastern.
- Simplified registrants (`RT9999` etc.) are confirmed by the same form; the published list adds
  legal/trade names and dereg dates. A business can appear several times (old RT0001 + new RT9999), so
  `SimplifiedList.lookup` picks the account active on the date.
- **Don't send an `Accept-Language` header to canada.ca**: requests hang through some proxies.

**Federal corporations** (`fedcorp.py`): open.canada.ca dataset `0032ce54-...`, CSVs on CloudFront.
Columns used: `Business number (BN)`, `Corporate name - form 1/2`. Only federally incorporated
companies (~694k BNs); provincial companies, sole proprietors and partnerships aren't there. In a
30-supplier live test, 10 rows used shortened vendor-master names: 4 were recovered as REGISTERED via
the official name and the other 6 got a definite NOT REGISTERED (CRA's date answer ignores the name).

**Revenu Québec QST** (`qst.py`)
- API: `GET https://svcnab2b.revenuquebec.ca/2019/02/ValidationTVQ/{1234567890TQ0001}`, no key.
  `StatutSousDossierUsager`: `R` (Régulier) = registered, `A` (Annulé) = cancelled; others →
  NOT CONFIRMED with the description. `GX.AucuneDonneeTrouvee` = not found,
  `GX.IdentifiantEntreeInvalide` = invalid. NR numbers are rejected by the API ("demande non conforme").
- A business can have several TQ accounts. A number given without a suffix is checked as `TQ0001`
  and the result says so if that account isn't active (seen live: Avient's TQ0001 is cancelled,
  TQ0004 is active).
- 9-digit "QST numbers" belong to listed financial institutions (CRA administers them) → GST check.
- The revenuquebec.ca website (not the API) is behind Cloudflare and blocks some networks.

**eTaxBC** (`bc_pst.py`): needs BN + PST number (the BN is taken from the GST column). Quirks: each
lookup needs a fresh browser context (state sticks otherwise); the terms checkbox sometimes ignores a
click, so it's re-clicked until checked (this caused intermittent timeouts before the fix). Results:
"PST number is valid" / "No Match Found". Has multi-day maintenance windows.

**Manitoba TAXcess** (`mb_rst.py`): `?Link=RSTLookup`. Business name + (RST number or BN). Name match
is case-insensitive and partial, but every word typed must be in the registered name ("Amazon Canada"
fails, "amazon" passes), so variants end with the first word. Its content-security policy blocks
`page.wait_for_function`; wait on elements instead. Results table: Status / Legal Name(s) /
Operating As. "No results found" is a dialog. The RST number is 7 digits; the 15-digit account number
on returns is not accepted.

**Saskatchewan SETS** (`sk_pst.py`): name-only search; confirms a vendor's licence or registered
consumer number without showing it. The server sends an incomplete certificate chain; normal
browsers fix it by fetching the intermediate (over plain HTTP), which failed in the proxied build
environment. Terms of use forbid commercial reproduction; internal verification is fine. **Never try
to bypass the CAPTCHA.**

## 8. Tailoring to a new dataset (the likely next job)

1. **Look before changing.** Run on the real file with `-v` (or read the log). The first lines show
   the header row found and which column each field was mapped to (`column gst <- 'GST #' (column E)`).
2. **Unrecognized headers:** add the spelling to `COLUMNS` in `taxcheck/workbook.py` (compared
   lowercase with spaces and punctuation removed, so `GST #` ≡ `gst#`). Don't add very generic words
   that could match unrelated columns ("date" is already generic; watch for "Date Added").
3. **Province values** in an unusual form (e.g. "Ont.", full addresses): extend `PROVINCES` or derive
   the province from an address column in `RowChecker.check`.
4. **Several numbers in one cell** (e.g. "GST 123... / QST 456..."): split them in
   `SupplierSheet.rows()` or a pre-processing step, rather than loosening the number parsers.
5. **Header not in the first 15 rows**, or multiple sheets: `_find_header` scans 15 rows; `--sheet`
   selects a sheet.
6. **Different output layout** (e.g. only a few columns, separate report): change
   `SupplierSheet.write_results`. Keep `as_text()` for external strings.
7. **Add tests for every adaptation**: a small synthetic workbook in `tests/test_workbook_io.py`
   style. Keep the real dataset out of git.
8. Run `python -m unittest discover tests` and, after changes to any checker,
   `TAXCHECK_LIVE=1 python -m unittest tests.test_live -v`.

Performance: about 3.5 s per supplier when the GST name matches first time (throttled ~1.5 s per CRA
request, ~4–5 s per browser lookup). 500 suppliers ≈ 30 minutes. Duplicate numbers are looked up once.
Ctrl+C saves what's done; re-running on the output file overwrites its result columns.

## 9. Tests

- `tests/test_offline.py`, `test_workbook_io.py`, `test_responses.py`, `test_gst_logic.py`,
  `test_more_sources.py`, `test_robustness.py`, `test_cli.py`: no network, < 1 s total.
- `tests/fixtures/`: **real** responses saved from CRA and Revenu Québec. If a site changes, save a new
  page into a fixture and add a case to `test_responses.py`.
- `tests/test_live.py`: real services with publicly published registrations (UBC, Amazon.com.ca,
  Avient, AIRGSM, a random federal corporation). Opt-in with `TAXCHECK_LIVE=1`; ~1–2 minutes.

Public test data used: UBC GST `108161779RT0001` + BC PST `PST-1000-7572`; Amazon.com.ca ULC
`857305932RT0001` (also active for Manitoba RST); Avient Canada ULC QST `1019288451TQ0004`
(TQ0001 cancelled); AIRGSM PTE. LTD. (Airalo) simplified `751950577RT9999`; North American Tutors
Inc. `774075766`.

## 10. Debugging playbook

| Symptom | Look at | Likely fix |
|---|---|---|
| Many GST NOT CONFIRMED | log lines `CRA registry bn=... name=... -> no_match` | Legal names in the sheet differ from CRA's; check "Name on Government Record" |
| `Transaction date cannot be a future date` | system clock | `cra_today()` should prevent it; check the PC's clock |
| BC/MB all ERROR, "answered HTTP 5xx" or "wasn't responding" | site status | Maintenance; re-run later |
| BC/MB ERROR with a timeout on a specific element | failure screenshot in `logs/` | The site's layout changed; update selectors in `_lookup` |
| `the CRA registry search page has changed` | log | Re-capture the form, update `_registry_lookup` and fixtures |
| QST NR rows ERROR "couldn't load ... NR list" | log | Save the NR page from a browser, re-run with `--nr-list file.html` |
| SK rows MANUAL "CAPTCHA wasn't completed" | | Nobody ticked it within 5 minutes; re-run with `--sk-assist` and watch for the window |
| Browser rows MANUAL "browser couldn't start" | log | `pip install playwright` and `playwright install chromium` |

## 11. Ground rules

- Use only official government sources; respect throttling (`Throttle`) and `SiteHealth`.
- Never bypass CAPTCHAs or disable TLS verification.
- Logs, outputs and caches contain supplier data: keep them out of git (already in `.gitignore`).

## 12. Change log

- **2026-10-11**: Fresh-clone testing on Python 3.11–3.13 with the browser `playwright install` gives
  (Chromium 153). Fixed: CRA "future date" rejection on clocks ahead of Ottawa; slow failures when a
  site is down (site-down detection, fail fast on HTTP errors; sample run 3.5 min → 31 s); CRA page-change
  error message. Added: CSV input; Excel scientific-notation detection; plain one-line input errors;
  Ctrl+C keeps partial results; external text never written as formulas; BC missing BN → NO NUMBER;
  Manitoba falls back to the RST number when the GST number is bad. 30-supplier live test: no errors.
- **2026-10-08**: Manitoba automated (TAXcess), Saskatchewan assisted mode, federal-name fallback for
  GST, NR-list fallbacks, shared lazy browser, CRA connection retries.
- **2026-10-07**: Logging, fixtures-based tests, QST suffix handling, CRA "not registered on date" fix,
  Excel number/formula handling, re-run on results file, duplicate caching. Initial GST/HST, QST,
  BC PST checker.
