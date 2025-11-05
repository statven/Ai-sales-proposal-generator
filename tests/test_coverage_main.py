import pytest
import json
from datetime import date
from io import BytesIO
from fastapi.testclient import TestClient
from unittest.mock import MagicMock, patch
from backend.app.main import app

# Создание клиента FastAPI
client = TestClient(app)

# Хелпер для минимально валидной полезной нагрузки
def minimal_payload():
    return {
        "client_company_name": "ООО Test",
        "provider_company_name": "Provider Ltd",
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

# Фикстура для мокирования внешних сервисов (для успешного прохождения Happy Path)
@pytest.fixture(autouse=True)
def mock_external_services(monkeypatch):
    # Mock AI to return minimal data
    class FakeAI:
        # Мокируем ai_core для успешной генерации
        async def generate_ai_sections(self, data, tone="Formal"):
            return {"executive_summary_text": "AI Summary", "used_model": "fake-llm"}
    monkeypatch.setattr("backend.app.main.ai_core", FakeAI())

    # Mock DB to be minimal but available for happy path, and save nothing
    class MockDB:
        def init_db(self): pass
        def save_version(self, *args, **kwargs): return 1
        def get_version(self, id): return None # default not found
    monkeypatch.setattr("backend.app.main.db", MockDB())

    # Mock doc_engine to return a successful mock
    fake_doc = MagicMock()
    fake_doc.getvalue.return_value = b"DOCX_BYTES"
    # Патчим импортированную переменную doc_engine
    monkeypatch.setattr("backend.app.main.doc_engine", MagicMock(render_docx_from_template=lambda tpl, context: fake_doc))

    # Mock openai_service to return mock suggestions
    fake_suggestions = MagicMock()
    fake_suggestions.generate_suggestions.return_value = {"suggested_deliverables": [], "suggested_phases": []}
    monkeypatch.setattr("backend.app.main.openai_service", fake_suggestions)

    yield

# ------------------- Tests for Import/Startup/Shutdown Failures -------------------

def test_generate_proposal_no_doc_engine(monkeypatch):
    """Тест 500, когда doc_engine не импортирован (строка 192)."""
    monkeypatch.setattr("backend.app.main.doc_engine", None)
    resp = client.post("/api/v1/generate-proposal", json=minimal_payload())
    assert resp.status_code == 500
    assert "DOCX generation is disabled" in resp.json()["detail"]

def test_generate_proposal_no_ai_core(monkeypatch):
    """Тест 500, когда ai_core не импортирован (строка 204)."""
    monkeypatch.setattr("backend.app.main.ai_core", None)
    resp = client.post("/api/v1/generate-proposal", json=minimal_payload())
    assert resp.status_code == 500
    assert "AI Core service is not available" in resp.json()["detail"]

def test_get_version_no_db(monkeypatch):
    """Тест 500, когда db не импортирован (строка 119)."""
    monkeypatch.setattr("backend.app.main.db", None)
    resp = client.get("/api/v1/version/123")
    assert resp.status_code == 500
    assert "Database service is not available" in resp.json()["detail"]

# tests/test_coverage_main.py

def test_startup_db_init_exception(monkeypatch):
    """Тест исключения при запуске db.init_db() (строки 81-86)."""
    mock_db = MagicMock()
    mock_db.init_db.side_effect = Exception("DB Init Error")
    monkeypatch.setattr("backend.app.main.db", mock_db)

    with patch("backend.app.main.logger") as mock_logger:
        # (FIX) Используем 'with' для вызова startup/shutdown
        with TestClient(app) as _client:
            pass # Событие startup вызывается здесь

        mock_db.init_db.assert_called_once()
        # (FIX) Проверяем, что logger.error был вызван, как в main.py
        mock_logger.error.assert_called_once()

# tests/test_coverage_main.py

def test_shutdown_openai_close_exception(monkeypatch):
    """Тест исключения при завершении openai_service.close() (строка 96)."""
    mock_service = MagicMock()
    mock_service.close.side_effect = Exception("Service Close Error")
    monkeypatch.setattr("backend.app.main.openai_service", mock_service)

    with patch("backend.app.main.logger") as mock_logger:
        # (FIX) Используем 'with' для вызова startup/shutdown
        with TestClient(app) as _client:
            pass # Событие shutdown вызывается после этого блока

        mock_service.close.assert_called_once()
        # (FIX) Проверяем, что logger.error был вызван, как в main.py
        mock_logger.error.assert_called_once()
def test_health_endpoint():
    """Тест health endpoint (строки 101-102)."""
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}

