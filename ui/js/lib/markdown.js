// Markdown → sanitized HTML. ALL model output must go through renderMarkdown(): it can echo untrusted
// email/document content, and the page has a bridge into Python, so unsanitized HTML would be dangerous.

import { marked } from "marked";
import DOMPurify from "dompurify";

marked.setOptions({ gfm: true, breaks: true });

DOMPurify.addHook("afterSanitizeAttributes", (node) => {
  if (node.tagName === "A") {
    // pywebview opens target=_blank links in the system browser, never inside the app.
    node.setAttribute("target", "_blank");
    node.setAttribute("rel", "noopener noreferrer");
  }
});

const PURIFY = {
  FORBID_TAGS: ["style", "form", "input", "button", "textarea", "select", "iframe", "object", "embed"],
  FORBID_ATTR: ["style"],
};

export function renderMarkdown(text) {
  return DOMPurify.sanitize(marked.parse(text ?? ""), PURIFY);
}
