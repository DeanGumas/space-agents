"""Run reports (PLAN.md §3.2 step 5): what a run delivers, through the `post_report` tool.

    {"summary": "3 POs delivered, 1 supplier delay",
     "items": [{"level": "action", "title": "PO 45821 ship date slipped", "detail": "...",
                "suggested_actions": ["Notify program lead"], "sources": ["Acme Supplier Update.pdf p. 2"]}]}

A tool call is more reliable across local models than JSON mode. If the model never calls it, its final text becomes
a single `info` item. The report is stored on the run (and rendered as a card); its Markdown form is the message
content, so history, previews, and anything that only reads text still make sense.
"""

from __future__ import annotations

import re
from typing import Any

LEVELS = ("info", "action", "urgent")
# Compared with the agent's recent runs. Small models are better at labelling repeats than at leaving them out, so
# repeats are kept but marked: the card shows unchanged items as "still open", and notifications skip them.
CHANGES = ("new", "changed", "unchanged")
LEVEL_LABELS = {"info": "Info", "action": "Action needed", "urgent": "Urgent"}
MAX_ITEMS = 20

POST_REPORT_SPEC: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "post_report",
        "description": (
            "Deliver this run's result to the user. Call it exactly once, when the task is done; the run ends there."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "One line on what you found, e.g. '3 POs delivered, 1 supplier delay'.",
                },
                "items": {
                    "type": "array",
                    "description": "Findings, most important first. Empty if there's nothing new.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "level": {
                                "type": "string",
                                "enum": list(LEVELS),
                                "description": "info: FYI. action: the user should do something. urgent: today.",
                            },
                            "change": {
                                "type": "string",
                                "enum": list(CHANGES),
                                "description": (
                                    "Compared with your recent runs: new (not reported before), changed (reported, "
                                    "but something is different now), or unchanged (already reported, still open)."
                                ),
                            },
                            "title": {"type": "string"},
                            "detail": {"type": "string"},
                            "suggested_actions": {"type": "array", "items": {"type": "string"}},
                            "sources": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "File names (with pages) the finding comes from.",
                            },
                        },
                        "required": ["level", "change", "title"],
                    },
                },
            },
            "required": ["summary"],
        },
    },
}


def _text(value: Any, limit: int) -> str:
    return " ".join(str(value).split())[:limit] if isinstance(value, (str, int, float)) else ""


def _strings(value: Any, count: int, limit: int) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [s for s in (_text(v, limit) for v in value[:count]) if s]


def parse_report(args: dict[str, Any]) -> dict[str, Any]:
    """Validate post_report's arguments. Raises ValueError with a message the model can act on."""
    summary = _text(args.get("summary"), 300)
    if not summary:
        raise ValueError("post_report needs a one-line summary.")
    raw_items = args.get("items") or []
    if not isinstance(raw_items, list):
        raise ValueError("items must be a list of findings.")
    items = []
    for i, raw in enumerate(raw_items[:MAX_ITEMS]):
        if not isinstance(raw, dict):
            raise ValueError(f"items[{i}] must be an object with level and title.")
        level = str(raw.get("level") or "info").lower()
        if level not in LEVELS:
            raise ValueError(f"items[{i}].level must be one of: {', '.join(LEVELS)}.")
        change = str(raw.get("change") or "new").lower()
        if change not in CHANGES:
            raise ValueError(f"items[{i}].change must be one of: {', '.join(CHANGES)}.")
        title = _text(raw.get("title"), 200)
        if not title:
            raise ValueError(f"items[{i}] needs a title.")
        detail = str(raw.get("detail") or "").strip()[:4000]
        items.append(
            {
                "level": level,
                "change": change,
                "title": title,
                "detail": detail,
                "suggested_actions": _strings(raw.get("suggested_actions"), 5, 200),
                "sources": _strings(raw.get("sources"), 10, 200),
            }
        )
    return {"summary": summary, "items": items}


def fallback_report(text: str) -> dict[str, Any]:
    """The model finished without post_report: its answer becomes a single info item."""
    text = text.strip()
    first = next((line.strip("#*_ ").strip() for line in text.splitlines() if line.strip()), "")
    first = re.sub(r"^(summary|report)\s*:\s*", "", first, flags=re.IGNORECASE)
    sentence = re.split(r"(?<=[.!?])\s", first, maxsplit=1)[0]  # the first sentence makes a better headline
    summary = (sentence if len(sentence) <= 200 else sentence[:197].rstrip() + "…") or "The run finished."
    item = {
        "level": "info",
        "change": "new",
        "title": summary[:200],
        "detail": text,
        "suggested_actions": [],
        "sources": [],
    }
    items = [item]
    return {"summary": summary, "items": items if text else []}


def report_markdown(report: dict[str, Any]) -> str:
    lines = [f"**{report['summary']}**"]
    for item in report["items"]:
        note = {"changed": " (changed)", "unchanged": " (still open, reported before)"}.get(item.get("change"), "")
        lines.append(f"\n**{LEVEL_LABELS[item['level']]}: {item['title']}**{note}")
        if item["detail"]:
            lines.append(item["detail"])
        lines += [f"- Suggested: {a}" for a in item["suggested_actions"]]
        if item["sources"]:
            lines.append(f"- Sources: {', '.join(item['sources'])}")
    if not report["items"]:
        lines.append("\nNothing new to report.")
    return "\n".join(lines)


def fresh_items(report: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Findings that are new or changed since the agent's recent runs (what's worth a notification)."""
    return [item for item in (report or {}).get("items", []) if item.get("change", "new") != "unchanged"]


def top_level(report: dict[str, Any] | None, fresh_only: bool = False) -> str | None:
    """The most serious level in a report (for notifications and badges), or None if it has no (fresh) items."""
    items = fresh_items(report) if fresh_only else (report or {}).get("items", [])
    levels = [item["level"] for item in items]
    return max(levels, key=LEVELS.index) if levels else None


def _ids(text: str) -> set[str]:
    """Reference numbers (PO, invoice, ticket…): 4+ digits, not years."""
    return {n for n in re.findall(r"\d{4,}", text) if not (len(n) == 4 and n[:2] in ("19", "20"))}


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2}


def looks_like(title: str, earlier: str) -> bool:
    """Same reference number, or mostly the same words."""
    if _ids(title) & _ids(earlier):
        return True
    a, b = _words(title), _words(earlier)
    return bool(a and b) and len(a & b) / len(a | b) >= 0.6


def possible_repeats(report: dict[str, Any], recent_runs: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    """Findings marked new that look like ones from recent runs: (title, earlier title, when it was reported)."""
    earlier = [
        (item["title"], (run["finished_at"] or run["created_at"])[:16].replace("T", " "))
        for run in recent_runs
        for item in run["report"]["items"]
    ]
    repeats = []
    for item in report["items"]:
        if item["change"] != "new":
            continue
        match = next(((title, when) for title, when in earlier if looks_like(item["title"], title)), None)
        if match:
            repeats.append((item["title"], *match))
    return repeats


def repeats_question(repeats: list[tuple[str, str, str]]) -> str:
    lines = [f'- "{title}" looks like "{earlier}" (reported {when} UTC)' for title, earlier, when in repeats]
    return (
        "Not posted yet. These findings are marked new, but look like ones you already reported:\n"
        + "\n".join(lines)
        + "\nFor each one, set change to unchanged if nothing is different, or changed if something is (say what "
        "in the detail). If it really is new, keep it. Then call post_report again."
    )
