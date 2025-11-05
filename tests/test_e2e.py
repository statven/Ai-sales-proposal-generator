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
    """
    Фикстура для мокирования всех внешних зависимостей:
    - doc_engine.render_docx_from_template -> FAKE_DOCX_CONTENT
    - db.save_version -> 321
    - ai_core.generate_ai_sections -> Mocked AI Response
    """
    fake_doc = BytesIO(b"FAKE_DOCX_CONTENT")
    # Патчим функцию в том модуле, где она реально определена (doc_engine.py)
    monkeypatch.setattr("backend.app.doc_engine.render_docx_from_template", lambda tpl, context: fake_doc)
    
    fake_save = MagicMock(return_value=321)
    try:
        import backend.app.db as dbmod
        monkeypatch.setattr(dbmod, "save_version", fake_save)
    except Exception:
        monkeypatch.setattr("backend.app.main.db", MagicMock(save_version=fake_save))
        
    class FakeAI:
        async def generate_ai_sections(self, payload, tone="Formal"):
            # Мокируем полный ответ AI для успешного E2E
            return {
                "executive_summary_text": f"AI Summary for {payload.get('client_company_name')}", 
                "used_model": "fake-llm",
                # Добавляем все обязательные поля для успешного выполнения
                "project_mission_text": "AI Mission",
                "solution_concept_text": "AI Solution",
                "financial_justification_text": "AI Justification",
                "payment_terms_text": "AI Payment",
                "development_note": "Dev Note",
                "licenses_note": "Lic Note",
                "support_note": "Support Note"
            }
    monkeypatch.setattr("backend.app.main.ai_core", FakeAI())
    yield

def minimal_payload():
    """Возвращает минимально валидную полезную нагрузку."""
    return {
        "client_company_name": "ООО Тест",
        "provider_company_name": "Provider Co",
        "project_goal": "Test project",
        "scope_description": "Detailed scope",
        "technologies": ["Python", "FastAPI"],
        "deadline": date.today().isoformat(),
        "tone": "Formal",
        "financials": {"development_cost": 1000.0, "licenses_cost": 200.0, "support_cost": 50.0},
        "deliverables": [
            {"title": "Del1", "description": "desc for d1 is long enough", "acceptance_criteria": "accept"}
        ],
        "phases": [
            {"duration_weeks": 2, "tasks": "Requirements, scope"}
        ]
    }

def test_generate_proposal_happy_path():
    """Проверка базового успешного E2E-сценария."""
    resp = client.post("/api/v1/generate-proposal", json=minimal_payload())
    assert resp.status_code == 200
    assert resp.content == b"FAKE_DOCX_CONTENT"
    assert "application/vnd.openxmlformats-officedocument.wordprocessingml.document" in resp.headers["content-type"]

def test_generate_proposal_with_special_chars():
    """Проверка E2E с данными, содержащими специальные символы и Юникод (граничный случай)."""
    payload_with_chars = minimal_payload()
    
    # Добавляем специальные символы в критически важные поля
    payload_with_chars["client_company_name"] = "ООО 'Спец' & $123"
    payload_with_chars["project_goal"] = "Проект с ценой $\\pm 1000$"
    payload_with_chars["deliverables"][0]["title"] = "API & DB-sync (v1.0)"
    
    resp = client.post("/api/v1/generate-proposal", json=payload_with_chars)
    
    # Успешный E2E-тест должен проходить с кодом 200
    assert resp.status_code == 200
    assert resp.content == b"FAKE_DOCX_CONTENT"
    
def test_generate_proposal_empty_deliverables():
    """Проверка E2E с пустыми списками Deliverables и Phases (граничный случай)."""
    payload_minimal_lists = minimal_payload()
    payload_minimal_lists["deliverables"] = []
    payload_minimal_lists["phases"] = []
    
    # AI должен по-прежнему генерировать секции, а DOCX-движок должен уметь 
    # обрабатывать пустые списки при рендеринге таблиц.
    resp = client.post("/api/v1/generate-proposal", json=payload_minimal_lists)
    
    assert resp.status_code == 200
    assert resp.content == b"FAKE_DOCX_CONTENT"
    
# TODO: Добавить тест с очень длинными строками, если pydantic-модели 
# не содержат явных ограничений max_length (сейчас полагаемся на Pydantic).
# Если Pydantic используется, то слишком длинные строки отловятся тестами валидации.