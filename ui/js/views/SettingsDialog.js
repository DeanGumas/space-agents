import { html } from "htm/preact";
import { useState } from "preact/hooks";
import { Field, Icon, Modal, Notice, Segmented } from "../components/common.js";
import { boot, theme, setTheme, closeDialog, saveEndpointOverrides, toast, toastError } from "../state.js";
import { call } from "../api.js";
import { THEMES } from "../lib/theme.js";

const SOURCE_LABEL = {
  default: "built-in",
  user: "set by you",
  ".env": ".env file",
  env: "environment",
  cli: "command line",
};
const ENDPOINT_KEYS = ["llm_base_url", "mcp_base_url"];

function SourceChip({ source }) {
  return html`<span class="chip source" title="Where this value comes from">${SOURCE_LABEL[source] || source}</span>`;
}

export function SettingsDialog() {
  const b = boot.value;
  const config = b.config;
  const endpoints = config.filter((c) => ENDPOINT_KEYS.includes(c.key));
  const others = config.filter((c) => !ENDPOINT_KEYS.includes(c.key));
  const [values, setValues] = useState(Object.fromEntries(endpoints.map((c) => [c.key, c.value || ""])));
  const [saving, setSaving] = useState(false);
  const dirty = endpoints.some((c) => c.editable && (values[c.key] || "") !== (c.value || ""));

  const saveEndpoints = async () => {
    setSaving(true);
    try {
      const overrides = Object.fromEntries(endpoints.filter((c) => c.editable).map((c) => [c.key, values[c.key]]));
      await saveEndpointOverrides(overrides);
      toast("Connection settings saved", "success");
    } catch (e) {
      toastError(e);
    } finally {
      setSaving(false);
    }
  };

  return html`
    <${Modal} title="Settings" onClose=${closeDialog} width=${680}>
      <div class="modal-body">
        <div class="settings-section">
          <h3>Appearance</h3>
          <${Segmented} options=${THEMES} value=${theme.value} onChange=${setTheme} />
        </div>

        <div class="settings-section">
          <h3>Connection</h3>
          <${Notice} icon="info">
            ${b.llm?.ready
              ? html`Connected to the model gateway${b.llm.default_model ? ` (default model: ${b.llm.default_model})` : ""}. `
              : html`No model gateway is set up yet, so agents send placeholder replies. `}
            Values from a <code>.env</code> file or environment variables take priority and can't be changed here.
          <//>
          ${endpoints.map(
            (c) => html`<${Field} label=${html`${c.label} <${SourceChip} source=${c.source} />`} help=${c.help}>
              <input
                class="input"
                placeholder="https://…"
                disabled=${!c.editable}
                value=${values[c.key]}
                onInput=${(e) => setValues({ ...values, [c.key]: e.currentTarget.value })}
              />
            <//>`,
          )}
          <button class="btn" disabled=${!dirty || saving} onClick=${saveEndpoints}>
            ${saving ? "Saving…" : "Save connection settings"}
          </button>
        </div>

        <div class="settings-section">
          <h3>Data</h3>
          <div class="kv" style=${{ marginBottom: "10px" }}>
            ${others.map(
              (c) => html`<span class="k">${c.label}</span>
                <span class="v">
                  ${Array.isArray(c.value) ? c.value.join(", ") || "—" : c.value === null || c.value === "" ? "—" : String(c.value)}
                  <${SourceChip} source=${c.source} />
                </span>`,
            )}
          </div>
          <button class="btn" onClick=${() => call("open_data_folder").catch(toastError)}>
            <${Icon} name="folder-open" /> Open data folder
          </button>
        </div>

        <div class="settings-section">
          <h3>About</h3>
          <div class="kv">
            <span class="k">Version</span><span class="v">${b.version}</span>
            <span class="k">Platform</span><span class="v">${b.platform}</span>
          </div>
        </div>
      </div>
    <//>
  `;
}
