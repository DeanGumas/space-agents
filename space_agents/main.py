"""Entry point: resolve config, open storage, start the core thread, and run the pywebview window."""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import sys
import threading
from pathlib import Path
from typing import Any

import webview

from space_agents import __version__, paths
from space_agents.bridge import Api
from space_agents.changes import ChangeManager
from space_agents.config import Config, load_config
from space_agents.core import Core, EventBus
from space_agents.documents.library import DocumentLibrary
from space_agents.platform.appearance import system_prefers_dark
from space_agents.runtime.loop import AgentRunner
from space_agents.sandbox.pyodide import PyodideSandbox
from space_agents.server import make_app
from space_agents.settings import UserSettings
from space_agents.store import Store

log = logging.getLogger("space_agents")

WINDOW_BG = {"light": "#FFFFFF", "dark": "#1A1D21"}  # must match --bg in ui/css/tokens.css


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="space-agents", description="Space Agents desktop app")
    p.add_argument("--debug", action="store_true", default=None, help="enable devtools and verbose logging")
    p.add_argument("--data-dir", type=Path, help="use this data folder (handy for testing with throwaway data)")
    p.add_argument(
        "--sandbox-selftest",
        action="store_true",
        help="open the window, run the Pyodide sandbox checks, print results, then exit (0 = success)",
    )
    p.add_argument(
        "--smoke-test",
        action="store_true",
        help="open the window, wait for the UI to render, verify it, then exit (0 = success)",
    )
    return p.parse_args(argv)


def setup_logging(config: Config) -> Path:
    log_dir = paths.default_log_dir() if config.source("data_dir") == "default" else config.data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    level = logging.DEBUG if config.debug else logging.INFO
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "space-agents.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(file_handler)
    root.addHandler(console)
    if not config.debug:
        logging.getLogger("pywebview").setLevel(logging.WARNING)
    return log_dir


def resolve_theme(theme: str) -> str:
    if theme in ("light", "dark"):
        return theme
    return "dark" if system_prefers_dark() else "light"


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:  # use the OS certificate store for HTTPS (corporate CAs)
        import truststore

        truststore.inject_into_ssl()
    except ImportError:
        pass
    cli: dict[str, Any] = {"debug": args.debug, "data_dir": args.data_dir}
    config = load_config(cli=cli)
    config.data_dir.mkdir(parents=True, exist_ok=True)
    log_dir = setup_logging(config)
    log.info("Space Agents %s starting (data: %s, logs: %s)", __version__, config.data_dir, log_dir)
    if config.dotenv_path:
        log.info("Loaded %s", config.dotenv_path)

    store = Store(config.data_dir)
    store.recover_interrupted()
    settings = UserSettings(config.data_dir)
    core = Core()
    core.start()
    events = EventBus(core)
    events.start()
    sandbox = PyodideSandbox(config.data_dir / "workspaces")
    docs = DocumentLibrary(store)
    changes = ChangeManager(store, config.data_dir, events, mark_files=config.mark_agent_files)
    changes.prune_history()
    runner = AgentRunner(store=store, events=events, docs=docs, sandbox=sandbox, changes=changes, config=config)
    if config.llm_ready:
        log.info("Model gateway: %s (default model: %s)", config.llm_base_url, (config.models or ["(none)"])[0])

    exit_code = {"value": 0}
    on_ui_ready = None
    if args.smoke_test:
        exit_code["value"] = 1  # until the UI proves itself
        on_ui_ready = _smoke_test_checker(exit_code)
    elif args.sandbox_selftest:
        exit_code["value"] = 1
        on_ui_ready = _sandbox_selftest(core, sandbox, exit_code)

    api = Api(
        config=config,
        reload_config=lambda: load_config(cli=cli),
        store=store,
        settings=settings,
        core=core,
        events=events,
        sandbox=sandbox,
        runner=runner,
        changes=changes,
        on_ui_ready=on_ui_ready,
    )
    window = webview.create_window(
        "Space Agents",
        url=make_app(paths.ui_dir(), sandbox),  # our WSGI app on 127.0.0.1 (see server.py)
        js_api=api,
        width=1280,
        height=820,
        min_size=(960, 620),
        background_color=WINDOW_BG[resolve_theme(settings.theme)],
        text_select=True,
    )
    api._attach_window(window)
    if args.smoke_test or args.sandbox_selftest:
        # Fail (exit 1) if the UI never reports ready / the checks never finish.
        limit = 30 if args.smoke_test else 300
        timer = threading.Timer(limit, lambda: (log.error("Self-test timed out"), window.destroy()))
        timer.daemon = True
        timer.start()

    # Debug mode enables devtools (right-click → Inspect) without popping them open on every launch.
    webview.settings["OPEN_DEVTOOLS_IN_DEBUG"] = False
    try:
        webview.start(debug=config.debug, private_mode=True)
    finally:
        core.stop()
        docs.close()
        store.close()
        log.info("Space Agents stopped")
    return exit_code["value"]


def _smoke_test_checker(exit_code: dict[str, int]):
    def check(info: dict[str, Any]) -> None:
        def run_checks() -> None:
            window = webview.windows[0]
            try:
                result = window.evaluate_js(
                    "JSON.stringify({sidebar: !!document.querySelector('.sidebar'),"
                    " main: !!document.querySelector('.main'),"
                    " theme: document.documentElement.dataset.resolvedTheme || null,"
                    " errors: (window.__spaceErrors || []).length})"
                )
                log.info("Smoke test UI state: %s (ui_ready info: %s)", result, info)
                state = json.loads(result)
                if state["sidebar"] and state["main"] and state["errors"] == 0:
                    exit_code["value"] = 0
                else:
                    log.error("Smoke test failed: %s", state)
            except Exception:
                log.exception("Smoke test check failed")
            finally:
                window.destroy()

        t = threading.Timer(1.0, run_checks)
        t.daemon = True
        t.start()

    return check


def _sandbox_selftest(core: Core, sandbox: PyodideSandbox, exit_code: dict[str, int]):
    from space_agents.sandbox.selftest import run_selftest

    def start(_info: dict[str, Any]) -> None:
        future = core.submit(run_selftest(sandbox))

        def done(f) -> None:
            try:
                results = f.result()
                print(json.dumps(results, indent=2), flush=True)
                exit_code["value"] = 0 if results["pass"] else 1
            except Exception:
                log.exception("Sandbox self-test crashed")
            finally:
                # Let in-flight bridge calls (e.g. the last sandbox_done) finish before the window goes away.
                threading.Timer(0.5, webview.windows[0].destroy).start()

        future.add_done_callback(done)

    return start


if __name__ == "__main__":
    sys.exit(run())
