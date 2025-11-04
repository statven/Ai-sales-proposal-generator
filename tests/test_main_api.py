# tests/test_main_api.py

import json
from datetime import date
import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock

from backend.app.main import app

client = TestClient(app)

# --- Mock Data and Classes for Fixtures ---

# Используем минимальный payload для моков
MOCK_PAYLOAD = {
    "client_name": "ООО Test",
    "provider_name": "Provider Ltd",
    "project_goal": "Goal short",
    "scope_description": "Scope detailed",
    "tone": "Formal",
    "deadline": date.today().isoformat(),
    "financials": {"development_cost": 1000.0, "licenses_cost": 200.0, "support_cost": 50.0},
    "deliverables": [{"title": "D1", "description": "Some desc here 12345", "acceptance_criteria": "Works"}],
    "phases": [{"duration_weeks": 2, "tasks": "Requirements"}]
}
MOCK_VERSION_ID = 42
MOCK_AI_SECTIONS = {
    "executive_summary_text": "AI Summary", 
    "used_model": "test-llm", 
    "project_mission_text": "AI Mission",
    "solution_concept_text": "AI Solution",
}

class FakeDB:
    """Имитация поведения базы данных."""
    def get_version(self, version_id: int):
        # Возвращает данные только для MOCK_VERSION_ID
        if version_id == MOCK_VERSION_ID:
            return {
                "id": MOCK_VERSION_ID,
                "created_at": "2023-10-27T10:00:00",
                "payload": json.dumps(MOCK_PAYLOAD),
                "ai_sections": json.dumps(MOCK_AI_SECTIONS)
            }
        return None
    
    def get_all_versions(self):
        # Возвращаем список для эндпоинта /versions
        return [{
            "id": MOCK_VERSION_ID,
            "created_at": "2023-10-27T10:00:00",
            "client_name": MOCK_PAYLOAD["client_name"],
            "project_goal": MOCK_PAYLOAD["project_goal"]
        }]
    
    def save_version(self, payload: dict, ai_sections: dict, used_model: str):
        return 1 # ID новой версии

class FakeAI:
    """Имитация успешного AI-ответа."""
    async def generate_ai_sections(self, data, tone="Formal"):
        return MOCK_AI_SECTIONS
    async def generate_suggestions(self, data, tone="Formal"):
        return {"suggested_deliverables": [], "suggested_phases": []}

# --- Fixtures ---

@pytest.fixture
def minimal_payload():
    return MOCK_PAYLOAD.copy()

@pytest.fixture(autouse=True)
def patch_doc_and_db_and_ai(monkeypatch):
    # 1. Mock Doc Engine
    fake_doc = MagicMock()
    fake_doc.getvalue.return_value = b"DOCX_BYTES"
    # Патчим функцию в doc_engine
    monkeypatch.setattr("backend.app.doc_engine.render_docx_from_template", lambda tpl, context: fake_doc)
    
    # 2. Mock DB (Используем класс FakeDB для мокинга)
    # Это позволяет избежать ошибки 'local variable 'args' referenced before assignment' 
    # в старом коде мока get_version
    try:
        import backend.app.db as dbmod
        monkeypatch.setattr(dbmod, "save_version", FakeDB().save_version)
        monkeypatch.setattr(dbmod, "get_version", FakeDB().get_version)
        monkeypatch.setattr(dbmod, "get_all_versions", FakeDB().get_all_versions)
    except Exception:
        # Fallback для моков, если db не импортируется (как в main.py)
        monkeypatch.setattr("backend.app.main.db", FakeDB())

    # 3. Mock AI Core
    monkeypatch.setattr("backend.app.main.ai_core", FakeAI())
    yield


# --- Test API Endpoints (Success Paths) ---

def test_generate_proposal_happy_path(minimal_payload):
    resp = client.post("/api/v1/generate-proposal", json=minimal_payload)
    assert resp.status_code == 200
    assert resp.content == b"DOCX_BYTES"
    assert "content-disposition" in resp.headers
    assert "application/vnd.openxmlformats-officedocument.wordprocessingml.document" in resp.headers["content-type"]

def test_generate_proposal_validation_error(minimal_payload):
    """Тестирует Pydantic ValidationError (Covers 422 response)."""
    # missing provider_name
    bad = minimal_payload
    bad.pop("provider_name", None)
    r = client.post("/api/v1/generate-proposal", json=bad)
    assert r.status_code == 422
    assert "detail" in r.json()

def test_get_all_versions():
    """Тестирует /api/v1/versions (должен вернуть 200)."""
    resp = client.get("/api/v1/versions")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["id"] == MOCK_VERSION_ID

def test_get_version_by_id():
    """Тестирует /api/v1/versions/{id} (должен вернуть 200)."""
    resp = client.get(f"/api/v1/versions/{MOCK_VERSION_ID}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == MOCK_VERSION_ID
    assert "payload" in data

def test_get_version_not_found():
    """Тестирует /api/v1/versions/{id} при отсутствии записи (должен вернуть 404)."""
    resp = client.get("/api/v1/versions/999") 
    assert resp.status_code == 404
    # После добавления эндпоинта в main.py, детализированное сообщение будет возвращено
    assert resp.json()["detail"] == "Version not found"

# --- Test Error Paths (500 Responses) ---

def test_generate_proposal_ai_fail(monkeypatch, minimal_payload):
    """
    Тестирует сбой AI-ядра.
    """
    # 1. Мокаем AI-ядро, чтобы оно вызывало исключение
    class FakeAIFail:
        async def generate_ai_sections(self, data, tone="Formal"):
            raise RuntimeError("AI service failed catastrophically")
        async def generate_suggestions(self, data, tone="Formal"):
            raise RuntimeError("AI service failed catastrophically")

    monkeypatch.setattr("backend.app.main.ai_core", FakeAIFail())
    
    # 2. Вызываем эндпоинт
    resp = client.post("/api/v1/generate-proposal", json=minimal_payload)
    
    # 3. Проверяем 500
    assert resp.status_code == 500
    # После корректировки main.py, сообщение об ошибке будет содержать текст исключения.
    assert "AI service failed catastrophically" in resp.json()["detail"]

def test_generate_proposal_doc_fail(monkeypatch, minimal_payload):
    """
    Тестирует сбой движка генерации DOCX.
    """
    # 1. Мокаем doc_engine.render_docx_from_template, чтобы она вызывала исключение
    def fail_render(tpl, context):
        raise RuntimeError("DOCX generation failed")
    
    monkeypatch.setattr("backend.app.doc_engine.render_docx_from_template", fail_render)
    
    # 2. Вызываем эндпоинт
    resp = client.post("/api/v1/generate-proposal", json=minimal_payload)
    
    # 3. Проверяем 500
    assert resp.status_code == 500
    assert "DOCX generation failed" in resp.json()["detail"]