"""Trust the same certificates Windows does, so the tool works on corporate networks.

Corporate networks often inspect web traffic and re-sign it with the company's own certificate. Windows and
browsers trust that certificate; Python, by default, only trusts its own bundled list, so every lookup
fails with "CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate". The `truststore` package
makes Python use the operating system's certificate store instead. Verification stays fully on: this
only changes *which* trusted certificates are used, never whether they're checked.
"""

import logging

log = logging.getLogger(__name__)
_done = None


def use_system_certificates():
    """Switch Python's HTTPS to the system certificate store. Returns True if it's active."""
    global _done
    if _done is not None:
        return _done
    try:
        import truststore

        truststore.inject_into_ssl()
        _done = True
        log.debug("Using the system certificate store (truststore %s)", getattr(truststore, "__version__", "?"))
    except Exception as e:  # not installed (old setup) or an unsupported Python: fall back to Python's own list
        _done = False
        log.debug("System certificate store not in use (%s: %s); using Python's certificate list", type(e).__name__, e)
    return _done
