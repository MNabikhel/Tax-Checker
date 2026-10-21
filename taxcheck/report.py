"""Turns each supplier's results into plain, filterable report columns: an Issue and an Action per tax, and a
Main Issue / Action Needed per supplier.

The Status columns say *what* the government source answered; the Issue columns say *what that means for us*
in a short fixed phrase (so a filter shows every situation), and the Action columns say what to do next.
Severity orders issues so the worst one becomes the row's Main Issue.
"""

import re

from .result import (
    ERROR, INVALID, MANUAL, MISSING, NAME_NOT_MATCHED, NAME_UPDATE, NOT_APPLICABLE, NOT_CHECKED, NOT_CONFIRMED, NOT_LOOKED_UP,
    NOT_REGISTERED, REGISTERED,
)

TAX_LABELS = {"gst": "GST/HST", "qst": "QST", "bc_pst": "BC PST", "sk_pst": "SK PST", "mb_rst": "MB RST"}

# Severity: higher = more urgent. Used to pick the Main Issue and to colour the Issue cells.
SEV_NA, SEV_OK, SEV_NAME, SEV_MANUAL, SEV_NOT_FOUND, SEV_NAME_UNCONFIRMED, SEV_LATE, SEV_BAD_NUMBER, SEV_OTHER_NAME, \
    SEV_NOT_REGISTERED, SEV_ERROR = -1, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9

SEVERITY_FILL = {
    SEV_NA: "EDEDED", SEV_OK: "C6EFCE", SEV_NAME: "E2EFDA", SEV_MANUAL: "DDEBF7", SEV_NOT_FOUND: "FFEB9C",
    SEV_NAME_UNCONFIRMED: "FFEB9C", SEV_LATE: "F8CBAD", SEV_BAD_NUMBER: "F8CBAD", SEV_OTHER_NAME: "F8CBAD",
    SEV_NOT_REGISTERED: "FFC7CE", SEV_ERROR: "FF9999",
}

SOURCE_LABELS = {
    "the BC registry (OrgBook BC)": "BC registry (OrgBook BC)",
    "the federal corporations registry": "Federal corporations registry",
    "CRA's simplified registrant list": "CRA simplified registrant list",
    "Revenu Québec (QST)": "Revenu Québec (QST)",
}


def _key(name):
    return " ".join(re.sub(r"[^a-z0-9\s]", "", str(name or "").lower()).split())


def name_source(res):
    """Where the name CRA accepted came from, in plain words."""
    f = res.facts
    accepted, source = f.get("accepted_name"), f.get("name_source")
    if not accepted:
        return ""
    if source == "sheet":
        if _key(accepted) == _key(f.get("vendor_name")):
            return "Vendor name as entered"
        if _key(accepted) == _key(f.get("name_used")):
            # The name lost a do-not-use marker, or text the tailoring file removes ("- INACTIVE").
            return "Vendor name without do-not-use marker" if f.get("dnu") else "Vendor name without vendor-master marking"
        return "Vendor name, reworded (legal part / suffix)"
    return SOURCE_LABELS.get(source, source or "")


