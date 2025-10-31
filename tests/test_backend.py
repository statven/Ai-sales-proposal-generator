import pytest
import json
from datetime import date
from pydantic import ValidationError
from fastapi.testclient import TestClient
from unittest.mock import MagicMock, patch

# Импорты из вашего приложения (предполагаем структуру backend.app.*)
# NOTE: Для запуска тестов может потребоваться правильная настройка PYTHONPATH.
from backend.app.models import ProposalInput, Financials, Deliverable, Phase
from backend.app.ai_core import _extract_json_blob, _safe_stringify, process_ai_content # Добавляем process_ai_content
from backend.app.doc_engine import _format_currency, render_docx_from_template 
from backend.app.main import app 

# Создание клиента для тестирования FastAPI
client = TestClient(app)

# ------------------- 1. Unit Tests (Pydantic & Helpers) -------------------

# (Ваши существующие тесты для _extract_json_blob, _safe_stringify, _format_currency остаются)

# Дополнительные тесты для _extract_json_blob
def test_extract_json_blob_nested():
    """Тестирование извлечения JSON с вложенными скобками."""
    text = 'Before {{JSON}} here. {"key": 1, "nested": [1, 2, {"a": "b"}]} More text.'
    expected = '{"key": 1, "nested": [1, 2, {"a": "b"}]}'
    assert _extract_json_blob(text) == expected

def test_extract_json_blob_unclosed_end():
    """Тестирование, когда JSON не закрывается в конце строки."""
    text = 'JUNK {"key": "value", "incomplete": ['
    assert _extract_json_blob(text) == "" # Ожидаем пустую строку, так как нет закрывающей }

# Unit Tests: Pydantic Validation (models.py)
def test_proposal_input_validation_min_length():
    """Тестирование минимальной длины полей."""
    with pytest.raises(ValidationError):
        ProposalInput(
            client_name="A",  # Слишком коротко (min_length=2)
            provider_name="Provider",
            project_goal="Goal",
            scope_description="Scope",
            tone="Formal",
        )

def test_proposal_input_validation_valid():
    """Тестирование валидного объекта."""
    data = {
        "client_name": "Acme Inc.",
        "provider_name": "Our Co",
        "project_goal": "Goal",
        "scope_description": "Scope",
        "tone": "Marketing",
        "deadline": date.today().isoformat()
    }
    proposal = ProposalInput(**data)
    assert proposal.client_name == "Acme Inc."
    assert proposal.tone == "Marketing"

# ------------------- 2. LLM Robustness Test (ai_core) -------------------

def test_process_ai_content_json_failure_and_fallback(mocker):
    """
    Тестирование логики регенерации (fallback) в process_ai_content:
    Первый вызов возвращает невалидный JSON, второй вызов заполняет недостающие поля.
    """
    # 1. Мокируем первый вызов LLM: невалидный JSON, но с 'executive_summary_text'
    raw_bad_json = "This is junk text. {'executive_summary_text': 'First try summary.', 'project_mission_text': 'M1'} More junk."
    # 2. Мокируем второй вызов LLM (регенерация): валидный JSON с недостающими ключами
    raw_good_json = "JUNK HERE: {\"solution_concept_text\": \"S2\", \"financial_justification_text\": \"F2\"} END"
    
    # Мокируем функцию, которая делает вызов к LLM
    mock_generate = mocker.patch(
        "backend.app.ai_core.generate_ai_json",
        side_effect=[
            (raw_bad_json, "gpt-4"),  # Первый вызов LLM (основной)
            (raw_good_json, "gpt-4o") # Второй вызов LLM (регенерация)
        ]
    )

    # Входные данные (Payload)
    input_payload = ProposalInput(
        client_name="TestClient",
        provider_name="TestProvider",
        project_goal="TestGoal",
        scope_description="TestScope",
        tone="Formal",
    ).dict()

    # Запуск тестируемой функции
    result, model = process_ai_content(input_payload)

    # Проверки
    assert mock_generate.call_count == 2
    assert model == "gpt-4o" # Возвращается модель, использованная в последнем успешном вызове

    # Проверяем, что _extract_json_blob извлек JSON из второго вызова
    # Проверяем, что текст из первого вызова не потерян (хотя он не JSON, но попал как текст)
    assert "First try summary." in result["executive_summary_text"]
    
    # Проверяем, что недостающие поля заполнены из второго (валидного) ответа
    assert result["solution_concept_text"] == "S2"
    assert result["financial_justification_text"] == "F2"
    # Проверяем, что поля, которые должны были быть пустыми, остались пустыми
    assert not result.get("payment_terms_text") 
    

# ------------------- 3. Automated E2E Test (FastAPI) -------------------

