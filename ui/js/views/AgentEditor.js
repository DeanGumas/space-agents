// Create/edit an agent: a tabbed form over the manifest (PLAN.md §3.1).
//
// The editor works on a *draft*: the manifest fields plus UI-only fields for the avatar picture:
//   avatar_url   current picture to display (existing file or a freshly cropped data URL)
//   image_data   a new cropped PNG (data URL) to upload on save
// toPayload() turns a draft into what bridge.save_agent expects.

import { html } from "htm/preact";
import { useEffect, useState, useMemo } from "preact/hooks";
import { Avatar, Field, Icon, Modal, Notice, SwitchRow } from "../components/common.js";
import { AvatarPicker } from "../components/AvatarPicker.js";
import { boot, closeDialog, saveAgent, deleteAgent, selectAgent, toast, toastError } from "../state.js";
import { call } from "../api.js";
import { clone, cx, setPath } from "../lib/util.js";

const TABS = [
  { id: "general", label: "General", icon: "id-card", owns: ["name", "description", "avatar"] },
  { id: "instructions", label: "Instructions", icon: "file-text", owns: ["instructions", "skills"] },
  { id: "model", label: "Model", icon: "cpu", owns: ["model", "compaction"] },
  { id: "sources", label: "Data sources", icon: "server", owns: ["mcp_servers"] },
  { id: "files", label: "Files & folders", icon: "folder", owns: ["filesystem", "file_changes"] },
  { id: "schedule", label: "Schedule", icon: "clock", owns: ["schedule", "task", "notifications"] },
  { id: "advanced", label: "Advanced", icon: "sliders-horizontal", owns: ["limits", "permissions", "memory", "id", ""] },
];

const UI_ONLY = ["avatar_url", "image_data", "status", "last_message"];

export function toDraft(agent) {
  const d = clone(agent);
  delete d.status;
  delete d.last_message;
  d.image_data = null;
  return d;
}

export function toPayload(draft) {
  const p = clone(draft);
  for (const k of UI_ONLY) delete p[k];
  p.avatar = { emoji: draft.avatar.emoji || null, color: draft.avatar.color };
  if (draft.image_data) p.avatar.image_data = draft.image_data;
  else if (draft.avatar_url && draft.id) p.avatar.keep_image = true;
  return p;
}

const tabOf = (path) =>
  TABS.find((t) => t.owns.some((o) => path === o || path.startsWith(`${o}.`)))?.id || "advanced";

