// Avatar picker: choose an emoji (with background color) or upload a picture and crop it.
// Pictures are cropped/resized here to a 256×256 PNG and sent to Python as a data URL (see bridge.save_agent).

import { html } from "htm/preact";
import { useState, useRef, useEffect } from "preact/hooks";
import { Icon, Segmented } from "./common.js";
import { EMOJI_CATEGORIES, AVATAR_COLORS, searchEmoji } from "../lib/emoji.js";
import { cx } from "../lib/util.js";
import { toast } from "../state.js";

const FRAME = 180; // crop frame size in CSS px
const OUTPUT = 256; // saved avatar size in px
const MAX_UPLOAD_BYTES = 20 * 1024 * 1024;

/**
 * value:    { emoji, color, avatar_url }   (avatar_url = current picture, if any)
 * onChange: (patch) => void, patch is any of { emoji, color, image_data, remove_image }
 */
export function AvatarPicker({ value, onChange, onDone }) {
  const [mode, setMode] = useState(value.avatar_url ? "picture" : "emoji");
  return html`
    <div class="picker">
      <div class="picker-top">
        <${Segmented}
          value=${mode}
          onChange=${setMode}
          options=${[
            { value: "emoji", label: "Emoji", icon: "smile" },
            { value: "picture", label: "Picture", icon: "image" },
          ]}
        />
        <span style=${{ flex: 1 }}></span>
        <button class="btn sm" type="button" onClick=${onDone}>Done</button>
      </div>
      ${mode === "emoji"
        ? html`<${EmojiTab} value=${value} onChange=${onChange} />`
        : html`<${PictureTab} value=${value} onChange=${onChange} />`}
      <div class="swatches" title="Background color">
        ${AVATAR_COLORS.map(
          (c) => html`<button
            type="button"
            class=${cx("swatch", value.color === c && "on")}
            style=${{ background: c }}
            aria-label=${`Color ${c}`}
            onClick=${() => onChange({ color: c })}
          ></button>`,
        )}
      </div>
    </div>
  `;
}

function EmojiTab({ value, onChange }) {
  const [query, setQuery] = useState("");
  const results = searchEmoji(query);
  const pick = (char) => onChange({ emoji: char, remove_image: true });
  const btn = (e) => html`<button
    type="button"
    class=${cx("emoji-btn", value.emoji === e.char && !value.avatar_url && "on")}
    title=${e.keywords}
    onClick=${() => pick(e.char)}
  >
    ${e.char}
  </button>`;

  return html`
    <div class="picker-top">
      <input
        class="input"
        placeholder="Search, or type/paste any emoji"
        value=${query}
        onInput=${(e) => setQuery(e.currentTarget.value)}
        onKeyDown=${(e) => {
          // Enter on a pasted emoji (not a word) uses it directly.
          if (e.key === "Enter") {
            e.preventDefault();
            const v = query.trim();
            if (v && !/^[\p{L}\p{N}\s]+$/u.test(v)) pick([...v].slice(0, 8).join(""));
            else if (results?.length) pick(results[0].char);
          }
        }}
      />
    </div>
    <div class="emoji-grid">
      ${results
        ? results.length
          ? results.map(btn)
          : html`<div class="emoji-cat">No matches. Paste any emoji and press Enter</div>`
        : EMOJI_CATEGORIES.map(
            (c) => html`<div class="emoji-cat">${c.name}</div>
              ${c.items.map(btn)}`,
          )}
    </div>
  `;
}

