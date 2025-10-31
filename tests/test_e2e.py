from fastapi.testclient import TestClient
from backend.app.main import app
from unittest.mock import patch, MagicMock
import json

client = TestClient(app)

@patch("backend.app.doc_engine.render_docx_from_template")
@patch("backend.app.ai_core.generate_ai_sections")
def test_generate_proposal_happy(mock_ai, mock_doc):
    mock_ai.return_value = {
        "executive_summary_text":"Sum",
        "project_mission_text":"Mission",
    }
    mock_doc.return_value = MagicMock(getvalue=lambda: b"DOCX_BYTES")
    payload = {
        "client_company_name":"Acme",
        "provider_company_name":"Provider",
        "project_goal":"Goal",
        "scope":"Scope",
        "technologies":["Python"],
        "deadline":"2026-01-01",
        "tone":"Formal",
        "financials":{"development_cost":1000, "licenses_cost":10, "support_cost":5},
        "deliverables":[],
        "phases":[]
    }
    r = client.post("/api/v1/generate-proposal", json=payload)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/vnd.openxmlformats-officedocument")
    assert r.content == b"DOCX_BYTES"
