"""Download pinned front-end libraries into ui/vendor/ (no npm required).

Usage:
    python scripts/vendor.py            # download missing files, verify against the lock
    python scripts/vendor.py --update   # re-download everything and rewrite the lock (after bumping versions)
    python scripts/vendor.py --check    # offline: verify files on disk match the lock

Files come from the jsDelivr CDN (raw npm package contents). SHA-256 hashes are recorded in
ui/vendor/vendor.lock.json so tampered or corrupted files are detected.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import tarfile
import urllib.request
from pathlib import Path

try:  # use the OS certificate store (corporate CAs) when available
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "ui" / "vendor"
LOCK = VENDOR / "vendor.lock.json"
CDN = "https://cdn.jsdelivr.net/npm"

# Bump versions here, then run with --update.
PACKAGES = {
    "preact": "10.29.8",
    "htm": "3.1.1",
    "@preact/signals-core": "1.14.4",
    "@preact/signals": "2.11.3",
    "marked": "18.0.14",
    "dompurify": "3.4.16",
    "lucide-static": "1.49.0",
}

# local file name -> (package, path inside package)
FILES = {
    "preact.module.js": ("preact", "dist/preact.module.js"),
    "preact-hooks.module.js": ("preact", "hooks/dist/hooks.module.js"),
    "htm.module.js": ("htm", "dist/htm.module.js"),
    "htm-preact.module.js": ("htm", "preact/index.module.js"),
    "signals-core.module.js": ("@preact/signals-core", "dist/signals-core.module.js"),
    "signals.module.js": ("@preact/signals", "dist/signals.module.js"),
    "marked.esm.js": ("marked", "lib/marked.esm.js"),
    "purify.es.mjs": ("dompurify", "dist/purify.es.mjs"),
    # Licenses, for redistribution compliance.
    "licenses/preact.LICENSE": ("preact", "LICENSE"),
    "licenses/htm.LICENSE": ("htm", "LICENSE"),
    "licenses/signals-core.LICENSE": ("@preact/signals-core", "LICENSE"),
    "licenses/signals.LICENSE": ("@preact/signals", "LICENSE"),
    "licenses/marked.LICENSE": ("marked", "LICENSE"),
    "licenses/dompurify.LICENSE": ("dompurify", "LICENSE"),
    "licenses/lucide.LICENSE": ("lucide-static", "LICENSE"),
}

# Icon SVGs are extracted from the package tarball (one request instead of ~1,600).
ICONS_PACKAGE = "lucide-static"
ICONS_DIR = "lucide"  # -> ui/vendor/lucide/<name>.svg


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "space-agents-vendor/1"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def download_icons() -> dict[str, str]:
    version = PACKAGES[ICONS_PACKAGE]
    tgz = fetch(f"https://registry.npmjs.org/{ICONS_PACKAGE}/-/{ICONS_PACKAGE}-{version}.tgz")
    out = VENDOR / ICONS_DIR
    out.mkdir(parents=True, exist_ok=True)
    hashes = {}
    with tarfile.open(fileobj=io.BytesIO(tgz), mode="r:gz") as tar:
        for member in tar.getmembers():
            # package/icons/<name>.svg
            parts = Path(member.name).parts
            if member.isfile() and len(parts) == 3 and parts[1] == "icons" and parts[2].endswith(".svg"):
                data = tar.extractfile(member).read()
                (out / parts[2]).write_bytes(data)
                hashes[f"{ICONS_DIR}/{parts[2]}"] = sha256(data)
    print(f"  {ICONS_DIR}/  ({len(hashes)} icons)")
    return hashes


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--update", action="store_true", help="re-download everything and rewrite the lock")
    ap.add_argument("--check", action="store_true", help="verify files against the lock without network access")
    args = ap.parse_args()

    lock = json.loads(LOCK.read_text()) if LOCK.exists() else {"packages": {}, "files": {}}

    if args.check:
        bad = [
            name
            for name, digest in lock["files"].items()
            if not (VENDOR / name).exists() or sha256((VENDOR / name).read_bytes()) != digest
        ]
        for name in bad:
            print(f"MISMATCH or missing: {name}")
        print("OK" if not bad else f"{len(bad)} problem(s)")
        return 1 if bad else 0

    if lock["packages"] and lock["packages"] != PACKAGES and not args.update:
        print("Package versions changed since the lock was written; run with --update.")
        return 1

    VENDOR.mkdir(parents=True, exist_ok=True)
    new_files: dict[str, str] = {}
    for name, (pkg, path) in FILES.items():
        dest = VENDOR / name
        if dest.exists() and not args.update and name in lock["files"]:
            new_files[name] = lock["files"][name]
            continue
        data = fetch(f"{CDN}/{pkg}@{PACKAGES[pkg]}/{path}")
        digest = sha256(data)
        if not args.update and name in lock["files"] and lock["files"][name] != digest:
            print(f"HASH MISMATCH for {name}: expected {lock['files'][name]}, got {digest}")
            return 1
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        new_files[name] = digest
        print(f"  {name}  ({len(data):,} bytes)")

    have_icons = any(k.startswith(f"{ICONS_DIR}/") for k in lock["files"])
    if args.update or not have_icons or not (VENDOR / ICONS_DIR).exists():
        new_files.update(download_icons())
    else:
        new_files.update({k: v for k, v in lock["files"].items() if k.startswith(f"{ICONS_DIR}/")})

    LOCK.write_text(json.dumps({"packages": PACKAGES, "files": dict(sorted(new_files.items()))}, indent=2) + "\n")
    print(f"Wrote {LOCK.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
