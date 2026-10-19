// An agent's "channel": header, timeline (intro + messages), and composer.

import { html } from "htm/preact";
import { useState, useRef, useLayoutEffect, useEffect } from "preact/hooks";
import { Avatar, Icon } from "../components/common.js";
import { ActivitySteps, ChangeCards } from "../components/Activity.js";
import { RunReport } from "../components/Report.js";
import { messages, sendMessage, stopAgent, runAgent, openEditor, toastError, boot, focusTarget } from "../state.js";
import { renderMarkdown } from "../lib/markdown.js";
import { cx, formatTime, formatDay, isSameDay, nextRunLabel, scheduleSummary } from "../lib/util.js";
import { toDraft } from "./AgentEditor.js";

const GROUP_GAP_MS = 5 * 60 * 1000;

export function Channel({ agent }) {
  const list = messages.value[agent.id];
  const edit = (tab) => openEditor(toDraft(agent), agent.id, tab);
  return html`
    <section class="main">
      <${ChannelHeader} agent=${agent} onEdit=${() => edit("general")} />
      <${Timeline} agent=${agent} list=${list} onEdit=${edit} />
      <${Composer} agent=${agent} />
    </section>
  `;
}

const STATUS_LABELS = { responding: "Responding", running: "Running its task" };

function ChannelHeader({ agent, onEdit }) {
  const busy = agent.status !== "idle";
  const sub = agent.description || scheduleSummary(agent.schedule) || "No description";
  const llm = boot.value?.llm || {};
  const model = agent.model?.name || (llm.ready ? llm.default_model : "");
  const runBlocked = !agent.task?.trim()
    ? "Give this agent a task first (Edit → Schedule)"
    : !llm.ready
      ? "Runs need a model (see Settings)"
      : null;
  return html`
    <header class="channel-header">
      <${Avatar} agent=${agent} size=${32} />
      <div class="who">
        <div class="name">${agent.name}</div>
        <div class="sub">${sub}</div>
      </div>
      ${busy
        ? html`<span class="chip accent"><span class="status-dot busy"></span> ${STATUS_LABELS[agent.status] || "Busy"}</span>`
        : !llm.ready &&
          html`<span class="chip warning" title="Set SPACE_AGENTS_LLM_BASE_URL and SPACE_AGENTS_API_KEY (see .env.example)">
            <${Icon} name="unplug" size="sm" /> Not connected
          </span>`}
      ${agent.next_run_at &&
      html`<span class="chip success" title=${`${scheduleSummary(agent.schedule)}. Runs only while Space Agents is open.`}>
        <${Icon} name="clock" size="sm" /> ${nextRunLabel(agent.next_run_at)}
      </span>`}
      ${model && html`<span class="chip" title="Model"><${Icon} name="cpu" size="sm" /> ${model}</span>`}
      <button
        class="btn sm"
        disabled=${!!runBlocked}
        title=${runBlocked || "Do its task now and post a report"}
        onClick=${() => runAgent(agent.id)}
      >
        <${Icon} name="play" size="sm" /> Run now
      </button>
      <button class="btn sm" onClick=${onEdit}><${Icon} name="pencil" size="sm" /> Edit</button>
    </header>
  `;
}

function Timeline({ agent, list, onEdit }) {
  const ref = useRef(null);
  const stick = useRef(true); // keep pinned to the bottom unless the user scrolled up

  const onScroll = () => {
    const el = ref.current;
    stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  };

  useLayoutEffect(() => {
    if (stick.current && ref.current) ref.current.scrollTop = ref.current.scrollHeight;
  });
  useEffect(() => {
    stick.current = true;
  }, [agent.id]);

  // Opened from Activity: scroll to the change card or report and flash it, instead of pinning to the bottom.
  useEffect(() => {
    const id = focusTarget.value;
    if (!id || list === undefined) return;
    const el = document.getElementById(id);
    focusTarget.value = null;
    if (!el) return;
    stick.current = false;
    el.scrollIntoView({ block: "center" });
    el.classList.add("flash");
    setTimeout(() => el.classList.remove("flash"), 1600);
  }, [focusTarget.value, list]);

  return html`
    <div class="timeline" ref=${ref} onScroll=${onScroll}>
      <${Intro} agent=${agent} onEdit=${onEdit} />
      ${list === undefined
        ? null
        : list.map((m, i) => {
            const prev = list[i - 1];
            const newDay = !prev || !isSameDay(prev.created_at, m.created_at);
            const first =
              newDay ||
              prev.role !== m.role ||
              new Date(m.created_at) - new Date(prev.created_at) > GROUP_GAP_MS;
            return html`
              ${newDay && html`<div class="day-divider">${formatDay(m.created_at)}</div>`}
              <${Message} key=${m.id} msg=${m} agent=${agent} first=${first} />
            `;
          })}
    </div>
  `;
}

