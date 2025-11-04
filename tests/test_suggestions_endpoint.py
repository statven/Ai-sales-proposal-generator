import pytest
import json
from datetime import date
from io import BytesIO
from fastapi.testclient import TestClient
from unittest.mock import MagicMock
from backend.app.main import app

client = TestClient(app)

# Helper function required for generate-proposal tests
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

# ------------------- Suggestion Endpoints Tests -------------------

def test_suggest_service_unavailable(monkeypatch):
    """Тест 503, когда openai_service не доступен."""
    # ensure openai_service is None in module
    monkeypatch.setattr("backend.app.main.openai_service", None)
    resp = client.post("/api/v1/suggest", json={"client_name":"A", "provider_name":"B"})
    assert resp.status_code == 503
    assert "AI suggestion service is not available" in resp.json()["detail"]

def test_suggest_happy(monkeypatch):
    """Тест 200 и корректный возврат данных от мока."""
    fake = MagicMock()
    fake.generate_suggestions.return_value = {
        "suggested_deliverables": [{"title":"T","description":"D","acceptance":"A"}], 
        "suggested_phases":[{"phase_name":"P","duration": "2 weeks", "tasks":"T"}]
    }
    monkeypatch.setattr("backend.app.main.openai_service", fake)
    pld = {"client_name":"A","provider_name":"B","project_goal":"G","scope":"S","tone":"Formal"}
    r = client.post("/api/v1/suggest", json=pld)
    assert r.status_code == 200
    data = r.json()
    assert "suggested_deliverables" in data
    assert len(data["suggested_deliverables"]) == 1

# ------------------- Proposal Error Path Tests (Fixes for current failures) -------------------

def test_generate_proposal_rendering_failure_raises_500(monkeypatch):
    """Тест 500, когда DOCX рендеринг вызывает исключение."""
    def failing_renderer(tpl, context):
        raise ValueError("DOCX render error")

    # FIX: Патчим метод на мок-объекте doc_engine, который используется в main.py
    monkeypatch.setattr("backend.app.main.doc_engine.render_docx_from_template", failing_renderer)

    resp = client.post("/api/v1/generate-proposal", json=minimal_payload())
    assert resp.status_code == 500
    # Дополнительная проверка на сообщение об ошибке для уверенности
    assert "DOCX rendering failed: ValueError: DOCX render error" in resp.json()["detail"]


def test_generate_proposal_byte_extraction_failure_raises_500(monkeypatch):
    """Тест 500, когда DOCX движок возвращает некорректный тип (нет getvalue)."""
    # Возвращаем простой объект, который не BytesIO, не bytes и не имеет getvalue()

    # FIX: Патчим метод на мок-объекте doc_engine, который используется в main.py
    monkeypatch.setattr("backend.app.main.doc_engine.render_docx_from_template", lambda tpl, context: [1, 2, 3])

    resp = client.post("/api/v1/generate-proposal", json=minimal_payload())
    assert resp.status_code == 500
    # Дополнительная проверка на сообщение об ошибке для уверенности
    assert "DOCX generation returned unexpected type" in resp.json()["detail"]
