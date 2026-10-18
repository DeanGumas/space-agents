import json
from pathlib import Path

from space_agents.config import load_config, parse_dotenv
from space_agents.settings import UserSettings


def make_defaults(tmp_path: Path, **values) -> Path:
    p = tmp_path / "defaults.json"
    p.write_text(json.dumps({"llm_base_url": "https://default/api", "mcp_base_url": "", "models": [], **values}))
    return p


def test_parse_dotenv():
    env = parse_dotenv(
        """
        # comment
        A=1
        export B = two
        C="quoted # not a comment"
        D=value # trailing comment
        E=
        not a pair
        """
    )
    assert env == {"A": "1", "B": "two", "C": "quoted # not a comment", "D": "value", "E": ""}


def test_layer_precedence(tmp_path):
    data = tmp_path / "data"
    UserSettings(data).set_overrides({"llm_base_url": "https://user/api/", "mcp_base_url": "https://user"})
    dotenv = tmp_path / ".env"
    dotenv.write_text("SPACE_AGENTS_MCP_BASE_URL=https://dotenv\n")
    defaults = make_defaults(tmp_path)

    # user beats default; .env beats user
    cfg = load_config(environ={"SPACE_AGENTS_DATA_DIR": str(data)}, dotenv_path=dotenv, defaults_path=defaults)
    assert (cfg.llm_base_url, cfg.source("llm_base_url")) == ("https://user/api", "user")
    assert (cfg.mcp_base_url, cfg.source("mcp_base_url")) == ("https://dotenv", ".env")

    # env beats .env; cli beats env
    cfg = load_config(
        environ={"SPACE_AGENTS_DATA_DIR": str(data), "SPACE_AGENTS_MCP_BASE_URL": "https://env"},
        dotenv_path=dotenv,
        defaults_path=defaults,
        cli={"debug": True, "data_dir": None},
    )
    assert (cfg.mcp_base_url, cfg.source("mcp_base_url")) == ("https://env", "env")
    assert (cfg.debug, cfg.source("debug")) == (True, "cli")
    assert cfg.source("data_dir") == "env"


def test_default_layer_and_types(tmp_path):
    cfg = load_config(
        environ={"SPACE_AGENTS_DATA_DIR": str(tmp_path / "d"), "SPACE_AGENTS_MODELS": "a, b,,c"},
        dotenv_path=False,
        defaults_path=make_defaults(tmp_path),
    )
    assert (cfg.llm_base_url, cfg.source("llm_base_url")) == ("https://default/api", "default")
    assert cfg.models == ["a", "b", "c"]
    assert cfg.debug is False
    assert cfg.catalog_file is None


def test_dotenv_relative_paths_resolve_against_its_folder(tmp_path):
    (tmp_path / "proj").mkdir()
    dotenv = tmp_path / "proj" / ".env"
    dotenv.write_text("SPACE_AGENTS_DATA_DIR=.devdata\nSPACE_AGENTS_CATALOG_FILE=dev/catalog.json\n")
    cfg = load_config(environ={}, dotenv_path=dotenv, defaults_path=make_defaults(tmp_path))
    assert cfg.data_dir == (tmp_path / "proj" / ".devdata").resolve()
    assert cfg.catalog_file == (tmp_path / "proj" / "dev" / "catalog.json").resolve()


def test_describe_marks_editability(tmp_path):
    cfg = load_config(
        environ={"SPACE_AGENTS_DATA_DIR": str(tmp_path), "SPACE_AGENTS_MCP_BASE_URL": "https://env"},
        dotenv_path=False,
        defaults_path=make_defaults(tmp_path),
    )
    by_key = {d["key"]: d for d in cfg.describe()}
    assert by_key["llm_base_url"]["editable"] is True
    assert by_key["mcp_base_url"]["editable"] is False  # env wins, so editing would do nothing
    assert by_key["data_dir"]["editable"] is False
    json.dumps(cfg.describe())  # must be serializable for the bridge


def test_sandbox_warmup_modes(tmp_path):
    from space_agents.sandbox.pyodide import warm_packages

    cfg = load_config(
        environ={"SPACE_AGENTS_DATA_DIR": str(tmp_path)}, dotenv_path=False, defaults_path=make_defaults(tmp_path)
    )
    assert warm_packages(cfg.get("sandbox_warmup")) == ["openpyxl", "pandas"]  # default: full
    assert warm_packages("light") == ["openpyxl"]
    assert warm_packages("off") is None and warm_packages("0") is None and warm_packages("false") is None
    assert warm_packages("1") == ["openpyxl", "pandas"] and warm_packages("bogus") == ["openpyxl", "pandas"]


def test_file_safety_settings(tmp_path):
    cfg = load_config(environ={}, dotenv_path=False, cli={"data_dir": tmp_path})
    assert cfg.extra_file_types == [] and cfg.mark_agent_files is True
    env = {"SPACE_AGENTS_EXTRA_FILE_TYPES": ".py, .cpp", "SPACE_AGENTS_MARK_AGENT_FILES": "0"}
    cfg = load_config(environ=env, dotenv_path=False, cli={"data_dir": tmp_path})
    assert cfg.extra_file_types == [".py", ".cpp"] and cfg.mark_agent_files is False
    assert {"extra_file_types", "mark_agent_files"}.isdisjoint(c["key"] for c in cfg.describe() if c["editable"])


def test_schedule_jitter_setting(tmp_path):
    assert load_config(environ={}, dotenv_path=False, cli={"data_dir": tmp_path}).schedule_jitter_minutes == 5
    env = {"SPACE_AGENTS_SCHEDULE_JITTER_MINUTES": "0", "SPACE_AGENTS_MAX_CONCURRENT_RUNS": "3"}
    cfg = load_config(environ=env, dotenv_path=False, cli={"data_dir": tmp_path})
    assert cfg.schedule_jitter_minutes == 0 and cfg.max_concurrent_runs == 3
    high = load_config(
        environ={"SPACE_AGENTS_SCHEDULE_JITTER_MINUTES": "90"}, dotenv_path=False, cli={"data_dir": tmp_path}
    )
    assert high.schedule_jitter_minutes == 30
    help_text = next(c["help"] for c in cfg.describe() if c["key"] == "schedule_jitter_minutes")
    assert "slow responses" in help_text
