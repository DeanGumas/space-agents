# UI

Preact + htm with no build step and no npm. The browser runs the source files directly.

## Files

```
ui/
  index.html              CSP, import map, entry script
  css/tokens.css          design tokens (colors, radii, shadows) for light + dark
  css/app.css             all component styles (uses tokens only)
  js/main.js              render <App/>, init(), then call("ui_ready")
  js/api.js               call() wrapper, ApiError, event fan-out, JS error capture
  js/state.js             signals + actions (agents, messages, dialogs, toasts, theme)
  js/app.js               layout + dialogs + keyboard shortcuts
  js/views/               Sidebar, Channel (header/timeline/composer), AgentEditor, SettingsDialog, Welcome
  js/components/          common.js (Icon, Avatar, Modal, Field, Switch…), AvatarPicker.js
  js/lib/                 theme, markdown (marked + DOMPurify), emoji list, util (paths, time)
  vendor/                 pinned third-party files + lucide/ icons + licenses (see scripts/vendor.py)
  img/logo.svg
```

## Components and state

htm uses template literals instead of JSX:

```js
html`<${Avatar} agent=${agent} size=${32} />`
html`<${Field} label="Name" error=${err("name")}>…<//>`
```

**Global state** lives in signals in `state.js`. Components read `signal.value` and re-render when it changes. Data
flows one way:

1. A UI action calls a function in `state.js`.
2. That function calls the bridge with `call()`.
3. It updates signals from the response.
4. Pushed events (`handleEvent`) also update signals.

Local UI state (form drafts, open/closed toggles) uses `useState`.

The **agent editor** edits a draft copy and saves through `saveAgent()`. A failed save puts `ApiError.fields` into
the editor's `errors` and jumps to the first tab with an error. Closing with unsaved changes asks for confirmation.

## Theming

- **Tokens:** every color is a CSS variable in `tokens.css`. `:root` holds light, `:root[data-theme="dark"]` holds
  dark.
- **Applying the theme:** `lib/theme.js` resolves `system`/`light`/`dark` and sets `<html data-theme>`, then follows
  OS changes while set to `system`.
- **Sidebar:** dark navy in both themes, Slack-style.
- **Launch flash:** Python sets the native window background to the theme's `--bg` before the page paints
  (`WINDOW_BG` in `main.py`, kept in sync by a test).

## Icons

Lucide SVGs (`ui/vendor/lucide/`) are rendered as CSS masks so they take `currentColor`:
`<${Icon} name="folder" size="sm" />`. Any of the ~2,100 names works. We'll trim the set to what's used before
packaging.

## Security (CSP and sanitizing)

- **CSP** (in `index.html`): no remote origins. No inline scripts except the import map, which is pinned by SHA-256
  (`scripts/update_csp.py`). `'unsafe-eval'` is needed because pywebview's injected bridge builds API stubs with
  `new Function`.
- **Sanitizing:** all model/agent output is rendered through `renderMarkdown()` (marked → DOMPurify). Never inject
  agent text any other way. Links get `target=_blank`, which pywebview opens in the system browser.
- **Static file server:** pywebview serves `ui/` from `127.0.0.1` because ES modules can't load from `file://`. It
  serves static files only; all app actions go through the bridge.

## Conventions

- **Styles:** use tokens, never raw colors.
- **Avatars and icons:** use the `Avatar` and `Icon` components.
- **Errors:** show them with `toast`/`toastError`. Don't use `alert`/`confirm` (unreliable in webviews); use inline
  two-step confirms like the delete button.
- **Wording:** user-facing copy is written for non-technical users, e.g. "Creativity" rather than "temperature",
  and "Data sources" rather than "MCP servers".
