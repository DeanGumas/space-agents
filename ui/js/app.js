import { html } from "htm/preact";
import { useEffect } from "preact/hooks";
import { Toasts } from "./components/common.js";
import { ContextMenu } from "./components/ContextMenu.js";
import { Sidebar } from "./views/Sidebar.js";
import { Channel } from "./views/Channel.js";
import { AgentEditor } from "./views/AgentEditor.js";
import { SettingsDialog } from "./views/SettingsDialog.js";
import { Welcome, NewAgentDialog } from "./views/Welcome.js";
import { Activity } from "./views/Activity.js";
import {
  boot,
  bootError,
  view,
  dialog,
  selectedAgent,
  openNewAgent,
  openSettings,
  sidebarWidth,
  toggleSidebar,
} from "./state.js";

export function App() {
  useEffect(() => {
    const onKey = (e) => {
      const mod = e.metaKey || e.ctrlKey;
      if (!mod || dialog.value) return;
      if (e.key === "n") {
        e.preventDefault();
        openNewAgent();
      } else if (e.key === ",") {
        e.preventDefault();
        openSettings();
      } else if (e.key === "b") {
        e.preventDefault();
        toggleSidebar();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  if (bootError.value) {
    return html`<div class="placeholder" style=${{ height: "100%" }}>
      <div><strong>Space Agents couldn't start</strong><div>${bootError.value}</div></div>
    </div>`;
  }
  if (!boot.value) return html`<div class="app"><aside class="sidebar"></aside><section class="main"></section></div>`;

  const d = dialog.value;
  let main;
  if (view.value.kind === "activity") main = html`<${Activity} />`;
  else if (selectedAgent.value) main = html`<${Channel} key=${selectedAgent.value.id} agent=${selectedAgent.value} />`;
  else main = html`<${Welcome} />`;

  return html`
    <div class="app" style=${{ "--sidebar-w": `${sidebarWidth.value}px` }}>
      <${Sidebar} />
      ${main}
    </div>
    ${d?.kind === "new" && html`<${NewAgentDialog} />`}
    ${d?.kind === "editor" && html`<${AgentEditor} key=${d.agentId || "new"} agentId=${d.agentId} draft=${d.draft} tab=${d.tab} />`}
    ${d?.kind === "settings" && html`<${SettingsDialog} />`}
    <${ContextMenu} />
    <${Toasts} />
  `;
}
