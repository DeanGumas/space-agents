// The report card a run delivers (post_report): summary, then findings by level with suggested actions and sources.
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
  return html`
    <div class="report-card">
      <div class="report-head">
        <${Icon} name="clipboard-list" size="sm" />
        <span>Run report</span>
        <span class="trigger">${run.trigger === "schedule" ? "Scheduled" : "Run now"}</span>
      </div>
      <div class="report-summary">${report.summary}</div>
      ${report.items.length === 0
        ? html`<div class="report-empty">Nothing new to report.</div>`
        : html`<ul class="report-items">
            ${report.items.map((item) => html`<${ReportItem} item=${item} />`)}
          </ul>`}
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
