from dataclasses import dataclass, field

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
# Foreign supplier with no Canadian tax number: no Canadian sales-tax check applies.
NOT_APPLICABLE = "NOT APPLICABLE"
# --offline: the number passed every offline check but wasn't looked up (no internet / dry run).
NOT_LOOKED_UP = "NOT LOOKED UP"


@dataclass
class Result:
    status: str
    detail: str = ""
    registered_name: str = ""
    # GST/HST only: is the vendor name in the sheet the name CRA has? NAME_MATCHES / NAME_UPDATE / NAME_UNCONFIRMED.
    name_check: str = ""
    # Structured facts for the report columns, e.g. {"accepted_name": ..., "name_source": ..., "official_name": ...,
    # "cra_answer": ...}. Set by the checkers and RowChecker; never needed for the status itself.
    facts: dict = field(default_factory=dict)

    def copy(self):
        return Result(self.status, self.detail, self.registered_name, self.name_check, dict(self.facts))


NAME_MATCHES = "MATCHES"            # CRA accepted the vendor name exactly as entered
NAME_UPDATE = "UPDATE NAME"         # registration confirmed, but under a different form of the name: fix the vendor record
NAME_UNCONFIRMED = "NOT CONFIRMED"  # CRA accepted no name we have; the legal name must come from the supplier