def gst_issue(res):
    """(issue, action, severity) for the GST/HST result."""
    f, st = res.facts, res.status
    date, today = f.get("date_checked", ""), f.get("today", "")
    problem = f.get("tax_id_problem", "")
    if st == NOT_APPLICABLE:
        return "Foreign supplier - not applicable", "", SEV_NA
    if st == NOT_CHECKED:
        return "Not checked - not paid since the cut-off date", "", SEV_NA
    if st == NOT_LOOKED_UP:
        return "Not looked up (offline run)", "Run the online check.", SEV_OK
    if st == ERROR:
        return "Lookup failed", "Double-click \"Resume Last Run.bat\" later (the CRA site may have been busy or down).", SEV_ERROR
    if st == REGISTERED and f.get("digital_economy_match"):
        check = ""
        if f.get("digital_economy_match") == "probable":
            check = (f"First check that this vendor is '{f.get('accepted_name', '')}' (the names differ only in legal "
                     "form, region or a word like 'Technologies'). ")
        return ("Simplified (digital economy) registrant: number not on file",
                f"{check}CRA lists this supplier as a simplified GST/HST registrant ({f.get('number_checked', '')}). "
                "GST/HST they charge isn't eligible for input tax credits: give them our GST/HST number so they stop "
                "charging it, and add their simplified number to the vendor record.", SEV_OTHER_NAME)
    if st in (REGISTERED, NAME_NOT_MATCHED) and f.get("simplified_registrant") and f.get("cra_answer") != "deregistered":
        return ("Simplified (digital economy) registrant",
                "CRA lists this supplier as a simplified GST/HST registrant (a non-resident digital seller). GST/HST "
                "they charge isn't eligible for input tax credits: give them our GST/HST number so they stop "
                "charging it.", SEV_LATE)
    if st in (REGISTERED, NAME_NOT_MATCHED) and f.get("cancelled_after_payment"):
        end = f.get("registration_end", "")
        return ("Registration cancelled since payment date",
                f"Registered on {date}, but the registration ended after {end} (its last registered day). Don't claim "
                f"input tax credits on GST/HST charged on invoices dated after {end}; confirm with the supplier.", SEV_LATE)
    if st == REGISTERED:
        fix_id = ""
        if problem and problem != "No Tax ID":
            bn9 = str(f.get("number_checked") or "")[:9]
            fix_id = (f"Correct the Tax ID to the GST/HST account ({problem}), e.g. {bn9}RT0001; confirm the RT number "
                      "with the supplier.")
        if res.name_check == NAME_UPDATE:
            accepted = f.get("accepted_name") or res.registered_name
            action = f"Update the vendor name to '{accepted}' (the name CRA accepted)."
            return ("Registered - vendor name differs from CRA", f"{action} {fix_id}".strip(), SEV_NAME)
        if fix_id:
            return "Registered - Tax ID needs correcting", fix_id, SEV_NAME
        return "Registered - name matches", "", SEV_OK
    if st == NAME_NOT_MATCHED:
        accepted, official = f.get("accepted_name"), f.get("official_name")
        if accepted:
            return ("Registered under another name",
                    f"CRA confirms this number belongs to '{accepted}'. If that is this supplier, update the vendor "
                    "name; if not, get the correct GST/HST number from the supplier.", SEV_OTHER_NAME)
        hint = f" The registry name for this number is '{official}', but CRA didn't accept it." if official else ""
        return ("Registered - legal name not confirmed",
                "The number is registered, but not under the vendor name." + hint + " Get the legal name from the "
                "supplier (e.g. their invoice), update the vendor name and re-check.", SEV_NAME_UNCONFIRMED)
    if st == NOT_REGISTERED:
        answer = f.get("cra_answer")
        if answer == "unknown_number":
            return ("Number not known to CRA",
                    "Likely a typo or not a GST/HST number. Ask the supplier for their GST/HST number and correct "
                    "the Tax ID.", SEV_NOT_REGISTERED)
        if answer == "deregistered":
            return ("Registration cancelled", "Don't claim input tax credits on GST/HST from this supplier after the "
                    "cancellation; confirm with the supplier.", SEV_NOT_REGISTERED)
        now = f.get("status_today")
        if now in (REGISTERED, NAME_NOT_MATCHED):
            since = f.get("registered_since")
            when = f"registered since {since}" if since else f"registered today ({today})"
            return ("Registered after payment date",
                    f"Not registered on {date}, {when}. Check any GST/HST charged on invoices from before the "
                    "registration date.", SEV_LATE)
        if f.get("cancelled_before_payment"):
            return ("Registration cancelled before payment date",
                    f"Registered until {f.get('registration_end')}, not on {date} or today. Don't claim input tax credits "
                    "on GST/HST charged after the cancellation; confirm with the supplier (re-registered under another "
                    "number?).", SEV_NOT_REGISTERED)
        when = f"on {date} or today" if now == NOT_REGISTERED else f"on {date}"
        return ("Not registered",
                f"Not registered for GST/HST {when}. Don't claim input tax credits on GST/HST from this supplier; "
                "confirm with the supplier (small supplier, or registration cancelled).", SEV_NOT_REGISTERED)
    if st == INVALID:
        issue = "Tax ID typo (fails check digit)" if "check digit" in (problem + res.detail) else "Tax ID not a GST/HST number"
        return issue, f"Correct the Tax ID in the vendor record ({problem or res.detail}).", SEV_BAD_NUMBER
    if st == MISSING:
        if "placeholder" in res.detail.lower():
            return ("No GST/HST number (placeholder Tax ID)", "Replace the placeholder with the supplier's GST/HST "
                    "number (or record that they're a small supplier).", SEV_BAD_NUMBER)
        if f.get("possible_number"):
            bn = f["possible_number"]
            return ("No GST/HST number - possible number found", f"{f.get('possible_number_source', 'A registry')} "
                    f"lists '{f.get('possible_number_name', '')}' with business number {bn}, and CRA confirms it's "
                    "registered for GST/HST under that name. Confirm with the supplier that it's theirs, then add it to "
                    f"the vendor record (the GST/HST account is usually {bn}RT0001).", SEV_BAD_NUMBER)
        return ("No GST/HST number", "Ask the supplier for their GST/HST number (or confirm they're a small "
                "supplier) and add it to the vendor record.", SEV_BAD_NUMBER)
    if st == NOT_CONFIRMED:
        return "Not confirmed", "See the details; check by hand on the CRA GST/HST Registry.", SEV_NAME_UNCONFIRMED
    if st == MANUAL:
        return "Manual check needed", "See the details.", SEV_MANUAL
    return st.title(), "", SEV_OK


