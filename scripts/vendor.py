"""Download pinned third-party front-end assets into ui/vendor/ (no npm required).

Usage:
    python scripts/vendor.py            # download what's missing, verify everything against the lock
    python scripts/vendor.py --update   # re-download everything and rewrite the lock (after bumping versions)
    python scripts/vendor.py --check    # offline: verify files on disk match the lock
    python scripts/vendor.py --check --strict   # ...and fail if the (git-ignored) Pyodide files are missing

What gets vendored:
  - JS libraries (Preact, htm, signals, marked, DOMPurify) and Lucide icons, from npm via jsDelivr. Small; committed.
  - The Pyodide sandbox runtime plus an allow-list of Python packages (PLAN.md §2.9). ~50 MB; git-ignored, fetched
    by this script. Wheels are verified against upstream hashes (Pyodide's lock file, or PyPI for pure-Python wheels
    that Pyodide doesn't ship). A trimmed pyodide-lock.json is written so the sandbox can only load allowed packages.

SHA-256 hashes of everything are recorded in ui/vendor/vendor.lock.json, so tampering or corruption is detected.
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
NPM_CDN = "https://cdn.jsdelivr.net/npm"

# ---------------------------------------------------------------------------------------------------------------
# Configuration: bump versions here, then run with --update.
# ---------------------------------------------------------------------------------------------------------------

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

# Icon SVGs are extracted from the package tarball (one request instead of ~2,000).
ICONS_PACKAGE = "lucide-static"
ICONS_DIR = "lucide"  # -> ui/vendor/lucide/<name>.svg

PYODIDE = {
    "version": "314.0.7",  # Pyodide now versions by CPython release: 314.x runs Python 3.14 in the sandbox
    # Packages the sandbox may load (dependencies are resolved from Pyodide's lock file automatically).
    "packages": ["numpy", "pandas", "lxml", "matplotlib", "typing-extensions"],
    # Pure-Python packages Pyodide doesn't ship: universal py3-none-any wheels from PyPI work unchanged.
    # name -> (PyPI project, version, import names, dependencies by lock name)
    "extra_wheels": {
        "et-xmlfile": ("et-xmlfile", "2.0.0", ["et_xmlfile"], []),
        "openpyxl": ("openpyxl", "3.1.5", ["openpyxl"], ["et-xmlfile"]),
        "python-docx": ("python-docx", "1.2.0", ["docx"], ["lxml", "typing-extensions"]),
        "pypdf": ("pypdf", "6.19.0", ["pypdf"], []),
        "xlsxwriter": ("xlsxwriter", "3.2.9", ["xlsxwriter"], []),
    },
}
PYODIDE_DIR = "pyodide"  # -> ui/vendor/pyodide/ (git-ignored)
PYODIDE_CORE = ["pyodide.mjs", "pyodide.asm.mjs", "pyodide.asm.wasm", "python_stdlib.zip"]
PYODIDE_CDN = "https://cdn.jsdelivr.net/pyodide/v{version}/full/{file}"
PYODIDE_LICENSE = "https://cdn.jsdelivr.net/gh/pyodide/pyodide@{version}/LICENSE"  # jsDelivr's GitHub mirror

# ---------------------------------------------------------------------------------------------------------------


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "space-agents-vendor/1"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        return resp.read()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Vendor:
    def __init__(self, lock: dict, update: bool):
        self.lock = lock
        self.update = update
        self.files: dict[str, str] = {}  # new lock entries: relative path -> sha256

    def keep_if_current(self, name: str, expected: str | None = None) -> bool:
        """Reuse a file already on disk if it matches the expected (or previously locked) hash."""
        expected = expected or self.lock["files"].get(name)
        if self.update or not expected or file_sha256(VENDOR / name) != expected:
            return False
        self.files[name] = expected
        return True

    def store(self, name: str, data: bytes, expected: str | None = None) -> None:
        digest = sha256(data)
        pinned = expected or (None if self.update else self.lock["files"].get(name))
        if pinned and digest != pinned:
            raise SystemExit(f"HASH MISMATCH for {name}: expected {pinned}, got {digest}")
        dest = VENDOR / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        self.files[name] = digest
        print(f"  {name}  ({len(data):,} bytes)")

    # --- npm libraries and icons --------------------------------------------------------------------------------

    def npm(self) -> None:
        for name, (pkg, path) in FILES.items():
            if not self.keep_if_current(name):
                self.store(name, fetch(f"{NPM_CDN}/{pkg}@{PACKAGES[pkg]}/{path}"))

    def icons(self) -> None:
        locked = {k: v for k, v in self.lock["files"].items() if k.startswith(f"{ICONS_DIR}/")}
        if locked and not self.update and (VENDOR / ICONS_DIR).is_dir():
            self.files.update(locked)
            return
        version = PACKAGES[ICONS_PACKAGE]
        tgz = fetch(f"https://registry.npmjs.org/{ICONS_PACKAGE}/-/{ICONS_PACKAGE}-{version}.tgz")
        out = VENDOR / ICONS_DIR
        out.mkdir(parents=True, exist_ok=True)
        count = 0
        with tarfile.open(fileobj=io.BytesIO(tgz), mode="r:gz") as tar:
            for member in tar.getmembers():
                parts = Path(member.name).parts  # package/icons/<name>.svg
                if member.isfile() and len(parts) == 3 and parts[1] == "icons" and parts[2].endswith(".svg"):
                    data = tar.extractfile(member).read()
                    (out / parts[2]).write_bytes(data)
                    self.files[f"{ICONS_DIR}/{parts[2]}"] = sha256(data)
                    count += 1
        print(f"  {ICONS_DIR}/  ({count} icons)")

    # --- Pyodide ------------------------------------------------------------------------------------------------

    def pyodide(self) -> None:
        version = PYODIDE["version"]
        locked = {k: v for k, v in self.lock["files"].items() if k.startswith(f"{PYODIDE_DIR}/")}
        if (
            f"{PYODIDE_DIR}/LICENSE" in locked
            and not self.update
            and self.lock.get("pyodide") == json.loads(json.dumps(PYODIDE))  # tuples -> lists, as stored
            and all(file_sha256(VENDOR / k) == v for k, v in locked.items())
        ):
            self.files.update(locked)
            print(f"  {PYODIDE_DIR}/  (up to date, {len(locked)} files)")
            return

        print(f"  Pyodide {version}:")
        upstream = json.loads(fetch(PYODIDE_CDN.format(version=version, file="pyodide-lock.json")))
        pkgs = upstream["packages"]

        for f in PYODIDE_CORE:
            name = f"{PYODIDE_DIR}/{f}"
            if not self.keep_if_current(name):
                self.store(name, fetch(PYODIDE_CDN.format(version=version, file=f)))

        name = f"{PYODIDE_DIR}/LICENSE"
        if not self.keep_if_current(name):
            try:
                self.store(name, fetch(PYODIDE_LICENSE.format(version=version)))
            except OSError as e:
                print(f"  (warning: could not fetch Pyodide LICENSE: {e})")

        # Allowed Pyodide packages + their dependency closure, verified against upstream hashes.
        wanted, stack = set(), list(PYODIDE["packages"])
        while stack:
            n = stack.pop()
            if n in wanted:
                continue
            if n not in pkgs:
                raise SystemExit(f"Pyodide {version} has no package {n!r}")
            wanted.add(n)
            stack.extend(pkgs[n].get("depends", []))
        trimmed = {}
        for n in sorted(wanted):
            spec = pkgs[n]
            fname = f"{PYODIDE_DIR}/{spec['file_name']}"
            if not self.keep_if_current(fname, spec["sha256"]):
                self.store(fname, fetch(PYODIDE_CDN.format(version=version, file=spec["file_name"])), spec["sha256"])
            trimmed[n] = spec

        # Pure-Python wheels from PyPI, verified against PyPI's published hashes.
        for n, (project, ver, imports, depends) in PYODIDE["extra_wheels"].items():
            missing = [d for d in depends if d not in trimmed and d not in PYODIDE["extra_wheels"]]
            if missing:
                raise SystemExit(f"{n} depends on {missing}, which are not vendored")
            meta = json.loads(fetch(f"https://pypi.org/pypi/{project}/{ver}/json"))
            wheel = next((u for u in meta["urls"] if u["filename"].endswith("-none-any.whl")), None)
            if not wheel or "py3" not in wheel["filename"]:
                raise SystemExit(f"No universal py3 wheel for {project} {ver}")
            fname = f"{PYODIDE_DIR}/{wheel['filename']}"
            if not self.keep_if_current(fname, wheel["digests"]["sha256"]):
                self.store(fname, fetch(wheel["url"]), wheel["digests"]["sha256"])
            trimmed[n] = {
                "name": n,
                "version": ver,
                "file_name": wheel["filename"],
                "install_dir": "site",
                "sha256": wheel["digests"]["sha256"],
                "package_type": "package",
                "imports": imports,
                "depends": depends,
                "unvendored_tests": False,
            }

        # Trimmed lock: the sandbox can load exactly these packages and nothing else.
        lock_bytes = (json.dumps({"info": upstream["info"], "packages": trimmed}, indent=1) + "\n").encode()
        dest = VENDOR / PYODIDE_DIR / "pyodide-lock.json"
        dest.write_bytes(lock_bytes)
        self.files[f"{PYODIDE_DIR}/pyodide-lock.json"] = sha256(lock_bytes)

        # Remove stale files from older versions.
        keep = {k for k in self.files if k.startswith(f"{PYODIDE_DIR}/")}
        for path in (VENDOR / PYODIDE_DIR).iterdir():
            if f"{PYODIDE_DIR}/{path.name}" not in keep:
                path.unlink()
        total = sum((VENDOR / k).stat().st_size for k in keep)
        print(f"  {PYODIDE_DIR}/  ({len(keep)} files, {total / 1e6:.1f} MB; packages: {', '.join(sorted(trimmed))})")


def check(lock: dict, strict: bool) -> int:
    pyodide_files = {k: v for k, v in lock["files"].items() if k.startswith(f"{PYODIDE_DIR}/")}
    skip_pyodide = not strict and not (VENDOR / PYODIDE_DIR).exists()
    bad = []
    for name, digest in lock["files"].items():
        if skip_pyodide and name in pyodide_files:
            continue
        if file_sha256(VENDOR / name) != digest:
            bad.append(name)
    for name in bad:
        print(f"MISMATCH or missing: {name}")
    if skip_pyodide and pyodide_files:
        print("Note: Pyodide sandbox files not downloaded (git-ignored); run `python scripts/vendor.py` to fetch them.")
    print("OK" if not bad else f"{len(bad)} problem(s)")
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--update", action="store_true", help="re-download everything and rewrite the lock")
    ap.add_argument("--check", action="store_true", help="verify files against the lock without network access")
    ap.add_argument("--strict", action="store_true", help="with --check: also require the Pyodide files")
    args = ap.parse_args()

    lock = json.loads(LOCK.read_text()) if LOCK.exists() else {"packages": {}, "files": {}}
    lock.setdefault("files", {})

    if args.check:
        return check(lock, args.strict)

    if lock.get("packages") and lock["packages"] != PACKAGES and not args.update:
        print("JS package versions changed since the lock was written; run with --update.")
        return 1

    VENDOR.mkdir(parents=True, exist_ok=True)
    v = Vendor(lock, args.update)
    v.npm()
    v.icons()
    v.pyodide()

    LOCK.write_text(
        json.dumps({"packages": PACKAGES, "pyodide": PYODIDE, "files": dict(sorted(v.files.items()))}, indent=2) + "\n"
    )
    print(f"Wrote {LOCK.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
