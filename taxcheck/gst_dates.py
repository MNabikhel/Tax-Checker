"""GST/HST registration start and end dates from the CRA registry, which only answers "registered on this date?".

CRA checks the date before the name (see gst.GstChecker._number_only): for a business number that has a GST/HST
account, asking with any name about a date answers "was not registered on this transaction date" when it wasn't,
and "Insufficient information" (the name didn't match) when it was. So whether the number was registered on a
given day can be asked with a placeholder name, and the day the answer changes can be found by halving the gap
between a day it was and a day it wasn't (about 9 lookups for a year, 14 for 35 years).

All lookups go through GstChecker.registry_lookup, so CRA's pacing and site-down detection still apply.
Registrations don't expire; "end" means the registration was cancelled.
"""

import datetime as dt
import logging

from .gst import PLACEHOLDER_NAME

log = logging.getLogger(__name__)

GST_START = dt.date(1990, 1, 1)  # GST took effect on 1991-01-01; businesses could register from late 1990


def registered_on(gst, bn9, date):
    """True / False, or None when CRA's answer doesn't settle it (number not valid)."""
    outcome = gst.registry_lookup(bn9, PLACEHOLDER_NAME, date)
    if outcome == "not_registered":
        return False
    if outcome in ("no_match", "registered"):
        return True
    return None


def first_change(gst, bn9, lo, hi, lo_state):
    """The first day after `lo` whose answer differs from `lo_state`, given `hi` already differs.

    lo < hi. Returns hi when they're adjacent. Day precision.
    """
    while (hi - lo).days > 1:
        mid = lo + (hi - lo) // 2
        state = registered_on(gst, bn9, mid)
        if state is None:
            raise RuntimeError(f"CRA gave no clear answer for {bn9} on {mid}")
        if state == lo_state:
            lo = mid
        else:
            hi = mid
    return hi


def registration_start(gst, bn9, registered_day):
    """First day registered, searching back from a day it's known to be registered. None if registered since
    before GST began (the earliest answer CRA can give)."""
    if registered_on(gst, bn9, GST_START):
        return None
    return first_change(gst, bn9, GST_START, registered_day, False)


def cancellation_date(gst, bn9, registered_day, not_registered_day):
    """Last day registered, between a day it was registered and a later day it wasn't.

    CRA's simplified-registrant list gives the last day too (its "deregistered" date): checked live on
    2026-10-16, a registrant the list shows deregistered 2025-06-30 answers "registered" for 2025-06-30
    and "not registered" from 2025-07-01. Start dates matched the list exactly for 5 of 5 registrants.
    """
    return first_change(gst, bn9, registered_day, not_registered_day, True) - dt.timedelta(days=1)
