// What an agent did while answering: plain-language activity steps (with optional details for power users) and
// proposed-change cards (Apply / Discard / Undo / Show in folder). See PLAN.md §2.7 and §2.10.

import { html } from "htm/preact";
import { useState } from "preact/hooks";
import { Icon } from "./common.js";
import { changeAction, discardChange } from "../state.js";
import { cx } from "../lib/util.js";

const STEP_ICONS = {
  list_files: "folder-search",
  read_document: "file-text",
  search_document: "search",
  run_python: "square-terminal",
  compact_context: "fold-vertical",
};

export function ActivitySteps({ calls }) {
  if (!calls?.length) return null;
  return html`<div class="steps">${calls.map((c) => html`<${Step} key=${c.id} call=${c} />`)}</div>`;
}

function Step({ call }) {
  const [open, setOpen] = useState(false);
  const running = call.status === "running";
  const failed = call.status === "error";
  return html`
    <div class=${cx("step", failed && "failed")}>
      <button class="step-row" onClick=${() => setOpen(!open)} title="Show details">
        ${running
          ? html`<span class="spinner"></span>`
          : html`<${Icon} name=${failed ? "circle-x" : STEP_ICONS[call.name] || "wrench"} size="sm" />`}
        <span class="step-text">${call.summary}</span>
        ${call.duration_ms != null && html`<span class="step-time">${(call.duration_ms / 1000).toFixed(1)}s</span>`}
        <${Icon} name=${open ? "chevron-up" : "chevron-down"} size="sm" class="step-caret" />
      </button>
      ${open &&
      html`<div class="step-details">
        ${call.code && html`<div class="label">Code</div><pre>${call.code}</pre>`}
        ${Object.keys(call.args || {}).length > 0 &&
        html`<div class="label">Details</div><pre>${JSON.stringify(call.args, null, 2)}</pre>`}
        ${call.result && html`<div class="label">Result</div><pre>${call.result}</pre>`}
      </div>`}
    </div>
  `;
}

const FILE_ICONS = { xlsx: "file-spreadsheet", xlsm: "file-spreadsheet", csv: "file-spreadsheet", docx: "file-text" };

const STATUS = {
  pending: { label: "Needs your review", cls: "warning" },
  applied: { label: "Saved", cls: "success" },
  discarded: { label: "Discarded", cls: "" },
  conflicted: { label: "File changed since", cls: "warning" },
  undone: { label: "Undone", cls: "" },
};

export function ChangeCards({ changes }) {
  if (!changes?.length) return null;
  return html`<div class="change-cards">${changes.map((c) => html`<${ChangeCard} key=${c.id} change=${c} />`)}</div>`;
}

