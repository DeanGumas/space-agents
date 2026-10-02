"""Static checks on the front end: CSP/import map in sync, vendored files intact, referenced assets exist."""

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "ui"


def run_script(*args):
    return subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True, text=True)


def test_csp_hash_matches_import_map():
    r = run_script("scripts/update_csp.py", "--check")
    assert r.returncode == 0, r.stdout + r.stderr


def test_vendored_files_match_lock():
    r = run_script("scripts/vendor.py", "--check")
    assert r.returncode == 0, r.stdout + r.stderr


def test_import_map_targets_exist():
    html = (UI / "index.html").read_text()
    imports = json.loads(re.search(r'<script type="importmap">(.*?)</script>', html, re.S).group(1))["imports"]
    for target in imports.values():
        assert (UI / target.lstrip("/")).is_file(), target


def test_relative_js_imports_resolve():
    for js in (UI / "js").rglob("*.js"):
        for spec in re.findall(r'from\s+"(\.[^"]+)"', js.read_text()):
            assert (js.parent / spec).resolve().is_file(), f"{js.relative_to(ROOT)} imports missing {spec}"


def test_icons_exist():
    """Every literal icon name used in the UI must exist in the vendored Lucide set."""
    names = set()
    for js in (UI / "js").rglob("*.js"):
        src = js.read_text()
        names |= set(re.findall(r'Icon\} name="([a-z0-9-]+)"', src))
        names |= set(re.findall(r'icon: "([a-z0-9-]+)"', src))
    names |= {"folder-pen", "folder", "circle-alert", "circle-check", "info"}  # used in expressions
    missing = sorted(n for n in names if not (UI / "vendor" / "lucide" / f"{n}.svg").is_file())
    assert not missing, missing


def test_templates_reference_known_dev_catalog_servers():
    catalog = {s["id"] for s in json.loads((ROOT / "dev" / "catalog.json").read_text())["servers"]}
    for path in (ROOT / "templates" / "agents").glob("*.json"):
        for grant in json.loads(path.read_text()).get("mcp_servers", []):
            assert grant["id"] in catalog, f"{path.name}: unknown server {grant['id']}"


def test_window_background_matches_tokens():
    from space_agents.main import WINDOW_BG

    tokens = (UI / "css" / "tokens.css").read_text()
    light, dark = tokens.split(':root[data-theme="dark"]')[:2]
    assert f"--bg: {WINDOW_BG['light'].lower()};" in light.lower()
    assert f"--bg: {WINDOW_BG['dark'].lower()};" in dark.lower()
