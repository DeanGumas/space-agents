// The report card a run delivers (post_report): summary, then new/changed findings by level with suggested actions
// and sources, then findings already reported before ("still open") as a compact, dimmed list.
// Item details are model output, so they go through renderMarkdown (DOMPurify) like every other agent text.

import { html } from "htm/preact";
import { Icon } from "./common.js";
import { renderMarkdown } from "../lib/markdown.js";
import { cx } from "../lib/util.js";

const LEVELS = {
  urgent: { label: "Urgent", icon: "siren" },
  action: { label: "Action needed", icon: "circle-alert" },
  info: { label: "Info", icon: "info" },
};

export function RunReport({ run }) {
  const report = run.report;
  const fresh = report.items.filter((i) => i.change !== "unchanged");
  const stillOpen = report.items.filter((i) => i.change === "unchanged");
  return html`
    <div class="report-card">
      <div class="report-head">
        <${Icon} name="clipboard-list" size="sm" />
        <span>Run report</span>
        <span class="trigger">${run.trigger === "schedule" ? "Scheduled" : "Run now"}</span>
      </div>
      <div class="report-summary">${report.summary}</div>
      ${fresh.length === 0
        ? html`<div class="report-empty">Nothing new to report.</div>`
        : html`<ul class="report-items">
            ${fresh.map((item) => html`<${ReportItem} item=${item} />`)}
          </ul>`}
      ${stillOpen.length > 0 &&
      html`<div class="report-still-open">
        <div class="label">Still open (reported before)</div>
        <ul>
          ${stillOpen.map(
            (item) => html`<li title=${item.detail}>
              <${Icon} name=${(LEVELS[item.level] || LEVELS.info).icon} size="sm" /> ${item.title}
            </li>`,
          )}
        </ul>
      </div>`}
    </div>
  `;
}

function ReportItem({ item }) {
  const level = LEVELS[item.level] || LEVELS.info;
  return html`
    <li class=${cx("report-item", item.level)}>
      <div class="report-item-head">
        <span class="level"><${Icon} name=${level.icon} size="sm" /> ${level.label}</span>
        <span class="title">${item.title}</span>
        ${item.change === "changed" && html`<span class="chip accent">Changed</span>`}
      </div>
      ${item.detail && html`<div class="md" dangerouslySetInnerHTML=${{ __html: renderMarkdown(item.detail) }}></div>`}
      ${item.suggested_actions.length > 0 &&
      html`<ul class="report-actions">
        ${item.suggested_actions.map((a) => html`<li><${Icon} name="arrow-right" size="sm" /> ${a}</li>`)}
      </ul>`}
      ${item.sources.length > 0 &&
      html`<div class="report-sources">
        ${item.sources.map((s) => html`<span class="chip"><${Icon} name="file-text" size="sm" /> ${s}</span>`)}
      </div>`}
    </li>
  `;
}
