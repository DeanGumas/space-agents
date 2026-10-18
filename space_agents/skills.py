"""Agent skills: folders of instructions (and supporting files) the user installs for specific kinds of tasks.

The standard layout, one folder per skill, each with a SKILL.md that starts with YAML frontmatter:

    skills/
      pdf-forms/
        SKILL.md          ---\\nname: pdf-forms\\ndescription: Fill in PDF forms. Use when…\\n---\\n<instructions>
        reference.md      optional extra files the instructions point to
        scripts/fill.py

The user picks a folder in the agent's Instructions tab: the skills folder itself, a folder that contains `skills/`,
or a single skill's folder. Skills load progressively, so many can be installed cheaply:
1. Every skill's name and description goes in the system prompt.
2. When a task matches, the model calls `use_skill(name)`, which returns the SKILL.md instructions and the list of
   the skill's files.
3. The model reads those files with the normal tools (read_document, run_python inputs) as the instructions direct.

Skills are trusted instructions, so agents can read them but never change them (FolderAccess treats skill folders
as read-only, even inside a read & write grant), and the folder can only be set through the native folder picker.
Skills are re-read at the start of every run, so edits apply without re-saving the agent.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from space_agents.tools.access import display_path

log = logging.getLogger(__name__)

SKILL_FILE = "SKILL.md"
MAX_SKILLS = 100
MAX_NAME = 64
MAX_DESCRIPTION = 1024
MAX_SKILL_BYTES = 512 * 1024
MAX_LISTED_FILES = 200


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    folder: Path  # resolved

    @property
    def skill_file(self) -> Path:
        return self.folder / SKILL_FILE


@dataclass
class SkillSet:
    skills: list[Skill] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # shown in the editor so the user can fix them

    def get(self, name: str) -> Skill | None:
        wanted = name.strip().lower()
        return next((s for s in self.skills if s.name.lower() == wanted), None)


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Top-level `key: value` pairs from a `---` YAML header, and the body after it.

    Handles what SKILL.md files use: plain and quoted scalars, indented continuation lines, and `|` / `>` blocks.
    Nested mappings and lists (e.g. `metadata:`) aren't needed and are skipped. No YAML dependency.
    """
    lines = text.lstrip("﻿").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return {}, text
    meta: dict[str, str] = {}
    i = 1
    while i < end:
        match = re.match(r"^([A-Za-z0-9_-]+):\s*(.*?)\s*$", lines[i])
        i += 1
        if not match:
            continue
        key, value = match.groups()
        indented: list[str] = []
        while i < end and (lines[i][:1] in (" ", "\t") or not lines[i].strip()):
            indented.append(lines[i].strip())
            i += 1
        if value[:1] in ("|", ">"):
            value = ("\n" if value[0] == "|" else " ").join(indented).strip()
        elif value:
            value = " ".join([value, *(line for line in indented if line)])
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
        else:
            continue  # a nested mapping or list
        meta[key] = value
    return meta, "\n".join(lines[end + 1 :]).lstrip("\n")


def skill_folders(folder: Path) -> list[Path]:
    """The skill folders under what the user picked: a single skill, a skills folder, or a folder with skills/."""
    if (folder / SKILL_FILE).is_file():
        return [folder]
    for base in (folder, folder / "skills"):
        if not base.is_dir():
            continue
        found = sorted(
            d for d in base.iterdir() if not d.name.startswith(".") and d.is_dir() and (d / SKILL_FILE).is_file()
        )
        if found:
            return found
    return []


def load_skills(folder: str) -> SkillSet:
    """Read every skill's name and description. Problems are collected, so one bad skill can't hide the rest."""
    out = SkillSet()
    if not folder:
        return out
    root = Path(os.path.expanduser(folder))
    if not root.is_dir():
        out.problems.append(f"The folder {folder} doesn't exist.")
        return out
    dirs = skill_folders(root)
    if not dirs:
        out.problems.append(f"No skills found in {folder}. Each skill is a folder with a {SKILL_FILE} inside.")
    for d in dirs[:MAX_SKILLS]:
        label = d.name
        try:
            skill_file = d.resolve() / SKILL_FILE
            if skill_file.stat().st_size > MAX_SKILL_BYTES:
                out.problems.append(f"{label}: {SKILL_FILE} is too large (max {MAX_SKILL_BYTES // 1024} KB).")
                continue
            meta, _ = parse_frontmatter(skill_file.read_text(encoding="utf-8", errors="replace"))
        except OSError as e:
            out.problems.append(f"{label}: couldn't read {SKILL_FILE} ({e.strerror or e}).")
            continue
        name = (meta.get("name") or d.name).strip()[:MAX_NAME]
        description = " ".join((meta.get("description") or "").split())[:MAX_DESCRIPTION]
        if not description:
            out.problems.append(f"{label}: {SKILL_FILE} needs a description in its header, saying when to use it.")
            continue
        if out.get(name):
            out.problems.append(f"{label}: another skill is already named {name!r}; skipped.")
            continue
        out.skills.append(Skill(name, description, d.resolve()))
    if len(dirs) > MAX_SKILLS:
        out.problems.append(f"Only the first {MAX_SKILLS} skills are used.")
    return out


def skill_files(skill: Skill) -> list[Path]:
    """The skill's other files (not hidden, not SKILL.md), for the model to read or run when the skill says so."""
    files = []
    for p in sorted(skill.folder.rglob("*")):
        rel = p.relative_to(skill.folder)
        if any(part.startswith(".") for part in rel.parts) or not p.is_file() or rel.as_posix() == SKILL_FILE:
            continue
        files.append(p)
        if len(files) >= MAX_LISTED_FILES:
            break
    return files


def use_skill_result(skill: Skill) -> str:
    """What `use_skill` returns to the model: the instructions, then the files it can read or run."""
    _, body = parse_frontmatter(skill.skill_file.read_text(encoding="utf-8", errors="replace"))
    files = skill_files(skill)
    lines = [
        f'Skill "{skill.name}" — follow these instructions for this task.',
        f"(The full file is {display_path(skill.skill_file)}; if this is cut off, read the rest with read_document.)",
        "",
        body.strip() or "(This skill has no instructions beyond its description.)",
    ]
    if files:
        lines += ["", "Files in this skill (read them with read_document when the instructions point to them):"]
        lines += [f"- {display_path(p)}" for p in files]
        if len(files) >= MAX_LISTED_FILES:
            lines.append(f"- … (only the first {MAX_LISTED_FILES} are listed)")
    if any(p.suffix == ".py" for p in files):
        lines += [
            "",
            "To run one of this skill's Python scripts, pass it (and any files it needs) to run_python in input_files,"
            " then run it, e.g.: import runpy, sys; sys.argv = ['script.py', 'arg1']; "
            "runpy.run_path('in/script.py', run_name='__main__'). Shell and Node scripts can't run here; do those"
            " steps another way.",
        ]
    return "\n".join(lines)
