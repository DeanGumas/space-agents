"""The app's local web server (127.0.0.1, random port), passed to pywebview as a WSGI app.

Serves:
- ui/ static files (with correct types for .mjs/.wasm and no caching)
- the sandbox worker with its own strict Content-Security-Policy header: a Web Worker gets its CSP from its HTTP
  response, not from the page, and this is what keeps sandboxed code off the network
- /workspace/<token>/in|out/<name>: per-run file transfer for the Pyodide sandbox, valid only while that run is active

Nothing else is reachable. App actions go through the pywebview bridge, not HTTP.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path

import bottle

from space_agents.sandbox.pyodide import MAX_OUTPUT_BYTES, PyodideSandbox

# Sandboxed code may only talk back to this server (inputs, outputs, packages). 'unsafe-eval' is for Pyodide's
# runtime, which compiles small JS shims; 'wasm-unsafe-eval' compiles the WebAssembly interpreter.
# Don't add `worker-src 'none'`: WebKit applies it to a module worker's own imports, so Pyodide can't load. Without
# it, nested workers fall back to script-src 'self' (our own files only), which is fine.
WORKER_CSP = (
    "default-src 'none'; script-src 'self' 'unsafe-eval' 'wasm-unsafe-eval'; connect-src 'self'; "
    "base-uri 'none'; form-action 'none'"
)

mimetypes.add_type("text/javascript", ".mjs")
mimetypes.add_type("application/wasm", ".wasm")


def make_app(ui_root: Path, sandbox: PyodideSandbox) -> bottle.Bottle:
    app = bottle.Bottle()
    ui_root = ui_root.resolve()

    # Note: static_file() returns its own response object, so headers must be passed to it (headers set on
    # bottle.response beforehand would be discarded).
    base_headers = {"Cache-Control": "no-cache, no-store, must-revalidate", "X-Content-Type-Options": "nosniff"}

    @app.get("/workspace/<token>/in/<name>")
    def workspace_in(token: str, name: str):
        path = sandbox.input_path(token, name)
        if path is None:
            bottle.abort(404)
        return bottle.static_file(
            path.name, root=str(path.parent), mimetype="application/octet-stream", headers=dict(base_headers)
        )

    @app.put("/workspace/<token>/out/<name>")
    def workspace_out(token: str, name: str):
        length = int(bottle.request.content_length or 0)
        if length > MAX_OUTPUT_BYTES:
            bottle.abort(413, "too large")
        error = sandbox.accept_output(token, name, bottle.request.body.read(MAX_OUTPUT_BYTES + 1))
        if error:
            bottle.abort(403, error)
        return "ok"

    @app.get("/")
    def index():
        return asset("index.html")

    @app.get("/<path:path>")
    def asset(path: str):
        headers = dict(base_headers)
        if path.startswith("sandbox/"):
            headers["Content-Security-Policy"] = WORKER_CSP
        return bottle.static_file(path, root=str(ui_root), headers=headers)

    return app
