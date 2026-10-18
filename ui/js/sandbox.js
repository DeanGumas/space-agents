// Main-thread side of the Pyodide sandbox: owns the Web Worker and relays jobs/results between it and Python.
// Python calls window.__sandbox.run(job) / .kill(runId) via run_js; results go back through call("sandbox_done").
// Messages from the worker are untrusted (the worker runs model-written code), so they're validated here and only
// forwarded for run ids we actually started.

import { call, reportError } from "./api.js";

let worker = null;
const pending = new Map(); // run_id -> job
let warmPackages = null; // set by warm(); a replacement worker re-warms with the same list

function str(v, max) {
  return typeof v === "string" ? v.slice(0, max) : "";
}

function finish(runId, result) {
  if (!pending.delete(runId)) return;
  call("sandbox_done", runId, result).catch(reportError);
}

function ensureWorker() {
  if (worker) return worker;
  worker = new Worker("/sandbox/worker.js", { type: "module" });
  worker.onmessage = (event) => {
    const msg = event.data;
    if (msg && msg.type === "warm") {
      const info = { ms: typeof msg.ms === "number" ? msg.ms : null, error: msg.error ? str(String(msg.error), 500) : null };
      call("sandbox_warmed", info).catch(reportError);
      return;
    }
    if (!msg || msg.type !== "result" || typeof msg.run_id !== "string" || !pending.has(msg.run_id)) return;
    finish(msg.run_id, {
      ok: msg.ok === true,
      stdout: str(msg.stdout, 25000),
      stderr: str(msg.stderr, 25000),
      error: msg.error == null ? null : str(String(msg.error), 8000),
      outputs: Array.isArray(msg.outputs) ? msg.outputs.filter((n) => typeof n === "string").slice(0, 50) : [],
    });
  };
  worker.onerror = (event) => {
    const error = `The Python sandbox failed to start: ${event.message || "unknown error"}`;
    for (const runId of [...pending.keys()]) finish(runId, { ok: false, error, outputs: [] });
    kill();
  };
  return worker;
}

function kill(runId) {
  if (worker) worker.terminate();
  worker = null;
  // Terminating stops every queued run; report the others as interrupted (the killed one already timed out).
  for (const id of [...pending.keys()]) {
    if (id !== runId) finish(id, { ok: false, error: "Interrupted because another run was stopped.", outputs: [] });
  }
  if (runId) pending.delete(runId);
  // Keep the sandbox ready: warm a replacement worker in the background.
  // (If a new run arrives first, the warm-up just queues behind it; loading is idempotent.)
  if (warmPackages) setTimeout(() => warm(warmPackages), 500);
}

function warm(packages) {
  warmPackages = Array.isArray(packages) ? packages.filter((p) => typeof p === "string") : [];
  ensureWorker().postMessage({ type: "warm", packages: warmPackages });
}

window.__sandbox = {
  run(job) {
    pending.set(job.run_id, job);
    ensureWorker().postMessage({ type: "run", ...job });
  },
  kill,
  warm,
};
