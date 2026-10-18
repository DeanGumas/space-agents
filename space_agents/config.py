"""Layered configuration.

Each setting is resolved from these layers, lowest to highest precedence:

    1. default   built-in space_agents/defaults.json (the org "bakes in" its URLs here for release builds)
    2. user      settings.json overrides from Settings → Advanced (endpoint URLs only)
    3. .env      a .env file in the working directory or the repo root (dev/test scaffolding)
    4. env       real process environment variables
    5. cli       command-line flags (--debug, --data-dir)

Environment variables use the SPACE_AGENTS_ prefix, e.g. SPACE_AGENTS_LLM_BASE_URL.
The resolved value *and the layer it came from* are kept so Settings can show where a value came from.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from space_agents import paths
from space_agents.settings import USER_OVERRIDABLE, UserSettings

ENV_PREFIX = "SPACE_AGENTS_"


@dataclass(frozen=True)
class Spec:
    key: str
    kind: str  # "url" | "bool" | "int" | "path" | "list" | "str" | "secret"
    label: str
    help: str


SPECS: tuple[Spec, ...] = (
    Spec("llm_base_url", "url", "Model gateway URL", "Base for /chat/completions and /models, e.g. https://site/api"),
    Spec("mcp_base_url", "url", "Tool server URL", "Base for the /mcp catalog and /mcp/{id} servers"),
    Spec("data_dir", "path", "Data folder", "Where agents, settings, and history are stored"),
    Spec("debug", "bool", "Debug mode", "Enables webview devtools and verbose logging"),
    Spec("catalog_file", "path", "Offline catalog file", "Dev only: JSON file used instead of GET /mcp"),
    Spec(
        "models", "list", "Model list", "Comma-separated models offered in the editor (until GET /models is wired up)"
    ),
    Spec("api_key", "secret", "API key", "Dev/test only via .env; moves to the OS keychain with onboarding"),
    Spec(
        "sandbox_warmup",
        "str",
        "Sandbox warm-up",
        "Preload the Python sandbox at launch: full (~420 MB, first run <1 s), light (~210 MB), or off",
    ),
    Spec(
        "extra_file_types",
        "list",
        "Extra file types",
        "Comma-separated file types agents may also save, e.g. .py,.cpp. Program launchers (.exe, .bat…) never are",
    ),
    Spec(
        "mark_agent_files",
        "bool",
        "Mark agent files",
        "Tag files agents save as downloaded from the internet: Office opens them in Protected View, blocks macros",
    ),
    Spec(
        "schedule_jitter_minutes",
        "int",
        "Schedule jitter (minutes)",
        "Scheduled runs start at a random time up to this many minutes before or after their scheduled time (0–30, "
        "default 5). Lowering it may lead to slow responses, because many users' agents would then run at precisely "
        "the same time (e.g. on the hour)",
    ),
    Spec(
        "max_concurrent_runs",
        "int",
        "Runs at once",
        "How many agent runs (scheduled or Run now) may work at the same time, across all agents (1–10)",
    ),
    Spec(
        "reasoning_effort",
        "str",
        "Reasoning effort",
        "Sent to reasoning models (minimal/low/medium/high) to bound token use; dropped if a model rejects it",
    ),
)
SPEC_BY_KEY = {s.key: s for s in SPECS}


@dataclass(frozen=True)
class Resolved:
    value: Any
    source: str  # default | user | .env | env | cli


def parse_dotenv(text: str) -> dict[str, str]:
    """Minimal .env parser: KEY=VALUE lines, optional `export`, quotes, and # comments."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        out[key] = value
    return out


def find_dotenv() -> Path | None:
    for candidate in (Path.cwd() / ".env", paths.resource_root() / ".env"):
        if candidate.is_file():
            return candidate
    return None


def _coerce(spec: Spec, raw: Any, base_dir: Path | None) -> Any:
    if spec.kind == "int":
        try:
            return int(str(raw).strip())
        except ValueError:
            return None  # the property supplies the default
    if spec.kind == "bool":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if spec.kind == "list":
        if isinstance(raw, list):
            return [str(x).strip() for x in raw if str(x).strip()]
        return [x.strip() for x in str(raw).split(",") if x.strip()]
    if spec.kind == "path":
        if raw in ("", None):
            return None
        p = Path(str(raw)).expanduser()
        if not p.is_absolute() and base_dir is not None:
            p = base_dir / p
        return p.resolve()
    if spec.kind == "url":
        url = str(raw).strip().rstrip("/")
        # Accept a full endpoint URL too: ".../v1/chat/completions" → ".../v1".
        for suffix in ("/chat/completions", "/completions", "/models"):
            if spec.key == "llm_base_url" and url.endswith(suffix):
                url = url[: -len(suffix)]
        return url
    return str(raw).strip()


