// Pyodide sandbox worker (PLAN.md §2.9). Runs untrusted, model-written Python.
//
// Isolation comes from the platform, not from this code:
// - this worker has its own CSP (sent by space_agents/server.py): connect-src 'self', so no network beyond our
//   local server, where only this run's token-scoped workspace routes accept anything
// - Pyodide's filesystem is in-memory; the host disk is not reachable
// - workers can't reach window.pywebview (the bridge), and the main thread validates every message we send
// - the host enforces a timeout by terminating this worker
//
// Protocol: {type: "run", run_id, token, code, inputs: [names]}  →  {type: "result", run_id, ok, stdout, stderr,
// error, outputs: [names]}

import { loadPyodide } from "/vendor/pyodide/pyodide.mjs";

const MAX_STREAM_CHARS = 20000;
let pyodidePromise = null;

function getPyodide() {
  pyodidePromise ??= loadPyodide({ indexURL: "/vendor/pyodide/", fullStdLib: false });
  return pyodidePromise;
}

function cleanTraceback(text) {
  // Drop Pyodide's own frames ("File \"/lib/python…/_pyodide/…\"" plus the source line under each) so the model
  // sees only its code's frames and the error.
  const lines = String(text).split("\n");
  const out = [];
  for (let i = 0; i < lines.length; i++) {
    if (/^\s+File "\/lib\/python[^"]*\/(_pyodide|pyodide)\//.test(lines[i])) {
      if (i + 1 < lines.length && /^\s{4,}\S/.test(lines[i + 1])) i++;
      continue;
    }
    out.push(lines[i]);
  }
  return out.slice(-25).join("\n").trim();
}

async function run(job) {
  const py = await getPyodide();
  let stdout = "";
  let stderr = "";
  const cap = (s) => (s.length > MAX_STREAM_CHARS ? s.slice(0, MAX_STREAM_CHARS) + "\n…(truncated)" : s);
  py.setStdout({ batched: (s) => { if (stdout.length < MAX_STREAM_CHARS) stdout += s + "\n"; } });
  py.setStderr({ batched: (s) => { if (stderr.length < MAX_STREAM_CHARS) stderr += s + "\n"; } });

  // Fresh working directory for every run: /work/in (inputs), /work/out (files to hand back).
  py.runPython(
    "import os, shutil\n" +
      "shutil.rmtree('/work', ignore_errors=True)\n" +
      "os.makedirs('/work/in'); os.makedirs('/work/out'); os.chdir('/work')",
  );
  // Inputs live at /work/in/<name>; aliases put extra copies where the code expects them (e.g. the host path it
  // mentions). `tracked` remembers originals so in-place edits can be returned as outputs.
  const tracked = new Map(); // sandbox path -> {name, bytes}
  for (const name of job.inputs) {
    const resp = await fetch(`/workspace/${job.token}/in/${encodeURIComponent(name)}`);
    if (!resp.ok) throw new Error(`Couldn't load input file ${name} (${resp.status})`);
    const bytes = new Uint8Array(await resp.arrayBuffer());
    py.FS.writeFile(`/work/in/${name}`, bytes);
    tracked.set(`/work/in/${name}`, { name, bytes });
  }
  for (const [rawPath, name] of Object.entries(job.aliases || {})) {
    const original = tracked.get(`/work/in/${name}`);
    if (!original) continue;
    const path = rawPath.startsWith("/") ? rawPath : `/work/${rawPath}`;
    try {
      py.FS.mkdirTree(path.slice(0, path.lastIndexOf("/")) || "/");
      py.FS.writeFile(path, original.bytes);
      tracked.set(path, { name, bytes: original.bytes });
    } catch (e) {
      // Unusual paths that can't exist in the virtual filesystem are skipped; ./in/<name> still works.
    }
  }

  // Load only allow-listed packages the code imports (the trimmed lock file has nothing else).
  const quiet = { messageCallback: () => {}, errorCallback: () => {} };
  await py.loadPackagesFromImports(job.code, quiet);
  // Optional dependencies that libraries import lazily (so they aren't detected from the code's own imports).
  const companions = [];
  if (/\bpandas\b/.test(job.code)) {
    companions.push("openpyxl", "xlsxwriter"); // pd.read_excel / to_excel engines
    if (/\.plot\(|\.hist\(|matplotlib/.test(job.code)) companions.push("matplotlib");
  }
  if (companions.length) await py.loadPackage(companions, quiet);

  let ok = false;
  let error = null;
  const globals = py.globals.get("dict")();
  try {
    await py.runPythonAsync(job.code, { globals });
    ok = true;
  } catch (e) {
    error = cleanTraceback(e && e.message ? e.message : e);
  } finally {
    globals.destroy();
  }

  // Outputs: files written to /work/out (skipping pre-placed copies the code didn't change), plus inputs/aliases
  // edited in place when no ./out/<name> was written for them.
  const outFiles = new Map(); // name -> bytes
  for (const name of py.FS.readdir("/work/out")) {
    if (name === "." || name === "..") continue;
    const path = `/work/out/${name}`;
    if (!py.FS.isFile(py.FS.stat(path).mode)) continue;
    const bytes = py.FS.readFile(path);
    const placed = tracked.get(path);
    if (placed && sameBytes(bytes, placed.bytes)) continue;
    outFiles.set(name, bytes);
  }
  for (const [path, { name, bytes }] of tracked) {
    if (path.startsWith("/work/out/") || outFiles.has(name)) continue;
    let now;
    try {
      now = py.FS.readFile(path);
    } catch (e) {
      continue;
    }
    if (!sameBytes(now, bytes)) outFiles.set(name, now);
  }

  const outputs = [];
  for (const [name, bytes] of outFiles) {
    const resp = await fetch(`/workspace/${job.token}/out/${encodeURIComponent(name)}`, { method: "PUT", body: bytes });
    if (resp.ok) outputs.push(name);
    else stderr += `Could not save ${name}: ${await resp.text()}\n`;
  }
  return { ok, stdout: cap(stdout), stderr: cap(stderr), error, outputs };
}

function sameBytes(a, b) {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
  return true;
}

let queue = Promise.resolve();

// Warm-up: boot Pyodide and import the usual document libraries ahead of time, so the first real run doesn't pay
// for it. Goes through the same queue as runs, so a run that arrives early simply waits for the warm-up to finish.
async function warm(packages) {
  const started = performance.now();
  const py = await getPyodide();
  await py.loadPackage(packages, { messageCallback: () => {}, errorCallback: () => {} });
  const modules = packages.map((p) => ({ "python-docx": "docx" })[p] || p.replace(/-/g, "_"));
  py.runPython(modules.map((m) => `import ${m}`).join("\n"));
  return Math.round(performance.now() - started);
}

self.onmessage = (event) => {
  const job = event.data;
  if (job && job.type === "warm") {
    queue = queue.then(async () => {
      try {
        self.postMessage({ type: "warm", ms: await warm(job.packages || []) });
      } catch (e) {
        self.postMessage({ type: "warm", error: String(e && e.message ? e.message : e) });
      }
    });
    return;
  }
  if (!job || job.type !== "run") return;
  // One run at a time: Pyodide is single-threaded and runs share /work.
  queue = queue.then(async () => {
    let result;
    try {
      result = await run(job);
    } catch (e) {
      result = { ok: false, stdout: "", stderr: "", error: String(e && e.message ? e.message : e), outputs: [] };
    }
    self.postMessage({ type: "result", run_id: job.run_id, ...result });
  });
};

self.postMessage({ type: "hello" });
