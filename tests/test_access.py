import os
from pathlib import Path

import pytest

from space_agents.manifest import FolderGrant
from space_agents.tools.access import AccessDenied, FolderAccess


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "docs" / "sub").mkdir(parents=True)
    (tmp_path / "docs" / "a.pdf").write_text("x")
    (tmp_path / "docs" / ".ssh").mkdir()
    (tmp_path / "docs" / "secret.pem").write_text("k")
    (tmp_path / "out").mkdir()
    (tmp_path / "private").mkdir()
    (tmp_path / "private" / "p.txt").write_text("p")
    return tmp_path


def access(tree, **extra):
    grants = [FolderGrant(str(tree / "docs"), "read"), FolderGrant(str(tree / "out"), "read_write")]
    return FolderAccess(grants, **extra)


def test_resolves_inside_grants(tree):
    a = access(tree)
    assert a.resolve(str(tree / "docs" / "a.pdf")) == (tree / "docs" / "a.pdf").resolve()
    assert a.resolve("a.pdf") == (tree / "docs" / "a.pdf").resolve()  # relative: found in a granted folder
    assert a.resolve(str(tree / "out" / "new.xlsx"), write=True).name == "new.xlsx"


def test_blocks_escapes(tree):
    a = access(tree)
    with pytest.raises(AccessDenied, match="outside"):
        a.resolve(str(tree / "docs" / ".." / "private" / "p.txt"))
    os.symlink(tree / "private", tree / "docs" / "link")
    with pytest.raises(AccessDenied, match="outside"):
        a.resolve(str(tree / "docs" / "link" / "p.txt"))


def test_blocks_sensitive_locations(tree):
    a = access(tree, protected=[tree / "docs" / "sub"])
    for p in (tree / "docs" / ".ssh" / "id_rsa", tree / "docs" / "secret.pem", tree / "docs" / "sub" / "x"):
        with pytest.raises(AccessDenied, match="protected"):
            a.resolve(str(p))


def test_write_requires_read_write(tree):
    with pytest.raises(AccessDenied, match="read-only"):
        access(tree).resolve(str(tree / "docs" / "a.pdf"), write=True)


def test_no_folders():
    with pytest.raises(AccessDenied, match="no folders"):
        FolderAccess([]).resolve("x")


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path.resolve() / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


def test_writes_never_land_in_autostart_settings_or_hidden_folders(home):
    a = FolderAccess([FolderGrant(str(home), "read_write")])
    for p in (
        "Library/LaunchAgents/com.evil.plist",
        "AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup/run.txt",
        ".config/fish/config.txt",
        "project/.git/hooks/pre-commit.txt",
    ):
        with pytest.raises(AccessDenied, match="system, app-settings, or hidden"):
            a.resolve(str(home / p), write=True)
        assert a.resolve(str(home / p))  # reading is still fine
    assert a.resolve(str(home / "Documents" / "r.xlsx"), write=True)
    assert a.resolve(str(home / "Library/CloudStorage/OneDrive-Acme/r.xlsx"), write=True)  # cloud drives are ok


def test_only_allowed_file_types_can_be_saved():
    a = FolderAccess([])
    for ok in ("Report.xlsx", "notes.MD", "chart.png"):
        a.check_file_type(Path(ok))
    for bad, why in (
        ("Summary.pdf.bat", "never"),
        ("evil.LNK", "never"),
        ("script.py", "can't save .py"),
        ("Makefile", "without an extension"),
        ("NUL.txt", "valid file name"),
        ("report.txt.", "valid file name"),
    ):
        with pytest.raises(AccessDenied, match=why):
            a.check_file_type(Path(bad))


def test_extra_file_types_can_be_added_but_never_launchers():
    a = FolderAccess([], extra_file_types=["py", ".CPP", " .exe ", ""])
    a.check_file_type(Path("main.py"))
    a.check_file_type(Path("main.cpp"))
    assert ".exe" not in a.file_types
    with pytest.raises(AccessDenied, match="never"):
        a.check_file_type(Path("setup.exe"))