function PictureTab({ value, onChange }) {
  const fileInput = useRef(null);
  const [crop, setCrop] = useState(null); // {src, w, h, zoom, x, y}

  useEffect(() => () => crop && URL.revokeObjectURL(crop.src), [crop?.src]);

  const onFile = (e) => {
    const file = e.currentTarget.files?.[0];
    e.currentTarget.value = "";
    if (!file) return;
    if (!file.type.startsWith("image/")) return toast("Please choose an image file.", "error");
    if (file.size > MAX_UPLOAD_BYTES) return toast("That image is too large (max 20 MB).", "error");
    const src = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => setCrop({ src, w: img.naturalWidth, h: img.naturalHeight, zoom: 1, x: 0, y: 0 });
    img.onerror = () => {
      URL.revokeObjectURL(src);
      toast("Couldn't read that image.", "error");
    };
    img.src = src;
  };

  const chooser = html`<input
    ref=${fileInput}
    type="file"
    accept="image/png,image/jpeg,image/gif,image/webp"
    style=${{ display: "none" }}
    onChange=${onFile}
  />`;

  if (crop) {
    return html`${chooser}<${Cropper}
        crop=${crop}
        setCrop=${setCrop}
        onCancel=${() => setCrop(null)}
        onApply=${(dataUrl) => {
          onChange({ image_data: dataUrl });
          setCrop(null);
        }}
      />`;
  }

  return html`
    ${chooser}
    <div class="cropper">
      <div class="crop-frame" style=${{ cursor: "default" }}>
        ${value.avatar_url
          ? html`<img src=${value.avatar_url} alt="" style=${{ inset: 0, top: 0, left: 0, width: "100%", height: "100%" }} />`
          : html`<div class="placeholder" style=${{ height: "100%" }}><${Icon} name="image" /></div>`}
      </div>
      <div class="controls">
        <p class="help" style=${{ marginTop: 0, color: "var(--text-muted)" }}>
          Use a logo, icon, or photo. PNG, JPG, GIF, or WebP. You can crop it next.
        </p>
        <div style=${{ display: "flex", gap: "8px" }}>
          <button class="btn primary" type="button" onClick=${() => fileInput.current.click()}>
            <${Icon} name="upload" /> ${value.avatar_url ? "Replace picture" : "Upload picture"}
          </button>
          ${value.avatar_url &&
          html`<button class="btn" type="button" onClick=${() => onChange({ remove_image: true })}>Remove</button>`}
        </div>
      </div>
    </div>
  `;
}

function Cropper({ crop, setCrop, onApply, onCancel }) {
  const drag = useRef(null);
  const imgRef = useRef(null);
  const base = FRAME / Math.min(crop.w, crop.h); // "cover" scale at zoom 1
  const scale = base * crop.zoom;
  const dispW = crop.w * scale;
  const dispH = crop.h * scale;

  const clamp = (x, y, zoom = crop.zoom) => {
    const s = base * zoom;
    const maxX = (crop.w * s - FRAME) / 2;
    const maxY = (crop.h * s - FRAME) / 2;
    return { x: Math.max(-maxX, Math.min(maxX, x)), y: Math.max(-maxY, Math.min(maxY, y)) };
  };

  const onPointerDown = (e) => {
    e.currentTarget.setPointerCapture(e.pointerId);
    drag.current = { px: e.clientX, py: e.clientY, x: crop.x, y: crop.y };
  };
  const onPointerMove = (e) => {
    if (!drag.current) return;
    const d = drag.current;
    setCrop({ ...crop, ...clamp(d.x + e.clientX - d.px, d.y + e.clientY - d.py) });
  };
  const onPointerUp = () => (drag.current = null);
  const onWheel = (e) => {
    e.preventDefault();
    const zoom = Math.max(1, Math.min(5, crop.zoom * (e.deltaY < 0 ? 1.08 : 1 / 1.08)));
    setCrop({ ...crop, zoom, ...clamp(crop.x, crop.y, zoom) });
  };

  const apply = () => {
    const canvas = document.createElement("canvas");
    canvas.width = canvas.height = OUTPUT;
    const ctx = canvas.getContext("2d");
    ctx.imageSmoothingQuality = "high";
    const r = OUTPUT / FRAME;
    const dx = (FRAME / 2 + crop.x - dispW / 2) * r;
    const dy = (FRAME / 2 + crop.y - dispH / 2) * r;
    ctx.drawImage(imgRef.current, dx, dy, dispW * r, dispH * r);
    onApply(canvas.toDataURL("image/png"));
  };

  return html`
    <div class="cropper">
      <div
        class="crop-frame"
        onPointerDown=${onPointerDown}
        onPointerMove=${onPointerMove}
        onPointerUp=${onPointerUp}
        onWheel=${onWheel}
      >
        <img
          ref=${imgRef}
          src=${crop.src}
          alt=""
          draggable="false"
          style=${{
            width: `${dispW}px`,
            height: `${dispH}px`,
            transform: `translate(calc(-50% + ${crop.x}px), calc(-50% + ${crop.y}px))`,
          }}
        />
      </div>
      <div class="controls">
        <div class="field">
          <label>Zoom</label>
          <input
            type="range"
            min="1"
            max="5"
            step="0.01"
            value=${crop.zoom}
            onInput=${(e) => {
              const zoom = Number(e.currentTarget.value);
              setCrop({ ...crop, zoom, ...clamp(crop.x, crop.y, zoom) });
            }}
          />
          <div class="help">Drag the image to position it.</div>
        </div>
        <div style=${{ display: "flex", gap: "8px" }}>
          <button class="btn primary" type="button" onClick=${apply}><${Icon} name="check" /> Use picture</button>
          <button class="btn" type="button" onClick=${onCancel}>Cancel</button>
        </div>
      </div>
    </div>
  `;
}