def provincial_issue(key, res):
    """(issue, action, severity) for QST, BC PST, MB RST and SK PST."""
    st, detail = res.status, res.detail
    low = detail.lower()
    label = TAX_LABELS[key]
    if st == REGISTERED and res.facts.get("cancelled_after_payment"):
        end = res.facts.get("registration_end", "")
        return ("Registration cancelled since payment date", f"Registered on the payment date, but the {label} "
                f"registration was cancelled effective {end}. Confirm with the supplier before relying on {label} "
                f"charged on invoices dated {end} or later.", SEV_LATE)
    if st == NOT_REGISTERED and res.facts.get("registered_after_payment"):
        return ("Registered after payment date", f"Not registered for {label} on the payment date; registered since "
                f"{res.facts.get('registered_since', '')}. Check any {label} charged before then.", SEV_LATE)
    if st == REGISTERED:
        if key == "qst" and "no tq suffix" in low:
            checked = res.facts.get("number_checked", "")
            if checked and not checked.endswith("TQ0001"):
                return ("Registered under another QST account", f"Add the active QST number {checked} to the vendor "
                        "record (the number on file has no TQ suffix, and TQ0001 isn't active).", SEV_NAME)
            return ("Registered (TQ0001 assumed)", "Get the full QST number (with its TQ suffix) from the supplier.",
                    SEV_NAME)
        return "Registered", "", SEV_OK
    if st == ERROR:
        return "Lookup failed", "Double-click \"Resume Last Run.bat\" later (the site may have been busy or in maintenance).", SEV_ERROR
    if st == MANUAL:
        if key == "sk_pst":
            if "at least 4 letters" in low:
                return ("Manual check needed (name too short to search)", "Search the full name by hand on "
                        "Saskatchewan's PST registry.", SEV_MANUAL)
            if "searches per 24 hours" in low:
                return ("Manual check needed (daily search limit reached)", "Double-click \"Resume Last Run.bat\" after the "
                        "time in the details (the registry allows 100 searches a day).", SEV_MANUAL)
            if "window was closed" in low or "wasn't completed in time" in low or "asked for the captcha again" in low:
                return ("Manual check needed (Saskatchewan searches stopped)", "Double-click \"Resume Last Run.bat\" and tick "
                        "the CAPTCHA, or search the name on Saskatchewan's PST registry.", SEV_MANUAL)
            if "couldn't open a browser" in low:
                return ("Manual check needed (browser didn't open)", "Run \"Test Setup.bat\", then double-click "
                        "\"Resume Last Run.bat\" and answer Y to the Saskatchewan question, or search the name on "
                        "Saskatchewan's PST registry.", SEV_MANUAL)
            return ("Manual check needed (CAPTCHA)", "Double-click \"Resume Last Run.bat\" and answer Y to the "
                    "Saskatchewan question (tick the CAPTCHA once), or search the name on Saskatchewan's PST registry.",
                    SEV_MANUAL)
        return "Manual check needed", "See the details.", SEV_MANUAL
    if st == MISSING:
        if key == "mb_rst":
            return ("No RST or GST/HST number to search", "Add the supplier's GST/HST number (or RST number) to the "
                    "vendor record.", SEV_BAD_NUMBER)
        if "business number" in low:
            return ("No business number for BC lookup", "Add the supplier's GST/HST number to the vendor record.",
                    SEV_BAD_NUMBER)
        return f"No {label} number", f"Ask the supplier for their {label} number.", SEV_BAD_NUMBER
    if st == INVALID:
        return f"Invalid {label} number", f"Correct the {label} number ({detail}).", SEV_BAD_NUMBER
    if st == NOT_REGISTERED:
        if "cancelled" in low:
            return "Registration cancelled", f"Confirm with the supplier; don't rely on the {label} registration.", SEV_NOT_REGISTERED
        m = re.search(r"status '([^']+)'", detail)
        if m:
            return (f"Registration not active ({m.group(1)})", "Confirm with the supplier whether they should be "
                    f"charging {label}.", SEV_NOT_REGISTERED)
        if "nr" in low and "list" in low:
            return "Not on Québec NR list", "Confirm the QST number with the supplier.", SEV_NOT_REGISTERED
        return "Not registered", f"Confirm with the supplier whether they should be charging {label}.", SEV_NOT_REGISTERED
    if st == NOT_CONFIRMED:
        if key == "mb_rst":
            return ("Not found in Manitoba registry", "Not registered under this name and number. If the supplier "
                    "charges Manitoba RST, ask for their RST number; otherwise no action.", SEV_NOT_FOUND)
        if key == "bc_pst":
            return ("PST number doesn't match business number", "Confirm the BC PST number and GST/HST number with "
                    "the supplier.", SEV_NOT_FOUND)
        if key == "sk_pst":
            if "similar names" in low:
                return ("Only similar names in Saskatchewan registry", "Check whether one of the similar names in the "
                        "details is this supplier (if so, update the vendor name); if the supplier charges SK PST, ask "
                        "for their vendor licence.", SEV_NOT_FOUND)
            return ("Not found in Saskatchewan registry", "If the supplier charges SK PST, ask for their vendor "
                    "licence; check the saved screenshot.", SEV_NOT_FOUND)
        return "Not confirmed", "See the details.", SEV_NOT_FOUND
    if st == NOT_LOOKED_UP:
        return "Not looked up (offline run)", "Run the online check.", SEV_OK
    return st.title(), "", SEV_OK