export function AgentEditor({ agentId, draft: initial, tab: initialTab }) {
  const [draft, setDraft] = useState(initial);
  const [tab, setTab] = useState(initialTab || "general");
  const [errors, setErrors] = useState({});
  const [saving, setSaving] = useState(false);
  const [confirmDiscard, setConfirmDiscard] = useState(false);
  const original = useMemo(() => JSON.stringify(initial), []);
  const dirty = JSON.stringify(draft) !== original;
  const isNew = !agentId;

  const set = (path, value) => {
    setDraft((d) => setPath(d, path, value));
    if (errors[path]) {
      const { [path]: _gone, ...rest } = errors;
      setErrors(rest);
    }
  };
  const err = (path) => errors[path];
  const tabsWithErrors = new Set(Object.keys(errors).map(tabOf));

  const close = () => (dirty && !confirmDiscard ? setConfirmDiscard(true) : closeDialog());

  const save = async () => {
    setSaving(true);
    try {
      const saved = await saveAgent(toPayload(draft));
      closeDialog();
      await selectAgent(saved.id);
      toast(isNew ? `Created ${saved.name}` : "Changes saved", "success");
    } catch (e) {
      if (e.fields && Object.keys(e.fields).length) {
        setErrors(e.fields);
        setTab(tabOf(Object.keys(e.fields)[0]));
      }
      toastError(e);
    } finally {
      setSaving(false);
    }
  };

  const panelProps = { draft, set, err, setDraft, agentId };
  const footer = confirmDiscard
    ? html`<span class="err">Discard your unsaved changes?</span>
        <span class="spacer"></span>
        <button class="btn" onClick=${() => setConfirmDiscard(false)}>Keep editing</button>
        <button class="btn danger" onClick=${closeDialog}>Discard</button>`
    : html`${Object.keys(errors).length > 0 && html`<span class="err">Some fields need attention.</span>`}
        <span class="spacer"></span>
        <button class="btn" onClick=${close}>Cancel</button>
        <button class="btn primary" disabled=${saving || (!dirty && !isNew)} onClick=${save}>
          ${saving ? "Saving…" : isNew ? "Create agent" : "Save changes"}
        </button>`;

  return html`
    <${Modal}
      title=${isNew ? "New agent" : `Edit ${initial.name}`}
      onClose=${close}
      width=${880}
      tall
      headerExtra=${html`<${Avatar} agent=${{ ...draft, name: draft.name || "New agent" }} size=${28} />`}
      footer=${footer}
    >
      <div class="editor">
        <nav class="tabs">
          ${TABS.map(
            (t) => html`<button class=${cx("tab", tab === t.id && "active")} onClick=${() => setTab(t.id)}>
              <${Icon} name=${t.icon} /> ${t.label} ${tabsWithErrors.has(t.id) && html`<span class="err-dot"></span>`}
            </button>`,
          )}
        </nav>
        <div class="panel">
          ${tab === "general" && html`<${GeneralTab} ...${panelProps} />`}
          ${tab === "instructions" && html`<${InstructionsTab} ...${panelProps} />`}
          ${tab === "model" && html`<${ModelTab} ...${panelProps} />`}
          ${tab === "sources" && html`<${SourcesTab} ...${panelProps} />`}
          ${tab === "files" && html`<${FilesTab} ...${panelProps} />`}
          ${tab === "schedule" && html`<${ScheduleTab} ...${panelProps} />`}
          ${tab === "advanced" && html`<${AdvancedTab} ...${panelProps} />`}
        </div>
      </div>
    <//>
  `;
}

// ---------------------------------------------------------------------------------------------------------------

function GeneralTab({ draft, set, err, setDraft }) {
  const [picking, setPicking] = useState(false);
  const onAvatar = (patch) =>
    setDraft((d) => {
      let next = { ...d, avatar: { ...d.avatar } };
      if ("emoji" in patch) next.avatar.emoji = patch.emoji;
      if ("color" in patch) next.avatar.color = patch.color;
      if (patch.remove_image) next = { ...next, avatar_url: null, image_data: null };
      if (patch.image_data) next = { ...next, avatar_url: patch.image_data, image_data: patch.image_data };
      return next;
    });

  return html`
    <h3>General</h3>
    <p class="lead">Give your agent a name and a face so it's easy to spot in the sidebar.</p>
    <div class="avatar-editor">
      <button class="preview-btn" type="button" title="Change avatar" onClick=${() => setPicking(!picking)}>
        <${Avatar} agent=${{ ...draft, name: draft.name || "?" }} size=${72} />
        <span class="edit"><${Icon} name="pencil" size="sm" /></span>
      </button>
      <div class="fields">
        <${Field} label="Name" error=${err("name")} htmlFor="agent-name">
          <input
            id="agent-name"
            class="input"
            autofocus
            maxlength="60"
            placeholder="e.g. Purchase Order Manager"
            value=${draft.name}
            onInput=${(e) => set("name", e.currentTarget.value)}
          />
        <//>
      </div>
    </div>
    ${picking &&
    html`<${AvatarPicker}
      value=${{ emoji: draft.avatar.emoji, color: draft.avatar.color, avatar_url: draft.avatar_url }}
      onChange=${onAvatar}
      onDone=${() => setPicking(false)}
    />`}
    <${Field}
      label="Description"
      help="One line shown under the agent's name. Optional."
      error=${err("description")}
      htmlFor="agent-desc"
    >
      <input
        id="agent-desc"
        class="input"
        maxlength="280"
        placeholder="e.g. Tracks PO deliveries and supplier updates"
        value=${draft.description}
        onInput=${(e) => set("description", e.currentTarget.value)}
      />
    <//>
  `;
}

