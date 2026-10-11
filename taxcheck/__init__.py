"""Checks Canadian sales-tax registrations (GST/HST, QST, PST) for a list of suppliers."""

import logging

# Library convention: stay quiet unless the application (check_suppliers.py) configures logging.
logging.getLogger(__name__).addHandler(logging.NullHandler())
