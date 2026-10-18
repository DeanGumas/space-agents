"""Host side of the Pyodide sandbox (PLAN.md §2.9).

    run(code, inputs) ──► copy inputs into workspaces/<token>/in, mint a one-run token
                      ──► run_js("window.__sandbox.run({...})")      (UI main thread → Web Worker)
    worker            ──► GET  /workspace/<token>/in/<name>          (server.py, token-checked)
                      ──► runs code in Pyodide (no disk, no network beyond these routes)
                      ──► PUT  /workspace/<token>/out/<name>
                      ──► bridge.sandbox_done(run_id, result)        ──► complete() resolves the waiting future
    caller            ◄── RunResult(outputs=[Path, ...]); calls release() when done with the output files

Python code is never executed on the host. A timeout terminates the worker (the UI respawns it on next use).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SAFE_NAME = re.compile(r"^[^/\\:\x00]{1,200}$")
MAX_OUTPUT_BYTES = 100 * 1024 * 1024
MAX_OUTPUT_FILES = 20
DEFAULT_TIMEOUT = 90.0  # includes first-use load of Pyodide + packages
# Warm-up modes (setting `sandbox_warmup`): packages pre-imported at launch. Measured on macOS: the sandbox costs
# ~420 MB once pandas is loaded, which happens on first pandas use anyway; warm-up just pays it up front.
#   full  → ~420 MB idle, first pandas+openpyxl run ~0.5 s     light → ~210 MB idle, openpyxl instant, pandas ~8 s
# matplotlib (charts) always loads on demand.
WARM_MODES = {"full": ["openpyxl", "pandas"], "light": ["openpyxl"], "off": None}
WARM_PACKAGES = WARM_MODES["full"]


def warm_packages(mode: str) -> list[str] | None:
    """Setting value → package list (None = no warm-up). Also accepts true/false-style values."""
    mode = str(mode).strip().lower()
    if mode in ("0", "false", "no", "none", "off", ""):
        return None
    if mode in ("1", "true", "yes", "on"):
        return WARM_MODES["full"]
    return WARM_MODES.get(mode, WARM_MODES["full"])


def safe_name(name: str) -> bool:
    return bool(SAFE_NAME.match(name)) and name not in (".", "..") and not name.startswith(".")


class SandboxUnavailable(RuntimeError):
    pass


@dataclass
class RunResult:
    ok: bool
    stdout: str = ""
    stderr: str = ""
    error: str | None = None
    outputs: list[Path] = field(default_factory=list)
    duration_s: float = 0.0
    token: str = ""


@dataclass
class _Run:
    token: str
    dir: Path
    future: asyncio.Future
    loop: asyncio.AbstractEventLoop
    accepting_outputs: bool = True
    output_bytes: int = 0


class PyodideSandbox:
    def __init__(self, work_root: Path):
        self.work_root = work_root
        self._run_js: Callable[[str], Any] | None = None
        self._runs: dict[str, _Run] = {}  # run_id -> run
        self._tokens: dict[str, _Run] = {}  # token -> run (only while active)
        shutil.rmtree(work_root, ignore_errors=True)  # leftovers from a previous session
        work_root.mkdir(parents=True, exist_ok=True)

    def attach(self, run_js: Callable[[str], Any]) -> None:
        """Called once the UI is ready: run_js executes JavaScript in the main window."""
        self._run_js = run_js

    def warm(self, packages: list[str] | None = None) -> None:
        """Boot Pyodide and pre-import common packages in the background, so the first run is fast."""
        if self._run_js is None:
            return
        packages = WARM_PACKAGES if packages is None else packages
        self._run_js(f"window.__sandbox && window.__sandbox.warm({json.dumps(packages)})")

    @property
    def available(self) -> bool:
        return self._run_js is not None

    # --- used by server.py ----------------------------------------------------------------------------------------

    def input_path(self, token: str, name: str) -> Path | None:
        run = self._tokens.get(token)
        if run is None or not safe_name(name):
            return None
        path = run.dir / "in" / name
        return path if path.is_file() else None

    def accept_output(self, token: str, name: str, data: bytes) -> str | None:
        """Store an output file from the worker. Returns an error message, or None on success."""
        run = self._tokens.get(token)
        if run is None or not run.accepting_outputs:
            return "unknown or finished run"
        if not safe_name(name):
            return "invalid file name"
        out = run.dir / "out"
        if len(list(out.iterdir())) >= MAX_OUTPUT_FILES:
            return "too many output files"
        if run.output_bytes + len(data) > MAX_OUTPUT_BYTES:
            return "outputs too large"
        (out / name).write_bytes(data)
        run.output_bytes += len(data)
        return None

    # --- used by the bridge -----------------------------------------------------------------------------------------

    def complete(self, run_id: str, payload: Any) -> None:
        run = self._runs.get(run_id)
        if run is None or not isinstance(payload, dict):
            return
        run.loop.call_soon_threadsafe(lambda: run.future.done() or run.future.set_result(payload))

    # --- used by tools ----------------------------------------------------------------------------------------------

    async def run(
        self,
        code: str,
        inputs: dict[str, Path],
        timeout: float = DEFAULT_TIMEOUT,
        aliases: dict[str, str] | None = None,
    ) -> RunResult:
        """Run `code` with `inputs` (name → host file) copied to ./in/<name>.

        `aliases` maps extra in-sandbox paths to input names (e.g. the host path the code literally mentions), so
        code that opens a file "where it is" finds a copy there. If the code overwrites an input (at ./in/<name> or
        an alias) without writing ./out/<name>, the modified copy is returned as output <name>.
        """
        if self._run_js is None:
            raise SandboxUnavailable("The Python sandbox isn't ready yet (the app window must be open).")
        for name in inputs:
            if not safe_name(name):
                raise ValueError(f"Invalid input file name: {name!r}")
        run_id, token = secrets.token_hex(8), secrets.token_urlsafe(24)
        run_dir = self.work_root / token
        (run_dir / "in").mkdir(parents=True)
        (run_dir / "out").mkdir()
        for name, src in inputs.items():
            shutil.copy2(src, run_dir / "in" / name)
        loop = asyncio.get_running_loop()
        run = _Run(token, run_dir, loop.create_future(), loop)
        self._runs[run_id] = run
        self._tokens[token] = run
        aliases = {path: name for path, name in (aliases or {}).items() if name in inputs}
        job = {"run_id": run_id, "token": token, "code": code, "inputs": sorted(inputs), "aliases": aliases}
        started = time.monotonic()
        try:
            self._run_js(f"window.__sandbox && window.__sandbox.run({json.dumps(job)})")
            try:
                payload = await asyncio.wait_for(run.future, timeout)
            except TimeoutError:
                self._run_js(f"window.__sandbox && window.__sandbox.kill({json.dumps(run_id)})")
                return RunResult(
                    ok=False,
                    error=f"Timed out after {timeout:.0f} seconds. The code was stopped.",
                    duration_s=time.monotonic() - started,
                    token=token,
                )
        finally:
            run.accepting_outputs = False
            self._tokens.pop(token, None)
            self._runs.pop(run_id, None)
            shutil.rmtree(run_dir / "in", ignore_errors=True)
        outputs = sorted(p for p in (run_dir / "out").iterdir() if p.is_file())
        return RunResult(
            ok=bool(payload.get("ok")),
            stdout=str(payload.get("stdout") or "")[:20_000],
            stderr=str(payload.get("stderr") or "")[:10_000],
            error=str(payload["error"])[:6_000] if payload.get("error") else None,
            outputs=outputs,
            duration_s=time.monotonic() - started,
            token=token,
        )

    def release(self, result: RunResult) -> None:
        """Delete a finished run's leftover output files (call after moving the ones you want)."""
        if result.token:
            shutil.rmtree(self.work_root / result.token, ignore_errors=True)
