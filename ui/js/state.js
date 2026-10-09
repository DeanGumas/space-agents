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
export const contextMenu = signal(null); // {x, y, title?, items: [{label, icon, onSelect} | {divider: true}]}

// Sidebar width: draggable between SIDEBAR_MIN and SIDEBAR_MAX; narrower snaps to an icon-only rail.
export const SIDEBAR_DEFAULT = 268;
export const SIDEBAR_MIN = 200;
export const SIDEBAR_MAX = 480;
export const SIDEBAR_COMPACT = 72;
export const sidebarWidth = signal(SIDEBAR_DEFAULT);
let lastExpandedWidth = SIDEBAR_DEFAULT;

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
      const saved = data.ui?.sidebar_width;
      if (saved) {
        sidebarWidth.value = saved;
        if (saved > SIDEBAR_COMPACT) lastExpandedWidth = saved;
      }
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
  addExchange(agentId, user_message, agent_message);
}

function addExchange(agentId, userMessage, agentMessage) {
  const list = messages.value[agentId] || [];
  batch(() => {
    messages.value = { ...messages.value, [agentId]: [...list, userMessage, agentMessage] };
    patchAgent(agentId, {
      status: "responding",
      last_message: { role: "user", preview: userMessage.content.slice(0, 160), created_at: userMessage.created_at },
    });
  });
}

function updateMessage(agentId, messageId, fn) {
  const list = messages.value[agentId];
  if (!list) return;
  messages.value = { ...messages.value, [agentId]: list.map((m) => (m.id === messageId ? fn(m) : m)) };
}

function upsert(list = [], item) {
  return list.some((x) => x.id === item.id) ? list.map((x) => (x.id === item.id ? item : x)) : [...list, item];
}

function handleEvent(ev) {
  switch (ev.type) {
    case "message.delta":
      updateMessage(ev.agent_id, ev.message_id, (m) => ({ ...m, content: m.content + ev.delta }));
      break;
    case "message.done": {
      const m = ev.message;
      updateMessage(ev.agent_id, m.id, (old) => ({ ...old, ...m })); // keep tool_calls / changes
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
    case "tool.started":
    case "tool.done":
      updateMessage(ev.agent_id, ev.message_id, (m) => ({ ...m, tool_calls: upsert(m.tool_calls, ev.call) }));
      break;
    case "change.updated":
      updateMessage(ev.agent_id, ev.message_id, (m) => ({ ...m, changes: upsert(m.changes, ev.change) }));
      break;
    default:
      console.debug("Unhandled event", ev);
  }
}
onEvent(handleEvent);

export async function stopAgent(agentId) {
  try {
    await call("stop_agent", agentId);
  } catch (e) {
    toastError(e);
  }
}

// ---------- proposed changes ----------

/** Discard a proposed change; with feedback, the agent is told and starts revising. Returns true on success. */
export async function discardChange(change, feedback = "") {
  try {
    const res = await call("discard_change", change.id, feedback);
    if (res.user_message) addExchange(change.agent_id, res.user_message, res.agent_message);
    return true;
  } catch (e) {
    toastError(e);
    return false;
  }
}

/** action: "apply" | "discard" | "undo" | "reveal". The UI updates from the change.updated event. */
export async function changeAction(action, change, force = false) {
  try {
    if (action === "apply") await call("apply_change", change.id, force);
    else if (action === "discard") await call("discard_change", change.id);
    else if (action === "undo") await call("undo_change", change.id);
    else if (action === "reveal") await call("reveal_change", change.id);
  } catch (e) {
    toastError(e);
  }
}

// ---------- sidebar width ----------

/** Pointer x → sidebar width: below the snap point it collapses to the icon rail. */
export function sidebarWidthFor(x) {
  if (x < 150) return SIDEBAR_COMPACT;
  return Math.round(Math.max(SIDEBAR_MIN, Math.min(SIDEBAR_MAX, x)));
}

export function setSidebarWidth(width) {
  sidebarWidth.value = width;
  if (width > SIDEBAR_COMPACT) lastExpandedWidth = width;
}

export function saveSidebarWidth() {
  call("set_ui_pref", "sidebar_width", sidebarWidth.value).catch(toastError);
}

export function toggleSidebar() {
  setSidebarWidth(sidebarWidth.value <= SIDEBAR_COMPACT ? lastExpandedWidth : SIDEBAR_COMPACT);
  saveSidebarWidth();
}

// ---------- context menu ----------

export function openContextMenu(event, menu) {
  event.preventDefault();
  event.stopPropagation();
  contextMenu.value = { x: event.clientX, y: event.clientY, ...menu };
}

export function closeContextMenu() {
  contextMenu.value = null;
}

export async function duplicateAgent(id) {
  try {
    const clone = await call("duplicate_agent", id);
    upsertAgent(clone);
    await selectAgent(clone.id);
    toast(`Created ${clone.name}`, "success");
  } catch (e) {
    toastError(e);
  }
}

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