# FIX: Замокать doc_engine, чтобы не требовать template.docx и не тратить время на docx
# FIX: Замокать db, чтобы не писать в базу при каждом тесте
@patch("backend.app.doc_engine.render_docx_from_template")
@patch("backend.app.db.save_version", MagicMock(return_value=1)) # Мокаем сохранение в DB
def test_generate_proposal_happy_path(mock_render_docx, mocker):
    """
    Тестирование основного API эндпоинта (/api/v1/generate-proposal)
    по счастливой ветке: мокаем LLM, мокаем docx, проверяем ответ FastAPI.
    """
    # 1. Мокирование ответа LLM (успешный JSON)
    mock_ai_result = {
        "executive_summary_text": "AI Summary",
        "project_mission_text": "AI Mission",
        "solution_concept_text": "AI Solution",
        "project_methodology_text": "AI Methodology",
        "financial_justification_text": "AI Justification",
        "payment_terms_text": "AI Payment Terms",
        "development_note": "AI Dev Note",
        "licenses_note": "AI License Note",
        "support_note": "AI Support Note",
    }
    
    # Мокируем функцию, которая обрабатывает AI-контент в ai_core.py
    mocker.patch(
        "backend.app.main.process_ai_content",
        return_value=(mock_ai_result, "gpt-4")
    )

    # 2. Мокирование вывода DOCX (возвращаем фиктивные байты)
    # Используем MagicMock, который имитирует io.BytesIO
    mock_docx_bytes = MagicMock()
    mock_docx_bytes.getvalue.return_value = b"DOCX_FILE_CONTENT"
    mock_render_docx.return_value = mock_docx_bytes

    # 3. Входные данные для API
    input_data = {
        "client_name": "ООО Test Клиент",
        "provider_name": "ИП Test Продавец",
        "project_goal": "Разработать тестовый продукт",
        "scope_description": "Тестовое описание",
        "tone": "Formal",
        "financials": {
            "development_cost": 10000.0,
            "licenses_cost": 500.0,
            "support_cost": 250.0,
        },
        "deadline": date(2026, 1, 1).isoformat(),
        "deliverables": [
            {"title": "D1", "description": "Desc1", "acceptance_criteria": "A1"}
        ]
    }

    # 4. Вызов API
    response = client.post("/api/v1/generate-proposal", json=input_data)

    # 5. Проверки
    assert response.status_code == 200
    assert response.content == b"DOCX_FILE_CONTENT"
    assert "application/vnd.openxmlformats-officedocument.wordprocessingml.document" in response.headers["content-type"]
    
    # Проверка заголовка Content-Disposition (для E2E)
    expected_filename_part = "ООО Test Клиент_Разработать тестовый продукт.docx"
    assert expected_filename_part in response.headers["content-disposition"]
    
    # Проверка вызова docx-движка
    assert mock_render_docx.called
    
    # Проверка, что docx-движок получил все данные, включая AI-секции
    # Проверяем, что в контексте есть как минимум AI-секция
    call_context = mock_render_docx.call_args[1]['context']
    assert call_context['executive_summary_text'] == "AI Summary"
    assert call_context['total_investment_cost'] == 10750.0 # 10000 + 500 + 250
    assert len(call_context['deliverables_list']) == 1

@patch("backend.app.doc_engine.render_docx_from_template", MagicMock(return_value=MagicMock()))
@patch("backend.app.ai_core.process_ai_content") # Мокаем ai_core
def test_generate_proposal_llm_failure(mock_process_ai_content):
    """
    Тестирование API, когда LLM (или ai_core) выбрасывает исключение.
    """
    # Мокируем ai_core на выброс ошибки
    mock_process_ai_content.side_effect = Exception("LLM connection timeout")

    input_data = {
        "client_name": "TestClient",
        "provider_name": "TestProvider",
        "project_goal": "Goal",
        "scope_description": "Scope",
        "tone": "Formal",
    }
    
    # Вызов API
    response = client.post("/api/v1/generate-proposal", json=input_data)

    # Проверки
    assert response.status_code == 500
    assert response.json()["detail"].startswith("AI generation failed:")

# ------------------- 4. Robust Error Handling (Концепция) -------------------

# NOTE: Реализация "Robust error handling" требует изменения кода в main.py, 
# а не только написания тестов.

# Пример того, как улучшить main.py для Robust Error Handling:

# # backend/app/main.py (Улучшенный фрагмент)
# @app.post("/api/v1/generate-proposal", ...)
# async def generate_proposal(...):
#     # ... (Pydantic validation)
#     
#     try:
#         # 1. AI Generation (with retry/fallback in ai_core)
#         ai_sections, used_model = process_ai_content(context_for_ai)
#     except Exception as e:
#         logger.error("Critical AI generation failure: %s", e)
#         # В случае критического сбоя, возвращаем пользовательское сообщение
#         raise HTTPException(status_code=500, detail="AI generation failed: The proposal content could not be created. Please check your API key or try again.")
# 
#     # ... (Rest of the logic)
#     try:
#         doc_bytes = render_docx_from_template(...)
#     except Exception as e:
#         # Логируем ошибку, но возвращаем 500 с понятным сообщением
#         logger.error("Critical DOCX rendering failure: %s", e)
#         raise HTTPException(status_code=500, detail="Document rendering failed. The template may be corrupted.")
#     
#     # ... (Success response)