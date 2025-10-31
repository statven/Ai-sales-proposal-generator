# tests/test_main_api.py
import json
from datetime import date
import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock, patch

from backend.app.main import app

client = TestClient(app)

@pytest.fixture(autouse=True)
def patch_doc_and_db_and_ai(monkeypatch):
    # mock render_docx_from_template to return BytesIO-like object
    fake_doc = MagicMock()
    fake_doc.getvalue.return_value = b"DOCX_BYTES"
    monkeypatch.setattr("backend.app.doc_engine.render_docx_from_template", lambda tpl, context: fake_doc)
    # mock db.save_version and db.get_version if exists
    try:
        import backend.app.db as dbmod
        monkeypatch.setattr(dbmod, "save_version", lambda *args, **kwargs: 1)
        monkeypatch.setattr(dbmod, "get_version", lambda vid: {"payload": json.dumps(args[0])} if False else None)
    except Exception:
        pass
    # mock ai_core.generate_ai_sections to return predictable dict
    class FakeAI:
        async def generate_ai_sections(self, data, tone="Formal"):
            return {
                "executive_summary_text": "AI Summary",
                "project_mission_text": "AI Mission",
                "solution_concept_text": "AI Solution",
                "project_methodology_text": "AI Methodology",
                "financial_justification_text": "AI Justification",
                "payment_terms_text": "AI Payment",
                "development_note": "Dev Note",
                "licenses_note": "Lic Note",
                "support_note": "Support Note"
            }
    monkeypatch.setattr("backend.app.main.ai_core", FakeAI())
    yield

def minimal_payload():
    return {
        "client_name": "ООО Test",
        "provider_name": "Provider Ltd",
        "project_goal": "Goal short",
        "scope_description": "Scope detailed",
        "tone": "Formal",
        "deadline": date.today().isoformat(),
        "financials": {"development_cost": 1000.0, "licenses_cost": 200.0, "support_cost": 50.0},
        "deliverables": [{"title": "D1", "description": "Some desc here 12345", "acceptance_criteria":"Works"}],
        "phases": [{"duration_weeks": 2, "tasks": "Requirements"}]
    }

def test_generate_proposal_happy_path():
    resp = client.post("/api/v1/generate-proposal", json=minimal_payload())
    assert resp.status_code == 200
    assert resp.content == b"DOCX_BYTES"
    assert "content-disposition" in resp.headers
    assert "application/vnd.openxmlformats-officedocument.wordprocessingml.document" in resp.headers["content-type"]

def test_generate_proposal_validation_error():
    # missing provider_name
    bad = minimal_payload()
    bad.pop("provider_name", None)
    r = client.post("/api/v1/generate-proposal", json=bad)
    assert r.status_code == 422
    assert "detail" in r.json()
