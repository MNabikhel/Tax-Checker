# Tailoring guide: fitting Tax-Checker to the user's supplier data

**For the agent (GitHub Copilot or other) taking over.** Your job: make this tool give accurate results on the
user's real vendor file, which you (like the agent before you) can't see directly. The user runs things on their
PC and shares outputs that contain no supplier data (a profile report, `--check-columns` output, logs if they
choose). This guide explains, end to end, what the tool does and why, what is known about the user's data, and a
step-by-step way to tailor and verify. `HANDOFF.md` is the engineering reference (every site's quirks, code map,
change log); this file is the "how to think about it and what to do" guide. Read this first, then HANDOFF §0.

Last updated 2026-10-20. Tests: 225 offline, 21 live (`TAXCHECK_LIVE=1`).

---

## 1. Ground rules (non-negotiable; the user set these)

1. **Never edit the user's source workbook.** Results always go to a copy (`<name>_tax_check.xlsx`).
2. **Never guess a legal name.** Names tried with CRA are only forms of the vendor's own name (markers removed,
   legal part before "O/A", suffix spelled the other way) or names from an official registry for that business
   number. A registry name counts only when CRA accepts it for that number. Anything weaker is a *lead* in
   "Action Needed" for a person, never a status change.
3. **Never filter or prioritize by payment amount.** Only by last payment date, and only when the user asks
   (`--paid-since`, `--paid-within`); default is everyone.
4. **Never bypass a CAPTCHA, disable TLS verification, or remove throttling.** Saskatchewan's CAPTCHA is ticked
   by a person once per run; Saskatchewan allows 100 searches per 24 hours.
5. **Never commit supplier data**: no real names or numbers in tests, docs, fixtures, commit messages. Tests use
   made-up names and numbers (valid check digits) or the public test data listed in HANDOFF §9.
