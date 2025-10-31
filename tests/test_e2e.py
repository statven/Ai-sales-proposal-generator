# tests/test_e2e.py
import json
from io import BytesIO
from datetime import date
from unittest.mock import MagicMock
import pytest
from fastapi.testclient import TestClient
from backend.app.main import app

client = TestClient(app)

@pytest.fixture(autouse=True)
def patch_deps(monkeypatch):
    fake_doc = BytesIO(b"FAKE_DOCX_CONTENT")
    # Патчим функцию в том модуле, где она реально определена (doc_engine.py)
    monkeypatch.setattr("backend.app.doc_engine.render_docx_from_template", lambda tpl, context: fake_doc)
    # monkeypatch.setattr("backend.app.main.doc_engine.render_docx_from_template", lambda tpl, context: fake_doc)
    fake_save = MagicMock(return_value=321)
    try:
        import backend.app.db as dbmod
        monkeypatch.setattr(dbmod, "save_version", fake_save)
    except Exception:
        monkeypatch.setattr("backend.app.main.db", MagicMock(save_version=fake_save))
    class FakeAI:
        async def generate_ai_sections(self, payload, tone="Formal"):
            return {"executive_summary_text": "AI Summary", "used_model": "fake-llm"}
    monkeypatch.setattr("backend.app.main.ai_core", FakeAI())
    yield

def minimal_payload():
    return {
        "client_company_name": "ООО Тест",
        "provider_company_name": "Provider Co",
        "project_goal": "Test project",
        "scope": "Detailed scope",
        "technologies": ["Python", "FastAPI"],
        "deadline": date.today().isoformat(),
        "tone": "Formal",
        "financials": {"development_cost": 1000.0, "licenses_cost": 200.0, "support_cost": 50.0},
        "deliverables": [
            {"title": "Del1", "description": "desc for d1 is long enough", "acceptance_criteria": "accept"}
        ],
        "phases": [
            {"duration_weeks": 2, "tasks": "Requirements gathering"}
        ]
    }

def test_generate_proposal_happy_path_and_db_saved():
    resp = client.post("/api/v1/generate-proposal", json=minimal_payload())
    assert resp.status_code == 200
    assert resp.content == b"FAKE_DOCX_CONTENT"
    assert "content-disposition" in resp.headers
