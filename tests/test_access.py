import os

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