function ChangeCard({ change }) {
  const [busy, setBusy] = useState(false);
  const [denying, setDenying] = useState(false);
  const [feedback, setFeedback] = useState("");
  const ext = change.name.split(".").pop().toLowerCase();
  const verb = change.kind === "modify" ? "Update" : "Create";
  const status = STATUS[change.status] || { label: change.status, cls: "" };
  const s = change.summary || {};
  const act = async (action, force = false) => {
    setBusy(true);
    await changeAction(action, change, force);
    setBusy(false);
  };
  const deny = async () => {
    setBusy(true);
    const ok = await discardChange(change, feedback);
    setBusy(false);
    if (ok) setDenying(false);
  };
  return html`
    <div class=${cx("change-card", change.status)} id=${`change-${change.id}`}>
      <div class="change-head">
        <span class="change-icon"><${Icon} name=${FILE_ICONS[ext] || "file"} /></span>
        <div class="change-title">
          <div class="name">${verb} ${change.name}</div>
          <div class="path" title=${change.target}>${change.target}</div>
        </div>
        <span class=${cx("chip", status.cls)}>${status.label}</span>
      </div>
      ${s.description && html`<div class="change-desc">${s.description}</div>`}
      <${ChangeDetails} summary=${s} kind=${change.kind} />
      ${change.status === "conflicted" &&
      html`<div class="change-note">
        This file was edited after the agent proposed its change. Applying will replace those edits (the current file
        is kept in history).
      </div>`}
      ${denying &&
      html`<div class="deny-box">
        <label for=${`deny-${change.id}`}>What should be different? <span class="muted">(optional)</span></label>
        <textarea
          id=${`deny-${change.id}`}
          class="input"
          rows="3"
          autofocus
          placeholder="e.g. Use “Late” instead of “Delayed”, and leave the Notes column empty."
          value=${feedback}
          onInput=${(e) => setFeedback(e.currentTarget.value)}
          onKeyDown=${(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) deny();
            if (e.key === "Escape") setDenying(false);
          }}
        ></textarea>
        <div class="muted hint">
          ${feedback.trim()
            ? "The agent will get your feedback and propose a new version."
            : "Leave empty to just discard. The agent will still know you didn't apply it."}
        </div>
      </div>`}
      <div class="change-actions">
        ${(change.status === "pending" || change.status === "conflicted") &&
        (denying
          ? html`<button class="btn primary sm" disabled=${busy} onClick=${deny}>
                ${feedback.trim() ? "Discard & tell agent" : "Discard"}
              </button>
              <button class="btn sm" disabled=${busy} onClick=${() => setDenying(false)}>Cancel</button>`
          : html`<button
                class="btn primary sm"
                disabled=${busy}
                onClick=${() => act("apply", change.status === "conflicted")}
              >
                <${Icon} name="check" size="sm" /> ${change.status === "conflicted" ? "Apply anyway" : "Apply"}
              </button>
              <button class="btn sm" disabled=${busy} onClick=${() => setDenying(true)}>
                <${Icon} name="x" size="sm" /> Discard…
              </button>`)}
        ${change.can_undo &&
        html`<button class="btn sm" disabled=${busy} onClick=${() => act("undo")}>
          <${Icon} name="undo-2" size="sm" /> Undo
        </button>`}
        ${(change.status === "applied" || change.status === "undone") &&
        html`<button class="btn ghost sm" onClick=${() => act("reveal")}>
          <${Icon} name="folder-open" size="sm" /> Show in folder
        </button>`}
      </div>
    </div>
  `;
}

function ChangeDetails({ summary, kind }) {
  const x = summary.xlsx;
  if (x) {
    const sheets = x.sheets.map((sh) => {
      if (sh.status === "removed") return `Sheet “${sh.name}” removed`;
      if (sh.status === "added") return `Sheet “${sh.name}” added (${sh.rows} rows)`;
      if (sh.status === "new") return `“${sh.name}”: ${sh.rows} rows`;
      const rows = sh.rows !== sh.old_rows ? `, rows ${sh.old_rows} → ${sh.rows}` : "";
      return sh.changed_cells || rows ? `“${sh.name}”: ${sh.changed_cells} cell(s) changed${rows}` : null;
    }).filter(Boolean);
    if (x.dates_repaired) sheets.push(`${x.dates_repaired} date(s) kept as real dates (they were written as text)`);
    if (x.formats_lost) sheets.push(`⚠️ Formatting removed from ${x.formats_lost} cell(s)`);
    return html`
      <div class="change-details">
        ${sheets.length > 0 && html`<ul>${sheets.map((t) => html`<li>${t}</li>`)}</ul>`}
        ${kind === "modify" && x.samples?.length > 0 &&
        html`<table class="cell-diff">
          <thead><tr><th>Cell</th><th>Before</th><th>After</th></tr></thead>
          <tbody>
            ${x.samples.map(
              (c) => html`<tr>
                <td>${new Set(x.samples.map((v) => v.sheet)).size > 1 ? `${c.sheet}!` : ""}${c.cell}</td>
                <td class="old">${c.old || "—"}</td>
                <td class="new">${c.new || "—"}</td>
              </tr>`,
            )}
          </tbody>
        </table>`}
      </div>
    `;
  }
  if (summary.text_diff) {
    return html`<div class="change-details">
      <div class="muted">+${summary.lines_added} / −${summary.lines_removed} lines</div>
      <pre class="text-diff">${summary.text_diff}</pre>
    </div>`;
  }
  return null;
}
