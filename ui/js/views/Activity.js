// Activity: everything waiting on the user, one entry per agent. Entries come from the backend's attention list
// (changes.py + runtime/notices.py): an agent appears while it has proposals to review, while it revises after
// "Discard & tell agent", or while it has notable run reports (new or changed findings at its notification level, or a
// failed run) that the user hasn't seen. Opening the conversation or dismissing a report clears it.

import { html } from "htm/preact";
import { Avatar, Icon } from "../components/common.js";
import { ChangeCards } from "../components/Activity.js";
import { agents, attention, openChange, openReport, dismissReport } from "../state.js";
import { cx, formatTime, relativeTime } from "../lib/util.js";

const LEVEL_ICONS = { urgent: "siren", action: "circle-alert", info: "info" };

export function Activity() {
  const entries = attention.value
    .map((e) => ({ ...e, agent: agents.value.find((a) => a.id === e.agent_id) }))
    .filter((e) => e.agent);
  return html`
    <section class="main">
      <header class="channel-header">
        <${Icon} name="inbox" size="lg" />
        <div class="who">
          <div class="name">Activity</div>
          <div class="sub">${entries.length ? "Things waiting on you" : "Nothing waiting on you"}</div>
        </div>
      </header>
      ${entries.length === 0
        ? html`<div class="placeholder">
            <div>
              <${Icon} name="circle-check" />
              <div><strong>You're all caught up</strong></div>
              <div>
                When an agent needs your approval, or a run finds something new, it shows up here and in a desktop
                notification.
              </div>
            </div>
          </div>`
        : html`<div class="timeline activity-list">
            ${entries.map((e) => html`<${Entry} key=${e.agent_id} entry=${e} />`)}
          </div>`}
    </section>
  `;
}

function Entry({ entry }) {
  const { agent, changes, revising, reports = [] } = entry;
  const n = changes.length;
  const parts = [];
  if (revising) parts.push(n ? "Revising based on your feedback" : "Revising based on your feedback…");
  if (n) parts.push(`${n} change${n === 1 ? "" : "s"} ${revising ? "still to review" : "waiting for your approval"}`);
  if (reports.length) parts.push(`${reports.length} new report${reports.length === 1 ? "" : "s"}`);
  const status = parts.join(" · ");
  return html`
    <div class="attn-entry">
      <div class="attn-head">
        <${Avatar} agent=${agent} size=${36} />
        <div class="attn-who">
          <div class="name">${agent.name}</div>
          <div class="sub">
            ${revising && html`<span class="spinner"></span>`} ${status} · ${relativeTime(entry.updated_at)}
          </div>
        </div>
        <button
          class="btn sm"
          onClick=${() =>
            changes.length || !reports.length
              ? openChange(agent.id, changes[0]?.id || null)
              : openReport(agent.id, reports[reports.length - 1].message_id)}
        >
          <${Icon} name="message-square" size="sm" /> Open conversation
        </button>
      </div>
      ${reports.map((r) => html`<${ReportNotice} key=${r.run_id} agent=${agent} report=${r} />`)}
      <${ChangeCards} changes=${changes} />
    </div>
  `;
}

function ReportNotice({ agent, report }) {
  const failed = report.status === "error";
  return html`
    <div class=${cx("attn-report", failed && "failed")}>
      <div class="attn-report-head">
        <${Icon} name=${failed ? "circle-x" : "clipboard-list"} size="sm" />
        <span class="what">${failed ? "Run failed" : report.trigger === "schedule" ? "Scheduled run" : "Run"}</span>
        <span class="when">${report.finished_at ? formatTime(report.finished_at) : ""}</span>
        <span class="actions">
          <button class="btn sm" onClick=${() => openReport(agent.id, report.message_id)}>Open</button>
          <button class="btn ghost sm" title="Remove from Activity" onClick=${() => dismissReport(report.run_id)}>
            Dismiss
          </button>
        </span>
      </div>
      ${failed
        ? html`<div class="attn-report-summary">${report.error}</div>`
        : html`<div class="attn-report-summary">${report.summary}</div>
            <ul class="attn-report-items">
              ${report.items.map(
                (i) => html`<li class=${i.level}>
                  <${Icon} name=${LEVEL_ICONS[i.level] || "info"} size="sm" /> ${i.title}
                  ${i.change === "changed" && html`<span class="chip accent">Changed</span>`}
                </li>`,
              )}
              ${report.more > 0 && html`<li class="more">+${report.more} more</li>`}
            </ul>`}
    </div>
  `;
}