6. **Keep the double-click `.bat` flow working** (the user isn't a developer).
7. **Ask the user about business decisions** (what a column means, how to treat a group of vendors) instead of
   guessing; record the decision in HANDOFF §2 and the change in HANDOFF §12.

## 2. Canadian sales tax in five minutes (what the tool checks and why)

| Tax | Who runs it | Number format | Applies to | How the tool checks |
|---|---|---|---|---|
| **GST/HST** (federal; HST = GST + provincial part in ON, NS, NB, NL, PE) | CRA | Business number (BN) 9 digits + program `RT` + 4-digit account: `123456789RT0001` | Every supplier registered in Canada, plus non-residents selling to Canada | CRA GST/HST Registry (web form): BN + **name** + **date** |
| **QST** (Québec) | Revenu Québec | 10 digits + `TQ` + 4: `1234567890TQ0001`; non-residents: `NR00012345` | Québec suppliers, some non-residents | Revenu Québec JSON API; NR numbers from a published list |
| **BC PST** | BC | `PST-1234-5678` | BC suppliers | eTaxBC form: PST number + BN must match |
| **Manitoba RST** | Manitoba | 7 digits | Manitoba suppliers | TAXcess form: name + (RST number or BN) must match |
| **SK PST** | Saskatchewan | vendor licence (not used) | Saskatchewan suppliers | SETS search by **name only**, after a person ticks a CAPTCHA |

Key ideas the code depends on:
- **The business number (first 9 digits) identifies the business.** `RT` is the GST/HST program; `RC`
  (income tax), `RP` (payroll), `RR` (charity) are other programs of the same business. A vendor master often
  holds the RC number: the tool checks the same BN for GST/HST and flags the Tax ID ("Tax ID needs correcting").
- **Input tax credits (ITCs):** a buyer recovers GST/HST it paid only if the supplier was registered *on the
  invoice date*. That is why the check is "registered **on the payment date**" (the `Check/ Itm Date` column),
  and why a registration cancelled after that date is flagged with its last registered day.
- **CRA's registry needs the name CRA has on file.** It confirms "number X is registered on date D under name N".
  A wrong name gives "Insufficient information", which is ambiguous (wrong name? no account?). The tool resolves
  this with name variants, official-name registries and a date trick (HANDOFF §7: asking about 1980, before GST
  existed, separates "registered under another name" from "no such account").
- **Simplified (digital economy) registrants:** non-resident sellers of digital services (Google LLC, Dropbox,
  Slack, OpenAI, Canva, Spotify...) can register under a *simplified* regime; since July 2023 their numbers end in
  **RT9999** (older `RT0001` accounts were closed 2023-06-30). **GST/HST they charge is not eligible for ITCs**;
  a registered buyer should give them its own GST/HST number so they stop charging it. CRA publishes the list
  (legal name, trade names, BN, registration/de-registration dates). Vendor masters usually file these sellers as
  foreign vendors with a US EIN or no number, so the tool finds them **by name** (§3, step 6).

## 3. One supplier row, end to end

Example (made up): a row in the ERP export
`Company Name = "(DNU) PRAIRIE WIDGET O/A PW SUPPLY"`, `State = ""`, `Country = CA`, `Postal Code = R3H 0H3`,
`Tax ID = 123456782RC0001`, `Check/ Itm Date = 2026-03-31`.

1. **Read and map columns** (`taxcheck/workbook.py`, `SupplierSheet`). The header row is found in the first 15
   rows; each header is matched to a field through `COLUMNS` aliases (lowercased, punctuation removed, exact
   match). Fields: `name, trade_name, gst, bn, qst, pst, bc_pst, sk_pst, mb_rst, province, country, postal, date`.
   When two columns fit one field ("Zip" and "Postal Code"), one with data wins, then the more specific spelling
   (earlier in that field's `COLUMNS` list), then the leftmost; the choice is logged.
   Values are cleaned (`clean_value`: Excel floats such as `857305932.0` back to whole numbers; scientific notation is flagged later as an invalid number).
2. **Clean the name** (`checks.clean_vendor_name`, `vendor_name_candidates`). "(DNU)" is removed (the row is
   marked do-not-use and still checked); "O/A PW SUPPLY" makes "PRAIRIE WIDGET" the legal part and "PW SUPPLY" a
   trade name. Candidates, most likely first: as given, legal part (tidied: padded vendor numbers, "REMIT", "*" dropped), suffix variants (Ltd↔Limited),
   numbered-company forms ("1234567 AB" → "1234567 ALBERTA LTD."), then trade names. Max 8.
3. **Route** (`RowChecker._check_row`). Country "CA" (or blank with a Canadian postal code) → Canadian. Province
   from `State`, or from the postal code's first letter when blank ("R" → MB). Foreign vendors get only the
   GST/HST row: NOT APPLICABLE unless the Tax ID is an explicit Canadian number (9 digits + `RT`...), or a bare
   9-digit number that is on CRA's simplified list, or the name is a simplified registrant (step 6).
4. **Parse the Tax ID** (`numbers.normalize_tax_id`, `parse_gst`). `123456782RC0001` → BN `123456782`, note
   "RC account, not RT". Also handled: spaces/dashes, `R123456789`, QST numbers in the Tax ID column (checked as
   QST), placeholders `000000000` (NO NUMBER), Excel scientific notation, check-digit typos (INVALID NUMBER).
5. **GST/HST** (`gst.GstChecker.check`): (a) CRA's simplified list by BN; (b) CRA registry with each name
   candidate on the payment date (future dates → today, Ottawa time); (c) if none matches, names from other
   checks (Revenu Québec), then official-name sources by BN: Corporations Canada (federal, `fedcorp.py`),
   Canada's Business Registries (all provinces, `cbr.py`), OrgBook BC (`orgbook.py`); (d) if still none, the
   number-only inference (§2). Outcomes: REGISTERED (sheet name accepted), REGISTERED - NAME NOT MATCHED (number
   registered, not under our name; CRA's name shown when known), NOT REGISTERED, INVALID NUMBER, NO NUMBER.
   `RowChecker._confirm_official` turns NAME NOT MATCHED into REGISTERED + "UPDATE NAME" when the official name
   is just another form of the vendor's own name (punctuation, suffix, cut-off at 40 characters, a foreign
   legal form left out: "SPOTIFY" vs "SPOTIFY AB"; never a region or "International" left out, which names a sister
   company; never "ACME LP" vs "ACME INC.", different entities; "ACME" vs "ACME LP" is fine). Registration dates (`gst_dates.py`):
   Quick mode finds the cancellation day if it's no longer registered today.
6. **Digital economy** (`RowChecker._digital_economy`): for a foreign vendor with no usable Canadian number (or
   a Canadian one whose number is missing or invalid): exact name match against the simplified list's legal and trade names (case,
   punctuation, legal endings incl. foreign ones like Pty/AB/GmbH ignored; 5+ letters); else, foreign vendors
   only, a *probable* match (region, ".com" or a word like "Technologies" left out; exactly one listed business)
   flagged "check it's the same business". CRA must confirm the listed number under the listed name. Measured on
   CRA's list with realistic spellings: 50 of 56 found; on 694,224 unrelated company names: 11 probable hits.
   **Two paths, by what's in the Tax ID:** (a) an explicit Canadian number (`...RT9999`, `...RT0001`, or a bare
   9 digits that are on the list) goes through step 5: the list is found by business number, CRA is asked with
   the sheet's names and the listed names; if CRA accepts none, the number-only check decides (the list alone is
   never taken as CRA's confirmation). (b) No usable Canadian number (EIN, blank): the name match above.
   **Outcome either way:** Overall **REVIEW**, issue "Simplified (digital economy) registrant" with the action
   "give them our GST/HST number so they stop charging it" (their GST/HST isn't ITC-eligible). Dates come from
   CRA's list only (the registry answers per business number and can show an older ordinary account's dates).
   A wrong or 9-digit-only Tax ID gets the full `RT9999` number in "GST/HST Tax ID Problem".
