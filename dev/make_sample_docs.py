"""Generate realistic sample documents for trying agents locally and for tests.

    .venv/bin/python dev/make_sample_docs.py [output_dir]     # default: dev/sample-docs/ (git-ignored)

Grant that folder (Read & write) to an agent, then try e.g. "Read the Acme supplier update and update the PO tracker
with any new due dates and statuses."
Requires the dev dependencies (fpdf2, openpyxl, python-docx, python-pptx).
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

PO_ROWS = [
    # PO, Vendor, Item, Qty, Unit price, Due date, Status
    (45790, "Globex Industrial", "Hydraulic fittings, 1/2in", 400, 3.25, date(2026, 10, 2), "Delivered"),
    (45821, "Acme Corp", "Titanium fasteners M6", 2500, 0.84, date(2026, 10, 9), "Open"),
    (45833, "Initech Supply", "Wiring harness assy", 60, 112.00, date(2026, 10, 12), "Open"),
    (45907, "Acme Corp", "Aluminum bracket 6061", 300, 14.50, date(2026, 10, 1), "Open"),
    (45942, "Umbrella Materials", "Composite panel 4x8", 24, 389.00, date(2026, 10, 15), "Open"),
    (46010, "Acme Corp", "Stainless washers M6", 5000, 0.06, date(2026, 10, 13), "Open"),
    (46025, "Globex Industrial", "O-ring kit", 50, 22.40, date(2026, 10, 21), "Open"),
]


def make_po_tracker(path: Path) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "Open POs"
    ws.append(["PO Number", "Vendor", "Item", "Qty", "Unit Price", "Due Date", "Status", "Notes"])
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F3A5F")
    for row in PO_ROWS:
        ws.append([*row, ""])
    for r in range(2, ws.max_row + 1):
        ws.cell(r, 5).number_format = "$#,##0.00"
        ws.cell(r, 6).number_format = "yyyy-mm-dd"
    for col, width in zip("ABCDEFGH", (12, 20, 28, 8, 11, 12, 11, 40), strict=True):
        ws.column_dimensions[col].width = width
    vendors = wb.create_sheet("Vendors")
    vendors.append(["Vendor", "Contact", "Email"])
    vendors.append(["Acme Corp", "Dana Whitfield", "dana.whitfield@acme.example"])
    vendors.append(["Globex Industrial", "Raj Patel", "rpatel@globex.example"])
    vendors.append(["Initech Supply", "Sam Lee", "orders@initech.example"])
    vendors.append(["Umbrella Materials", "Ana Ruiz", "ana.ruiz@umbrella.example"])
    wb.save(path)


NEXT = {"new_x": "LMARGIN", "new_y": "NEXT"}  # fpdf2: move to the next line after each multi_cell


def _pdf():
    from fpdf import FPDF

    pdf = FPDF()
    pdf.set_auto_page_break(True, margin=18)
    pdf.set_font("Helvetica", size=11)
    return pdf


def make_supplier_update(path: Path) -> None:
    pdf = _pdf()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 15)
    pdf.cell(0, 10, "Acme Corp - Order Status Update", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", size=11)
    body = [
        "Date: September 30, 2026",
        "To: Purchasing Department",
        "From: Dana Whitfield, Acme Corp Customer Operations",
        "",
        "Thank you for your continued business. Please note the following updates to your open purchase orders:",
        "",
        "1. PO 45821 (Titanium fasteners M6, qty 2,500): due to a heat-treat furnace outage, this order will ship "
        "late. The new expected delivery date is October 14, 2026 (originally October 9).",
        "",
        "2. PO 45907 (Aluminum bracket 6061, qty 300): shipped complete via FastFreight, tracking FF-88213. "
        "Delivered September 30, 2026.",
        "",
        "3. PO 46010 (Stainless washers M6, qty 5,000): raw material is constrained. We can ship 3,000 pieces by "
        "October 13 and the remaining 2,000 by October 20, 2026. Please confirm whether a partial shipment is "
        "acceptable.",
        "",
        "We apologize for the inconvenience. Contact me directly with any questions.",
    ]
    for line in body:
        pdf.multi_cell(0, 6, line, new_x="LMARGIN", new_y="NEXT")
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 8, "Appendix: Shipping terms", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", size=10)
    pdf.multi_cell(0, 5, "All shipments FOB origin. Partial shipments are invoiced separately. " * 6, **NEXT)
    pdf.output(str(path))


def make_scorecard(path: Path, pages: int = 120) -> None:
    pdf = _pdf()
    vendors = ["Acme Corp", "Globex Industrial", "Initech Supply", "Umbrella Materials"]
    for i in range(1, pages + 1):
        pdf.add_page()
        pdf.set_font("Helvetica", "B", 13)
        v = vendors[i % len(vendors)]
        pdf.cell(0, 9, f"Q3 2026 Supplier Scorecard - Section {i}: {v}", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", size=10)
        on_time = 80 + (i * 7) % 19
        pdf.multi_cell(
            0,
            5,
            f"On-time delivery for {v} in this category was {on_time}%. Quality acceptance rate was "
            f"{95 + i % 5}%. Lead times averaged {10 + i % 9} days. "
            + "Performance is reviewed monthly against contracted service levels. "
            * 8,
            **NEXT,
        )
        if i == 87:
            pdf.multi_cell(
                0,
                5,
                "CORRECTIVE ACTION: Globex Industrial was issued a corrective action request (CAR-2026-114) for "
                "repeated packaging damage on hydraulic fittings. Response due November 1, 2026.",
                **NEXT,
            )
    pdf.output(str(path))


def make_scanned(path: Path) -> None:
    """A PDF page with only a drawing and no text layer, like a scan."""
    pdf = _pdf()
    pdf.add_page()
    pdf.set_fill_color(230, 230, 230)
    pdf.rect(20, 20, 170, 120, style="F")
    pdf.output(str(path))


def make_meeting_notes(path: Path) -> None:
    from docx import Document

    doc = Document()
    doc.add_heading("Supplier Kickoff - Composite Panels", level=1)
    doc.add_paragraph("Date: September 22, 2026. Attendees: Purchasing, Engineering, Umbrella Materials.")
    doc.add_heading("Decisions", level=2)
    doc.add_paragraph("First article inspection required before the second lot.", style="List Bullet")
    doc.add_paragraph("Umbrella to provide cure-cycle data with each shipment.", style="List Bullet")
    doc.add_heading("Action items", level=2)
    table = doc.add_table(rows=1, cols=3)
    for cell, text in zip(table.rows[0].cells, ("Owner", "Action", "Due"), strict=True):
        cell.text = text
    for row in (("Ana Ruiz", "Send cure-cycle spec", "Sep 29"), ("Purchasing", "Issue PO 45942 revision", "Oct 1")):
        cells = table.add_row().cells
        for cell, text in zip(cells, row, strict=True):
            cell.text = text
    doc.save(path)


def make_deck(path: Path) -> None:
    from pptx import Presentation

    prs = Presentation()
    for title, body in (
        ("Q4 Sourcing Plan", "Dual-source fasteners; consolidate washers with Acme"),
        ("Risks", "Acme heat-treat capacity; titanium pricing"),
    ):
        slide = prs.slides.add_slide(prs.slide_layouts[1])
        slide.shapes.title.text = title
        slide.placeholders[1].text = body
    prs.save(path)


def make_all(out: Path) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    files = {
        "PO Tracker.xlsx": make_po_tracker,
        "Acme Supplier Update.pdf": make_supplier_update,
        "Q3 Supplier Scorecard.pdf": make_scorecard,
        "Scanned Receipt.pdf": make_scanned,
        "Kickoff Meeting Notes.docx": make_meeting_notes,
        "Q4 Sourcing Plan.pptx": make_deck,
    }
    for name, fn in files.items():
        fn(out / name)
    return [out / n for n in files]


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent / "sample-docs"
    for p in make_all(target):
        print(f"  {p}  ({p.stat().st_size:,} bytes)")
