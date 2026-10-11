from dataclasses import dataclass

REGISTERED = "REGISTERED"
# The number is registered for GST/HST, but CRA didn't accept any name we tried, so it isn't confirmed
# that the number belongs to this supplier (see gst.GstChecker._number_only).
NAME_NOT_MATCHED = "REGISTERED - NAME NOT MATCHED"
NOT_REGISTERED = "NOT REGISTERED"
NOT_CONFIRMED = "NOT CONFIRMED"
INVALID = "INVALID NUMBER"
MANUAL = "MANUAL CHECK"
MISSING = "NO NUMBER"
ERROR = "ERROR"


@dataclass
class Result:
    status: str
    detail: str = ""
    registered_name: str = ""
