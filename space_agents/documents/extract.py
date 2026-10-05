"""Local text extraction (PLAN.md §2.8). No OCR: scanned pages are reported so the agent can use the GPU server.

Each document becomes a list of "pages":
- PDF: real pages, each classified as text / no_text (likely scanned) / garbled (broken font encoding)
- DOCX/TXT: ~3,000-character chunks split at paragraph boundaries
- XLSX: one page per block of rows per sheet (header repeated), rendered as Markdown tables
- PPTX: one page per slide
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

PAGE_CHARS = 3000
XLSX_ROWS_PER_PAGE = 100
MAX_FILE_BYTES = 200 * 1024 * 1024

TEXT_SUFFIXES = {".txt", ".md", ".csv", ".tsv", ".json", ".log", ".xml", ".yaml", ".yml", ".eml", ".html", ".htm"}
SUPPORTED = {".pdf", ".docx", ".xlsx", ".xlsm", ".pptx", *TEXT_SUFFIXES}

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"


class UnsupportedDocument(ValueError):
    pass


@dataclass
class Page:
    number: int
    text: str
    status: str = "text"  # text | no_text | garbled


@dataclass
class Extracted:
    kind: str
    pages: list[Page]
    info: dict = field(default_factory=dict)


def extract(path: Path) -> Extracted:
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED:
        raise UnsupportedDocument(
            f"{path.name}: {suffix or 'this file type'} can't be read locally. "
            "Supported: PDF, Word (.docx), Excel (.xlsx), PowerPoint (.pptx), and text files."
        )
    if path.stat().st_size > MAX_FILE_BYTES:
        raise UnsupportedDocument(f"{path.name} is larger than {MAX_FILE_BYTES // 1024 // 1024} MB.")
    if suffix == ".pdf":
        return _pdf(path)
    if suffix == ".docx":
        return _docx(path)
    if suffix in (".xlsx", ".xlsm"):
        return _xlsx(path)
    if suffix == ".pptx":
        return _pptx(path)
    return Extracted("text", _chunk(path.read_text(encoding="utf-8", errors="replace").split("\n\n")))


# --- PDF ----------------------------------------------------------------------------------------------------------


def _classify(text: str) -> str:
    stripped = text.strip()
    if len(stripped) < 15:
        return "no_text"
    odd = sum(
        1 for ch in stripped if ch == "\ufffd" or 0xE000 <= ord(ch) <= 0xF8FF or (ord(ch) < 32 and ch not in "\n\r\t")
    )
    return "garbled" if odd / len(stripped) > 0.2 else "text"


def _pdf(path: Path) -> Extracted:
    import pypdfium2 as pdfium  # imported lazily: only needed for PDFs

    try:
        doc = pdfium.PdfDocument(str(path))
    except pdfium.PdfiumError as e:
        raise UnsupportedDocument(
            f"{path.name} couldn't be opened as a PDF ({e}). It may be encrypted or damaged."
        ) from e
    pages = []
    try:
        for i in range(len(doc)):
            page = doc[i]
            textpage = page.get_textpage()
            text = textpage.get_text_bounded().replace("\r\n", "\n")
            textpage.close()
            page.close()
            pages.append(Page(i + 1, text, _classify(text)))
    finally:
        doc.close()
    return Extracted("pdf", pages)


# --- Word -----------------------------------------------------------------------------------------------------------


def _docx_paragraph(p: ET.Element) -> str:
    parts = []
    for el in p.iter():
        if el.tag == f"{W}t" and el.text:
            parts.append(el.text)
        elif el.tag == f"{W}tab":
            parts.append("\t")
        elif el.tag in (f"{W}br", f"{W}cr"):
            parts.append("\n")
    text = "".join(parts).strip()
    style = p.find(f"{W}pPr/{W}pStyle")
    level = re.match(r"Heading(\d)", style.get(f"{W}val", "")) if style is not None else None
    if text and level:
        text = "#" * int(level.group(1)) + " " + text
    return text


def _docx(path: Path) -> Extracted:
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    body = root.find(f"{W}body")
    blocks: list[str] = []
    for el in body if body is not None else []:
        if el.tag == f"{W}p":
            text = _docx_paragraph(el)
            if text:
                blocks.append(text)
        elif el.tag == f"{W}tbl":
            rows = []
            for tr in el.iter(f"{W}tr"):
                cells = [
                    " ".join(filter(None, (_docx_paragraph(p) for p in tc.iter(f"{W}p")))) for tc in tr.iter(f"{W}tc")
                ]
                rows.append("| " + " | ".join(c.replace("|", "\\|").replace("\n", " ") for c in cells) + " |")
            if rows:
                ncols = rows[0].count(" | ") + 1
                rows.insert(1, "|" + " --- |" * ncols)
                blocks.append("\n".join(rows))
    return Extracted("docx", _chunk(blocks))


# --- Excel ----------------------------------------------------------------------------------------------------------


def _col_index(ref: str) -> int:
    n = 0
    for ch in ref:
        if ch.isalpha():
            n = n * 26 + (ord(ch.upper()) - 64)
        else:
            break
    return n - 1


BUILTIN_DATE_FORMATS = {14, 15, 16, 17, 18, 19, 20, 21, 22, 45, 46, 47}
EXCEL_EPOCH = datetime(1899, 12, 30)


def _date_styles(z: zipfile.ZipFile) -> set[int]:
    """Indexes of cell styles (the `s` attribute) whose number format is a date/time."""
    if "xl/styles.xml" not in z.namelist():
        return set()
    root = ET.fromstring(z.read("xl/styles.xml"))
    custom = {int(n.get("numFmtId")): n.get("formatCode", "") for n in root.iter(f"{S}numFmt")}

    def is_date(fmt_id: int) -> bool:
        if fmt_id in BUILTIN_DATE_FORMATS:
            return True
        code = re.sub(r'"[^"]*"|\[[^\]]*\]|\\.', "", custom.get(fmt_id, "")).lower()  # drop literals/colors
        return bool(re.search(r"[dy]|m{3,}", code)) or ("m" in code and "d" in code)

    xfs = root.find(f"{S}cellXfs")
    return {i for i, xf in enumerate(xfs if xfs is not None else []) if is_date(int(xf.get("numFmtId", 0)))}


def _excel_date(value: str) -> str:
    try:
        dt = EXCEL_EPOCH + timedelta(days=float(value))
    except (ValueError, OverflowError):
        return value
    return dt.strftime("%Y-%m-%d") if dt.time() == datetime.min.time() else dt.strftime("%Y-%m-%d %H:%M")


def _xlsx(path: Path) -> Extracted:
    with zipfile.ZipFile(path) as z:
        date_styles = _date_styles(z)
        shared: list[str] = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(f"{S}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{S}t")))
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        targets = {r.get("Id"): r.get("Target") for r in rels.iter(f"{PKG_REL}Relationship")}
        pages: list[Page] = []
        sheets_info = []
        for sheet in wb.iter(f"{S}sheet"):
            name = sheet.get("name")
            target = targets.get(sheet.get(f"{R}id"), "")
            member = target.lstrip("/") if target.startswith("/") else f"xl/{target}"
            if member not in z.namelist():
                continue
            rows: list[list[str]] = []
            with z.open(member) as f:
                for _, el in ET.iterparse(f):
                    if el.tag != f"{S}row":
                        continue
                    values: dict[int, str] = {}
                    for c in el.iter(f"{S}c"):
                        t = c.get("t")
                        v = c.find(f"{S}v")
                        if t == "s" and v is not None and v.text is not None:
                            val = shared[int(v.text)] if int(v.text) < len(shared) else ""
                        elif t == "inlineStr":
                            val = "".join(x.text or "" for x in c.iter(f"{S}t"))
                        elif t == "b" and v is not None:
                            val = "TRUE" if v.text == "1" else "FALSE"
                        else:
                            val = v.text if v is not None and v.text is not None else ""
                            if val and t in (None, "n") and int(c.get("s", 0)) in date_styles:
                                val = _excel_date(val)
                        values[_col_index(c.get("r", "A"))] = val
                    el.clear()
                    if any(values.values()):
                        width = max(values) + 1
                        rows.append([values.get(i, "") for i in range(width)])
            sheets_info.append({"name": name, "rows": len(rows)})
            if not rows:
                pages.append(Page(len(pages) + 1, f"## Sheet: {name}\n\n(empty)"))
                continue
            header, body = rows[0], rows[1:] or []
            ncols = max(len(r) for r in rows)
            for start in range(0, max(len(body), 1), XLSX_ROWS_PER_PAGE):
                chunk = body[start : start + XLSX_ROWS_PER_PAGE]
                lines = [
                    f"## Sheet: {name} (rows {start + 2}–{start + 1 + len(chunk)} of {len(rows)}; row 1 is the header)",
                    "",
                    _md_row(header, ncols),
                    "|" + " --- |" * ncols,
                    *(_md_row(r, ncols) for r in chunk),
                ]
                pages.append(Page(len(pages) + 1, "\n".join(lines)))
    note = "Values shown are as stored (formulas show their last saved result). Use run_python to edit the file."
    return Extracted("xlsx", pages, {"sheets": sheets_info, "note": note})


def _md_row(cells: list[str], ncols: int) -> str:
    padded = list(cells) + [""] * (ncols - len(cells))
    return "| " + " | ".join(c.replace("|", "\\|").replace("\n", " ") for c in padded) + " |"


# --- PowerPoint -----------------------------------------------------------------------------------------------------


def _pptx(path: Path) -> Extracted:
    with zipfile.ZipFile(path) as z:
        slides = sorted(
            (n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
            key=lambda n: int(re.search(r"(\d+)", n.rsplit("/", 1)[1]).group(1)),
        )
        pages = []
        for i, name in enumerate(slides, start=1):
            root = ET.fromstring(z.read(name))
            paras = ["".join(t.text or "" for t in p.iter(f"{A}t")).strip() for p in root.iter(f"{A}p")]
            text = "\n".join(p for p in paras if p)
            pages.append(Page(i, f"## Slide {i}\n\n{text}" if text else f"## Slide {i}\n\n(no text)"))
    return Extracted("pptx", pages)


# --- helpers ----------------------------------------------------------------------------------------------------------


def _chunk(blocks: list[str]) -> list[Page]:
    pages: list[Page] = []
    buf: list[str] = []
    size = 0
    for block in blocks:
        if buf and size + len(block) > PAGE_CHARS:
            pages.append(Page(len(pages) + 1, "\n\n".join(buf)))
            buf, size = [], 0
        while len(block) > PAGE_CHARS * 2:  # very long paragraph: hard split
            pages.append(Page(len(pages) + 1, block[:PAGE_CHARS]))
            block = block[PAGE_CHARS:]
        buf.append(block)
        size += len(block)
    if buf or not pages:
        pages.append(Page(len(pages) + 1, "\n\n".join(buf)))
    return pages
