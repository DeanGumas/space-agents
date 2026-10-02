// Thin wrapper over the pywebview bridge (space_agents/bridge.py).
//
//   await call("save_agent", payload)  -> resolves with `data`, or throws ApiError (with per-field errors)
//   onEvent(fn)                        -> receive pushed events ({type, ...}) from the Python core

export class ApiError extends Error {
  constructor(message, fields = {}) {
    super(message);
    this.name = "ApiError";
    this.fields = fields;
  }
}

const bridgeReady = new Promise((resolve) => {
  const isReady = () => window.pywebview && window.pywebview.api && window.pywebview.api.bootstrap;
  if (isReady()) return resolve();
  window.addEventListener("pywebviewready", () => resolve(), { once: true });
});

export const whenBridgeReady = bridgeReady;

export async function call(name, ...args) {
  await bridgeReady;
  const fn = window.pywebview.api[name];
  if (typeof fn !== "function") throw new ApiError(`Unknown API method: ${name}`);
  let res;
  try {
    res = await fn(...args);
  } catch (e) {
    throw new ApiError(e?.message || String(e));
  }
  if (!res || res.ok !== true) throw new ApiError(res?.error || "Unknown error", res?.fields || {});
  return res.data;
}

const listeners = new Set();

export function onEvent(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

// Called by the Python EventBus with a batch (array) of events.
window.__spaceEvents = (batch) => {
  for (const event of batch) {
    for (const fn of listeners) {
      try {
        fn(event);
      } catch (e) {
        reportError(e);
      }
    }
  }
};

// Uncaught errors are collected so the --smoke-test run can fail on them.
window.__spaceErrors = window.__spaceErrors || [];
export function reportError(e) {
  window.__spaceErrors.push(String(e?.stack || e));
  console.error(e);
}
window.addEventListener("error", (e) => reportError(e.error || e.message));
window.addEventListener("unhandledrejection", (e) => reportError(e.reason));
