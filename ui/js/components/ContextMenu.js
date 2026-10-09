// Right-click menu. Open it with openContextMenu(event, {title, items}) from state.js; one menu is shown at a time.
// Closes on click outside, Escape, scroll, resize, or window blur. Arrow keys move between items.

import { html } from "htm/preact";
import { useEffect, useLayoutEffect, useRef, useState } from "preact/hooks";
import { Icon } from "./common.js";
import { contextMenu, closeContextMenu } from "../state.js";
import { cx } from "../lib/util.js";

export function ContextMenu() {
  const menu = contextMenu.value;
  const ref = useRef(null);
  const [pos, setPos] = useState(null);

  // Keep the menu on screen, then focus the first item for keyboard use.
  useLayoutEffect(() => {
    if (!menu || !ref.current) return setPos(null);
    const r = ref.current.getBoundingClientRect();
    setPos({
      x: Math.max(8, Math.min(menu.x, window.innerWidth - r.width - 8)),
      y: Math.max(8, Math.min(menu.y, window.innerHeight - r.height - 8)),
    });
    ref.current.querySelector(".cm-item")?.focus();
  }, [menu]);

  useEffect(() => {
    if (!menu) return;
    const onDown = (e) => ref.current && !ref.current.contains(e.target) && closeContextMenu();
    const onKey = (e) => {
      if (e.key === "Escape") return closeContextMenu();
      if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
      e.preventDefault();
      const items = [...ref.current.querySelectorAll(".cm-item")];
      const i = items.indexOf(document.activeElement);
      items[(i + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length]?.focus();
    };
    window.addEventListener("mousedown", onDown, true);
    window.addEventListener("keydown", onKey);
    window.addEventListener("scroll", closeContextMenu, true);
    window.addEventListener("resize", closeContextMenu);
    window.addEventListener("blur", closeContextMenu);
    return () => {
      window.removeEventListener("mousedown", onDown, true);
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("scroll", closeContextMenu, true);
      window.removeEventListener("resize", closeContextMenu);
      window.removeEventListener("blur", closeContextMenu);
    };
  }, [menu]);

  if (!menu) return null;
  const at = pos || { x: menu.x, y: menu.y };
  return html`
    <div
      class="context-menu"
      role="menu"
      ref=${ref}
      style=${{ left: `${at.x}px`, top: `${at.y}px`, visibility: pos ? "visible" : "hidden" }}
      onContextMenu=${(e) => e.preventDefault()}
    >
      ${menu.title && html`<div class="cm-title">${menu.title}</div>`}
      ${menu.items.map((item, i) =>
        item.divider
          ? html`<div class="cm-divider" key=${i}></div>`
          : html`<button
              key=${i}
              role="menuitem"
              class=${cx("cm-item", item.danger && "danger")}
              onClick=${() => {
                closeContextMenu();
                item.onSelect();
              }}
            >
              <${Icon} name=${item.icon} size="sm" />
              <span>${item.label}</span>
            </button>`,
      )}
    </div>
  `;
}
