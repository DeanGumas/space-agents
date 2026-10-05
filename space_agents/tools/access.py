"""Folder access policy (PLAN.md §4.1), enforced in code, not prompts.

Every path an agent asks for is expanded and resolved with realpath (so `..` and symlinks can't escape), must land
inside a folder granted in the agent's manifest, and must not touch sensitive locations, even inside a granted folder.
"""

from __future__ import annotations

import os
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


class AccessDenied(PermissionError):
    pass


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
    def __init__(self, grants: list[FolderGrant], protected: list[Path] = ()):
        self.roots = [
            Root(Path(os.path.expanduser(g.path)).resolve(), g.path, g.access == "read_write") for g in grants
        ]
        self._protected = [p.resolve() for p in protected]

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
        return real

    def root_for(self, real: Path) -> Root | None:
        matches = [r for r in self.roots if real == r.path or real.is_relative_to(r.path)]
        return max(matches, key=lambda r: len(r.path.parts)) if matches else None  # most specific grant wins

    def describe(self) -> str:
        if not self.roots:
            return "- (no folders)"
        return "\n".join(f"- {r.display} ({'read & write' if r.writable else 'read only'})" for r in self.roots)