function InstructionsTab({ draft, set, err }) {
  return html`
    <h3>Instructions</h3>
    <p class="lead">
      Describe the agent's job in plain English: who it helps, what it should pay attention to, and how it should
      respond. This becomes its "manifest" (its standing orders).
    </p>
    <${Field} error=${err("instructions")}>
      <textarea
        class="input tall"
        placeholder=${"You help a supply chain buyer manage open purchase orders.\n\n" +
        "- Watch for deliveries and late shipments\n- Flag anything that needs action\n" +
        "- Keep answers short and suggest next steps"}
        value=${draft.instructions}
        onInput=${(e) => set("instructions", e.currentTarget.value)}
      ></textarea>
    <//>
    <${Notice} icon="lightbulb">
      Tip: be specific about what "important" means to you, e.g. "a delivery date slipping more than 3 days".
    <//>
    <${SkillsSection} draft=${draft} set=${set} err=${err} />
  `;
}

function SkillsSection({ draft, set, err }) {
  const skills = draft.skills;
  const [found, setFound] = useState(null); // {skills: [{name, description}], problems: [...]}
  useEffect(() => {
    setFound(null);
    if (!skills.enabled || !skills.folder) return;
    let live = true;
    call("list_skills", skills.folder)
      .then((r) => live && setFound(r))
      .catch((e) => live && setFound({ skills: [], problems: [e.message] }));
    return () => {
      live = false;
    };
  }, [skills.enabled, skills.folder]);

  const choose = async () => {
    try {
      const picked = await call("pick_folder");
      if (picked) set("skills.folder", picked.path);
    } catch (e) {
      toastError(e);
    }
  };
  const hiddenTip =
    boot.value?.platform === "darwin" ? " For a hidden folder like ~/.claude/skills, press ⌘⇧. in the picker." : "";

  return html`
    <h3>Skills</h3>
    <p class="lead">
      Ready-made instructions for specific kinds of tasks, like filling in a form or writing a report in your team's
      format. The agent picks the right skill when a task calls for it.
    </p>
    <${SwitchRow}
      title="Enable skills"
      help="Let this agent use a folder of skills. It can read them but never change them."
      on=${skills.enabled}
      onChange=${(v) => set("skills.enabled", v)}
    />
    ${skills.enabled &&
    html`<${Field}
        label="Skills folder"
        help=${`One subfolder per skill, each with a SKILL.md inside.${hiddenTip}`}
        error=${err("skills.folder")}
      >
        <div class="folder-row">
          <${Icon} name="graduation-cap" />
          <span class="path" title=${skills.folder}>${skills.folder || "No folder chosen yet"}</span>
          <button class="btn sm" onClick=${choose}><${Icon} name="folder-open" /> Choose folder…</button>
        </div>
      <//>
      ${found?.problems?.length > 0 &&
      html`<${Notice} icon="triangle-alert" kind="warning">
        ${found.problems.map((p) => html`<div>${p}</div>`)}
      <//>`}
      ${found?.skills?.length > 0 &&
      html`<div class="card-list skill-list">
        ${found.skills.map(
          (s) => html`<div class="card">
            <div class="skill-name">${s.name}</div>
            <div class="desc">${s.description}</div>
          </div>`,
        )}
      </div>`}`}
  `;
}

