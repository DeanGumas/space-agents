"""`--sandbox-selftest`: prove the Pyodide sandbox works in the real webview on this machine (use in CI on both OSes).

Checks: openpyxl round-trip through copy-in/copy-out, no network, no host filesystem, timeout kills the worker, and
the worker comes back afterwards.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Any

from space_agents.sandbox.pyodide import PyodideSandbox

log = logging.getLogger(__name__)

ROUND_TRIP = """
import csv, openpyxl
rows = list(csv.reader(open("in/data.csv")))
wb = openpyxl.Workbook()
ws = wb.active
ws.title = "Data"
for r in rows:
    ws.append([int(x) if x.isdigit() else x for x in r])
wb.save("out/report.xlsx")
check = openpyxl.load_workbook("out/report.xlsx")
print("total", sum(c.value for c in check["Data"]["B"][1:]))
"""

# A CSP-blocked fetch rejects in Chromium (WebView2) but never settles in WebKit, so bound it with a timeout: only an
# actual response counts as "open". Same-origin fetch must still work (that's how inputs/outputs move).
NETWORK = """
import asyncio
from js import fetch
try:
    r = await asyncio.wait_for(fetch("https://example.com/"), 5)
    print("NETWORK_OPEN", r.status)
except Exception as e:
    print("NETWORK_BLOCKED", type(e).__name__)
r = await asyncio.wait_for(fetch("/index.html"), 5)
print("SELF_OK" if r.status == 200 else "SELF_FAILED")
"""

# Paths that exist on the host but can't exist in Pyodide's in-memory filesystem (which has its own /home/pyodide).
HOST_FS = """
import os
paths = {paths!r}
print("HOST_FS_VISIBLE" if any(os.path.exists(p) for p in paths) else "HOST_FS_HIDDEN")
"""


async def run_selftest(sandbox: PyodideSandbox) -> dict[str, Any]:
    results: dict[str, Any] = {}
    with tempfile.TemporaryDirectory() as tmp:
        csv_path = Path(tmp) / "data.csv"
        csv_path.write_text("item,qty\nbolts,10\nnuts,32\n")
        r = await sandbox.run(ROUND_TRIP, {"data.csv": csv_path}, timeout=180)
        out = r.outputs[0].read_bytes() if r.outputs else b""
        results["round_trip"] = {
            "pass": r.ok and "total 42" in r.stdout and out.startswith(b"PK"),
            "seconds_including_cold_start": round(r.duration_s, 2),
            "error": r.error,
        }
        sandbox.release(r)

    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "notes.txt"
        src.write_text("v1")
        alias = str(Path(tmp, "notes.txt"))  # code opens the file by its host path
        code = f"p = {alias!r}\nassert open(p).read() == 'v1'\nopen(p, 'w').write('v2')"
        r = await sandbox.run(code, {"notes.txt": src}, aliases={alias: "notes.txt", "out/notes.txt": "notes.txt"})
        got = {p.name: p.read_text() for p in r.outputs}
        results["in_place_edit_via_alias"] = {"pass": r.ok and got == {"notes.txt": "v2"}, "error": r.error}
        sandbox.release(r)
        r = await sandbox.run(
            "print(open('out/notes.txt').read())", {"notes.txt": src}, aliases={"out/notes.txt": "notes.txt"}
        )
        results["unchanged_copies_not_returned"] = {"pass": r.ok and "v1" in r.stdout and not r.outputs}
        sandbox.release(r)

    pandas_code = (
        "import pandas as pd\n"
        "pd.DataFrame({'po': [1, 2], 'qty': [10, 32]}).to_excel('out/t.xlsx', index=False)\n"
        "print('rows', len(pd.read_excel('out/t.xlsx')))"
    )
    r = await sandbox.run(pandas_code, {}, timeout=120)
    results["pandas_excel_engines"] = {"pass": r.ok and "rows 2" in r.stdout, "error": r.error}
    sandbox.release(r)

    r = await sandbox.run("print('warm')", {})
    results["warm_run"] = {"pass": r.ok and "warm" in r.stdout, "seconds": round(r.duration_s, 2)}

    r = await sandbox.run(NETWORK, {})
    results["network_blocked"] = {
        "pass": "NETWORK_BLOCKED" in r.stdout and "SELF_OK" in r.stdout,
        "stdout": r.stdout.strip(),
        "error": r.error,
    }

    host_paths = [str(Path.home().resolve()), str(sandbox.work_root.resolve())]
    r = await sandbox.run(HOST_FS.format(paths=host_paths), {})
    results["host_fs_hidden"] = {"pass": "HOST_FS_HIDDEN" in r.stdout, "stdout": r.stdout.strip(), "error": r.error}

    r = await sandbox.run("while True:\n    pass\n", {}, timeout=5)
    results["timeout_kills"] = {"pass": not r.ok and "Timed out" in (r.error or ""), "error": r.error}

    r = await sandbox.run("print('back')", {}, timeout=180)
    results["recovers_after_kill"] = {"pass": r.ok and "back" in r.stdout, "seconds": round(r.duration_s, 2)}

    results["pass"] = all(v["pass"] for v in results.values() if isinstance(v, dict))
    return results