class Config:
    def __init__(self, values: dict[str, Resolved], dotenv_path: Path | None):
        self._values = values
        self.dotenv_path = dotenv_path

    def get(self, key: str) -> Any:
        return self._values[key].value

    def source(self, key: str) -> str:
        return self._values[key].source

    @property
    def llm_base_url(self) -> str:
        return self.get("llm_base_url")

    @property
    def mcp_base_url(self) -> str:
        return self.get("mcp_base_url")

    @property
    def data_dir(self) -> Path:
        return self.get("data_dir")

    @property
    def debug(self) -> bool:
        return self.get("debug")

    @property
    def api_key(self) -> str:
        return self.get("api_key")

    @property
    def reasoning_effort(self) -> str:
        return self.get("reasoning_effort")

    @property
    def llm_ready(self) -> bool:
        return bool(self.llm_base_url and self.api_key)

    @property
    def catalog_file(self) -> Path | None:
        return self.get("catalog_file")

    @property
    def models(self) -> list[str]:
        return self.get("models")

    @property
    def extra_file_types(self) -> list[str]:
        return self.get("extra_file_types")

    @property
    def mark_agent_files(self) -> bool:
        return self.get("mark_agent_files")

    @property
    def schedule_jitter_minutes(self) -> int:
        value = self.get("schedule_jitter_minutes")
        return max(0, min(30, value)) if isinstance(value, int) else 5

    @property
    def max_concurrent_runs(self) -> int:
        value = self.get("max_concurrent_runs")
        return max(1, min(10, value)) if isinstance(value, int) else 2

    def describe(self) -> list[dict[str, Any]]:
        """For the Settings UI: every value with its label, source, and whether the user may edit it."""
        out = []
        for spec in SPECS:
            r = self._values[spec.key]
            value = r.value
            if isinstance(value, Path):
                value = str(value)
            if spec.kind == "secret":
                value = "set" if value else ""  # never send secrets to the UI
            out.append(
                {
                    "key": spec.key,
                    "label": spec.label,
                    "help": spec.help,
                    "value": value,
                    "source": r.source,
                    # Higher layers (.env/env/cli) win over user overrides, so editing would have no effect.
                    "editable": spec.key in USER_OVERRIDABLE and r.source in ("default", "user"),
                }
            )
        return out


def load_config(
    *,
    environ: dict[str, str] | None = None,
    dotenv_path: Path | None | bool = True,
    cli: dict[str, Any] | None = None,
    defaults_path: Path | None = None,
) -> Config:
    """Resolve configuration. `dotenv_path=True` auto-discovers .env; False disables it."""
    environ = dict(os.environ) if environ is None else environ
    cli = {k: v for k, v in (cli or {}).items() if v is not None}
    defaults_path = defaults_path or Path(__file__).with_name("defaults.json")
    defaults = json.loads(defaults_path.read_text(encoding="utf-8"))
    defaults.setdefault("data_dir", str(paths.default_data_dir()))
    defaults.setdefault("debug", False)
    defaults.setdefault("api_key", "")
    defaults.setdefault("reasoning_effort", "low")
    defaults.setdefault("sandbox_warmup", "full")
    defaults.setdefault("extra_file_types", [])
    defaults.setdefault("mark_agent_files", True)
    defaults.setdefault("max_concurrent_runs", 2)
    defaults.setdefault("schedule_jitter_minutes", 5)

    if dotenv_path is True:
        dotenv_path = find_dotenv()
    elif dotenv_path is False:
        dotenv_path = None
    dotenv = parse_dotenv(dotenv_path.read_text(encoding="utf-8")) if dotenv_path else {}
    dotenv_dir = dotenv_path.parent if dotenv_path else None

    def resolve(spec: Spec, user: dict[str, str]) -> Resolved:
        env_key = ENV_PREFIX + spec.key.upper()
        if spec.key in cli:
            return Resolved(_coerce(spec, cli[spec.key], Path.cwd()), "cli")
        if environ.get(env_key, "") != "":
            return Resolved(_coerce(spec, environ[env_key], Path.cwd()), "env")
        if dotenv.get(env_key, "") != "":
            return Resolved(_coerce(spec, dotenv[env_key], dotenv_dir), ".env")
        if spec.key in user:
            return Resolved(_coerce(spec, user[spec.key], None), "user")
        return Resolved(_coerce(spec, defaults.get(spec.key, ""), None), "default")

    # data_dir first: user overrides live inside it.
    values = {"data_dir": resolve(SPEC_BY_KEY["data_dir"], {})}
    user = UserSettings(values["data_dir"].value).overrides
    for spec in SPECS:
        if spec.key != "data_dir":
            values[spec.key] = resolve(spec, user)
    return Config(values, dotenv_path if isinstance(dotenv_path, Path) else None)