function ModelTab({ draft, set, err }) {
  const models = boot.value?.models || [];
  const temp = draft.model.temperature;
  const compaction = draft.compaction;
  return html`
    <h3>Model</h3>
    <p class="lead">Which AI model this agent thinks with.</p>
    <${Field}
      label="Model"
      help=${models.length
        ? "Pick from the list or type a model name."
        : "Type a model name. The list will come from your server's /models endpoint once connected."}
      error=${err("model.name")}
      htmlFor="agent-model"
    >
      <input
        id="agent-model"
        class="input"
        list="model-options"
        placeholder="e.g. glm-5.3"
        value=${draft.model.name}
        onInput=${(e) => set("model.name", e.currentTarget.value)}
      />
      <datalist id="model-options">${models.map((m) => html`<option value=${m}></option>`)}</datalist>
    <//>
    <${Field}
      label=${`Creativity: ${temp.toFixed(2)}`}
      help="Lower is more precise and repeatable (best for tracking and reporting). Higher is more varied."
      error=${err("model.temperature")}
    >
      <input
        type="range"
        min="0"
        max="1"
        step="0.05"
        value=${temp}
        onInput=${(e) => set("model.temperature", Number(e.currentTarget.value))}
      />
    <//>
    <h3>Long tasks</h3>
    <p class="lead">
      Big jobs, like reading many documents, can outgrow how much the model can keep in mind at once. When that
      happens, the agent writes itself a summary of the work so far and carries on from it.
    </p>
    <${SwitchRow}
      title="Summarize when the conversation gets long"
      help="Uses the same model. Turn off only if your model has a very large context window."
      on=${compaction.enabled}
      onChange=${(v) => set("compaction.enabled", v)}
    />
    <div class="row">
      <${Field}
        label="Summarize at (tokens)"
        help="Keep this below the model's context window. A token is about ¾ of a word."
        error=${err("compaction.trigger_tokens")}
      >
        <input
          class="input"
          type="number"
          min="8000"
          max="2000000"
          step="1000"
          disabled=${!compaction.enabled}
          value=${compaction.trigger_tokens}
          onInput=${(e) => set("compaction.trigger_tokens", Number(e.currentTarget.value))}
        />
      <//>
      <${Field}
        label="Summary length (tokens)"
        help="Longer keeps more detail; shorter frees more room."
        error=${err("compaction.summary_tokens")}
      >
        <input
          class="input"
          type="number"
          min="200"
          max="20000"
          step="100"
          disabled=${!compaction.enabled}
          value=${compaction.summary_tokens}
          onInput=${(e) => set("compaction.summary_tokens", Number(e.currentTarget.value))}
        />
      <//>
    </div>
  `;
}

function SourcesTab({ draft, set, err }) {
  const catalog = boot.value?.catalog || [];
  const docServer = catalog.find((s) => s.role === "document_processing");
  const servers = catalog.filter((s) => s !== docServer);
  const grants = draft.mcp_servers;
  const unknown = grants.filter((g) => !catalog.some((s) => s.id === g.id));

  const toggle = (server) =>
    set(
      "mcp_servers",
      grants.some((g) => g.id === server.id)
        ? grants.filter((g) => g.id !== server.id)
        : [...grants, { id: server.id, tools: "read_only" }],
    );
  const setTools = (id, tools) => set("mcp_servers", grants.map((g) => (g.id === id ? { ...g, tools } : g)));

  return html`
    <h3>Data sources</h3>
    <p class="lead">Choose which company systems this agent can read from.</p>
    <${Notice} icon="shield-check">
      Agents are <strong>read-only</strong> in this version: tools that change data are not available yet.
    <//>
    ${err("mcp_servers") && html`<div class="field invalid"><div class="error">${err("mcp_servers")}</div></div>`}
    ${catalog.length === 0
      ? html`<div class="empty-box">
          No servers available yet. Once connected, servers come from your server's <code>/mcp</code> catalog.
          <br />For offline testing, set <code>SPACE_AGENTS_CATALOG_FILE</code> in <code>.env</code>.
        </div>`
      : html`<div class="card-list">
          ${docServer &&
          html`<div class="card locked">
            <div class="card-head">
              <span class="server-icon"><${Icon} name="file-scan" /></span>
              <div class="text">
                <div class="title">${docServer.name}</div>
                <div class="desc">GPU conversion for scans, images, and tricky layouts. Everyday PDFs and Office files are read locally. Always available.</div>
              </div>
              <span class="chip success">Always available</span>
            </div>
          </div>`}
          ${servers.map((s) => {
            const grant = grants.find((g) => g.id === s.id);
            return html`<${ServerCard} server=${s} grant=${grant} onToggle=${() => toggle(s)} onTools=${setTools} />`;
          })}
          ${unknown.map(
            (g) => html`<div class="card">
              <div class="card-head">
                <span class="server-icon"><${Icon} name="circle-help" /></span>
                <div class="text">
                  <div class="title">${g.id}</div>
                  <div class="desc">This server isn't in the catalog. It may have been renamed or removed.</div>
                </div>
                <button class="btn sm" onClick=${() => set("mcp_servers", grants.filter((x) => x !== g))}>Remove</button>
              </div>
            </div>`,
          )}
        </div>`}
  `;
}

