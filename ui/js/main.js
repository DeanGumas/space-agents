// Entry point: render the app, load initial data over the bridge, then tell Python the UI is ready
// (which starts event delivery).

import { render } from "preact";
import { html } from "htm/preact";
import { App } from "./app.js";
import { init } from "./state.js";
import { call, reportError } from "./api.js";
import { applyTheme } from "./lib/theme.js";

applyTheme("system"); // until the saved preference loads
render(html`<${App} />`, document.getElementById("app"));

init()
  .then(() => call("ui_ready", { userAgent: navigator.userAgent }))
  .catch(reportError);