# The same advice without supplier-specific names, for the summary sheet.
GENERIC_ACTIONS = {
    "Registered - vendor name differs from CRA": "Update the vendor name to the name in 'Suggested Vendor Name' (the name CRA accepted).",
    "Registered - Tax ID needs correcting": "Correct the Tax ID to the supplier's GST/HST (RT) account; see 'GST/HST Tax ID Problem'.",
    "Registered under another name": "CRA confirms the number under the name in 'Suggested Vendor Name'. If that is this supplier, "
                                     "update the vendor name; if not, get the correct GST/HST number from the supplier.",
    "Not registered": "Don't claim input tax credits on GST/HST from these suppliers; confirm with each supplier.",
    "No GST/HST number - possible number found": "A business registered under exactly the vendor's name has a GST/HST "
        "number CRA confirms ('GST/HST Details'). Confirm with the supplier that it's theirs, then add it.",
    "Registered after payment date": "Check GST/HST charged on invoices from before the supplier's registration date "
                                     "('GST/HST Registered Since').",
    "Registration cancelled since payment date": "Don't claim input tax credits on GST/HST charged on invoices dated "
                                                 "after 'GST/HST Registration End' (the last registered day); confirm "
                                                 "with the supplier.",
    "Tax ID not a GST/HST number": "Correct the Tax ID in the vendor record; see 'GST/HST Tax ID Problem'.",
    "Tax ID typo (fails check digit)": "Correct the Tax ID in the vendor record (a digit is wrong).",
    "Simplified (digital economy) registrant": "GST/HST these suppliers charge isn't eligible for input tax credits: "
        "give them our GST/HST number so they stop charging it.",
    "Simplified (digital economy) registrant: number not on file": "GST/HST these suppliers charge isn't eligible for "
        "input tax credits: give them our GST/HST number so they stop charging it, and add their simplified number "
        "('GST/HST Number Checked') to the vendor record.",
}


def generic_action(issue, action):
    if issue in GENERIC_ACTIONS:
        return GENERIC_ACTIONS[issue]
    if issue.startswith("Invalid ") or issue.startswith("Registration not active"):
        return re.sub(r"\s*\(.*\)\.?$", ".", action)
    return action


def number_registered(res):
    """The "GST/HST Number Registered" column: is the number registered, separately from whose name it's under."""
    f, st = res.facts, res.status
    if st == REGISTERED:
        return "Yes"
    if st == NAME_NOT_MATCHED:
        return "Yes - under another name" if f.get("accepted_name") else "Yes - name not confirmed"
    if st == NOT_REGISTERED:
        return "No - number not known to CRA" if f.get("cra_answer") == "unknown_number" else "No"
    return {INVALID: "No - invalid number", MISSING: "No number", ERROR: "Lookup failed"}.get(st, "")


def issue_for(key, res):
    return gst_issue(res) if key == "gst" else provincial_issue(key, res)


def row_summary(by_tax, taxes):
    """Main Issue, Action Needed, Other Issues and the worst severity for one supplier."""
    found = []
    for key, label in taxes:
        res = by_tax.get(key)
        if res is None:
            continue
        issue, action, sev = issue_for(key, res)
        found.append((sev, label, issue, action))
    if not found:
        return {"main": "", "action": "", "other": "", "severity": SEV_OK}
    found.sort(key=lambda x: -x[0])
    sev, label, issue, action = found[0]
    if sev == SEV_NA:
        main = issue  # foreign supplier, or not paid since the cut-off date
    elif sev == SEV_OK:
        main = "None"
    else:
        main = f"{label}: {issue}"
    others = [f"{lab}: {iss}" for s, lab, iss, _ in found[1:] if s > SEV_OK]
    return {"main": main, "action": action, "other": "; ".join(others), "severity": sev}
