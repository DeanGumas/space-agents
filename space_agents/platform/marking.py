"""Mark files agents write as downloaded from the internet ("Mark of the Web").

Agent output can be shaped by untrusted content (prompt injection), so it's treated like a download:
- Windows: a `Zone.Identifier` stream with ZoneId=3 (Internet). Office opens the file in Protected View and blocks
  its macros, and Windows warns before running it.
- macOS: the `com.apple.quarantine` attribute, so Gatekeeper checks anything launchable before it runs.

Best effort: some file systems (FAT/exFAT drives, some network shares) can't store the mark, and that's only logged.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import time
from pathlib import Path

log = logging.getLogger(__name__)

ZONE_IDENTIFIER = b"[ZoneTransfer]\r\nZoneId=3\r\n"
QUARANTINE_ATTR = b"com.apple.quarantine"
QUARANTINE_AGENT = "Space Agents"


def mark_as_downloaded(path: Path) -> bool:
    """Tag `path` as coming from the internet. Returns False where that isn't supported or possible."""
    try:
        if sys.platform == "win32":
            with open(f"{path}:Zone.Identifier", "wb") as f:
                f.write(ZONE_IDENTIFIER)
            return True
        if sys.platform == "darwin":
            _set_quarantine(path)
            return True
    except OSError as e:
        log.info("Couldn't mark %s as downloaded: %s", path, e)
    return False


def _set_quarantine(path: Path) -> None:
    # "flags;hex time;agent;". 0081 is what macOS writes for files received over AirDrop (Firefox writes 0281).
    value = f"0081;{int(time.time()):08x};{QUARANTINE_AGENT};".encode()
    libc = ctypes.CDLL(None, use_errno=True)
    libc.setxattr.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_uint32,
        ctypes.c_int,
    ]
    if libc.setxattr(os.fsencode(path), QUARANTINE_ATTR, value, len(value), 0, 0) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), str(path))