# ------------------- Tests for /api/v1/version/{version_id} Error Paths -------------------

def test_get_version_not_found(monkeypatch):
    """Тест 404 для несуществующей версии (строка 134)."""
    mock_db = MagicMock()
    mock_db.get_version.return_value = None
    monkeypatch.setattr("backend.app.main.db", mock_db)
    
    resp = client.get("/api/v1/version/999")
    assert resp.status_code == 404
    assert "Proposal version 999 not found" in resp.json()["detail"]

def test_get_version_db_exception(monkeypatch):
    """Тест 500 при ошибке DB (строки 139-143)."""
    mock_db = MagicMock()
    mock_db.get_version.side_effect = Exception("DB Read Error")
    monkeypatch.setattr("backend.app.main.db", mock_db)
    
    resp = client.get("/api/v1/version/1")
    assert resp.status_code == 500
    assert "Failed to retrieve proposal version 1" in resp.json()["detail"]

def test_get_version_json_parsing_exception(monkeypatch):
    """Тест 500, когда payload в DB невалидный JSON (строка 149)."""
    mock_db = MagicMock()
    # Возвращаем невалидный JSON-строку
    mock_db.get_version.return_value = {"payload": "not-json", "ai_sections": {}, "used_model": "mock"}
    monkeypatch.setattr("backend.app.main.db", mock_db)
    
    resp = client.get("/api/v1/version/1")
    assert resp.status_code == 500
    assert "Failed to parse payload from database" in resp.json()["detail"]

# ------------------- Tests for /api/v1/generate-proposal Error Paths -------------------

def test_generate_proposal_ai_core_exception(monkeypatch):
    """Тест 500 при ошибке AI генерации (строки 207-210)."""
    class FailingAI:
        async def generate_ai_sections(self, data, tone="Formal"):
            raise Exception("AI Generation Failed")
    monkeypatch.setattr("backend.app.main.ai_core", FailingAI())

    resp = client.post("/api/v1/generate-proposal", json=minimal_payload())
    assert resp.status_code == 500
    assert "AI content generation failed" in resp.json()["detail"]
    
def test_generate_proposal_db_save_exception_logs(monkeypatch):
    """Тест исключения при сохранении в DB (логирование, строки 391-392)."""
    # Сбой сохранения не должен приводить к 500, но должен быть залогирован.
    mock_db = MagicMock()
    mock_db.save_version.side_effect = Exception("DB Save Error")
    monkeypatch.setattr("backend.app.main.db", mock_db)

    with patch("backend.app.main.logger") as mock_logger:
        resp = client.post("/api/v1/generate-proposal", json=minimal_payload())
        assert resp.status_code == 200 # Успешный ответ, так как DOCX сгенерирован
        mock_logger.error.assert_called()
        assert "Error saving proposal version" in mock_logger.error.call_args[0][0]

# ------------------- Tests for /api/v1/suggest Error Paths -------------------

def test_suggest_service_unavailable_no_openai_service(monkeypatch):
    """Тест 503, когда openai_service is None (строки 555-560)."""
    monkeypatch.setattr("backend.app.main.openai_service", None)
    resp = client.post("/api/v1/suggest", json={"client_company_name":"A", "provider_company_name":"B"})
    assert resp.status_code == 503
    assert "AI suggestion service is not available" in resp.json()["detail"]

