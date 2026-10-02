import { html } from "htm/preact";
import { Avatar, Icon, Logo } from "../components/common.js";
import { agents, boot, view, unread, theme, selectAgent, showActivity, openNewAgent, openSettings, setTheme } from "../state.js";
import { THEMES } from "../lib/theme.js";
import { cx, relativeTime, scheduleSummary } from "../lib/util.js";

export function Sidebar() {
  const current = view.value;
  const list = agents.value;
  return html`
    <aside class="sidebar">
      <div class="sidebar-header">
        <${Logo} />
        <span class="title">Space Agents</span>
        <button class="btn ghost icon-only sm" title="New agent (Ctrl/⌘ N)" onClick=${openNewAgent}>
          <${Icon} name="square-pen" />
        </button>
      </div>

      <div class="sidebar-scroll">
        <button class=${cx("nav-item", current.kind === "activity" && "active")} onClick=${showActivity}>
          <${Icon} name="inbox" />
          <span class="label">Activity</span>
          <span class="soon">Soon</span>
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
          />`,
        )}

        <button class="new-agent-btn" onClick=${openNewAgent}>
          <span class="plus"><${Icon} name="plus" size="sm" /></span>
          <span>Add an agent</span>
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

function AgentRow({ agent, active, unreadCount }) {
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
      title=${last ? `Last activity ${relativeTime(last.created_at)}` : agent.name}
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

function ConnectionStatus() {
  const url = boot.value?.config?.find((c) => c.key === "llm_base_url")?.value;
  return html`
    <span class="conn" title=${url ? `Model gateway: ${url}\nNot connected yet (Phase 1)` : "No model gateway configured"}>
      <span class="dot"></span>
      <span>${url ? "Offline mode" : "No server set"}</span>
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
