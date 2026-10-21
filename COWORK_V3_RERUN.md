# Handoff to Copilot Cowork: the v3 overnight re-run (2026-10-21)

Read this first, then `COWORK.md` as before. It explains what happened since your v1/v2 comparison, what changed
in the tool, what the user is about to run, and what to do with the results.

Terms used here: **v1** = the first full run (the one sent to Craig). **v2** = the run you just compared with v1.
**v3** = tonight's re-run with this version of the tool.

## 1. What your v1/v2 comparison raised, and what it turned out to be

Every row below was checked live against CRA's GST/HST Registry on 2026-10-21 by the tool's maintainer.

**a) Four rows changed from REGISTERED / name matched (v1) to something worse (v2), with "CRA answer changed".
These are real changes on CRA's side, not tool errors; v2 is right.**
- Three suppliers are no longer registered on their payment dates. CRA now says each was registered until a
  date shortly before the payment (2026-09-30 for two, 2026-09-01 for one) and cancelled since. CRA recorded
  these cancellations weeks late, back-dated, after v1 had run, so v1 was right at the time and v2 reflects
  CRA's corrected records.
- One supplier's number is still active, but CRA no longer accepts the vendor-master name: the business changed
  its legal name (v2's "Suggested Vendor Name" shows the new one).

What to tell the user/Craig: these are findings, not noise. For a cancelled supplier, GST/HST charged on invoices
after the cancellation date can't be claimed as input tax credits; confirm with the supplier (they may have
re-registered under a new number). For the name change, update the vendor name if it's the same business.

Expect more of this in any later run: CRA keeps correcting its records, so a few statuses can change between
runs with no change to the tool. When a row changes, the tool can't tell which run was "wrong"; a live CRA answer
is the current truth.

**b) One online-seller (digital economy) match was wrong.** A US vendor named like "<dealer company> LLC DBA
<one word>" was reported as a simplified GST/HST registrant (REGISTERED, "First check that this vendor is ...").
The match came from the operating name after "DBA": the tool's looser "probable" matching dropped a generic word
("Digital") from a listed registrant's name and the remaining word equalled the DBA word. The two are unrelated
businesses. Fixed in this version (section 2).

## 2. What changed in the tool (this version, `main` f0875f0)

1. **Probable online-seller matches only from the vendor's own legal name.** Names after "O/A" / "DBA", in
   brackets, or in a trade-name column now count only when they match a name on CRA's list *exactly*. Checked
   live: the wrong match above is now NOT APPLICABLE; real sellers (Slack, Canva, Google, Airalo) are still found.
2. **Cancellation dates before the payment date.** When a number isn't registered on the payment date or today,
   the tool now also asks CRA about two years earlier. If it was registered then, it finds the exact cancellation
   date. The row's issue becomes **"Registration cancelled before payment date"**, with the date in
   "GST/HST Registration End" and in Action Needed. Before, such rows only said "Not registered" with no date.
   Cost: one extra lookup per not-registered supplier (about 10 more when it was cancelled): minutes, not hours.

Nothing else changed in how rows are routed or judged, and no settings file is needed (your earlier check of the
vendor list still stands). Don't edit the `.py` files; if something needs code, write a change request (COWORK.md).

## 3. Tonight's run: what the user does

1. Unzip the new `Tax-Checker.zip` over the **local** Tax-Checker folder (e.g. Downloads) and choose
   **Replace the files**. The vendor workbook stays where it is in OneDrive.
2. Double-click **`Run Tax Check.bat`**, choose the same workbook, and answer:
   - Which taxes: **Enter** (everything)
   - Check Saskatchewan now: **N** (same as v2, so the runs stay comparable)
   - Cut-off date: **Enter** (check everyone)
   - Registration dates: **Enter** (Quick)
3. Leave it overnight (about the same time as v2, plus a few minutes). Keep the PC awake and on the network/VPN.
4. If it stops for any reason: double-click **`Resume Last Run.bat`** (same workbook and answers, no typing).
5. Results: `<workbook>_tax_check.xlsx` next to the workbook (the v2 file is replaced, so **copy the v2 results
   workbook to another name first** if you still need it for the comparison), log and progress file in the
   `logs` folder next to it.

Saskatchewan: starting v3 replaces v2 as the "last run", so Saskatchewan batches go on v3 from now on. After v3
finishes: `Resume Last Run.bat` once a day, answer **Y**, tick the CAPTCHA; each day searches up to 100
Saskatchewan suppliers (about 20 minutes) and keeps everything else. Don't start another full run until those
are done.

## 4. After the run: what Cowork should check

Compare **v3 with v2** (not only with v1). Expected differences, each with its reason:

| Change v2 → v3 | Why | Expected count |
|---|---|---|
| Online seller (REGISTERED, probable) → NOT APPLICABLE | the match came from an operating name (DBA / O/A / brackets / trade-name column) | the wrong match from 1b, and any other of the ~22 online sellers found through an operating name |
| "Not registered" → "Registration cancelled before payment date" (same status NOT REGISTERED, now with a Registration End date) | new cancellation-date search | some of the NOT REGISTERED rows |
| Any other status change | CRA corrected its records between the runs (as in 1a) | a handful at most |

Check that:
- the log has no block of ERROR rows (a site down for a while; `Resume Last Run.bat` retries them);
- the "Tax Check Summary" notes list no source that failed to load (CRA simplified list, Revenu Québec NR list);
- every "probable" online seller left in v3 matched on the vendor's own name (not a DBA/trade name);
- the rows from 1a now show their cancellation date (three) and the new legal name (one).

If a change fits none of the reasons above, look at that row's "GST/HST Details" first (it says what CRA
answered and on which date). If it still looks like the tool's mistake, write a change request with the row's
shape (not the supplier's data if it leaves the company), what v2 and v3 said, and what you expected.

## 5. For Craig's update

- v2/v3 cover the same vendors as v1 with the same routing, so they're comparable.
- New in v2/v3: online sellers (simplified GST/HST registrants) found by name and confirmed by CRA, and
  Québec non-resident (NR) QST numbers checked.
- New in v3: the cancellation date for suppliers whose registration was cancelled shortly before a payment.
- Status changes since v1 are mostly CRA's own late corrections (back-dated cancellations, a legal name change):
  worth acting on, not errors.
- Saskatchewan (946 vendors) is checked separately in daily batches of 100 (the registry's limit).
- Five vendors have Country = US with a Canadian province/postal code: a vendor-master error to correct; until
  then they're treated as foreign.