7. **No number at all** (`RowChecker._possible_number`): a Canadian vendor with no GST/HST number gets a lead
   when Canada's Business Registries has exactly one BN under exactly that name (same kind of entity) and CRA
   confirms it, and only when every match fits on the result page. Status stays NO NUMBER. `--no-number-search`
   turns it off (it costs one registry search per Canadian supplier with no number).
8. **Provincial checks**: QST if a QST number exists (no TQ suffix → TQ0001; if it wasn't registered on the
   payment date, TQ0002, TQ0003... each read against that date, stopping at the first account Revenu Québec
   doesn't know); BC PST if a BC PST number exists; MB RST if province is MB (searched with the BN; one
   distinctive word per name; the name CRA accepted is tried too); SK PST if province is SK (only with
   `--sk-assist`; one CAPTCHA, all searches up front, 100/day). Browser checks run on the main thread.
9. **Facts and report** (`_annotate`, `report.py`, `workbook.write_results`): per tax a Status, Issue,
   Details; per row Overall (OK / REVIEW / NOT APPLICABLE / NOT CHECKED), Main Issue, Action Needed, Other Issues,
   Vendor Name Check (MATCHES / UPDATE NAME / NOT CONFIRMED), Suggested Vendor Name. Cross-row notes: GST/HST
   numbers shared by differently named vendors; names at the file's longest length (probably cut off).
   Sheets: suppliers (original columns untouched + result columns), Action List (REVIEW rows, worst first),
   Tax Check Summary (counts, issues with generic actions, run notes: sources, options, cut-offs, errors).

Errors never stop a run: each row's exceptions become ERROR (traceback in the log); a site that keeps failing
is paused and retried every 2 minutes, given up after 30 (`http.SiteHealth`). `--resume` re-checks ERROR rows.

## 4. What is known about the user's data and setup

From the user's trial runs (logs they shared; no supplier data kept):
- **File:** ERP vendor-master export, `.xlsx`, one sheet, header on row 1, **19,911 rows**. Columns used:
  `Company Name` (name), `State` (province), `Country`, `Postal Code`, `Tax ID` (GST/HST for Canadian vendors,
  EIN or other IDs for foreign ones), `Check/ Itm Date` (last payment date). Also `Internal Supplier Number`
  (not used). **No QST, PST, RST, trade-name or business-number columns.**
- **Roughly half or more of the rows are foreign** (mostly US): 37 of the trial's first 64 rows; the earlier
  full run was "about half Canadian". Canadian rows cover all provinces. The 64-row trial had 25 rows needing an
  online lookup.
- **Names:** mostly upper case; the name field holds **40 characters** (longer names are cut off); markers seen:
  `(DNU)`, `(DNU)NAME`, `(DO NOT USE)`, `NAME* DO NOT USE`, `*****NAME*** DO NOT USE`, `O/A`, `DBA`, `ITF`,
  bracketed trade names `NAME INC. (TRADE)`, numbered companies `1000353971 ONTARIO INC.`, `0777792 B.C LTD`,
  Québec `9168-7590 QUEBEC INC`.
- **Tax IDs:** mostly `999999999RT0001`; also `999999999 RT0001`, RC accounts, blanks; foreign rows have EINs
  (`99-9999999`, `999999999`).
- **Dates:** Excel date cells, 2025–2026 in the trial.
- **Environment:** Windows 11, Python 3.14, corporate network with TLS inspection (the tool uses the Windows
  certificate store via `truststore`), Playwright's Chromium download blocked → **Microsoft Edge** is used; the
  tool folder and workbooks live in OneDrive. The user runs `.bat` files: `Test Setup.bat`, `Run Trial (25
  suppliers).bat`, `Run Tax Check.bat`, `Saskatchewan Probe.bat`, `Profile Workbook.bat`.
