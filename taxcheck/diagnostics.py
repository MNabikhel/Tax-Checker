"""Copies run reports, logs and failure screenshots into a "diagnostics" folder, typically a cloud-synced folder,
so someone helping remotely can read them without the user pasting console output.

Off unless configured. The folder is taken from, in order:
  1. the TAXCHECK_DIAGNOSTICS_DIR environment variable ("off" turns copying off);
  2. the first non-blank line of `diagnostics_dir.txt` in the tool's folder (git-ignored, per user).
Environment variables in the path are expanded, e.g. "%OneDriveCommercial%\\Projects\\Tax\\Diagnostics".
Nothing is copied if the folder's parent doesn't exist (e.g. the settings file was copied to another PC).
"""

import logging
import os
import re
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent.parent  # the tool's folder
SETTING_FILE = HERE / "diagnostics_dir.txt"


def _configured():
    setting = os.environ.get("TAXCHECK_DIAGNOSTICS_DIR", "").strip()
    if not setting and SETTING_FILE.is_file():
        try:
            lines = [l.strip() for l in SETTING_FILE.read_text(encoding="utf-8").splitlines()]
            setting = next((l for l in lines if l and not l.startswith("#")), "")
        except OSError:
            setting = ""
    return setting


def diagnostics_dir():
    """The diagnostics folder to copy into, or None when none is configured (or it can't exist on this PC)."""
    setting = _configured()
    if not setting or setting.lower() == "off":
        return None
    # %VAR% (Windows style) is expanded on every platform, then $VAR / ${VAR}.
    expanded = re.sub(r"%([^%]+)%", lambda m: os.environ.get(m.group(1), m.group(0)), setting)
    folder = Path(os.path.expandvars(expanded)).expanduser()
    if "%" in str(folder) or "$" in str(folder):  # an environment variable that isn't set on this PC
        return None
    return folder if folder.parent.is_dir() else None


def copy_to_diagnostics(*paths):
    """Copy files into the diagnostics folder. Never raises: this is a convenience, not part of the check."""
    folder = diagnostics_dir()
    if not folder:
        return None
    copied = None
    try:
        folder.mkdir(parents=True, exist_ok=True)
        for p in paths:
            if p and Path(p).is_file():
                shutil.copy2(p, folder / Path(p).name)
                copied = folder
    except OSError as e:
        log.debug("Couldn't copy to %s: %s", folder, e)
        return None
    return copied
