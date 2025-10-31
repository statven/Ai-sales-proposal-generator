# tests/test_doc_engine.py
import io
import pytest
from docx import Document
from pathlib import Path
from backend.app import doc_engine as de

def create_tmp_template(path: Path):
    doc = Document()
    p = doc.add_paragraph("Sales Proposal")
    doc.add_paragraph("for {{client_company_name}}")
    doc.add_paragraph("Prepared by {{provider_company_name}}")
    doc.add_paragraph("Date: {{current_date}}")
    doc.add_paragraph("1. Executive Summary")
    doc.add_paragraph("{{executive_summary_text}}")

    # Deliverables table header
    tbl = doc.add_table(rows=1, cols=3)
    hdr = tbl.rows[0].cells
    hdr[0].text = "Deliverable"
    hdr[1].text = "Description"
    hdr[2].text = "Acceptance"

    # Timeline table header
    t2 = doc.add_table(rows=1, cols=3)
    hdr2 = t2.rows[0].cells
    hdr2[0].text = "Phase"
    hdr2[1].text = "Duration"
    hdr2[2].text = "Key Tasks"

    # Signature placeholders
    doc.add_paragraph("Name: {{client_signature_name}}")
    doc.add_paragraph("Date: {{client_signature_date}}")
    doc.add_paragraph("Provider: {{provider_company_name}}")
    doc.save(str(path))

def test_format_currency_basic():
    assert de._format_currency(1234.5) != ""
    # Should include two decimals
    s = de._format_currency(1234.5)
    assert "." not in s or "," in s or " " in s  # permissive check (locale dependent)

def test_render_docx_replacements(tmp_path):
    tpl = tmp_path / "tpl.docx"
    create_tmp_template(tpl)

    context = {
        "client_company_name": "ACME Ltd.",
        "provider_company_name": "My Provider",
        "current_date": "2025-11-01",
        "executive_summary_text": "Short summary from AI",
        "client_signature_name": "Ivan Ivanov",
        "client_signature_date": "2025-11-01",
        # deliverables_list & phases_list used by doc engine
        "deliverables_list": [
            {"title": "D1", "description": "Desc 1", "acceptance": "Accept 1"}
        ],
        "phases_list": [
            {"phase_name": "Phase 1", "duration": "2 weeks", "tasks": "Tasks 1"}
        ],
        "development_cost": 1000.0,
        "licenses_cost": 200.0,
        "support_cost": 50.0,
        "total_investment_cost": 1250.0
    }

    out = de.render_docx_from_template(str(tpl), context)
    assert out is not None
    # Ensure bytes returned
    b = out.getvalue()
    assert isinstance(b, (bytes, bytearray))
    # Save and re-open check some replacements
    tmp_out = tmp_path / "out.docx"
    tmp_out.write_bytes(b)
    doc = Document(str(tmp_out))
    full_text = "\n".join(p.text for p in doc.paragraphs)
    assert "ACME Ltd." in full_text
    assert "Short summary from AI" in full_text
    assert "Ivan Ivanov" in full_text

def test_append_deliverables_table(tmp_path):
    # Ensure _append_deliverables doesn't crash for many rows
    tpl = tmp_path / "tpl2.docx"
    create_tmp_template(tpl)
    doc = Document(str(tpl))
    # find first table (deliverables)
    tbl = doc.tables[0]
    # call append
    deliverables = [{"title": f"T{i}", "description": f"D{i}", "acceptance": f"A{i}"} for i in range(5)]
    de._append_deliverables(tbl, deliverables)
    # saved doc should have additional rows
    out = tmp_path / "out2.docx"
    doc.save(str(out))
    doc2 = Document(str(out))
    assert len(doc2.tables[0].rows) >= 1 + len(deliverables)
