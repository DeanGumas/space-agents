"""Document cache + read/search over extracted pages (PLAN.md §2.8).

Files are extracted once per content version: lookups go (path, size, mtime) → sha256 → cached pages. Search uses an
SQLite FTS5 index with BM25 ranking. Extraction runs on a single dedicated thread because pypdfium2 isn't
thread-safe (a separate worker process is the planned hardening step).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from space_agents.documents.extract import Extracted, extract
from space_agents.store import Store, now_iso

log = logging.getLogger(__name__)

DEFAULT_READ_CHARS = 12_000


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class DocInfo:
    sha256: str
    kind: str
    page_count: int
    info: dict
    page_status: dict[str, list[int]]  # status -> page numbers (only non-"text" statuses)


def parse_pages(spec: str | None, page_count: int) -> list[int]:
    """'3', '1-5', '2,4,10-12' → page numbers within range. Empty → []."""
    if not spec:
        return []
    pages: list[int] = []
    for part in str(spec).replace(" ", "").split(","):
        if not part:
            continue
        a, _, b = part.partition("-")
        try:
            start = int(a)
            end = int(b) if b else start
        except ValueError as e:
            raise ValueError(f"Invalid page range {spec!r}. Use forms like '3', '1-5', or '2,4,10-12'.") from e
        pages.extend(range(max(1, start), min(page_count, end) + 1))
    return sorted(set(pages))


def _status_ranges(numbers: list[int]) -> str:
    out, start, prev = [], None, None
    for n in numbers + [None]:
        if start is None:
            start = prev = n
        elif n is not None and n == prev + 1:
            prev = n
        else:
            out.append(f"{start}" if start == prev else f"{start}–{prev}")
            start = prev = n
    return ", ".join(x for x in out if x != "None")


class DocumentLibrary:
    def __init__(self, store: Store):
        self._store = store
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="doc-extract")

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    async def load(self, path: Path) -> DocInfo:
        """Return cached info for `path`, extracting it first if this version hasn't been seen."""
        st = path.stat()
        row = self._store.query_one(
            "SELECT * FROM doc_cache WHERE path = ? AND size = ? AND mtime = ?", (str(path), st.st_size, st.st_mtime)
        )
        loop = asyncio.get_running_loop()
        if row is None:
            sha = await loop.run_in_executor(self._executor, file_sha256, path)
            row = self._store.query_one("SELECT * FROM doc_cache WHERE sha256 = ?", (sha,))
            if row is None:
                extracted: Extracted = await loop.run_in_executor(self._executor, extract, path)
                self._insert(sha, path, st.st_size, st.st_mtime, extracted)
                row = self._store.query_one("SELECT * FROM doc_cache WHERE sha256 = ?", (sha,))
            else:
                self._store.execute(
                    "UPDATE doc_cache SET path = ?, size = ?, mtime = ? WHERE sha256 = ?",
                    (str(path), st.st_size, st.st_mtime, sha),
                )
        self._store.execute("UPDATE doc_cache SET used_at = ? WHERE sha256 = ?", (now_iso(), row["sha256"]))
        statuses: dict[str, list[int]] = {}
        for r in self._store.query(
            "SELECT page, status FROM doc_pages WHERE sha256 = ? AND status != 'text' ORDER BY page", (row["sha256"],)
        ):
            statuses.setdefault(r["status"], []).append(r["page"])
        return DocInfo(row["sha256"], row["kind"], row["page_count"], json.loads(row["info"]), statuses)

    def _insert(self, sha: str, path: Path, size: int, mtime: float, ex: Extracted) -> None:
        self._store.execute("DELETE FROM doc_pages WHERE sha256 = ?", (sha,))
        self._store.execute("DELETE FROM doc_fts WHERE sha256 = ?", (sha,))
        self._store.executemany(
            "INSERT INTO doc_pages (sha256, page, status, text) VALUES (?, ?, ?, ?)",
            [(sha, p.number, p.status, p.text) for p in ex.pages],
        )
        self._store.executemany(
            "INSERT INTO doc_fts (sha256, page, text) VALUES (?, ?, ?)",
            [(sha, p.number, p.text) for p in ex.pages if p.status == "text"],
        )
        self._store.execute(
            "INSERT OR REPLACE INTO doc_cache (sha256, path, size, mtime, kind, page_count, info, used_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (sha, str(path), size, mtime, ex.kind, len(ex.pages), json.dumps(ex.info), now_iso()),
        )

    def header(self, name: str, doc: DocInfo) -> str:
        unit = {"pdf": "pages", "pptx": "slides"}.get(doc.kind, "pages")
        lines = [f"{name}: {doc.page_count} {unit} ({doc.kind})"]
        if doc.kind == "xlsx":
            sheets = ", ".join(f"{s['name']} ({s['rows']} rows)" for s in doc.info.get("sheets", []))
            lines.append(f"Sheets: {sheets}")
        if doc.page_status.get("no_text"):
            lines.append(
                f"Pages {_status_ranges(doc.page_status['no_text'])} have no text layer (likely scanned images); "
                "they can't be read locally."
            )
        if doc.page_status.get("garbled"):
            lines.append(f"Pages {_status_ranges(doc.page_status['garbled'])} have unreadable (garbled) text.")
        if doc.info.get("note"):
            lines.append(doc.info["note"])
        return "\n".join(lines)

    async def read(self, path: Path, name: str, pages: str | None = None, max_chars: int = DEFAULT_READ_CHARS) -> str:
        doc = await self.load(path)
        wanted = parse_pages(pages, doc.page_count) or list(range(1, doc.page_count + 1))
        rows = self._store.query(
            f"SELECT page, status, text FROM doc_pages WHERE sha256 = ? AND page IN ({','.join('?' * len(wanted))})"
            " ORDER BY page",
            (doc.sha256, *wanted),
        )
        out, used, last = [self.header(name, doc), ""], 0, None
        for r in rows:
            block = f"--- page {r['page']} ---\n{r['text'].strip() or '(no text)'}"
            if used + len(block) > max_chars and last is not None:
                remaining = wanted[wanted.index(last) + 1 :]
                out.append(
                    f"[Stopped at page {last} to keep this short. {len(remaining)} more requested page(s): "
                    f"call read_document with pages='{remaining[0]}-{remaining[-1]}', or use search_document.]"
                )
                break
            out.append(block)
            used += len(block)
            last = r["page"]
        return "\n".join(out)

    async def search(self, path: Path, name: str, query: str, limit: int = 5) -> str:
        doc = await self.load(path)
        terms = re.findall(r"\w+", query)
        if not terms:
            return "Search query was empty."
        fts = " OR ".join('"' + t.replace('"', "") + '"' for t in terms[:12])
        rows = self._store.query(
            "SELECT page, snippet(doc_fts, 2, '«', '»', ' … ', 24) AS snip FROM doc_fts"
            " WHERE doc_fts MATCH ? AND sha256 = ? ORDER BY bm25(doc_fts) LIMIT ?",
            (fts, doc.sha256, limit),
        )
        if not rows:
            return f"{self.header(name, doc)}\n\nNo matches for {query!r}."
        hits = "\n".join(f"- page {r['page']}: {r['snip'].strip()}" for r in rows)
        tip = "Use read_document with pages=… to read more."
        return f"{self.header(name, doc)}\n\nBest matches for {query!r}:\n{hits}\n\n{tip}"
