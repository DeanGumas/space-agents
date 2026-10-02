// Small shared components: Icon, Avatar, Modal, form fields, toasts.

import { html } from "htm/preact";
import { useEffect, useRef } from "preact/hooks";
import { cx, initials } from "../lib/util.js";
import { toasts } from "../state.js";

/** Lucide icon by name (files in ui/vendor/lucide), colored with currentColor via a CSS mask. */
export function Icon({ name, size, class: className }) {
  return html`<span
    class=${cx("icon", size, className)}
    style=${{ "--icon": `url(/vendor/lucide/${name}.svg)` }}
    aria-hidden="true"
  ></span>`;
}

/**
 * Agent avatar. `agent` needs {name, avatar: {emoji, color}, avatar_url}.
 * Priority: picture → emoji → initials, on the agent's color.
 */
export function Avatar({ agent, size = 28 }) {
  const a = agent.avatar || {};
  const style = { width: `${size}px`, height: `${size}px`, background: a.color || "#6366f1" };
  if (agent.avatar_url) {
    return html`<span class="avatar" style=${style}><img src=${agent.avatar_url} alt="" draggable="false" /></span>`;
  }
  if (a.emoji) {
    return html`<span class="avatar" style=${{ ...style, fontSize: `${Math.round(size * 0.58)}px` }}>
      <span class="emoji">${a.emoji}</span>
    </span>`;
  }
  return html`<span class="avatar" style=${{ ...style, fontSize: `${Math.round(size * 0.38)}px` }}>
    ${initials(agent.name)}
  </span>`;
}

export function Modal({ title, onClose, children, footer, width, tall, headerExtra }) {
  const ref = useRef(null);
  useEffect(() => {
    const onKey = (e) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    const first = ref.current?.querySelector("[autofocus], input, textarea, select");
    first?.focus();
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  return html`
    <div class="overlay" onMouseDown=${(e) => e.target === e.currentTarget && onClose()}>
      <div
        class=${cx("modal", tall && "tall")}
        style=${width ? { "--modal-w": `${width}px` } : null}
        role="dialog"
        aria-modal="true"
        aria-label=${title}
        ref=${ref}
      >
        <div class="modal-header">
          ${headerExtra}
          <h2>${title}</h2>
          <button class="btn ghost icon-only" onClick=${onClose} title="Close (Esc)"><${Icon} name="x" /></button>
        </div>
        ${children}
        ${footer && html`<div class="modal-footer">${footer}</div>`}
      </div>
    </div>
  `;
}

export function Field({ label, help, error, children, htmlFor }) {
  return html`
    <div class=${cx("field", error && "invalid")}>
      ${label && html`<label for=${htmlFor}>${label}</label>`}
      ${children}
      ${error ? html`<div class="error">${error}</div>` : help && html`<div class="help">${help}</div>`}
    </div>
  `;
}

export function Switch({ on, onChange, label }) {
  return html`<button
    type="button"
    role="switch"
    aria-checked=${on}
    aria-label=${label}
    class=${cx("switch", on && "on")}
    onClick=${() => onChange(!on)}
  ></button>`;
}

export function SwitchRow({ title, help, on, onChange }) {
  return html`
    <div class="switch-row">
      <div class="text">
        <div class="title">${title}</div>
        ${help && html`<div class="help">${help}</div>`}
      </div>
      <${Switch} on=${on} onChange=${onChange} label=${title} />
    </div>
  `;
}

export function Segmented({ options, value, onChange }) {
  return html`
    <div class="segmented" role="radiogroup">
      ${options.map(
        (o) => html`<button
          type="button"
          role="radio"
          aria-checked=${value === o.value}
          class=${cx(value === o.value && "on")}
          onClick=${() => onChange(o.value)}
        >
          ${o.icon && html`<${Icon} name=${o.icon} size="sm" />`} ${o.label}
        </button>`,
      )}
    </div>
  `;
}

export function Notice({ icon = "info", kind, children }) {
  return html`<div class=${cx("notice", kind)}><${Icon} name=${icon} /><div>${children}</div></div>`;
}

export function Toasts() {
  return html`
    <div class="toasts" aria-live="polite">
      ${toasts.value.map(
        (t) => html`<div class=${cx("toast", t.kind)} key=${t.id}>
          <${Icon} name=${t.kind === "error" ? "circle-alert" : t.kind === "success" ? "circle-check" : "info"} />
          <span>${t.text}</span>
        </div>`,
      )}
    </div>
  `;
}

export function Logo({ size = 26 }) {
  return html`<img class="logo" src="/img/logo.svg" alt="" width=${size} height=${size} draggable="false" />`;
}
