"""Skills: SKILL.md parsing, folder layouts, what use_skill returns, and read-only access."""

import os

import pytest

from space_agents.manifest import FolderGrant
from space_agents.skills import load_skills, parse_frontmatter, use_skill_result
from space_agents.tools.access import AccessDenied, FolderAccess

PDF_SKILL = """---
name: pdf-forms
description: >
  Fill in PDF forms.
  Use when the user asks to complete a form.
metadata:
  author: someone
license: "MIT"
---
# Filling forms

1. Read reference.md.
2. Run scripts/fill.py.
"""


def write_skill(folder, text, files=()):
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(text)
    for name in files:
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text("x")


@pytest.fixture
def skills_dir(tmp_path):
    root = tmp_path / "skills"
    write_skill(root / "pdf-forms", PDF_SKILL, ["reference.md", "scripts/fill.py", ".hidden/x.txt"])
    write_skill(
        root / "report", '---\nname: weekly-report\ndescription: "Weekly status report: our format."\n---\nBody'
    )
    write_skill(root / "broken", "---\nname: broken\n---\nNo description.")
    (root / "notes").mkdir()  # not a skill
    return root


def test_frontmatter_scalars_blocks_and_nesting():
    meta, body = parse_frontmatter(PDF_SKILL)
    assert meta == {
        "name": "pdf-forms",
        "description": "Fill in PDF forms. Use when the user asks to complete a form.",
        "license": "MIT",
    }
    assert body.startswith("# Filling forms")
    literal, _ = parse_frontmatter("---\ndescription: |\n  line one\n  line two\n---\n")
    assert literal["description"] == "line one\nline two"
    assert parse_frontmatter("# No header\ntext") == ({}, "# No header\ntext")


def test_finds_skills_and_reports_problems(skills_dir):
    found = load_skills(str(skills_dir))
    assert [s.name for s in found.skills] == ["pdf-forms", "weekly-report"]
    assert found.get("Weekly-Report").description == "Weekly status report: our format."
    assert len(found.problems) == 1 and "broken" in found.problems[0] and "description" in found.problems[0]


def test_accepts_a_parent_folder_or_a_single_skill(skills_dir, tmp_path):
    assert len(load_skills(str(tmp_path)).skills) == 2  # a folder that contains skills/
    single = load_skills(str(skills_dir / "pdf-forms"))
    assert [s.name for s in single.skills] == ["pdf-forms"]
    missing = load_skills(str(tmp_path / "nope"))
    assert missing.skills == [] and "doesn't exist" in missing.problems[0]


def test_symlinked_skills_resolve_to_their_real_folder(skills_dir, tmp_path):
    elsewhere = tmp_path / "shared" / "linked"
    write_skill(elsewhere, "---\ndescription: Linked skill.\n---\nHi")
    os.symlink(elsewhere, skills_dir / "linked")
    skill = load_skills(str(skills_dir)).get("linked")
    assert skill.folder == elsewhere.resolve()


def test_use_skill_returns_instructions_files_and_how_to_run_python(skills_dir):
    text = use_skill_result(load_skills(str(skills_dir)).get("pdf-forms"))
    assert "# Filling forms" in text and "description:" not in text  # the body, not the header
    assert "reference.md" in text and "scripts/fill.py" in text and ".hidden" not in text
    assert "runpy.run_path" in text


def test_skill_folders_are_readable_but_never_writable(skills_dir, tmp_path):
    skill = load_skills(str(skills_dir)).get("pdf-forms")
    access = FolderAccess([FolderGrant(str(tmp_path), "read_write")], skill_folders=[skill.folder])
    assert access.resolve(str(skill.folder / "reference.md"))
    with pytest.raises(AccessDenied, match="Skills are read-only"):
        access.resolve(str(skill.folder / "SKILL.md"), write=True)
    with pytest.raises(AccessDenied, match="Skills are read-only"):
        access.resolve(str(skill.folder / "new.md"), write=True)
    assert access.resolve(str(tmp_path / "out.md"), write=True)  # the rest of the grant is unaffected
    only_skills = FolderAccess([], skill_folders=[skill.folder])
    assert only_skills.resolve(str(skill.folder / "reference.md"))