function ServerCard({ server, grant, onToggle, onTools }) {
  const readTools = server.tools.filter((t) => t.read_only);
  const writeTools = server.tools.filter((t) => !t.read_only);
  const all = grant?.tools === "read_only";
  const chosen = all ? readTools.map((t) => t.name) : grant?.tools || [];
  const toggleTool = (name) =>
    onTools(server.id, chosen.includes(name) ? chosen.filter((n) => n !== name) : [...chosen, name]);

  return html`
    <div class=${cx("card", grant && "selected")}>
      <label class="card-head" style=${{ cursor: "pointer" }}>
        <span class="server-icon"><${Icon} name="server" /></span>
        <span class="text">
          <span class="title">${server.name}</span>
          <span class="desc" style=${{ display: "block" }}>
            ${server.description || `${readTools.length} read-only tools`}
          </span>
        </span>
        <input type="checkbox" checked=${!!grant} onChange=${onToggle} />
      </label>
      ${grant &&
      server.tools.length > 0 &&
      html`<div class="tool-list">
        <label class="check" style=${{ marginBottom: "6px" }}>
          <input type="radio" checked=${all} onChange=${() => onTools(server.id, "read_only")} />
          <span>All read-only tools (${readTools.length})</span>
        </label>
        <label class="check" style=${{ marginBottom: "6px" }}>
          <input type="radio" checked=${!all} onChange=${() => onTools(server.id, readTools.map((t) => t.name))} />
          <span>Only the tools I pick</span>
        </label>
        ${!all &&
        readTools.map(
          (t) => html`<label class="tool-item">
            <input type="checkbox" checked=${chosen.includes(t.name)} onChange=${() => toggleTool(t.name)} />
            <span><code>${t.name}</code> <span class="desc">${t.description}</span></span>
          </label>`,
        )}
        ${writeTools.map(
          (t) => html`<div class="tool-item disabled" title="Write access isn't available yet">
            <${Icon} name="lock" size="sm" />
            <span><code>${t.name}</code> <span class="desc">${t.description}</span></span>
          </div>`,
        )}
      </div>`}
    </div>
  `;
}

function FilesTab({ draft, set, err }) {
  const folders = draft.filesystem;
  const add = async () => {
    try {
      const picked = await call("pick_folder");
      if (!picked) return;
      if (folders.some((f) => f.path === picked.path)) return toast("That folder is already listed.");
      set("filesystem", [...folders, { path: picked.path, access: "read" }]);
    } catch (e) {
      toastError(e);
    }
  };
  const update = (i, patch) => set("filesystem", folders.map((f, j) => (j === i ? { ...f, ...patch } : f)));

  return html`
    <h3>Files & folders</h3>
    <p class="lead">Folders on this computer the agent may look through, such as Documents or Downloads.</p>
    <${Notice} icon="shield-check">
      The agent can only see inside these folders. Sensitive locations (like <code>.ssh</code> and password stores)
      are always blocked. <strong>Read & write</strong> lets it save documents there, but it can never delete files,
      create programs, or save into system or hidden folders.
    <//>
    ${folders.length === 0
      ? html`<div class="empty-box">No folders yet.</div>`
      : html`<div class="card-list">
          ${folders.map(
            (f, i) => html`<div class=${cx("card", err(`filesystem.${i}.path`) && "selected")}>
              <div class="folder-row">
                <${Icon} name=${f.access === "read_write" ? "folder-pen" : "folder"} />
                <span class="path" title=${f.path}>${f.path}</span>
                <select class="input" value=${f.access} onChange=${(e) => update(i, { access: e.currentTarget.value })}>
                  <option value="read">Read only</option>
                  <option value="read_write">Read & write</option>
                </select>
                <button
                  class="btn ghost icon-only sm"
                  title="Remove"
                  onClick=${() => set("filesystem", folders.filter((_, j) => j !== i))}
                >
                  <${Icon} name="x" />
                </button>
              </div>
              ${err(`filesystem.${i}.path`) && html`<div class="field invalid"><div class="error">${err(`filesystem.${i}.path`)}</div></div>`}
            </div>`,
          )}
        </div>`}
    <div style=${{ marginTop: "12px", marginBottom: "24px" }}>
      <button class="btn" onClick=${add}><${Icon} name="folder-plus" /> Add folder…</button>
    </div>
    <h3>When the agent saves files</h3>
    <p class="lead">Applies to Read & write folders. Agents can never delete files, and every overwrite can be undone.</p>
    <div class="row">
      <${Field} label="New files" error=${err("file_changes.new_files")}>
        <select
          class="input"
          value=${draft.file_changes.new_files}
          onChange=${(e) => set("file_changes.new_files", e.currentTarget.value)}
        >
          <option value="allow">Save automatically</option>
          <option value="ask">Ask me first</option>
        </select>
      <//>
      <${Field} label="Changes to existing files" error=${err("file_changes.existing_files")}>
        <select
          class="input"
          value=${draft.file_changes.existing_files}
          onChange=${(e) => set("file_changes.existing_files", e.currentTarget.value)}
        >
          <option value="ask">Ask me first</option>
          <option value="allow_with_backup">Apply automatically (keep a backup)</option>
        </select>
      <//>
    </div>
  `;
}

