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
    kind: str  # "url" | "bool" | "path" | "list" | "str"
    label: str
    help: str


SPECS: tuple[Spec, ...] = (
    Spec("llm_base_url", "url", "Model gateway URL", "Base for /chat/completions and /models, e.g. https://site/api"),
    Spec("mcp_base_url", "url", "Tool server URL", "Base for the /mcp catalog and /mcp/{id} servers"),
    Spec("data_dir", "path", "Data folder", "Where agents, settings, and history are stored"),
    Spec("debug", "bool", "Debug mode", "Enables webview devtools and verbose logging"),
    Spec("catalog_file", "path", "Offline catalog file", "Dev only: JSON file used instead of GET /mcp"),
    Spec("models", "list", "Offline model list", "Dev only: comma-separated models used instead of GET /models"),
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
        return str(raw).strip().rstrip("/")
    return str(raw)


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
    def catalog_file(self) -> Path | None:
        return self.get("catalog_file")

    @property
    def models(self) -> list[str]:
        return self.get("models")

    def describe(self) -> list[dict[str, Any]]:
        """For the Settings UI: every value with its label, source, and whether the user may edit it."""
        out = []
        for spec in SPECS:
            r = self._values[spec.key]
            value = r.value
            if isinstance(value, Path):
                value = str(value)
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
