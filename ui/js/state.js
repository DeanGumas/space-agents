// App-wide state (Preact signals) and the actions that change it.
// Components read signals directly (`agents.value`) and re-render automatically when they change.
// All server interaction goes through call() from api.js; pushed events are applied in handleEvent().

import { signal, computed, batch } from "@preact/signals";
import { call, onEvent, ApiError } from "./api.js";
import { applyTheme } from "./lib/theme.js";

export const boot = signal(null); // {version, platform, config, templates, catalog, models, data_dir}
export const bootError = signal(null);
export const agents = signal([]);
export const view = signal({ kind: "welcome" }); // {kind: "welcome"|"activity"|"agent", id?}
export const messages = signal({}); // agentId -> message[]
export const unread = signal({}); // agentId -> count
export const theme = signal("system");
export const dialog = signal(null); // {kind: "new"} | {kind: "editor", agentId?, draft} | {kind: "settings"}
export const toasts = signal([]);

export const selectedAgent = computed(() =>
  view.value.kind === "agent" ? agents.value.find((a) => a.id === view.value.id) || null : null,
);

// ---------- toasts ----------

let toastSeq = 0;
export function toast(text, kind = "info") {
  const id = ++toastSeq;
  toasts.value = [...toasts.value, { id, text, kind }];
  setTimeout(() => (toasts.value = toasts.value.filter((t) => t.id !== id)), kind === "error" ? 6000 : 3500);
}

export function toastError(e) {
  toast(e instanceof ApiError ? e.message : String(e?.message || e), "error");
}

// ---------- boot ----------

export async function init() {
  try {
    const data = await call("bootstrap");
    batch(() => {
      boot.value = data;
      agents.value = data.agents;
      theme.value = data.theme;
      if (data.agents.length) view.value = { kind: "agent", id: data.agents[0].id };
    });
    applyTheme(data.theme);
    if (data.agents.length) await loadMessages(data.agents[0].id);
  } catch (e) {
    bootError.value = e.message || String(e);
    throw e;
  }
}

// ---------- navigation ----------

export async function selectAgent(id) {
  view.value = { kind: "agent", id };
  if (unread.value[id]) unread.value = { ...unread.value, [id]: 0 };
  if (!messages.value[id]) await loadMessages(id);
}

export function showActivity() {
  view.value = { kind: "activity" };
}

async function loadMessages(id) {
  try {
    const list = await call("list_messages", id);
    messages.value = { ...messages.value, [id]: list };
  } catch (e) {
    toastError(e);
  }
}

// ---------- agents ----------

function upsertAgent(agent) {
  const list = agents.value.filter((a) => a.id !== agent.id);
  list.push(agent);
  list.sort((a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: "base" }) || a.id.localeCompare(b.id));
  agents.value = list;
}

function patchAgent(id, patch) {
  agents.value = agents.value.map((a) => (a.id === id ? { ...a, ...patch } : a));
}

/** Save a draft from the editor. Throws ApiError (with .fields) on validation problems. */
export async function saveAgent(draft) {
  const saved = await call("save_agent", draft);
  upsertAgent(saved);
  return saved;
}

export async function deleteAgent(id) {
  await call("delete_agent", id);
  batch(() => {
    agents.value = agents.value.filter((a) => a.id !== id);
    const { [id]: _drop, ...rest } = messages.value;
    messages.value = rest;
    if (view.value.id === id) {
      view.value = agents.value.length ? { kind: "agent", id: agents.value[0].id } : { kind: "welcome" };
    }
  });
  if (view.value.kind === "agent") await selectAgent(view.value.id);
}

// ---------- chat ----------

export async function sendMessage(agentId, text) {
  const { user_message, agent_message } = await call("send_message", agentId, text);
  const list = messages.value[agentId] || [];
  batch(() => {
    messages.value = { ...messages.value, [agentId]: [...list, user_message, agent_message] };
    patchAgent(agentId, {
      status: "responding",
      last_message: { role: "user", preview: text.slice(0, 160), created_at: user_message.created_at },
    });
  });
}

function updateMessage(agentId, messageId, fn) {
  const list = messages.value[agentId];
  if (!list) return;
  messages.value = { ...messages.value, [agentId]: list.map((m) => (m.id === messageId ? fn(m) : m)) };
}

function handleEvent(ev) {
  switch (ev.type) {
    case "message.delta":
      updateMessage(ev.agent_id, ev.message_id, (m) => ({ ...m, content: m.content + ev.delta }));
      break;
    case "message.done": {
      const m = ev.message;
      updateMessage(ev.agent_id, m.id, () => m);
      patchAgent(ev.agent_id, {
        last_message: { role: m.role, preview: m.content.slice(0, 160), created_at: m.created_at },
      });
      const viewing = view.value.kind === "agent" && view.value.id === ev.agent_id && document.hasFocus();
      if (!viewing) unread.value = { ...unread.value, [ev.agent_id]: (unread.value[ev.agent_id] || 0) + 1 };
      break;
    }
    case "agent.status":
      patchAgent(ev.agent_id, { status: ev.status });
      break;
    default:
      console.debug("Unhandled event", ev);
  }
}
onEvent(handleEvent);

// ---------- dialogs ----------

export function openNewAgent() {
  dialog.value = { kind: "new" };
}

export function openEditor(draft, agentId = null, tab = "general") {
  dialog.value = { kind: "editor", agentId, draft, tab };
}

export function openSettings() {
  dialog.value = { kind: "settings" };
}

export function closeDialog() {
  dialog.value = null;
}

// ---------- settings ----------

export async function setTheme(value) {
  theme.value = value;
  applyTheme(value);
  try {
    await call("set_theme", value);
  } catch (e) {
    toastError(e);
  }
}

export async function saveEndpointOverrides(overrides) {
  const config = await call("save_endpoint_overrides", overrides);
  boot.value = { ...boot.value, config };
}