function Intro({ agent, onEdit }) {
  const catalog = boot.value?.catalog || [];
  const serverName = (id) => catalog.find((s) => s.id === id)?.name || id;
  const schedule = scheduleSummary(agent.schedule);
  return html`
    <div class="intro">
      <${Avatar} agent=${agent} size=${64} />
      <h2>${agent.name}</h2>
      <p>${agent.description || "This is the beginning of your conversation with this agent."}</p>
      <div class="chips">
        ${agent.mcp_servers.map((s) => html`<span class="chip"><${Icon} name="server" size="sm" /> ${serverName(s.id)}</span>`)}
        ${agent.filesystem.map(
          (f) => html`<span class="chip" title=${f.access === "read_write" ? "Read & write" : "Read only"}>
            <${Icon} name=${f.access === "read_write" ? "folder-pen" : "folder"} size="sm" /> ${f.path}
          </span>`,
        )}
        ${schedule && html`<span class="chip success"><${Icon} name="clock" size="sm" /> ${schedule}</span>`}
        ${!agent.mcp_servers.length && !agent.filesystem.length && !schedule &&
        html`<span class="chip">No data sources yet</span>`}
      </div>
      <div style=${{ display: "flex", gap: "8px" }}>
        <button class="btn sm" onClick=${() => onEdit("instructions")}><${Icon} name="file-text" size="sm" /> Instructions</button>
        <button class="btn sm" onClick=${() => onEdit("sources")}><${Icon} name="server" size="sm" /> Data sources</button>
        <button class="btn sm" onClick=${() => onEdit("schedule")}><${Icon} name="clock" size="sm" /> Schedule</button>
      </div>
    </div>
  `;
}

function Message({ msg, agent, first }) {
  const isUser = msg.role === "user";
  const streaming = msg.status === "streaming";
  const run = msg.run;
  let body;
  if (isUser) {
    body = html`<div class="md" style=${{ whiteSpace: "pre-wrap" }}>${msg.content}</div>`;
  } else if (msg.status === "queued") {
    body = html`<div class="typing"><${Icon} name="hourglass" size="sm" /> Waiting for ${agent.name} to finish its
      current work…</div>`;
  } else if (run?.report && msg.status === "done") {
    body = html`<${RunReport} run=${run} />`;
  } else if (streaming && !msg.content) {
    body = msg.tool_calls?.length ? null : html`<div class="typing">${run ? "Working on its task…" : "Thinking…"}</div>`;
  } else {
    body = html`<div class="md" dangerouslySetInnerHTML=${{ __html: renderMarkdown(msg.content) }}></div>
      ${streaming && html`<span class="cursor"></span>`}
      ${msg.status === "error" && html`<div class="typing">(Interrupted)</div>`}`;
  }
  return html`
    <div id=${`msg-${msg.id}`} class=${cx("msg", first && "first", msg.status === "error" && !msg.content && "error")}>
      <div class="gutter">
        ${first
          ? isUser
            ? html`<span class="user-avatar"><${Icon} name="user" /></span>`
            : html`<${Avatar} agent=${agent} size=${36} />`
          : html`<div class="time">${formatTime(msg.created_at)}</div>`}
      </div>
      <div class="content">
        ${first &&
        html`<div class="head">
          <span class="author">${isUser ? "You" : agent.name}</span>
          ${run && html`<span class="chip"><${Icon} name="play" size="sm" /> ${run.trigger === "schedule" ? "Scheduled run" : "Run"}</span>`}
          <span class="time">${formatTime(msg.created_at)}</span>
        </div>`}
        ${!isUser && html`<${ActivitySteps} calls=${msg.tool_calls} />`}
        <div class="body">${body}</div>
        ${!isUser && html`<${ChangeCards} changes=${msg.changes} />`}
      </div>
    </div>
  `;
}

function Composer({ agent }) {
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const ref = useRef(null);
  const busy = agent.status !== "idle"; // messages sent now wait in the agent's queue

  useEffect(() => ref.current?.focus(), [agent.id]);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, [text]);

  const submit = async () => {
    const value = text.trim();
    if (!value || sending) return;
    setSending(true);
    try {
      await sendMessage(agent.id, value);
      setText("");
    } catch (e) {
      toastError(e);
    } finally {
      setSending(false);
      ref.current?.focus();
    }
  };

  return html`
    <div class="composer">
      <div class="composer-box">
        <textarea
          ref=${ref}
          rows="1"
          placeholder=${`Message ${agent.name}…`}
          value=${text}
          onInput=${(e) => setText(e.currentTarget.value)}
          onKeyDown=${(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
              e.preventDefault();
              submit();
            }
          }}
        ></textarea>
        ${busy &&
        html`<button class="btn icon-only" title="Stop (and cancel anything waiting)" onClick=${() => stopAgent(agent.id)}>
          <${Icon} name="square" />
        </button>`}
        <button
          class="btn primary icon-only"
          title=${busy ? "Send (it waits until the agent is free)" : "Send (Enter)"}
          disabled=${!text.trim() || sending}
          onClick=${submit}
        >
          <${Icon} name="send-horizontal" />
        </button>
      </div>
      <div class="hint">Enter to send · Shift + Enter for a new line</div>
    </div>
  `;
}