const FREQUENCIES = [
  [15, "Every 15 minutes"],
  [30, "Every 30 minutes"],
  [60, "Every hour"],
  [120, "Every 2 hours"],
  [240, "Every 4 hours"],
  [480, "Every 8 hours"],
  [1440, "Once a day"],
];
const DAYS = [
  ["mon", "Mon"],
  ["tue", "Tue"],
  ["wed", "Wed"],
  ["thu", "Thu"],
  ["fri", "Fri"],
  ["sat", "Sat"],
  ["sun", "Sun"],
];

function ScheduleTab({ draft, set, err }) {
  const s = draft.schedule;
  const freqs = FREQUENCIES.some(([m]) => m === s.every_minutes)
    ? FREQUENCIES
    : [...FREQUENCIES, [s.every_minutes, `Every ${s.every_minutes} minutes`]];
  const toggleDay = (d) =>
    set("schedule.weekdays", s.weekdays.includes(d) ? s.weekdays.filter((x) => x !== d) : [...s.weekdays, d]);

  return html`
    <h3>Schedule</h3>
    <p class="lead">Let the agent check in on its own and notify you about anything new.</p>
    <${SwitchRow}
      title="Run automatically"
      help="Runs only while Space Agents is open and your computer is awake."
      on=${s.enabled}
      onChange=${(v) => set("schedule.enabled", v)}
    />
    ${s.enabled &&
    html`
      <div class="row">
        <${Field} label="How often" error=${err("schedule.every_minutes")}>
          <select
            class="input"
            value=${s.every_minutes}
            onChange=${(e) => set("schedule.every_minutes", Number(e.currentTarget.value))}
          >
            ${freqs.map(([m, label]) => html`<option value=${m}>${label}</option>`)}
          </select>
        <//>
        <${Field} label="From" error=${err("schedule.active_hours.start")}>
          <input
            class="input"
            type="time"
            value=${s.active_hours.start}
            onInput=${(e) => set("schedule.active_hours.start", e.currentTarget.value)}
          />
        <//>
        <${Field} label="Until" error=${err("schedule.active_hours.end")}>
          <input
            class="input"
            type="time"
            value=${s.active_hours.end}
            onInput=${(e) => set("schedule.active_hours.end", e.currentTarget.value)}
          />
        <//>
      </div>
      <${Field} label="On these days" error=${err("schedule.weekdays")}>
        <div class="days">
          ${DAYS.map(
            ([d, label]) => html`<button
              type="button"
              class=${cx("day", s.weekdays.includes(d) && "on")}
              onClick=${() => toggleDay(d)}
            >
              ${label}
            </button>`,
          )}
        </div>
      <//>
      <div class="field">
        <label class="check">
          <input
            type="checkbox"
            checked=${s.run_on_startup_if_missed}
            onChange=${(e) => set("schedule.run_on_startup_if_missed", e.currentTarget.checked)}
          />
          <span>
            Catch up after a missed run
            <div class="help">If the computer was asleep or the app was closed, run once when it's back.</div>
          </span>
        </label>
      </div>
    `}
    <${Field}
      label="What should it do on each run?"
      help="Plain English. This is the agent's checklist each time it runs."
      error=${err("task")}
    >
      <textarea
        class="input"
        placeholder="Check Costpoint for POs delivered since the last run. Check my email for supplier updates. Report anything new, flag delays, and suggest next actions."
        value=${draft.task}
        onInput=${(e) => set("task", e.currentTarget.value)}
      ></textarea>
    <//>
    <${SwitchRow}
      title="Desktop notifications"
      help="Pop up a notification when it needs your approval or a run finds something."
      on=${draft.notifications.desktop}
      onChange=${(v) => set("notifications.desktop", v)}
    />
    <${Field} label="Notify me about" error=${err("notifications.min_level")}>
      <select
        class="input"
        value=${draft.notifications.min_level}
        onChange=${(e) => set("notifications.min_level", e.currentTarget.value)}
      >
        <option value="urgent">Only urgent things</option>
        <option value="action">Things that need my action</option>
        <option value="info">Everything it finds</option>
      </select>
    <//>
  `;
}

