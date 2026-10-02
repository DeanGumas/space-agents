"""Keep the CSP in ui/index.html in sync with its inline import map.

The CSP forbids inline scripts except the import map, which is allowed by its SHA-256 hash. Any edit to the import
map (even whitespace) changes the hash, so run this after editing it:

    python scripts/update_csp.py          # rewrite the hash in index.html
    python scripts/update_csp.py --check  # exit 1 if out of date (used by tests)
"""

from __future__ import annotations

import base64
import hashlib
import re
import sys
from pathlib import Path

INDEX = Path(__file__).resolve().parent.parent / "ui" / "index.html"
IMPORTMAP_RE = re.compile(r'<script type="importmap">(.*?)</script>', re.S)
HASH_RE = re.compile(r"'sha256-[^']*'")


def importmap_hash(html: str) -> str:
    m = IMPORTMAP_RE.search(html)
    if not m:
        raise SystemExit("No inline import map found in index.html")
    digest = hashlib.sha256(m.group(1).encode("utf-8")).digest()
    return f"'sha256-{base64.b64encode(digest).decode()}'"


def main() -> int:
    html = INDEX.read_text(encoding="utf-8")
    expected = importmap_hash(html)
    current = HASH_RE.findall(html)
    if "--check" in sys.argv:
        ok = current == [expected]
        print("CSP hash OK" if ok else f"CSP hash out of date: have {current}, expected {expected}")
        return 0 if ok else 1
    INDEX.write_text(HASH_RE.sub(expected, html, count=1), encoding="utf-8")
    print(f"Updated CSP import map hash: {expected}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
