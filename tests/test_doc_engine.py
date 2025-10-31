# tests/test_doc_engine.py
import pytest
from pathlib import Path
from docx import Document
from io import BytesIO
from backend.app import doc_engine as de

def _create_template(path: Path):
    """
    Create a simple docx template with placeholders and two tables:
     - Deliverables table with headers: Deliverable | Description | Acceptance
     - Timeline table with headers: Phase | Duration | Key Tasks
    Also include signature placeholders.
    """
    doc = Document()
    doc.add_paragraph("Sales Proposal")
    doc.add_paragraph("for {{client_company_name}}")
    doc.add_paragraph("Prepared by {{provider_company_name}}")
    doc.add_paragraph("Date: {{current_date}}")
    doc.add_paragraph("1. Executive Summary")
    doc.add_paragraph("{{executive_summary_text}}")
    doc.add_paragraph("1.2 Project Mission")
    doc.add_paragraph("{{project_mission_text}}")

    # Deliverables table header
    t = doc.add_table(rows=1, cols=3)
    t.rows[0].cells[0].text = "Deliverable"
    t.rows[0].cells[1].text = "Description"
    t.rows[0].cells[2].text = "Acceptance"

    # Timeline table header
    t2 = doc.add_table(rows=1, cols=3)
    t2.rows[0].cells[0].text = "Phase"
    t2.rows[0].cells[1].text = "Duration"
    t2.rows[0].cells[2].text = "Key Tasks"

    # Signature area
    doc.add_paragraph("Accepted By (Client)")
    doc.add_paragraph("Name: {{client_signature_name}}")
    doc.add_paragraph("Date: {{client_signature_date}}")
    doc.add_paragraph("Approved By (Provider)")
    doc.add_paragraph("Name: {{provider_signature_name}}")
    doc.add_paragraph("Date: {{provider_signature_date}}")

    doc.save(str(path))

def test_render_docx_inserts_texts_and_tables(tmp_path):
    tpl = tmp_path / "tpl.docx"
    _create_template(tpl)

    context = {
        "client_company_name": "ООО Рога и Копыта",
        "provider_company_name": "Digital Forge Group",
        "current_date": "2025-11-01",
        "executive_summary_text": "This is an executive summary from AI.",
        "project_mission_text": "Mission: deliver value.",
        "client_signature_name": "Иван Иванов",
        "client_signature_date": "2025-11-01",
        "provider_signature_name": "Пётр Петров",
        "provider_signature_date": "2025-11-02",
        # deliverables_list expected by doc_engine
        "deliverables_list": [
            {"title": "CRM Integration Plan", "description": "Detailed plan for CRM integration", "acceptance": "Client approval"},
            {"title": "Data Migration", "description": "Migrate catalog and customer data", "acceptance": "Data validated"}
        ],
        # phases_list expected by doc_engine
        "phases_list": [
            {"phase_name": "Phase 1", "duration": "2 weeks", "tasks": "Gather requirements"},
            {"phase_name": "Phase 2", "duration": "4 weeks", "tasks": "Implement migration"}
        ],
        "development_cost": 45000,
        "licenses_cost": 5000,
        "support_cost": 2500,
        "total_investment_cost": 52500
    }

    out = de.render_docx_from_template(str(tpl), context)
    assert out is not None
    assert isinstance(out, BytesIO)

    # open output docx and inspect text & tables
    doc = Document(out)
    text = "\n".join(p.text for p in doc.paragraphs)
    # placeholder replacements
    assert "ООО Рога и Копыта" in text
    assert "Digital Forge Group" in text
    assert "This is an executive summary from AI." in text
    assert "Mission: deliver value." in text

    # signatures should be present (replaced)
    assert "Иван Иванов" in text
    assert "Пётр Петров" in text
    assert "2025-11-01" in text
    assert "2025-11-02" in text

    # tables: deliverables - find table with header 'Deliverable'
    found_deliv_table = None
    for tbl in doc.tables:
        hdr = [c.text.strip().lower() for c in tbl.rows[0].cells]
        if any("deliverable" in h for h in hdr):
            found_deliv_table = tbl
            break
    assert found_deliv_table is not None
    # header + 2 data rows
    assert len(found_deliv_table.rows) >= 1 + 2
    # check first data row contains title and description and acceptance
    first_data = found_deliv_table.rows[1].cells
    assert "CRM Integration Plan" in first_data[0].text
    assert "Detailed plan for CRM integration" in first_data[1].text
    assert "Client approval" in first_data[2].text

    # timeline table
    found_tl_table = None
    for tbl in doc.tables:
        hdr = [c.text.strip().lower() for c in tbl.rows[0].cells]
        if any("phase" in h for h in hdr) and any("duration" in h for h in hdr):
            found_tl_table = tbl
            break
    assert found_tl_table is not None
    assert len(found_tl_table.rows) >= 1 + 2
    # check phase names present
    assert "Phase 1" in found_tl_table.rows[1].cells[0].text
    assert "2 weeks" in found_tl_table.rows[1].cells[1].text
