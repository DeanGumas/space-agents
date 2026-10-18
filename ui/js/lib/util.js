// Small shared helpers.

export const clone = (v) => JSON.parse(JSON.stringify(v));

// Immutable set by dotted path: setPath(obj, "schedule.active_hours.start", "08:00")
export function setPath(obj, path, value) {
  const [head, ...rest] = path.split(".");
  const copy = Array.isArray(obj) ? [...obj] : { ...obj };
  copy[head] = rest.length ? setPath(obj?.[head] ?? {}, rest.join("."), value) : value;
  return copy;
}

export function getPath(obj, path) {
  return path.split(".").reduce((o, k) => (o == null ? o : o[k]), obj);
}

export function cx(...parts) {
  return parts.filter(Boolean).join(" ");
}

// ---- time ----

const sameDay = (a, b) => a.toDateString() === b.toDateString();

export function formatTime(iso) {
  return new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

export function formatDay(iso) {
  const d = new Date(iso);
  const today = new Date();
  const yesterday = new Date(Date.now() - 86_400_000);
  if (sameDay(d, today)) return "Today";
  if (sameDay(d, yesterday)) return "Yesterday";
  return d.toLocaleDateString([], { weekday: "long", month: "long", day: "numeric" });
}

export function isSameDay(a, b) {
  return sameDay(new Date(a), new Date(b));
}

export function relativeTime(iso) {
  const secs = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  if (secs < 60) return "just now";
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  if (days < 7) return `${days}d ago`;
  return new Date(iso).toLocaleDateString([], { month: "short", day: "numeric" });
}

export function initials(name) {
  const words = (name || "?").trim().split(/\s+/).filter(Boolean);
  const letters = words.length > 1 ? words[0][0] + words[1][0] : (words[0] || "?").slice(0, 2);
  return letters.toUpperCase();
}

/** "Next run 10:07", "Next run tomorrow 07:05", "Next run Mon 07:05" (next_run_at is local time, no zone). */
export function nextRunLabel(iso) {
  if (!iso) return null;
  const when = new Date(iso);
  const time = formatTime(iso);
  const today = new Date();
  const tomorrow = new Date(Date.now() + 86_400_000);
  if (sameDay(when, today)) return `Next run ${time}`;
  if (sameDay(when, tomorrow)) return `Next run tomorrow ${time}`;
  return `Next run ${when.toLocaleDateString([], { weekday: "short" })} ${time}`;
}

export function scheduleSummary(schedule) {
  if (!schedule?.enabled) return null;
  const m = schedule.every_minutes;
  let every = `every ${m} min`;
  if (m === 1440) every = "daily";
  else if (m === 60) every = "hourly";
  else if (m % 60 === 0) every = `every ${m / 60}h`;
  return `Runs ${every}, ${schedule.active_hours.start}–${schedule.active_hours.end}`;
}
