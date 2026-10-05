"""Agent manifest: schema, validation, and (de)serialization.

A manifest is the portable definition of one agent (see PLAN.md §3.1). It never contains secrets, so it can be
exported and shared. Validation collects *all* problems keyed by dotted field path (e.g. "schedule.every_minutes")
so the UI can show each message next to the right input.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

SCHEMA_VERSION = 1

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
LEVELS = ("info", "action", "urgent")
FOLDER_ACCESS = ("read", "read_write")
EXTERNAL_ACTIONS = ("never", "ask", "allow")
NEW_FILE_POLICIES = ("allow", "ask")
EXISTING_FILE_POLICIES = ("ask", "allow_with_backup")

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
SERVER_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

DEFAULT_COLOR = "#6366f1"


class ManifestError(ValueError):
    def __init__(self, errors: dict[str, str]):
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


@dataclass
class Avatar:
    emoji: str | None = "🤖"
    image: str | None = None  # path relative to the data dir, managed by the store (never set by the UI directly)
    color: str = DEFAULT_COLOR


@dataclass
class ModelConfig:
    name: str = ""
    temperature: float = 0.2


@dataclass
class ServerGrant:
    id: str
    tools: str | list[str] = "read_only"  # "read_only" or an explicit list of tool names


@dataclass
class FolderGrant:
    path: str
    access: str = "read"


@dataclass
class ActiveHours:
    start: str = "07:00"
    end: str = "18:00"


@dataclass
class Schedule:
    enabled: bool = False
    every_minutes: int = 60
    active_hours: ActiveHours = field(default_factory=ActiveHours)
    weekdays: list[str] = field(default_factory=lambda: ["mon", "tue", "wed", "thu", "fri"])
    run_on_startup_if_missed: bool = True


@dataclass
class Notifications:
    desktop: bool = True
    min_level: str = "action"


@dataclass
class Memory:
    enabled: bool = True


@dataclass
class Limits:
    max_tool_calls: int = 30
    max_minutes: int = 10


@dataclass
class FileChanges:
    """How proposed file changes are applied inside read_write folders (PLAN.md §2.10). Deletes are never possible."""

    new_files: str = "allow"
    existing_files: str = "ask"


@dataclass
class Permissions:
    external_actions: str = "never"


@dataclass
class Manifest:
    name: str
    id: str = ""
    schema_version: int = SCHEMA_VERSION
    avatar: Avatar = field(default_factory=Avatar)
    description: str = ""
    instructions: str = ""
    model: ModelConfig = field(default_factory=ModelConfig)
    mcp_servers: list[ServerGrant] = field(default_factory=list)
    filesystem: list[FolderGrant] = field(default_factory=list)
    file_changes: FileChanges = field(default_factory=FileChanges)
    schedule: Schedule = field(default_factory=Schedule)
    task: str = ""
    notifications: Notifications = field(default_factory=Notifications)
    memory: Memory = field(default_factory=Memory)
    limits: Limits = field(default_factory=Limits)
    permissions: Permissions = field(default_factory=Permissions)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> Manifest:
        """Parse and validate. Raises ManifestError with every problem found."""
        errors: dict[str, str] = {}
        m = _parse(data if isinstance(data, dict) else {}, errors)
        if not isinstance(data, dict):
            errors[""] = "Manifest must be a JSON object."
        if errors:
            raise ManifestError(errors)
        return m


# --- parsing ------------------------------------------------------------------------------------------------------


class _R:
    """Reads typed fields from a dict, recording errors under dotted paths instead of raising."""

    def __init__(self, data: Any, prefix: str, errors: dict[str, str]):
        self.data = data if isinstance(data, dict) else {}
        self.prefix = prefix
        self.errors = errors
        if data is not None and not isinstance(data, dict):
            errors[prefix.rstrip(".") or ""] = "Expected an object."

    def path(self, key: str) -> str:
        return f"{self.prefix}{key}"

    def err(self, key: str, msg: str) -> None:
        self.errors.setdefault(self.path(key), msg)

    def sub(self, key: str) -> _R:
        return _R(self.data.get(key), self.path(key) + ".", self.errors)

    def str(self, key: str, default: str = "", *, max_len: int = 10_000, required: bool = False) -> str:
        v = self.data.get(key, default)
        if v is None:
            v = default
        if not isinstance(v, str):
            self.err(key, "Expected text.")
            return default
        v = v.strip()
        if required and not v:
            self.err(key, "This field is required.")
        if len(v) > max_len:
            self.err(key, f"Must be {max_len:,} characters or fewer.")
        return v

    def opt_str(self, key: str, *, max_len: int) -> str | None:
        v = self.data.get(key)
        if v in (None, ""):
            return None
        if not isinstance(v, str):
            self.err(key, "Expected text.")
            return None
        v = v.strip()
        if len(v) > max_len:
            self.err(key, f"Must be {max_len} characters or fewer.")
        return v or None

    def bool(self, key: str, default: bool) -> bool:
        v = self.data.get(key, default)
        if not isinstance(v, bool):
            self.err(key, "Expected true or false.")
            return default
        return v

    def int(self, key: str, default: int, lo: int, hi: int) -> int:
        v = self.data.get(key, default)
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        if not isinstance(v, int) or isinstance(v, bool):
            self.err(key, "Expected a whole number.")
            return default
        if not lo <= v <= hi:
            self.err(key, f"Must be between {lo} and {hi}.")
        return v

    def float(self, key: str, default: float, lo: float, hi: float) -> float:
        v = self.data.get(key, default)
        if not isinstance(v, int | float) or isinstance(v, bool):
            self.err(key, "Expected a number.")
            return default
        if not lo <= v <= hi:
            self.err(key, f"Must be between {lo} and {hi}.")
        return float(v)

    def choice(self, key: str, default: str, choices: tuple[str, ...]) -> str:
        v = self.data.get(key, default)
        if v not in choices:
            self.err(key, f"Must be one of: {', '.join(choices)}.")
            return default
        return v

    def list(self, key: str) -> list[Any]:
        v = self.data.get(key, [])
        if v is None:
            return []
        if not isinstance(v, list):
            self.err(key, "Expected a list.")
            return []
        return v


def _parse(data: dict[str, Any], errors: dict[str, str]) -> Manifest:
    r = _R(data, "", errors)

    version = data.get("schema_version", SCHEMA_VERSION)
    if version != SCHEMA_VERSION:
        r.err("schema_version", f"Unsupported schema version {version!r} (expected {SCHEMA_VERSION}).")

    agent_id = r.str("id", "", max_len=48)
    if agent_id and not ID_RE.match(agent_id):
        r.err("id", "Use lowercase letters, numbers, and dashes.")

    a = r.sub("avatar")
    avatar = Avatar(
        emoji=a.opt_str("emoji", max_len=16) if "emoji" in a.data else Avatar().emoji,
        image=a.opt_str("image", max_len=200),
        color=a.str("color", DEFAULT_COLOR, max_len=7),
    )
    if not COLOR_RE.match(avatar.color):
        a.err("color", "Use a hex color like #3b82f6.")

    mr = r.sub("model")
    model = ModelConfig(name=mr.str("name", "", max_len=200), temperature=mr.float("temperature", 0.2, 0.0, 2.0))

    servers: list[ServerGrant] = []
    seen_servers: set[str] = set()
    for i, item in enumerate(r.list("mcp_servers")):
        sr = _R(item, f"mcp_servers.{i}.", errors)
        sid = sr.str("id", "", max_len=64, required=True)
        if sid and not SERVER_ID_RE.match(sid):
            sr.err("id", "Invalid server id.")
        if sid in seen_servers:
            sr.err("id", "This server is listed twice.")
        seen_servers.add(sid)
        tools = sr.data.get("tools", "read_only")
        if tools != "read_only" and not (isinstance(tools, list) and all(isinstance(t, str) for t in tools)):
            sr.err("tools", 'Use "read_only" or a list of tool names.')
            tools = "read_only"
        servers.append(ServerGrant(id=sid, tools=tools))

    folders: list[FolderGrant] = []
    seen_paths: set[str] = set()
    for i, item in enumerate(r.list("filesystem")):
        fr = _R(item, f"filesystem.{i}.", errors)
        p = fr.str("path", "", max_len=1024, required=True)
        if p in seen_paths:
            fr.err("path", "This folder is listed twice.")
        seen_paths.add(p)
        folders.append(FolderGrant(path=p, access=fr.choice("access", "read", FOLDER_ACCESS)))

    fc = r.sub("file_changes")
    file_changes = FileChanges(
        new_files=fc.choice("new_files", "allow", NEW_FILE_POLICIES),
        existing_files=fc.choice("existing_files", "ask", EXISTING_FILE_POLICIES),
    )

    s = r.sub("schedule")
    h = s.sub("active_hours")
    hours = ActiveHours(start=h.str("start", "07:00", max_len=5), end=h.str("end", "18:00", max_len=5))
    for key in ("start", "end"):
        if not TIME_RE.match(getattr(hours, key)):
            h.err(key, "Use 24-hour HH:MM.")
    if TIME_RE.match(hours.start) and TIME_RE.match(hours.end) and hours.start >= hours.end:
        h.err("end", "End must be after start.")
    weekdays = s.list("weekdays") if "weekdays" in s.data else list(Schedule().weekdays)
    bad_days = [d for d in weekdays if d not in WEEKDAYS]
    if bad_days:
        s.err("weekdays", f"Unknown day(s): {', '.join(map(str, bad_days))}.")
    weekdays = [d for d in WEEKDAYS if d in weekdays]  # normalize order, drop dupes
    schedule = Schedule(
        enabled=s.bool("enabled", False),
        every_minutes=s.int("every_minutes", 60, 5, 1440),
        active_hours=hours,
        weekdays=weekdays,
        run_on_startup_if_missed=s.bool("run_on_startup_if_missed", True),
    )
    if schedule.enabled and not weekdays:
        s.err("weekdays", "Pick at least one day.")

    task = r.str("task", "", max_len=20_000)
    if schedule.enabled and not task:
        r.err("task", "Scheduled agents need a task describing what to do on each run.")

    n = r.sub("notifications")
    lim = r.sub("limits")
    return Manifest(
        schema_version=SCHEMA_VERSION,
        id=agent_id,
        name=r.str("name", "", max_len=60, required=True),
        avatar=avatar,
        description=r.str("description", "", max_len=280),
        instructions=r.str("instructions", "", max_len=20_000),
        model=model,
        mcp_servers=servers,
        filesystem=folders,
        file_changes=file_changes,
        schedule=schedule,
        task=task,
        notifications=Notifications(desktop=n.bool("desktop", True), min_level=n.choice("min_level", "action", LEVELS)),
        memory=Memory(enabled=r.sub("memory").bool("enabled", True)),
        limits=Limits(
            max_tool_calls=lim.int("max_tool_calls", 30, 1, 200),
            max_minutes=lim.int("max_minutes", 10, 1, 120),
        ),
        permissions=Permissions(
            external_actions=r.sub("permissions").choice("external_actions", "never", EXTERNAL_ACTIONS)
        ),
    )


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40].strip("-")
    return slug or "agent"
