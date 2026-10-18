"""Folder access policy (PLAN.md §4.1), enforced in code, not prompts.

Every path an agent asks for is expanded and resolved with realpath (so `..` and symlinks can't escape), must land
inside a folder granted in the agent's manifest, and must not touch sensitive locations, even inside a granted folder.

Writes have stricter rules, because a file in the wrong place can run code without anyone opening it (a macOS
LaunchAgent, a script in the Windows Startup folder, a shell config file):
- never into system folders, app settings (~/Library, AppData), or hidden folders, even inside a read & write grant
- only file types on an allow-list (documents, data, images; admins can add more), and never program launchers
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from space_agents.manifest import FolderGrant

# Directory names that are never readable, wherever they appear.
DENY_NAMES = {
    ".ssh",
    ".gnupg",
    ".aws",
    ".azure",
    ".kube",
    ".docker",
    ".password-store",
    "Keychains",  # macOS
    "Credentials",  # Windows: AppData/*/Microsoft/Credentials
    "Protect",  # Windows DPAPI keys
}
DENY_SUFFIXES = (".kdbx", ".pem", ".key", ".p12", ".pfx", ".keychain", ".keychain-db")

# Writes: folders under the home directory where files can run code or change how apps behave. Cloud-drive folders
# inside ~/Library (OneDrive, Dropbox, iCloud Drive on macOS) hold ordinary documents, so they stay writable.
WRITE_PROTECTED_IN_HOME = ("Library", "Applications", "AppData")
WRITABLE_IN_PROTECTED = ("Library/CloudStorage", "Library/Mobile Documents")
WRITE_PROTECTED_SYSTEM = (
    "/System", "/Library", "/Applications", "/usr", "/bin", "/sbin", "/etc", "/private/etc", "/opt", "/boot", "/lib",
)  # fmt: skip
WRITE_PROTECTED_ENV = ("APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "SystemRoot", "ProgramFiles", "ProgramFiles(x86)")

# File types agents may save. Admins can add more with the `extra_file_types` setting (e.g. .py for a coding agent).
SAFE_FILE_TYPES = frozenset(
    ".xlsx .docx .pptx .pdf .csv .tsv .txt .md .json .xml .yaml .yml .log .png .jpg .jpeg .gif .webp".split()  # noqa: SIM905
)
# Never saved, whatever the settings say: opening (double-clicking) one of these runs a program or installs something.
NEVER_FILE_TYPES = frozenset(
    """.exe .com .scr .pif .bat .cmd .msi .msp .msix .appx .lnk .url .hta .vbs .vbe .jse .wsf .wsh .ps1 .psm1 .reg
    .scf .cpl .dll .sys .jar .xll .xlam .ppam .appref-ms .application .settingcontent-ms .library-ms .iso .img .vhd
    .vhdx .command .terminal .tool .app .pkg .mpkg .dmg .workflow .webloc .inetloc .fileloc .desktop""".split()  # noqa: SIM905
)
# Windows device names: "NUL.txt" isn't a file there.
RESERVED_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))]
)


class AccessDenied(PermissionError):
    pass


def write_protected(real: Path) -> bool:
    """True if agents may never save into `real` (a resolved path), whatever folders they were granted."""
    if any(part.startswith(".") and part not in (".", "..") for part in real.parts):
        return True  # hidden folders and files: shell configs, .git/hooks, .vscode tasks, ~/.config autostart
    home = Path.home().resolve()
    if any(real.is_relative_to(home / ok) for ok in WRITABLE_IN_PROTECTED):
        return False
    roots = [home / name for name in WRITE_PROTECTED_IN_HOME]
    roots += [Path(p) for p in WRITE_PROTECTED_SYSTEM]
    roots += [Path(os.environ[v]) for v in WRITE_PROTECTED_ENV if os.environ.get(v)]
    return any(real == root or real.is_relative_to(root) for root in roots)


def file_types(extra: Iterable[str] = ()) -> frozenset[str]:
    """The extensions agents may save: the safe defaults plus `extra` (".py" or "py"), minus program launchers."""
    added = {("." + e.strip().lower().lstrip(".")) for e in extra if e.strip().strip(".")}
    return (SAFE_FILE_TYPES | added) - NEVER_FILE_TYPES


@dataclass(frozen=True)
class Root:
    path: Path  # resolved
    display: str  # as written in the manifest, e.g. ~/Documents/PO Reports
    writable: bool


def display_path(p: Path) -> str:
    try:
        return "~/" + p.relative_to(Path.home().resolve()).as_posix()
    except ValueError:
        return str(p)


class FolderAccess:
    def __init__(self, grants: list[FolderGrant], protected: list[Path] = (), extra_file_types: Iterable[str] = ()):
        self.roots = [
            Root(Path(os.path.expanduser(g.path)).resolve(), g.path, g.access == "read_write") for g in grants
        ]
        self._protected = [p.resolve() for p in protected]
        self.file_types = file_types(extra_file_types)

    def resolve(self, raw: str, *, write: bool = False) -> Path:
        """Turn a path from the model into a safe absolute path, or raise AccessDenied with a helpful message."""
        if not self.roots:
            raise AccessDenied("This agent has no folders. The user can add some in the agent's Files & folders tab.")
        raw = (raw or "").strip().strip('"').strip("'")
        p = Path(os.path.expanduser(raw)) if raw else self.roots[0].path
        if not p.is_absolute():
            # Relative paths: try each granted folder, preferring one where the path exists.
            candidates = [r.path / p for r in self.roots]
            p = next((c for c in candidates if c.exists()), candidates[0])
        real = p.resolve()
        root = self.root_for(real)
        if root is None:
            folders = ", ".join(r.display for r in self.roots)
            raise AccessDenied(f"{display_path(real)} is outside this agent's folders ({folders}).")
        parts = set(real.relative_to(root.path).parts) | {real.name}
        if parts & DENY_NAMES or real.name.lower().endswith(DENY_SUFFIXES):
            raise AccessDenied(f"{display_path(real)} is a protected location and can't be accessed.")
        if any(real == prot or real.is_relative_to(prot) for prot in self._protected):
            raise AccessDenied(f"{display_path(real)} is a protected location and can't be accessed.")
        if write and not root.writable:
            raise AccessDenied(f"{root.display} is read-only for this agent, so files can't be saved there.")
        if write and write_protected(real):
            raise AccessDenied(
                f"{display_path(real)} is a system, app-settings, or hidden location. Agents can never save files"
                " there; save to a regular folder instead."
            )
        return real

    def check_file_type(self, path: Path) -> None:
        """Raise AccessDenied unless agents may save a file with this name (call for every file an agent writes)."""
        suffix = path.suffix.lower()
        if path.name.split(".")[0].upper() in RESERVED_NAMES or path.name != path.name.rstrip(". "):
            raise AccessDenied(f"{path.name} isn't a valid file name.")
        if suffix in NEVER_FILE_TYPES:
            raise AccessDenied(f"Agents can never save {suffix} files: opening one could run a program.")
        if suffix not in self.file_types:
            kind = f"{suffix} files" if suffix else "files without an extension"
            raise AccessDenied(f"Agents can't save {kind}. Allowed types: {', '.join(sorted(self.file_types))}.")

    def root_for(self, real: Path) -> Root | None:
        matches = [r for r in self.roots if real == r.path or real.is_relative_to(r.path)]
        return max(matches, key=lambda r: len(r.path.parts)) if matches else None  # most specific grant wins

    def describe(self) -> str:
        if not self.roots:
            return "- (no folders)"
        return "\n".join(f"- {r.display} ({'read & write' if r.writable else 'read only'})" for r in self.roots)
