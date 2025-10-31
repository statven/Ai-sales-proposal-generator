import io
from backend.app.doc_engine import render_docx_from_template, _format_currency

def test_format_currency_basic():
    assert _format_currency(45000) in ("45 000,00", "45000.00",)  # locale variants acceptable

def test_render_docx_minimal(tmp_path):
    # Prepare minimal template file — copy real template or create simple .docx with placeholders
    from docx import Document
    tpl = Document()
    tpl.add_paragraph("Client: {{client_company_name}}")
    pfile = tmp_path / "tpl.docx"
    tpl.save(pfile)

    ctx = {"client_company_name": "Acme Co", "development_cost": 1000, "licenses_cost": 0, "support_cost": 0}
    bio = render_docx_from_template(str(pfile), ctx)
    assert bio is not None
    data = bio.getvalue()
    assert b"Acme Co" in data