function AdvancedTab({ draft, set, err, agentId }) {
  const [confirming, setConfirming] = useState(false);
  const json = JSON.stringify(toPayload(draft), (k, v) => (k === "image_data" ? "<image>" : v), 2);
  const remove = async () => {
    try {
      await deleteAgent(agentId);
      closeDialog();
      toast(`Deleted ${draft.name}`, "success");
    } catch (e) {
      toastError(e);
    }
  };
  return html`
    <h3>Advanced</h3>
    <p class="lead">Guardrails and the raw manifest.</p>
    <div class="row">
      <${Field} label="Max tool calls per run" error=${err("limits.max_tool_calls")}>
        <input
          class="input"
          type="number"
          min="1"
          max="200"
          value=${draft.limits.max_tool_calls}
          onInput=${(e) => set("limits.max_tool_calls", Number(e.currentTarget.value))}
        />
      <//>
      <${Field} label="Max minutes per run" error=${err("limits.max_minutes")}>
        <input
          class="input"
          type="number"
          min="1"
          max="120"
          value=${draft.limits.max_minutes}
          onInput=${(e) => set("limits.max_minutes", Number(e.currentTarget.value))}
        />
      <//>
    </div>
    <${SwitchRow}
      title="Memory"
      help="Let the agent keep short notes between runs (e.g. what it already told you about)."
      on=${draft.memory.enabled}
      onChange=${(v) => set("memory.enabled", v)}
    />
    <${Field} label="Actions outside this computer" help="Sending email, posting to Teams, updating records, etc.">
      <select class="input" disabled value="never">
        <option value="never">Never (read-only in this version)</option>
      </select>
    <//>
    ${err("") && html`<div class="field invalid"><div class="error">${err("")}</div></div>`}
    <div class="field">
      <div class="label">Manifest (JSON)</div>
      <pre class="json">${json}</pre>
    </div>
    ${agentId &&
    html`<div class="danger-zone">
      <div class="title">Delete this agent</div>
      <p>Removes the agent, its picture, and its chat history from this computer. This can't be undone.</p>
      ${confirming
        ? html`<div style=${{ display: "flex", gap: "8px" }}>
            <button class="btn danger confirm" onClick=${remove}>Yes, delete ${draft.name}</button>
            <button class="btn" onClick=${() => setConfirming(false)}>Cancel</button>
          </div>`
        : html`<button class="btn danger" onClick=${() => setConfirming(true)}>
            <${Icon} name="trash-2" /> Delete agent
          </button>`}
    </div>`}
  `;
}

