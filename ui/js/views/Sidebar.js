import { html } from "htm/preact";
import { Avatar, Icon, Logo } from "../components/common.js";
import {
  agents,
  attention,
  boot,
  view,
  unread,
  theme,
  selectAgent,
  showActivity,
  openNewAgent,
  openSettings,
  setTheme,
  sidebarWidth,
  sidebarWidthFor,
  setSidebarWidth,
  saveSidebarWidth,
  SIDEBAR_COMPACT,
  SIDEBAR_DEFAULT,
  openContextMenu,
  openEditor,
  duplicateAgent,
} from "../state.js";
import { toDraft } from "./AgentEditor.js";
import { THEMES } from "../lib/theme.js";
import { cx, relativeTime, scheduleSummary } from "../lib/util.js";

export function Sidebar() {
  const current = view.value;
  const list = agents.value;
  const compact = sidebarWidth.value <= SIDEBAR_COMPACT;
  return html`
    <aside class=${cx("sidebar", compact && "compact")}>
      <${Resizer} />
      <div class="sidebar-header">
        <${Logo} />
        <span class="title">Space Agents</span>
        <button class="btn ghost icon-only sm" title="New agent (Ctrl/⌘ N)" onClick=${openNewAgent}>
          <${Icon} name="square-pen" />
        </button>
      </div>

      <div class="sidebar-scroll">
        <button
          class=${cx("nav-item", current.kind === "activity" && "active")}
          onClick=${showActivity}
          title=${compact ? "Activity" : undefined}
        >
          <${Icon} name="inbox" />
          <span class="label">Activity</span>
          ${attention.value.length > 0 &&
          html`<span class="badge" title=${`${attention.value.length} waiting for your approval`}>
            ${attention.value.length}
          </span>`}
        </button>

        <div class="section-title">
          <span>Agents</span>
          <button class="btn ghost icon-only sm" title="New agent" onClick=${openNewAgent}>
            <${Icon} name="plus" size="sm" />
          </button>
        </div>

        ${list.length === 0 && html`<div class="sidebar-empty">No agents yet. Create one to get started.</div>`}
        ${list.map(
          (a) => html`<${AgentRow}
            key=${a.id}
            agent=${a}
            active=${current.kind === "agent" && current.id === a.id}
            unreadCount=${unread.value[a.id] || 0}
            compact=${compact}
          />`,
        )}

        <button class="new-agent-btn" onClick=${openNewAgent} title=${compact ? "Add an agent" : undefined}>
          <span class="plus"><${Icon} name="plus" size="sm" /></span>
          <span class="label">Add an agent</span>
        </button>
      </div>

      <div class="sidebar-footer">
        <${ConnectionStatus} />
        <${ThemeButton} />
        <button class="btn ghost icon-only sm" title="Settings (Ctrl/⌘ ,)" onClick=${openSettings}>
          <${Icon} name="settings" />
        </button>
      </div>
    </aside>
  `;
}

function AgentRow({ agent, active, unreadCount, compact }) {
  const busy = agent.status === "responding";
  const last = agent.last_message;
  const preview = busy
    ? "Responding…"
    : last
      ? `${last.role === "user" ? "You: " : ""}${last.preview.replace(/[#*_`>]/g, "").replace(/\s+/g, " ")}`
      : scheduleSummary(agent.schedule) || agent.description || "No messages yet";
  return html`
    <button
      class=${cx("agent-row", active && "active", unreadCount > 0 && "unread")}
      onClick=${() => selectAgent(agent.id)}
      onContextMenu=${(e) =>
        openContextMenu(e, {
          title: agent.name,
          items: [
            { label: "Edit agent", icon: "pencil", onSelect: () => openEditor(toDraft(agent), agent.id) },
            { label: "Duplicate", icon: "copy", onSelect: () => duplicateAgent(agent.id) },
          ],
        })}
      title=${compact ? agent.name : last ? `Last activity ${relativeTime(last.created_at)}` : agent.name}
    >
      <${Avatar} agent=${agent} size=${32} />
      <span class="meta">
        <span class="name">${agent.name}</span>
        <span class="preview">${preview}</span>
      </span>
      <span class="side">
        ${unreadCount > 0 && html`<span class="badge">${unreadCount}</span>`}
        ${busy && html`<span class="status-dot busy" title="Responding"></span>`}
      </span>
    </button>
  `;
}

// Drag handle on the sidebar's right edge. Double-click resets the width; Ctrl/⌘ B toggles the icon rail.
function Resizer() {
  const onPointerDown = (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    const handle = e.currentTarget;
    try {
      handle.setPointerCapture(e.pointerId); // keep receiving moves when the pointer leaves the thin handle
    } catch {
      // not an active pointer (e.g. synthetic events); dragging still works while over the handle
    }
    document.body.classList.add("resizing");
    const move = (ev) => setSidebarWidth(sidebarWidthFor(ev.clientX));
    const up = () => {
      handle.removeEventListener("pointermove", move);
      document.body.classList.remove("resizing");
      saveSidebarWidth();
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", up, { once: true });
    handle.addEventListener("pointercancel", up, { once: true });
  };
  return html`<div
    class="sidebar-resizer"
    role="separator"
    aria-orientation="vertical"
    title="Drag to resize · double-click to reset · Ctrl/⌘ B to collapse"
    onPointerDown=${onPointerDown}
    onDblClick=${() => {
      setSidebarWidth(SIDEBAR_DEFAULT);
      saveSidebarWidth();
    }}
  ></div>`;
}

function ConnectionStatus() {
  const llm = boot.value?.llm || {};
  const url = boot.value?.config?.find((c) => c.key === "llm_base_url")?.value;
  if (llm.ready) {
    return html`
      <span class="conn" title=${`Model gateway: ${url}`}>
        <span class="dot ready"></span>
        <span>Ready${llm.default_model ? ` · ${llm.default_model}` : ""}</span>
      </span>
    `;
  }
  return html`
    <span class="conn" title=${url ? `Model gateway: ${url}\nNo API key set` : "No model gateway configured"}>
      <span class="dot"></span>
      <span>${url ? "No API key" : "No server set"}</span>
    </span>
  `;
}

function ThemeButton() {
  const idx = THEMES.findIndex((t) => t.value === theme.value);
  const cur = THEMES[idx] || THEMES[0];
  const next = THEMES[(idx + 1) % THEMES.length];
  return html`
    <button
      class="btn ghost icon-only sm"
      title=${`Theme: ${cur.label} (click for ${next.label})`}
      onClick=${() => setTheme(next.value)}
    >
      <${Icon} name=${cur.icon} />
    </button>
  `;
}
