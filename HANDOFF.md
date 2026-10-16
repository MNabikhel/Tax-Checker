# Handoff: Tax-Checker

For the agent (or person) taking this over. Read this first; it explains what the tool does, what
has been verified, how the code is laid out, and how to adapt it to a new supplier dataset.
`README.md` is the user-facing guide; this file is the engineering one.

**Last updated:** 2026-10-16 · **Branch:** `main` (current; all work is merged) · **Tests:** 155 offline + 19 live

---

## 0. Start here (for GitHub Copilot or any AI agent tailoring this tool)

The tool is already tailored to the user's ERP vendor-master export (README, "ERP vendor-master
exports") and has done a full live run of it on their corporate network. The decisions still open for
the user are in §2. For a new dataset, your job is almost certainly **adapting the tool to that file**,
not rewriting how the checks work. The checks are verified against live government sites and full of hard-won quirks (§7).
Work in this order:

1. **Set up and prove the baseline** (§3): install, `python -m unittest discover tests` must say OK.
2. **Map the user's file offline:** `python check_suppliers.py <their file> --check-columns`. This makes
   no lookups. Add header spellings to `COLUMNS` in `taxcheck/workbook.py` and re-run until every column
   that matters is mapped and the preview rows route correctly (§8).
3. **Ask the user** (don't guess) about anything that is a business decision: which column holds the
   GST/HST number when the header is ambiguous ("Tax Reg #"), what to do with US/foreign suppliers, which
   date to confirm registration on, whether `--trust-gst-number` is wanted (§8, §6).
4. **Add a test** for each adaptation with made-up rows in the same format (§8 step 7). Never commit the
   real supplier file, results or logs (§11).
5. **Run the live tests** once on the user's PC, including the two open items in §2, and record the
   outcome in the change log (§12).
6. **Small real run first:** a copy of the file trimmed to ~10 rows, check the results and the log in the
   `logs` folder next to it, then the full file.

Rules: keep the government-site code (`gst.py`, `qst.py`, `bc_pst.py`, `mb_rst.py`, `sk_pst.py`,
`orgbook.py`, `fedcorp.py`) unchanged unless a live test or a run shows a site changed; never bypass a
CAPTCHA, loosen TLS, or remove the throttling; update this file's change log with what you changed.

## 1. What it does

`check_suppliers.py` reads a supplier list (Excel or CSV), checks each supplier's Canadian sales-tax
registrations against the governments' own lookup services, and writes a copy of the workbook with
a status and an explanation per tax, plus a summary sheet and a detailed log file.

| Tax | Source | How | State |
|---|---|---|---|
| GST/HST | CRA GST/HST Registry (web form) | `requests` | Automated, verified live |
| GST/HST (simplified, non-resident digital sellers) | CRA registry + CRA's published registrant list | `requests` | Automated, verified live |
| GST/HST name recovery | Corporations Canada open data (federal corporations CSV, ~110 MB) | download, cached 7 days | Automated, verified live |
| GST/HST name recovery (BC) | OrgBook BC public API (search by business number) | `requests` | Automated, verified live |
| GST/HST number-only | CRA registry, date-before-name behaviour (§7) | `requests` | Automated, verified live |
| GST/HST registration dates | CRA registry asked about specific days (`gst_dates.py`, §7) | `requests` | Automated, verified live against CRA's published dates (2026-10-16) |
| Digital-economy registrants with no number on file | CRA's simplified list matched by name, then confirmed with the registry | `requests` | Automated, verified live (2026-10-16) |
| QST (`TQ` numbers) | Revenu Québec validation API (JSON) | `requests` | Automated, verified live |
| QST (`NR` numbers) | Revenu Québec's published NR list | `requests` → browser → saved file | Page reachable from the user's network; **parser untested** (see §2) |
| BC PST | eTaxBC "PST Number Verification Service" | Playwright | Automated, verified live (Chromium 141, 2026-10-16; page loads on the user's Edge 155) |
| MB RST | Manitoba TAXcess "RST Registration Registry" | Playwright | Automated, verified live; ~880 searches in the user's full run on Edge 155 |
| SK PST | SETS "PST On-Line Registry" | Playwright, **person completes a reCAPTCHA** | Assisted (`--sk-assist`); results page **unseen** (see §2) |

Alberta, the territories and the HST provinces (ON, NB, NS, PE, NL) only need the GST/HST check.

## 2. Open items: verify these on the new PC first

**Decisions for the user** (ask; don't decide for them). Current defaults in brackets:
1. Check do-not-use vendors at all? [yes, marker removed for lookups and noted]
2. Count "registered, name not confirmed" as OK (`--trust-gst-number`)? [no: REVIEW]
3. Registration-dates mode for the full run? [`end` when chosen in the menu; adds about an hour]
4. Limit a run to vendors paid recently (by date, never by amount)? [no limit]
5. Run Saskatchewan in assisted mode (someone ticks a CAPTCHA once at the start)? [asked in the launcher's
   menu when Saskatchewan is chosen; Enter = yes]

**Unverified parts.** Two parts couldn't be verified from the cloud environment this was built in. Each has a live test
that settles it in one command on a normal Canadian connection. Run them first and record the result
in the change log (§12).

```
TAXCHECK_LIVE=1 python -m unittest tests.test_live -v                        # item 1 (plus everything else)
TAXCHECK_LIVE=1 TAXCHECK_SK_ASSIST=1 python -m unittest tests.test_live.LiveSaskatchewanAssisted -v   # item 2
```
(Windows PowerShell: `$env:TAXCHECK_LIVE=1; $env:TAXCHECK_SK_ASSIST=1; python -m unittest ...`)

1. **Québec NR list** (`test_nr_list_loads_and_checks`). Revenu Québec's website refused the build
   environment with HTTP 403 (plain download, headless browser, archive sites and a separate fetch
   service all blocked), so the parser has never seen the real page. It reads the documented layout
   (a table: trade name, legal name, number like `NR 0013 0061`) in any column order and falls back to
   list/paragraph layouts. The test skips only if every source answers with an HTTP error; if the page
   loads but no NR numbers are found it **fails**, meaning `qst.parse_nr_list` needs updating: save the
   page from a browser and add it as a fixture.
2. **Saskatchewan results page** (`test_known_vendor`). Needs a person to tick the reCAPTCHA in the
   window that opens. The flow itself (one CAPTCHA, all searches straight after it, back to the search box,
   stopping if the CAPTCHA is asked for again, the PST form's own Search button rather than the site-wide one)
   is tested in a real browser against a local stand-in registry (`tests/test_sk_assisted.py`), and against
   the real site with nobody ticking (waits, then MANUAL CHECK for every row without asking again). The reCAPTCHA only guards the terms page (confirmed from the site's
   `pstLookup.js`); the search page has a `#pstSearch` box (min. 4 characters). The results layout is
   unknown, so `sk_pst.classify()` is conservative: REGISTERED only when a table row contains the
   supplier's name and "vendor" or "consumer". The test searches Federated Co-operatives Limited (a
   retailer across Saskatchewan, so certainly licensed); if it fails, the screenshot `sk_*.png` in the
   repo folder and the logged page text show the real layout to adapt `classify()` to.

## 3. Setup on a new PC

Python 3.9+ (tested on 3.11, 3.12, 3.13; code parses as 3.9).

Windows: double-click `Test Setup.bat` (creates or repairs `.venv`, installs requirements, tries to
download Chromium, then `check_setup.py` checks Python, certificates, every site, the browser, the
workbook and a few real lookups, ending READY / NOT READY), then `Run Trial (25 suppliers).bat` and
`Run Tax Check.bat` (file picker → tax and dates menus → run → Explorer opens on the results).
`START HERE.txt` has the user's steps. Python 3.14 was used for the user's full run without problems.
By hand:

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

**Where things go.** With no path argument, `choose_workbook()` opens a tkinter file picker (starting in
the folder used last time, remembered in `.last_folder`), or asks for a typed path when there's no
display or no tkinter (python.org installers for Windows include tkinter). Outputs follow the chosen
workbook: results as `<workbook>_tax_check.xlsx` beside it, logs and failure screenshots in a `logs`
folder beside it (`default_log_dir`). Reference data shared between runs (federal corporations CSVs,
Québec NR list) lives in the tool's own `cache/`, whatever the current directory. The first run downloads
the federal data (~110 MB), only if some GST name doesn't match; `--no-name-lookup` skips it. `logs/`,
`cache/` and `.last_folder` are git-ignored. `Setup.bat`/`Run Tax Check.bat` haven't been run on Windows
by the original author (built on Linux); if one misbehaves, the commands above do the same thing.

**Corporate networks.** `certs.use_system_certificates()` (truststore) makes Python trust the Windows
certificate store; without it every request failed with `CERTIFICATE_VERIFY_FAILED` behind the user's TLS
inspection. Verification stays on. `Browser` tries `TAXCHECK_CHROMIUM`, Playwright's Chromium, then the
installed Edge (`channel="msedge"`), then Chrome: the user's network blocks the Chromium download, and
Edge 155 worked for every page. canada.ca never answered Python there (read timeouts) but loads in the
browser, so CRA's simplified list falls back to the browser and is cached for a week.

`TAXCHECK_CHROMIUM=/path/to/chrome` points Playwright at a specific browser binary (used in the
build environment; normally not needed). `HTTPS_PROXY`, if set, is passed to the browser.

**Hosts contacted:** see README, "What gets checked". All are government sites, plus the CloudFront URL
open.canada.ca publishes for the federal corporations CSV. Document any new one there.

## 4. How a run works

```
check_suppliers.main()
  ├─ choose_workbook()                   file picker (or typed path) when no path is given
  ├─ logsetup.setup_logging()            <workbook folder>/logs/tax_check_<ts>.log (DEBUG) + console (INFO)
  └─ run()
      ├─ workbook.SupplierSheet(path)    find header row, map columns via COLUMNS aliases
      ├─ build checkers                  GstChecker(+FederalCorporations, OrgBookBC name sources), QstChecker,
      │                                  shared headless Browser (lazy) → BcPstChecker, MbRstChecker, QST NR fallback
      │                                  separate visible Browser (only with --sk-assist) → SkPstAssistant
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
| `check_suppliers.py` | CLI, file picker, menus (`--ask`), output locations, run orchestration with parallel workers (`--workers`), trial/resume/offline, background checkpoint saves, summary-sheet notes | `main`, `choose_workbook`, `run`, `parse_taxes`, `CheckpointSaver`, `save_workbook` |
| `check_setup.py` | Setup test: Python, certificates, each site, browser, workbook, real lookups → READY / NOT READY report | `main`, `check_*`, `explain_error` |
| `Test Setup.bat`, `Run Trial (25 suppliers).bat`, `Run Tax Check.bat`, `Setup.bat` | Windows double-click launchers (`Setup.bat` just calls `Test Setup.bat`) | |
| `taxcheck/workbook.py` | Read .xlsx/.xlsm/.csv, header detection, column aliases, provinces, dates, write results | `COLUMNS`, `PROVINCES`, `SupplierSheet`, `InputError`, `as_text` |
| `taxcheck/checks.py` | Per-row routing (country/postal, foreign vendors, which taxes), vendor-name candidates, official-name confirmation, digital-economy match, registration-date facts, caching, error containment, deferred browser checks for worker threads | `RowChecker`, `vendor_name_candidates`, `clean_vendor_name`, `same_business_name`, `Deferred`, `Offline`, `DryRun` |
| `taxcheck/report.py` | Issue / Action / severity per tax, Main Issue per supplier | `gst_issue`, `provincial_issue`, `row_summary` |
| `taxcheck/gst_dates.py` | GST/HST registration start and end found by asking the registry about specific days | `registered_on`, `registration_start`, `cancellation_date` |
| `taxcheck/certs.py` | Use the operating system's certificate store (truststore) | `use_system_certificates` |
| `taxcheck/diagnostics.py` | Optional copy of reports/logs to a folder set per user (off by default) | `copy_to_diagnostics` |
| `taxcheck/numbers.py` | Offline parsing/validation of every number format; CRA Luhn check digit; free-form "Tax ID" normalization | `parse_gst`, `parse_bn`, `normalize_tax_id`, `tax_id_problem`, `BadNumber` |
| `taxcheck/gst.py` | CRA registry form, result parsing, name variants, simplified list, federal-name fallback, Ottawa date | `GstChecker`, `parse_registry_result`, `SimplifiedList`, `cra_today`, `name_variants` |
| `taxcheck/fedcorp.py` | Corporations Canada CSVs → `{bn9: [official names]}`, weekly cache | `FederalCorporations` |
| `taxcheck/orgbook.py` | OrgBook BC API → official names for BC-registered organizations by business number | `OrgBookBC`, `parse_topics` |
| `taxcheck/qst.py` | Revenu Québec API; NR list with download → browser → file fallbacks and cache | `QstChecker`, `parse_nr_list` |
| `taxcheck/browser.py` | Shared lazily-started browser (Chromium → Edge → Chrome), fresh context per lookup, failure screenshots, fail-fast on HTTP errors | `Browser`, `BrowserUnavailable` |
| `taxcheck/bc_pst.py` | eTaxBC form automation | `BcPstChecker` |
| `taxcheck/mb_rst.py` | TAXcess form automation, name variants down to the first word | `MbRstChecker`, `parse_results` |
| `taxcheck/sk_pst.py` | Assisted SETS search after a human passes the reCAPTCHA | `SkPstAssistant`, `classify`, `search_terms` |
| `taxcheck/http.py` | `requests` session (UA, GET retries, request logging), throttling, site-down detection | `new_session`, `Throttle`, `SiteHealth` |
| `taxcheck/logsetup.py` | Log file + console handlers | `setup_logging` |
| `taxcheck/progress.py` | Per-supplier progress CSV, periodic progress/ETA lines, `--resume` loader | `ProgressReport`, `load_resume` |
| `taxcheck/result.py` | Status constants and `Result` (`status`, `detail`, `registered_name`, `name_check`, `facts`) | `Result.copy` |

## 6. Statuses

| Status | Meaning | Typical cause |
|---|---|---|
| REGISTERED | Source confirms an active registration (GST/HST: number **and** the sheet's name or trade name) | |
| REGISTERED - NAME NOT MATCHED | GST/HST number registered, but not under the sheet's name (CRA's name in the details when known) | Misnamed vendor, or a number belonging to another business |
| NOT REGISTERED | Source definitively says no (or cancelled / deregistered) | |
| NOT CONFIRMED | Source couldn't match; not definitive | BC PST/BN mismatch; no MB/SK match; unusual QST status |
| INVALID NUMBER | Malformed, fails check digit, or Excel scientific notation | Typos |
| NO NUMBER | Tax applies (by province) but no number given | Missing data |
| MANUAL CHECK | Not checked automatically | SK without `--sk-assist`; no browser; CAPTCHA not completed |
| ERROR | Lookup failed (network, site down, page changed) | Re-run later; see log |
| NOT APPLICABLE | Foreign supplier with no explicit Canadian GST/HST number | Country column not CA |
| NOT LOOKED UP | `--offline` run; passed offline checks | |

`Overall` is `OK` only if every status in the row is REGISTERED (or REGISTERED - NAME NOT MATCHED with
`--trust-gst-number`), otherwise `REVIEW`.

Which taxes are checked for a row (`RowChecker.check`):

| Tax | Looked up when | Otherwise |
|---|---|---|
| GST/HST | a GST/HST number is given | always reported: NO NUMBER if blank |
| QST | a QST number is given | NO NUMBER if province is QC; else not reported |
| BC PST | a BC PST number is given (BC PST column, or generic PST column + province BC); BN from the GST or Business Number column | NO NUMBER if province is BC |
| MB RST | province is MB or an RST number is given; searches with the RST number and/or the BN from the GST column | NO NUMBER if neither number exists |
| SK PST | province is SK or an SK PST number is given (name-only search, so the trade name matters; the SK number itself is never used, and the result says so) | MANUAL CHECK without `--sk-assist` |

A generic `PST` value whose province isn't BC, SK or MB (blank, ON, a US state...) isn't checked; a
warning is logged for it. Non-Canadian suppliers (unrecognized province) only get the GST/HST row,
which is NO NUMBER if they have none, so they show as REVIEW. See §8 for options.

## 7. Source-by-source details and gotchas

These were all learned by testing; don't "simplify" them away.

**CRA GST/HST Registry** (`gst.py`)
- Form: GET `reg_01_Ld.action` for a Struts token, POST `reg_01_Sbmt.action` with `businessNumber`
  (9 digits), `businessName`, `requestDate` (YYYY-MM-DD). No CAPTCHA. ~15 lookups in a row are fine.
- Four answers: *registered on this date* / *was not registered on this date* / *Insufficient
  information* / field error *GST/HST number is not valid*.
- **CRA checks the date before the name.** Not registered on the date → "was not registered", whatever
  name is entered. Registered on the date → "Insufficient information" unless the name matches. No GST
  account at all → "Insufficient information" for every date. So when no name matches today, one more
  lookup with any name for 1980-01-01 (before GST existed) settles it from the number alone: "was not
  registered" means the number has an account and, given today's answer, is registered now; "Insufficient
  information" means CRA doesn't know the number. This is `GstChecker._number_only`. Verified 2026-10-11
  on 27 registered and 11 unregistered federal corporations, de-registered simplified accounts (correct
  both inside and after their registration period), and made-up numbers: no disagreement. It proves the
  number is registered, **not** that it belongs to the supplier, hence the separate status. Any match via a
  name that didn't come from the sheet (CRA's simplified list, Revenu Québec, federal registry, OrgBook BC)
  gets the same status.
- Name matching ignores case and punctuation but **not extra or missing words**: "The University of
  British Columbia" fails, "University of British Columbia" passes. Tested 2026-10-13 on 15 registered
  corporations: dropping the suffix ("ACME LTD" → "ACME") failed 15/15, swapping it (LTD → INC) 15/15,
  spelling it out (INC → INCORPORATED) 14/15. The 2012-era claim that CRA only compares the first 10
  characters is false. CRA accepts the legal name **or** an operating/trade name it has on file, each
  exactly. Vendor-master names are split into the likely legal and operating names before CRA is
  asked (`checks.vendor_name_candidates`: do-not-use markers, bracketed status notes, "O/A", "DBA",
  "C.O.B.", "operating as", padded vendor numbers, "-REMIT"...). `gst.name_variants` only adds
  "without a leading The"; official registry names are used as given. Add other shorthand your vendor
  master uses to the patterns at the top of `checks.py`, with a test in `tests/test_vendor_names.py`.
- Rejects dates after **Ottawa's** today. `cra_today()` uses UTC-5 (never ahead of Ottawa; tested
  every hour of a year). Using the local date broke lookups on a UTC machine after 8 PM Eastern.
- Simplified registrants (`RT9999` etc.) are confirmed by the same form; the published list adds
  legal/trade names and dereg dates. A business can appear several times (old RT0001 + new RT9999), so
  `SimplifiedList.lookup` picks the account active on the date.
- **Don't send an `Accept-Language` header to canada.ca**: requests hang through some proxies.
- **How CRA compares names** (measured 2026-10-16 on public registrations; `gst._name_key` mirrors it and
  `LiveCraNameRules` re-checks it): case and extra spaces don't matter; `.` and `&` separate words like a
  space ("Amazon.com.ca" = "Amazon com ca"; "U.L.C." = "U L C", which is not "ULC"); every other punctuation
  mark is dropped and joins its neighbours ("British-Columbia" = "BritishColumbia", which fails;
  "Columbia's" = "Columbias"); a missing space always fails. So "B.C." and "BC" are different searches.
- **Registration dates** (`gst_dates.py`): with a placeholder name, a day the number wasn't registered
  answers "not registered" and a registered day answers "Insufficient information", so dates are found by
  halving (about 16 lookups for a start date, 10 for a cancellation). Checked 2026-10-16 against CRA's
  simplified list: start dates exact; a cancellation is reported as the **last registered day**, the list's
  own convention. The registry answers per **business number**: a business with an older ordinary account
  answers "registered" from that account's start, years before its simplified one.
- **Digital-economy registrants with no number on file** (`checks._digital_economy`): vendors with no usable
  Canadian number (foreign vendors; Canadian rows with none or an invalid one) are compared with every legal
  and trade name on the simplified list using `core_name` (case, punctuation, "&"/"and", legal suffixes
  ignored; whole name only, at least 6 characters: partial matches gave many false positives in a manual
  pass). A match is reported only when CRA confirms the listed number under the listed legal name on the date.

**Official-name sources for GST/HST.** When the sheet's names don't match, `GstChecker._candidates`
tries, in order: CRA's simplified list names; names other checks returned for the same row (Revenu
Québec's legal and trade name, because `RowChecker` runs QST before GST); then each `name_sources`
entry (federal registry, then OrgBook BC), consulted lazily. Any match through these gives REGISTERED -
NAME NOT MATCHED with that name in "GST/HST Name Accepted by CRA"; `RowChecker._confirm_official` turns it
into REGISTERED when the official name is the vendor's own apart from spacing, punctuation, "&"/"and", legal
suffix, a successor year, one half of a bilingual name, or a vendor name cut off at the ERP's 40
characters. To add a source, give it `.label` and
`.names(bn9)` and append it to `name_sources` in `check_suppliers.run`.

**OrgBook BC** (`orgbook.py`): `GET https://orgbook.gov.bc.ca/api/v4/search/topic?q=<bn9>`, no key.
Each result ("topic") has `names` (types `entity_name`, `business_number`) and `attributes` (`entity_status`
`ACT` = active). Covers BC-incorporated companies, extraprovincial registrations (companies from elsewhere
registered in BC), societies, firms. Verified 2026-10-11: for 20 BC-incorporated companies absent from the
federal data, CRA accepted OrgBook's name for all 19 GST-registered ones. Not covered: companies only
registered in other provinces (Ontario, Alberta... have no free BN-searchable API found), sole proprietors.

**Researched and rejected (2026-10-11)**, so nobody repeats it: Québec's open enterprise register
(Données Québec) has only the NEQ, no CRA business number; third-party "number-only" GST APIs
(VerifyVat: format check only; Vatlas: same federal data as ours); Canada's Business Registries federated
search (no official API, blocks automated access); Manitoba TAXcess with a one-letter name (rejected);
CRA charities open data (only historical yearly files found; few suppliers are charities).

**Federal corporations** (`fedcorp.py`): open.canada.ca dataset `0032ce54-...`, CSVs on CloudFront.
Columns used: `Business number (BN)`, `Corporate name - form 1/2`. Only federally incorporated
companies (~694k BNs); provincial companies, sole proprietors and partnerships aren't there. In a
30-supplier live test, 10 rows used shortened vendor-master names: 4 were matched via the official name
(now reported as REGISTERED - NAME NOT MATCHED with that name) and the other 6 got a definite NOT
REGISTERED (CRA's date answer ignores the name).

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

**eTaxBC** (`bc_pst.py`): needs BN + PST number (the BN is taken from the GST column). Since its October
2026 maintenance it has a content security policy that blocks `page.wait_for_function` (like TAXcess):
wait on elements/text instead. Quirks: each
lookup needs a fresh browser context (state sticks otherwise); the terms checkbox sometimes ignores a
click, so it's re-clicked until checked (this caused intermittent timeouts before the fix). Results:
"PST number is valid" / "No Match Found". Has multi-day maintenance windows.

**Manitoba TAXcess** (`mb_rst.py`): `?Link=RSTLookup`. Business name + (RST number or BN). Name match
is case-insensitive and partial (any part of the name: "mazon" finds AMAZON.COM.CA ULC), but every word typed
must be in the registered name ("Amazon Canada" fails, "amazon" passes), so variants end with the first
word. Checked 2026-10-16: a term with an apostrophe finds nothing, and a term under 4 characters leaves the
page on the search form until the wait times out (the user's full run had six such timeouts, all names with
an apostrophe). `_searchable` sends the longest apostrophe-free stretch, at least 4 characters; a name with
nothing searchable is MANUAL CHECK. Saskatchewan's `search_terms` may need the same, but can't be tested
without a person at the CAPTCHA. Its content-security policy blocks
`page.wait_for_function`; wait on elements instead. Results table: Status / Legal Name(s) /
Operating As. "No results found" is a dialog. The RST number is 7 digits; the 15-digit account number
on returns is not accepted.

**Saskatchewan SETS** (`sk_pst.py`): name-only search; confirms a vendor's licence or registered
consumer number without showing it. The server sends an incomplete certificate chain; normal
browsers fix it by fetching the intermediate (over plain HTTP), which failed in the proxied build
environment. Terms of use forbid commercial reproduction; internal verification is fine. **Never try
to bypass the CAPTCHA.** The CAPTCHA is needed once per browser session: in `pstLookup.js` only the terms
page's Next button calls `grecaptcha.getResponse()`; `submitPSTSearch` only validates 4+ characters. So
`check_suppliers.prefetch_saskatchewan` runs every Saskatchewan search (`RowChecker.saskatchewan_searches`, the
same arguments the rows use, so the rows hit the cache) right after the person passes it at the start of the
run. Every SETS page has a site-wide "Search SETS" button in the header: click only the button inside
`form:has(#pstSearch)`.

## 8. Tailoring to a new dataset (the likely next job)

1. **Look before changing, offline:**
   `python check_suppliers.py their_file.xlsx --check-columns` makes **no lookups**. It prints the header
   row found, which column feeds which field, unrecognized headers with the closest known alias, fields
   with no column, and what the first 10 rows would check (`--preview N` for more). If no header row is
   recognized at all, it picks the most header-like row and gives a per-column suggestion instead.
   Expect to iterate: add aliases, re-run `--check-columns`, until every column that matters is mapped
   and each preview row routes the way you'd expect (e.g. a BC row with a PST number shows `bc_pst`).
2. **Unrecognized headers:** add the spelling to `COLUMNS` in `taxcheck/workbook.py`. Matching rules
   (`_key`): lowercase, then remove everything except letters, digits and `#`, then require an **exact**
   match with an alias treated the same way. So `GST #` → `gst#` and `GST No` → `gstno` are different
   keys, and `Supplier Legal Name` won't match `legal name`. If two columns match the same field, the
   **leftmost** wins. The header row needs a name column plus at least one other recognized column.
   Don't add very generic words that could match unrelated columns ("date" is already generic; watch for
   "Date Added"). The suggestions from `--check-columns` are hints; confirm what the column holds.
   Identifier columns ("Vendor ID", "Supplier Code") never get a suggestion, and headers mentioning a
   tax are only suggested as tax-number fields. A generic header like "Tax Reg #" gets no suggestion:
   ask what it holds (usually the GST/HST number → add it to `COLUMNS["gst"]`).
3. **Province values** in an unusual form (e.g. "Ont.", full addresses): extend `PROVINCES`. To derive
   the province from an address, add an `address` key to `COLUMNS` and parse it in `RowChecker.check`.
   US/foreign suppliers: decide with the user whether they should be skipped, reported as "not
   applicable", or checked only if they have a GST number (non-residents selling digital services may
   be simplified registrants); implement that in `RowChecker.check`. If the file has a Country column,
   that's the natural signal: add a `country` key to `COLUMNS` and branch on it there (it isn't read today).
4. **Several numbers in one cell** (e.g. "GST 123... / QST 456..."): split them in
   `SupplierSheet.rows()` or a pre-processing step, rather than loosening the number parsers.
5. **Header not in the first 15 rows**, or multiple sheets: `_find_header` scans 15 rows; `--sheet`
   selects a sheet.
6. **Different output layout** (e.g. only a few columns, separate report): change
   `SupplierSheet.write_results`. Keep `as_text()` for external strings.
7. **Add tests for every adaptation**, with made-up rows in the same format as the real file (CSV if
   it's a CSV): one for the column mapping (`SupplierSheet(path).cols` and the values from `.rows()`, see
   `tests/test_workbook_io.py`) and, if routing changed, one through `RowChecker` with stand-in checkers
   (`checks.DryRun`, or the small fakes in `tests/test_more_sources.py`). Keep the real dataset out of git.
8. **Dates:** `parse_date` accepts ISO (with or without time), `YYYYMMDD`, `15/09/2026` (day first:
   `09/10/2026` is 9 October), `15-Sep-2026`, `15 Sep 2026`, `September 15, 2026`, and Excel date cells.
   A blank date uses today (`--date` overrides); an unreadable one also uses today, with a warning in
   the log. The GST/HST check confirms registration *on that date*. A "last invoice date" is a reasonable
   choice for "was it registered when they charged us"; for "is it registered now", don't map a date
   column (or use `--date`). Confirm the column holds dates, not invoice numbers.
9. Run `python -m unittest discover tests` and, after changes to any checker,
   `TAXCHECK_LIVE=1 python -m unittest tests.test_live -v`.

To poke at the code from another folder, run Python from the repo root or set `PYTHONPATH` to it
(`taxcheck` isn't an installed package).

Performance: about 3.5 s per supplier when the GST name matches first time (throttled ~1.5 s per CRA
request, ~4–5 s per browser lookup). 500 suppliers ≈ 30 minutes. A past check date adds one CRA lookup (~1.5 s) per registered number, to see
whether it's still registered. Duplicate numbers are looked up once.
Ctrl+C saves what's done; re-running on the output file overwrites its result columns.

## 9. Tests and examples

- `tests/test_offline.py`, `test_workbook_io.py`, `test_responses.py`, `test_gst_logic.py`,
  `test_more_sources.py`, `test_robustness.py`, `test_cli.py`, and for the vendor export
  `test_vendor_export.py`, `test_vendor_names.py`, `test_report.py`, `test_gst_dates.py`,
  `test_digital_economy.py`, `test_fallbacks.py`, `test_check_setup.py`, `test_diagnostics.py`: no network,
  a few seconds in all. Names and numbers in them are made up (or public test data below).
- `tests/fixtures/`: **real** responses saved from CRA and Revenu Québec. If a site changes, save a new
  page into a fixture and add a case to `test_responses.py`.
- `tests/test_live.py`: real services with publicly published registrations (UBC, Amazon.com.ca,
  Avient, AIRGSM, a random federal corporation, simplified registrants picked from CRA's list). Also CRA's
  name-comparison rules, registration dates against the list, the browser copy of the list, digital-economy
  matching, Manitoba apostrophes, Edge (Windows only) and an end-to-end run with 3 workers and dates.
  Opt-in with `TAXCHECK_LIVE=1`; ~2–3 minutes.

`examples/`: `suppliers_template.xlsx` (blank headers), `sample_suppliers.xlsx` (11 public test
suppliers covering every outcome), `sample_suppliers_results.xlsx` and `sample_run.log` (a full run's
output and log from a fresh clone on 2026-10-13, useful to see what a log looks like).

Public test data used: UBC GST `108161779RT0001` + BC PST `PST-1000-7572`; Amazon.com.ca ULC
`857305932RT0001` (also active for Manitoba RST); Avient Canada ULC QST `1019288451TQ0004`
(TQ0001 cancelled); AIRGSM PTE. LTD. (Airalo) simplified `751950577RT9999`; North American Tutors
Inc. `774075766`.

## 10. Debugging playbook

| Symptom | Look at | Likely fix |
|---|---|---|
| Many GST "REGISTERED - NAME NOT MATCHED" | the **Action List** sheet, "GST/HST Registry Name" and "Details" columns, log lines `CRA registry bn=... -> no_match` | Vendor names differ from CRA's: update the vendor master to the legal name on the supplier's invoice, add the vendor master's shorthand to the patterns in `checks.py`, or use `--trust-gst-number` if only the number matters |
| `Transaction date cannot be a future date` | system clock | `cra_today()` should prevent it; check the PC's clock |
| BC/MB all ERROR, "answered HTTP 5xx" or "wasn't responding" | site status | Maintenance; re-run later |
| BC/MB ERROR with a timeout on a specific element | failure screenshot in the `logs` folder next to the workbook | The site's layout changed; update selectors in `_lookup` |
| `the CRA registry search page has changed` | log | Re-capture the form, update `_registry_lookup` and fixtures |
| QST NR rows ERROR "couldn't load ... NR list" | log | Save the NR page from a browser, re-run with `--nr-list file.html` |
| SK rows MANUAL "CAPTCHA wasn't completed" | | Nobody ticked it within 5 minutes; re-run with `--sk-assist` and watch for the window |
| Browser rows MANUAL "browser couldn't start" | log | `pip install playwright` and `playwright install chromium` |
| "Couldn't find a header row" | `--check-columns` output | Add the file's header spellings to `COLUMNS` |
| A whole tax column is missing from results | `--check-columns` "Fields with no column" | Map that column; for PST, check the province values |

## 11. Ground rules

- Use only official government sources; respect throttling (`Throttle`) and `SiteHealth`.
- Never bypass CAPTCHAs or disable TLS verification.
- Logs, outputs and caches contain supplier data: keep them out of git. `.gitignore` covers `logs/`,
  `cache/` and `*_tax_check.xlsx`, but not results written elsewhere with `-o`.

## 12. Change log

- **2026-10-16 (second agent's work reviewed and merged)**: A second agent tailored the tool to the user's
  ERP vendor-master export and ran it live on their corporate network (Windows 11, Python 3.14, Edge 155,
  TLS inspection; ~20k rows in 2 h 46 min with 3 workers, 13,469 CRA searches, no CRA errors). Its work came
  back as a patch with a handoff package (requirements R1–R16, live evidence); summarized in §2, §3, §5, §7.
  Reviewed, merged and verified live here:
  - Names: their candidates replace the earlier `gst.name_variants` cleanup; added `[DO NOT USE]` (it lost
    the whole name), bracketed status notes such as `(INACTIVE)` (were tried as trade names), "doing /
    carrying on business as", "C.O.B.", "O.A.". `_name_key` now follows CRA's measured rules (§7); their
    "spaces count" assumption was right, but punctuation isn't simply ignored.
  - The suffix swap (Ltd ↔ Limited) stays: CRA rejects the other spelling 14–15 times in 15, so when the
    vendor master spells it differently from CRA, it's the only way to match.
  - Registration dates verified against CRA's list; cancellations now report the last registered day.
  - Manitoba: apostrophes and short terms fixed (§7), verified live.
  - Checkpoint saves run in a background thread (they paused the run 5–90 s each).
  - New: digital-economy registrants among vendors with no usable number (§7), verified live.
  - Live tests added for all of the above; test names and numbers that weren't clearly made up were
    replaced. Not verified live: Edge (no Windows here), the Québec NR parser (Revenu Québec refuses this
    network), Saskatchewan, BC PST with the export, a real `--resume`.
- **2026-10-13 (file picker)**: Running without a path opens a file picker (typed-path fallback without a
  display), starting in the last folder used. Logs and screenshots now go to a `logs` folder next to the
  workbook (results already did); the shared cache is anchored to the tool's folder. Added `Setup.bat` and
  `Run Tax Check.bat`. Tested: the real tkinter dialog on a virtual display (driven with xdotool), the
  typed fallback with a quoted path containing spaces, cancel, and the remembered folder.
- **2026-10-13 (handoff)**: Fresh clone of `main` re-verified end to end (install, offline tests, full
  sample run: all 11 rows correct in 40 s); example outputs refreshed. Added §0 and
  `.github/copilot-instructions.md` for the Copilot handoff.
- **2026-10-13**: Re-verified BC after eTaxBC's maintenance with Chromium 153. The maintenance added a
  content security policy that broke the result wait (`wait_for_function`); now waits on the "Search
  Date" text. 12 lookups in a row, no failures. Full live suite: everything passes except the two items in
  §2 that need the new PC (Québec NR list refused this network; Saskatchewan needs a person).
- **2026-10-11 (more name sources)**: Researched other ways to identify a business from its GST number
  (see §7 "Researched and rejected"). Added OrgBook BC as an official name source, and the GST check now
  also tries the legal name Revenu Québec returns for the same supplier's QST number. Misnamed BC
  suppliers (e.g. "KIP Trucking", a blank name) now come back with their legal name.
- **2026-10-11 (number-only GST)**: GST/HST results no longer depend on having the right vendor name.
  When no name matches, one extra lookup (date before GST existed) tells registered / not registered /
  unknown number from the number alone (see §7). New status REGISTERED - NAME NOT MATCHED for numbers
  registered under another name (including matches found via the federal registry or CRA's simplified
  list); `--trust-gst-number` counts it as OK. GST/HST no longer returns NOT CONFIRMED. Tested on a
  misnamed 13-vendor list (blank names, typos, "Misc Vendor"): every row got a definite answer.
- **2026-10-11 (open items)**: Tried every route to verify the three open items from the build
  environment (archives, a separate fetch service, browser download); all blocked. Made them one-command
  checks on the new PC instead: live tests for the NR list (skips only when refused, fails if the page
  can't be read) and for Saskatchewan (assisted). NR parser accepts any column order and list layouts.
- **2026-10-11 (second review)**: A fresh agent tailored a copy to an unfamiliar CSV using only this
  file (knew what to do on reaching §8; 6 aliases + 1 test). Its notes led to: `--check-columns` gives
  per-column suggestions even when no header row is recognized; no suggestions for ID/code columns;
  tax-ish headers only suggested as tax fields; "provincial sales tax" aliases; SK results note that the
  sheet's SK number isn't used; docs on dates, Country columns and what tests to write.
- **2026-10-11 (later)**: A cold read of this handoff by a fresh agent found gaps, now fixed: added
  `--check-columns` (offline column mapping and routing preview with alias suggestions); header-not-found
  error lists the headers seen; more date formats; warning when a PST number is ignored because of the
  province; common headers like "Legal Entity", "DBA Name", "State/Prov" recognized; quiet library
  logging. Docs: exact matching rules, routing table, non-Canadian suppliers, examples.
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
