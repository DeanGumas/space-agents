// Activity: everything waiting on the user, one entry per agent. Entries come from the backend's attention list
// (changes.py): an agent appears while it has proposals to review, or while it revises after "Discard & tell agent",
// and disappears once nothing is pending and it's done working.

import { html } from "htm/preact";
import { Avatar, Icon } from "../components/common.js";
import { ChangeCards } from "../components/Activity.js";
import { agents, attention, openChange } from "../state.js";
import { relativeTime } from "../lib/util.js";

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
              <div>When an agent needs your approval, it shows up here and in a desktop notification.</div>
            </div>
          </div>`
        : html`<div class="timeline activity-list">
            ${entries.map((e) => html`<${Entry} key=${e.agent_id} entry=${e} />`)}
          </div>`}
    </section>
  `;
}

function Entry({ entry }) {
  const { agent, changes, revising } = entry;
  const n = changes.length;
  const status = revising
    ? n
      ? `Revising based on your feedback · ${n} change${n === 1 ? "" : "s"} still to review`
      : "Revising based on your feedback…"
    : `${n} change${n === 1 ? "" : "s"} waiting for your approval`;
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
        <button class="btn sm" onClick=${() => openChange(agent.id, changes[0]?.id || null)}>
          <${Icon} name="message-square" size="sm" /> Open conversation
        </button>
      </div>
      <${ChangeCards} changes=${changes} />
    </div>
  `;
}
