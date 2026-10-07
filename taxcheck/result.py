from dataclasses import dataclass

REGISTERED = "REGISTERED"
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
