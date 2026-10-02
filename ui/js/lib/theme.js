// Applies the theme preference ("system" | "light" | "dark") as <html data-theme="light|dark">.

const media = window.matchMedia("(prefers-color-scheme: dark)");
let preference = "system";

function apply() {
  const resolved = preference === "system" ? (media.matches ? "dark" : "light") : preference;
  document.documentElement.dataset.theme = resolved;
  document.documentElement.dataset.resolvedTheme = resolved;
}

media.addEventListener("change", () => preference === "system" && apply());

export function applyTheme(pref) {
  preference = pref || "system";
  apply();
}

export const THEMES = [
  { value: "system", label: "System", icon: "monitor" },
  { value: "light", label: "Light", icon: "sun" },
  { value: "dark", label: "Dark", icon: "moon" },
];
