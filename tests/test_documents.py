import shutil

import pytest

from space_agents.documents.extract import UnsupportedDocument, extract
from space_agents.documents.library import DocumentLibrary, parse_pages
from space_agents.store import Store


def test_pdf_text_and_scanned_pages(sample_docs):
    ex = extract(sample_docs / "Acme Supplier Update.pdf")
    assert ex.kind == "pdf" and len(ex.pages) == 2
    assert "October 14, 2026" in ex.pages[0].text
    assert all(p.status == "text" for p in ex.pages)
    scanned = extract(sample_docs / "Scanned Receipt.pdf")
    assert [p.status for p in scanned.pages] == ["no_text"]


def test_xlsx_rows_and_dates(sample_docs):
    ex = extract(sample_docs / "PO Tracker.xlsx")
    first = ex.pages[0].text
    assert "## Sheet: Open POs" in first
    assert "| 45821 | Acme Corp | Titanium fasteners M6 | 2500 | 0.84 | 2026-10-09 | Open |" in first
    assert {s["name"] for s in ex.info["sheets"]} == {"Open POs", "Vendors"}


def test_docx_headings_and_tables(sample_docs):
    text = extract(sample_docs / "Kickoff Meeting Notes.docx").pages[0].text
    assert "# Supplier Kickoff - Composite Panels" in text
    assert "## Action items" in text
    assert "| Ana Ruiz | Send cure-cycle spec | Sep 29 |" in text


def test_pptx_slides(sample_docs):
    pages = extract(sample_docs / "Q4 Sourcing Plan.pptx").pages
    assert len(pages) == 2 and "Risks" in pages[1].text


def test_unsupported(tmp_path):
    (tmp_path / "x.exe").write_bytes(b"MZ")
    with pytest.raises(UnsupportedDocument):
        extract(tmp_path / "x.exe")
    (tmp_path / "bad.pdf").write_bytes(b"not a pdf")
    with pytest.raises(UnsupportedDocument):
        extract(tmp_path / "bad.pdf")


def test_parse_pages():
    assert parse_pages("2,4,10-12", 11) == [2, 4, 10, 11]
    assert parse_pages("", 5) == []
    with pytest.raises(ValueError):
        parse_pages("abc", 5)


@pytest.fixture
def library(tmp_path):
    store = Store(tmp_path / "data")
    lib = DocumentLibrary(store)
    yield lib
    lib.close()
    store.close()


async def test_read_pages_and_paging_hint(library, sample_docs):
    text = await library.read(sample_docs / "Q3 Supplier Scorecard.pdf", "Scorecard.pdf", max_chars=3000)
    assert text.startswith("Scorecard.pdf: 120 pages (pdf)")
    assert "--- page 1 ---" in text and "call read_document with pages=" in text
    one = await library.read(sample_docs / "Q3 Supplier Scorecard.pdf", "Scorecard.pdf", pages="87")
    assert "CAR-2026-114" in one and "--- page 86 ---" not in one


async def test_search_ranks_the_right_page(library, sample_docs):
    out = await library.search(sample_docs / "Q3 Supplier Scorecard.pdf", "S.pdf", "corrective action packaging")
    assert out.splitlines()[3].startswith("- page 87:")


async def test_scanned_pages_reported(library, sample_docs):
    text = await library.read(sample_docs / "Scanned Receipt.pdf", "Scanned Receipt.pdf")
    assert "no text layer" in text


async def test_cache_extracts_once_per_version(library, sample_docs, tmp_path, monkeypatch):
    import space_agents.documents.library as libmod

    calls = []
    real = libmod.extract
    monkeypatch.setattr(libmod, "extract", lambda p: calls.append(p) or real(p))
    doc = tmp_path / "copy.pdf"
    shutil.copy(sample_docs / "Acme Supplier Update.pdf", doc)
    await library.read(doc, doc.name)
    await library.read(doc, doc.name)
    renamed = tmp_path / "renamed.pdf"
    shutil.copy(doc, renamed)  # same content, new path → found by hash, not re-extracted
    await library.read(renamed, renamed.name)
    assert len(calls) == 1
    shutil.copy(sample_docs / "Scanned Receipt.pdf", doc)  # new content at the same path
    await library.read(doc, doc.name)
    assert len(calls) == 2
