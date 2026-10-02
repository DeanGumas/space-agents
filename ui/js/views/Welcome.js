// Welcome screen (no agents yet), the New Agent dialog (pick a template), and the Activity placeholder.

import { html } from "htm/preact";
import { Avatar, Icon, Logo, Modal } from "../components/common.js";
import { boot, openEditor, closeDialog } from "../state.js";
import { clone } from "../lib/util.js";

function startFrom(manifest) {
  openEditor({ ...clone(manifest), avatar_url: null, image_data: null });
}

export function TemplateGrid() {
  const templates = boot.value?.templates || [];
  return html`
    <div class="template-grid">
      <button class="template-card blank" onClick=${() => startFrom(boot.value.blank_manifest)}>
        <span class="avatar" style=${{ width: "36px", height: "36px", background: "var(--bg-active)", color: "var(--text-muted)" }}>
          <${Icon} name="plus" />
        </span>
        <span class="t-name">Start from scratch</span>
        <span class="t-desc">A blank agent you set up yourself.</span>
      </button>
      ${templates.map(
        (t) => html`<button class="template-card" key=${t.key} onClick=${() => startFrom(t.manifest)}>
          <${Avatar} agent=${t.manifest} size=${36} />
          <span class="t-name">${t.manifest.name}</span>
          <span class="t-desc">${t.manifest.description}</span>
        </button>`,
      )}
    </div>
  `;
}

export function Welcome() {
  return html`
    <section class="main">
      <div class="welcome">
        <div class="welcome-card">
          <${Logo} size=${64} />
          <h1>Welcome to Space Agents</h1>
          <p>
            Agents are AI helpers that keep an eye on your work systems and files, let you know what needs attention,
            and answer questions. Start from a template or build your own.
          </p>
          <${TemplateGrid} />
        </div>
      </div>
    </section>
  `;
}

export function NewAgentDialog() {
  return html`
    <${Modal} title="Create an agent" onClose=${closeDialog} width=${760}>
      <div class="modal-body">
        <p style=${{ marginTop: 0, color: "var(--text-muted)" }}>
          Pick a starting point. You can change everything on the next screen.
        </p>
        <${TemplateGrid} />
      </div>
    <//>
  `;
}

export function Activity() {
  return html`
    <section class="main">
      <header class="channel-header">
        <${Icon} name="inbox" size="lg" />
        <div class="who"><div class="name">Activity</div><div class="sub">Updates from all your agents</div></div>
      </header>
      <div class="placeholder">
        <div>
          <${Icon} name="inbox" />
          <div><strong>Nothing here yet</strong></div>
          <div>When agents run on a schedule, their findings and notifications will collect here.</div>
        </div>
      </div>
    </section>
  `;
}