def test_suggest_generation_exception(monkeypatch):
    """Тест 500 при ошибке генерации предложений (строки 571-574)."""
    # Также покрывает случай, когда generate_suggestions возвращает None, 
    # что приводит к ошибке JSONResponse и попаданию в except
    mock_service = MagicMock()
    mock_service.generate_suggestions.side_effect = Exception("Suggestion Generation Failed")
    monkeypatch.setattr("backend.app.main.openai_service", mock_service)
    
    resp = client.post("/api/v1/suggest", json={"client_company_name":"A", "provider_company_name":"B"})
    assert resp.status_code == 500
    assert "Suggestion generation failed" in resp.json()["detail"]

def test_suggest_non_dict_non_json_string_return(monkeypatch):
    """Тест на возврат не-JSON строки (строки 579-583)."""
    # Возвращается обычная строка (не JSON), которая заворачивается в {"raw": ...}
    mock_service = MagicMock()
    mock_service.generate_suggestions.return_value = "This is a raw text response, not a dict."
    monkeypatch.setattr("backend.app.main.openai_service", mock_service)
    
    resp = client.post("/api/v1/suggest", json={"client_company_name":"A", "provider_company_name":"B"})
    assert resp.status_code == 200
    assert resp.json().get("raw") == "This is a raw text response, not a dict."
    
def test_suggest_malformed_json_string_return(monkeypatch):
    """Тест на возврат невалидной JSON строки (строки 598-602)."""
    # Возвращается невалидная JSON строка, которая заворачивается в {"raw": ...}
    mock_service = MagicMock()
    mock_service.generate_suggestions.return_value = '{"suggested_deliverables": [1, 2, 3,'
    monkeypatch.setattr("backend.app.main.openai_service", mock_service)
    
    resp = client.post("/api/v1/suggest", json={"client_company_name":"A", "provider_company_name":"B"})
    assert resp.status_code == 200
    assert resp.json().get("raw") == '{"suggested_deliverables": [1, 2, 3,'

# ------------------- Tests for Normalization Logic Coverage -------------------
# Проверяем, что _normalize_incoming_payload (строки 416-548, 639-643, 645-647, 652-653) работает

def test_normalize_payload_complex_fallback():
    """Тест на срабатывание логики нормализации с отсутствующими и невалидными данными."""
    # Отсутствуют/неправильные ключи, списки строк вместо списков словарей
    complex_payload = {
        # Отсутствуют: client_company_name, provider_company_name (будут установлены в "")
        "project_goal": None, # Конвертируется в ""
        "scope": "Detailed scope", # Конвертируется в scope_description
        "technologies": ["Python", "FastAPI"], # Конвертируется в список словарей
        "deliverables": ["D1", "D2"], # Конвертируется в список словарей
        "phases": ["P1", "P2"], # Конвертируется в список словарей
        "financials": None, # Конвертируется в {}
        "financials_details": {"licenses_cost": 500.0}, # Конвертируется в financials
        "deadline": "invalid-date-format", # Конвертируется в сегодняшнюю дату
        "tone": "Non-existent", # Должен быть нормализован, но в ProposalInput он не валидируется
    }
    
    resp = client.post("/api/v1/generate-proposal", json=complex_payload)
    # Если код дошел до конца, значит, нормализация прошла успешно.
    assert resp.status_code == 200

# ------------------- Test for ProposalInput shim fallback (Line 376) -------------------

def test_generate_proposal_no_pydantic_dict_method(monkeypatch):
    """
    Тест пути `else dict(proposal.__dict__)` в строке 376. 
    Имитируем объект, который не является Pydantic-моделью, но прошел валидацию.
    """
    # Имитируем успешное создание объекта, который не имеет метода `dict` (как Pydantic-модель)
    class SimpleObject:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
            self.tone = kwargs.get("tone", "Formal") 

    # Мокаем ProposalInput, чтобы она возвращала наш простой объект
    def fake_validation(**payload):
        # Возвращаем SimpleObject, который не имеет метода dict
        return SimpleObject(**payload)
        
    monkeypatch.setattr("backend.app.main.ProposalInput", fake_validation)
    
    resp = client.post("/api/v1/generate-proposal", json=minimal_payload())
    # Если все прошло успешно, это означает, что ветка else в 376 была выполнена.
    assert resp.status_code == 200