- **Earlier full run** (before the latest improvements): ~2 h 45 min with 3 parallel lookups, dates off.

What is **not** known, and how to find out: the exact frequency of each Tax ID shape, province/country
spellings, name markers beyond those above, how many vendors are online sellers, duplicates. Get them from the
profile report (§5, step 1). Don't assume the trial's 64 rows are representative of all 19,911.

## 5. Tailoring playbook

**Step 1. Get the shape of the data.** Ask the user to double-click **`Profile Workbook.bat`** (or
`python profile_workbook.py <file>`) and send the text file it saves in the `logs` folder **next to the
workbook** (in the user's case inside OneDrive), named `profile_<date>.txt`. Ask them to glance at the
COUNTRY and PROVINCE sections first: values are printed as written unless they look like free text. It contains
only counts and patterns (digits as `9`, letters as `A`): column mapping; Canadian vs foreign row counts (the
tool's own `RowChecker.locate`); Tax ID shapes separately for Canadian and foreign rows, and for Canadian rows
what the tool reads them as and the Tax ID problems by kind; country and province spellings (and foreign rows
whose State is a Canadian code); postal shapes; name-marker counts and the name length limit; repeats; date
kinds and range; shared numbers; **online sellers** whose names are on CRA's simplified list (exact / probable;
the list is downloaded once, `--offline` skips it); and the offline routing per tax. Also ask for
`python check_suppliers.py <file> --check-columns` output if a column looks unmapped.

**Step 2. Act on what the profile shows.** Use this table; each fix needs a test with made-up rows in the same
shape (step 3).

| The profile shows | Meaning | Change |
|---|---|---|
| A tax/name/date column marked "(not used)" | header spelling unknown | add the spelling to `COLUMNS` (`taxcheck/workbook.py`) |
| "Fields with no column: qst / mb_rst / bc_pst" and the user can export them | more taxes checkable | nothing in code: the column is picked up by name; tell the user which headers work |
| Tax ID shapes not read as `gst` (e.g. `unreadable`, many `invalid`) | a format the parser doesn't know | extend `numbers.normalize_tax_id` / `_compact_tax_id`, with a test per shape; never loosen the check digit |
| Many "Corporate income tax account (RC)" | vendor master holds RC numbers | already handled (checked by BN, flagged); just report it |
| Province values left as `(blank)` or not two-letter codes in the routing | spelling not known | add to `PROVINCES`; postal codes already fill blanks |
| Country values for Canada other than `CA`, `CAN`, `CANADA`, `CDN` | normalization | add to `COUNTRIES_CA` in `workbook.py` |
| Name markers with high counts not in §4 (e.g. `- INACTIVE`, `ZZ`, `XX`) | vendor-master shorthand | `_DNU`, `_STATUS_TAG`, `_JUNK`, `_OPERATING_AS` in `checks.py`; candidates must stay forms of the vendor's own name |
| Longest name = N with many at N | field limit N | automatic (cross-row note); `same_business_name(truncated_at=...)` uses 34+ |
| Dates "unreadable text" | date format | add the format to `parse_date` (day-first is the Canadian default) |
| Many `gst NOT APPLICABLE` from foreign rows with Canadian-looking numbers | foreign vendors with GST numbers | explicit RT numbers are already checked; a bare 9-digit EIN is not sent (can pass the check digit by chance) unless it is on CRA's simplified list |
| `sk_pst WOULD CHECK` above ~100 | Saskatchewan daily limit | expected: the run marks the rest MANUAL with the reopening time; `--resume <progress csv>` the next day searches them (resume re-checks ERROR rows and Saskatchewan rows whose search couldn't run) |
| ONLINE SELLERS count high, or many foreign rows NOT APPLICABLE you expected to be online sellers | vendor-master spellings the matcher misses | measure first (as in HANDOFF §12, 2026-10-20) with made-up spellings; extend `_FOREIGN_FORMS`/`_REGIONS`/`_DESCRIPTORS` in `checks.py`; re-check the false-match rate on unrelated names before shipping |

**Step 3. Test every change** with made-up rows that copy the *shape* from the profile (never real values).
Patterns to follow: `tests/test_vendor_export.py` (the export layout end to end with stand-in checkers),
`tests/test_vendor_names.py` (name markers), `tests/test_offline.py` (number formats and routing),
`tests/test_profile.py` (profile output; extend it if you change what the profile prints, and keep its
"nothing identifying leaks" assertion). Run `python -m unittest discover tests` (about a minute; browser
tests need Playwright's Chromium or Edge, else they skip).

**Step 4. Verify on the user's PC, in this order** (each step is a `.bat` or one command for the user):
1. `Test Setup.bat` → READY.
2. `python check_suppliers.py <file> --offline` (2 minutes, no lookups): counts of INVALID / NO NUMBER /
   NOT APPLICABLE; anything surprising goes back to step 2.
3. `Run Trial (25 suppliers).bat`: the first rows of the file up to 25 that need an online lookup; ask for the
   log and the Tax Check Summary sheet. **Pass:** no ERROR rows (or only ones the log explains, e.g. a site in
   maintenance); no MANUAL CHECK on Manitoba/BC; every Canadian row has a GST/HST status other than NOT
   APPLICABLE; the counts look plausible to the user (NO NUMBER / NOT REGISTERED rows they can recognise).
4. Full run (`Run Tax Check.bat`), overnight; `--resume <progress csv>` after any stop.

**Step 5. Read results like this.** Start with the summary sheet's notes (sources that failed, cut-offs) and
counts per issue; then the Action List. Red flags: a block of ERROR on one tax (site down or changed: HANDOFF
§10), MANUAL CHECK on Manitoba/BC (browser didn't start), NOT APPLICABLE on vendors you know are Canadian
(country/province mapping), many NAME NOT MATCHED with the same pattern (a vendor-master shorthand to add).

## 6. Accuracy rules the code follows (keep them)

- REGISTERED for GST/HST means CRA confirmed the number **and** a name we can justify (sheet name or a form of
  it, or an official registry name CRA accepted). Number-only → REGISTERED - NAME NOT MATCHED (REVIEW by
  default; `--accept-number-only` makes it OK, flagged on the summary).
- A registry name is a candidate until CRA accepts it for **that** business number. Name searches (CBR by name)
  only produce leads, and only on exact name + same entity type + exactly one BN + CRA confirmation.
- Probable digital-economy matches are flagged for a person and limited to foreign vendors.
- Partnerships (LP, LLP, S.E.N.C.) are never matched to corporations of the same name.
- Every site's answer is read conservatively: an unexpected page is NOT CONFIRMED or ERROR, never REGISTERED.
- Saskatchewan: REGISTERED only when a result row's name equals the vendor's (apart from punctuation/suffix)
  and the type is a vendor licence (`PSTVEND`) or a registered consumer.

## 7. Speed

Time goes to CRA lookups (3 requests each, throttled ~1.5 s per worker; 3 workers by default) and browser
lookups (Manitoba ~4 s, BC ~5 s, Saskatchewan ~3 s, one at a time). Foreign and invalid rows cost nothing.
Duplicate numbers/names are looked up once (shared cache). Knobs: `--workers` (default 3, max 6; more risks CRA
throttling), `--gst-dates none|end|full` (none is fastest; end = "Quick", default), `--taxes gst,mb` (only some
taxes), `--paid-since` (skip vendors not paid since a date, if the user wants), `--limit N` (trial).
Don't remove throttles or raise workers to "speed up": CRA and Manitoba slow down or block.

## 8. Where to change what

| To change | File | Notes |
|---|---|---|
| Column spellings, provinces, countries, dates, output columns | `taxcheck/workbook.py` | safe |
| Name markers, candidates, routing, digital-economy matching | `taxcheck/checks.py` | safe with tests; keep "own name only" |
| Number formats | `taxcheck/numbers.py` | safe with tests; keep the CRA check digit |
| Issue / action wording, severity | `taxcheck/report.py` | safe |
| Menus, options, run flow | `check_suppliers.py` | keep the `.bat` flow |
| Government sites | `gst.py`, `qst.py`, `bc_pst.py`, `mb_rst.py`, `sk_pst.py`, `cbr.py`, `orgbook.py`, `fedcorp.py` | **only** when a live test or a run shows the site changed; read HANDOFF §7 first |

## 9. Glossary

BN: 9-digit business number. RT/RC/RP/RR: CRA program accounts (GST/HST, income tax, payroll, charity).
ITC: input tax credit. Simplified registrant: non-resident digital seller under the simplified GST/HST regime
(RT9999). NR: Québec non-resident QST number. DNU: "do not use" vendor marker. O/A, DBA: "operating as".
Vendor master: the ERP's supplier table the export comes from. CBR: Canada's Business Registries (ISED).
