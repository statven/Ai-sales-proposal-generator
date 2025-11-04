import json
from datetime import date
import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock, patch
from io import BytesIO

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
        # Патчим мок в main, если db не импортируется
        monkeypatch.setattr("backend.app.main.db", MagicMock(save_version=lambda *args, **kwargs: 1))
        pass
        
    # mock ai_core.generate_ai_sections to return predictable dict
    class FakeAI:
        async def generate_ai_sections(self, data, tone="Formal"):
            return {
                "executive_summary_text": "AI Summary",
                "project_mission_text": "AI Mission",
                "solution_concept_text": "AI Solution",
                "financial_justification_text": "AI Justification",
                "payment_terms_text": "AI Payment",
                "development_note": "Dev Note",
                "licenses_note": "Lic Note",
                "support_note": "Support Note",
                "_used_model": "test-model-v1"
            }
    monkeypatch.setattr("backend.app.main.ai_core", FakeAI())
    
    # Mock doc_engine to ensure it's not None for non-error tests
    monkeypatch.setattr("backend.app.main.doc_engine", MagicMock(render_docx_from_template=lambda tpl, context: BytesIO(b"DOCX_BYTES")))

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
        "deliverables": [
            {"title": "D1", "description": "Some desc here 12345", "acceptance_criteria":"Works"}
        ],
        "phases": [
            {"duration_weeks": 2, "tasks": "Requirements"}
        ]
    }

def test_generate_proposal_happy_path():
    resp = client.post("/api/v1/generate-proposal", json=minimal_payload())
    assert resp.status_code == 200
    assert resp.content == b"DOCX_BYTES"
    assert "content-disposition" in resp.headers
    assert "application/vnd.openxmlformats-officedocument.wordprocessingml.document" in resp.headers["content-type"]
    assert resp.headers["X-Proposal-Version"] == "1"

def test_generate_proposal_validation_error():
    # client_name слишком короткое (менее 3 символов)
    payload = minimal_payload()
    payload["client_name"] = "A" 
    resp = client.post("/api/v1/generate-proposal", json=payload)
    assert resp.status_code == 422
    data = resp.json()
    assert "detail" in data

# --- Новые тесты для покрытия ошибок ---

def test_generate_proposal_doc_engine_unavailable(monkeypatch):
    """Тест 503, когда doc_engine == None."""
    monkeypatch.setattr("backend.app.main.doc_engine", None)
    resp = client.post("/api/v1/generate-proposal", json=minimal_payload())
    assert resp.status_code == 503
    assert "Document engine is not available" in resp.json()["detail"]

@pytest.mark.asyncio
async def test_generate_proposal_ai_failure_raises_500(monkeypatch):
    """Тест 500, когда AI генерация вызывает исключение."""
    
    class FailingAI:
        async def generate_ai_sections(self, *args, **kwargs):
            raise Exception("AI internal error")
        # Мок для fallback-функции, чтобы не вызывать ошибку на try/except
        async def generate_ai_sections_safe(self, *args, **kwargs):
            raise Exception("Fallback failed too")
            
    monkeypatch.setattr("backend.app.main.ai_core", FailingAI())

    resp = client.post("/api/v1/generate-proposal", json=minimal_payload())
    # В коде есть два уровня try-except. Если AI упал, он должен вызвать 500.
    assert resp.status_code == 500
    assert "AI generation failed: Exception: AI internal error" in resp.json()["detail"]


def test_generate_proposal_rendering_failure_raises_500(monkeypatch):
    """Тест 500, когда DOCX рендеринг вызывает исключение."""
    def failing_renderer(tpl, context):
        raise ValueError("DOCX render error")
        
    monkeypatch.setattr("backend.app.doc_engine.render_docx_from_template", failing_renderer)
    
    resp = client.post("/api/v1/generate-proposal", json=minimal_payload())
    assert resp.status_code == 500
    assert "DOCX rendering failed: ValueError: DOCX render error" in resp.json()["detail"]

def test_generate_proposal_byte_extraction_failure_raises_500(monkeypatch):
    """Тест 500, когда DOCX движок возвращает некорректный тип (нет getvalue)."""
    # Возвращаем простой объект, который не BytesIO, не bytes и не имеет getvalue()
    monkeypatch.setattr("backend.app.doc_engine.render_docx_from_template", lambda tpl, context: [1, 2, 3])
    
    resp = client.post("/api/v1/generate-proposal", json=minimal_payload())
    assert resp.status_code == 500
    assert "DOCX generation returned unexpected type" in resp.json()["detail"]
