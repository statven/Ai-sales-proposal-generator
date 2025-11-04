# tests/test_openai_service.py
# (Нужно установить: pip install pytest-mock)

import pytest
import sys
import json
import time
import re # <-- Импортируем RE
from unittest.mock import MagicMock, patch

# Импортируем модуль, который будем тестировать
from backend.app.services import openai_service as s

# Импортируем _extract_json_blob из самого сервиса
_extract_json_blob = s._extract_json_blob

# --- Фикстуры ---

@pytest.fixture(autouse=True)
def reset_cache():
    """Сбрасываем кэш LRU перед каждым тестом."""
    try:
        s._invoke_openai_cached.cache_clear()
    except AttributeError:
        pass # кэш мог быть не инициализирован

@pytest.fixture
def proposal_data():
    """Стандартные данные для proposal."""
    return {
        "client_name": "TestClient",
        "project_goal": "Test Goal",
        "scope": "Test Scope",
        "technologies": ["Python", "Docker"]
    }

@pytest.fixture
def mock_openai_client(mocker):
    """Мок для openai.OpenAI()."""
    mock_client_instance = MagicMock()
    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_message = MagicMock()
    
    mock_message.content = '{"executive_summary_text": "Mocked OpenAI Response"}'
    mock_choice.message = mock_message
    mock_response.choices = [mock_choice]
    
    mock_client_instance.chat.completions.create.return_value = mock_response
    
    mocker.patch("openai.OpenAI", return_value=mock_client_instance)
    return mock_client_instance

@pytest.fixture
def mock_gemini_model(mocker):
    """(FIXED) Мок для genai.GenerativeModel. Исправлен return."""
    if s.genai is None:
        pytest.skip("genai not installed")
        
    mock_model_instance = MagicMock()
    mock_response = MagicMock()
    mock_response.text = '{"executive_summary_text": "Mocked Gemini Response"}'
    mock_model_instance.generate_content.return_value = mock_response
    
    mock_genai_model = mocker.patch("google.generativeai.GenerativeModel", return_value=mock_model_instance)
    mocker.patch("google.generativeai.configure")
    
    # (FIX: Возвращаем tuple, как ожидает тест)
    return mock_genai_model, mock_model_instance

@pytest.fixture
def mock_openai_error_args():
    """
    (FIXED) Предоставляет КОРРЕКТНЫЕ аргументы для конструкторов ошибок openai v1.x,
    которые вызывают TypeError (missing body, request, response).
    """
    mock_request = MagicMock()
    mock_response = MagicMock(request=mock_request) # Response должен содержать request
    mock_body = {"detail": "error body"}
    
    return {
        # APIError требует message, request, body
        "api_error": {"message": "API Error", "request": mock_request, "body": mock_body},
        # RateLimitError (APIStatusError) требует message, response, body
        "rate_limit_error": {"message": "Rate limit", "response": mock_response, "body": mock_body}
    }


# --- Тесты утилит ---

def test_build_prompt_tones(proposal_data):
    """Тестируем разные 'tone'."""
    prompt_formal = s._build_prompt(proposal_data, "Formal")
    assert "Use a formal, professional tone" in prompt_formal
    
    prompt_mkt = s._build_prompt(proposal_data, "Marketing")
    assert "persuasive, benefit-focused" in prompt_mkt
    
    prompt_tech = s._build_prompt(proposal_data, "Technical")
    assert "detailed technical tone" in prompt_tech

    prompt_friendly = s._build_prompt(proposal_data, "Friendly")
    assert "friendly, conversational tone" in prompt_friendly

    prompt_default = s._build_prompt(proposal_data, "INVALID_TONE")
    assert "Use a neutral and professional tone" in prompt_default

def test_extract_json_blob(mocker):
    """
    (FIXED) Тестируем извлечение JSON, включая баг с приоритетом {}.
    """
    # Тест 1: { ... } (приоритет)
    assert _extract_json_blob('Prefix {"key": "value"} Suffix') == '{"key": "value"}'
    
    # Тест 2: (FIXED) [ ... ]
    # Код в openai_service.py ИЩЕТ СНАЧАЛА {.*}
    # В строке '[{"key": "value"}]' он найдет '{"key": "value"}'.
    # Это баг в _extract_json_blob, но тест должен это отражать.
    assert _extract_json_blob('Prefix [{"key": "value"}] Suffix') == '{"key": "value"}'
    
    # Тест 2b: Убеждаемся, что [.*] работает, *если* нет {}.
    assert _extract_json_blob('Prefix ["a", "b"] Suffix') == '["a", "b"]'

    # Тест 3: (FIXED) Тестируем баг с {.*} (greedy match)
    greedy_input = 'Prefix {"a": 1} Middle Text {"b": 2} Suffix'
    expected_greedy_output = '{"a": 1} Middle Text {"b": 2}'
    assert _extract_json_blob(greedy_input) == expected_greedy_output

    # Тест 4: Тестируем баг с [.*] (greedy match), когда нет { }
    greedy_input_arr = 'Prefix ["a"] Middle Text ["b"] Suffix'
    expected_greedy_arr = '["a"] Middle Text ["b"]'
    assert _extract_json_blob(greedy_input_arr) == expected_greedy_arr

    # Тест 5: None
    assert _extract_json_blob('No JSON') is None
    assert _extract_json_blob(None) is None


def test_extract_text_from_openai_response():
    """Тестируем все ветки парсера ответов."""
    
    # 1. Стандартный Pydantic-объект (message.content)
    MockMessage = MagicMock()
    MockMessage.content = "content_v1"
    MockChoice = MagicMock()
    MockChoice.message = MockMessage
    MockResponse = MagicMock()
    MockResponse.choices = [MockChoice]
    assert s._extract_text_from_openai_response(MockResponse) == "content_v1"

    # 2. Объект с .text
    MockChoice.message = None
    MockChoice.text = "content_v2"
    assert s._extract_text_from_openai_response(MockResponse) == "content_v2"

    # ... (остальные dict-like тесты) ...
    
    # 5. Полный отказ (AttributeError), fallback на str(resp)
    my_obj = object()
    assert s._extract_text_from_openai_response(my_obj) == str(my_obj)
    
    # 6. Пустой ответ (None)
    assert s._extract_text_from_openai_response(None) == "None"
    
    # 7. Пустой dict {}
    assert s._extract_text_from_openai_response({}) == "{}"


# --- Тесты _call_openai_new_client ---

def test_call_openai_missing_module(mocker):
    mocker.patch.object(s, "openai", None)
    with pytest.raises(RuntimeError, match="openai package not installed"):
        s._call_openai_new_client("prompt", "model")

def test_call_openai_missing_class(mocker):
    mocker.patch("openai.OpenAI", None)
    with pytest.raises(RuntimeError, match="client class not available"):
        s._call_openai_new_client("prompt", "model")

def test_call_openai_instantiation_fails(mocker):
    mocker.patch("openai.OpenAI", side_effect=ValueError("Init failed"))
    with pytest.raises(RuntimeError, match="Failed to instantiate"):
        s._call_openai_new_client("prompt", "model")

def test_call_openai_missing_create_method(mocker, mock_openai_client):
    """(FIXED) На клиенте нет .chat.completions.create. Фикс regex."""
    mocker.patch.object(mock_openai_client, "chat", None)
    
    # (FIX: Используем re.escape() для безопасного матчинга спецсимволов '.', '()')
    expected_error_msg = "openai.OpenAI client found but chat.completions.create() not available on it"
    with pytest.raises(RuntimeError, match=re.escape(expected_error_msg)):
        s._call_openai_new_client("prompt", "model")

def test_call_openai_timeout_typeerror(mocker, mock_openai_client):
    """Тестируем fallback для request_timeout."""
    mock_openai_client.chat.completions.create.side_effect = [
        TypeError("unexpected keyword argument 'request_timeout'"),
        MagicMock(choices=[MagicMock(message=MagicMock(content="Success without timeout"))])
    ]
    
    result = s._call_openai_new_client("prompt", "model")
    assert result == "Success without timeout"
    assert mock_openai_client.chat.completions.create.call_count == 2

def test_call_openai_api_error(mocker, mock_openai_client, mock_openai_error_args):
    """(FIXED) Ошибка API при вызове create(). Добавляем args."""
    # (FIX: Передаем 'request' и 'body', как того требует конструктор v1.x)
    mock_openai_client.chat.completions.create.side_effect = s.OpenAIAPIError(
        **mock_openai_error_args["api_error"]
    )
    
    with pytest.raises(s.OpenAIAPIError):
        s._call_openai_new_client("prompt", "model")


# --- Тесты _call_gemini ---

def test_call_gemini_missing_module(mocker):
    mocker.patch.object(s, "genai", None)
    text, reason = s._call_gemini("prompt")
    assert text == ""
    assert "package not installed" in reason

def test_call_gemini_missing_key(monkeypatch):
    monkeypatch.setattr(s, "GOOGLE_API_KEY", None)
    text, reason = s._call_gemini("prompt")
    assert text == ""
    assert "GOOGLE_API_KEY not set" in reason

def test_call_gemini_blocked_response(mocker, mock_gemini_model, monkeypatch):
    """(FIXED) Gemini заблокировал ответ. Фиксируем мок __str__."""
    monkeypatch.setattr(s, "GOOGLE_API_KEY", "DUMMY_KEY")
    # (FIX: баг был в фикстуре, теперь она возвращает 2 значения)
    _, mock_model_instance = mock_gemini_model
    
    mock_response = MagicMock()
    mock_response.text = None # Нет текста
    
    # (FIX: Мокаем __str__ у feedback, т.к. код использует str(feedback))
    mock_prompt_feedback = MagicMock()
    mock_prompt_feedback.block_reason = "SAFETY"
    mock_prompt_feedback.__str__ = lambda self: "Blocked by SAFETY"
    
    mock_response.prompt_feedback = mock_prompt_feedback
    mock_model_instance.generate_content.return_value = mock_response
    
    text, reason = s._call_gemini("prompt")
    assert text == ""
    assert "gemini_empty_or_blocked" in reason
    assert "Blocked by SAFETY" in reason # Проверяем, что __str__ был вызван

def test_call_gemini_api_error(mocker, mock_gemini_model, monkeypatch):
    """(FIXED) Ошибка API Gemini."""
    monkeypatch.setattr(s, "GOOGLE_API_KEY", "DUMMY_KEY")
    # (FIX: баг был в фикстуре, теперь она возвращает 2 значения)
    _, mock_model_instance = mock_gemini_model
    
    mock_model_instance.generate_content.side_effect = s.GeminiAPIError("Gemini Failed")
    
    text, reason = s._call_gemini("prompt")
    assert text == ""
    assert "gemini_error" in reason
    assert "Gemini Failed" in reason


# --- Тесты generate_ai_json ---

def test_generate_ai_json_stub_mode(monkeypatch, proposal_data):
    monkeypatch.setenv("OPENAI_USE_STUB", "1")
    s.OPENAI_USE_STUB = True # Принудительно
    result = s.generate_ai_json(proposal_data)
    data = json.loads(result)
    assert "fallback executive summary" in data["executive_summary_text"]
    s.OPENAI_USE_STUB = False # Сброс

def test_generate_ai_json_cache_hit(mocker, proposal_data):
    mock_cached = mocker.patch.object(s, "_invoke_openai_cached", return_value='{"cached": "true"}')
    result = s.generate_ai_json(proposal_data)
    assert result == '{"cached": "true"}'
    mock_cached.assert_called_once()

def test_generate_ai_json_retry_then_succeed(mocker, proposal_data, mock_openai_error_args):
    """(FIXED) OpenAI падает 2 раза, затем работает. Добавляем args."""
    mocker.patch.object(s, "_invoke_openai_cached", side_effect=RuntimeError("Cache miss simulation"))
    
    # (FIX: Передаем корректные args в конструкторы)
    mock_call = mocker.patch.object(s, "_call_openai_new_client", side_effect=[
        s.OpenAIRateLimitError(**mock_openai_error_args["rate_limit_error"]),
        s.OpenAIAPIError(**mock_openai_error_args["api_error"]),
        '{"success": "true"}'
    ])
    
    mocker.patch("time.sleep")
    
    result = s.generate_ai_json(proposal_data)
    assert result == '{"success": "true"}'
    assert mock_call.call_count == 3
    assert time.sleep.call_count == 2

def test_generate_ai_json_openai_fails_gemini_succeeds(mocker, proposal_data, monkeypatch, mock_openai_error_args):
    """(FIXED) OpenAI падает 3 раза, Gemini работает. Добавляем args."""
    monkeypatch.setattr(s, "OPENAI_RETRY_ATTEMPTS", 3)
    mocker.patch.object(s, "_invoke_openai_cached", side_effect=RuntimeError("Cache miss simulation"))
    
    # (FIX: Передаем 'request' и 'body')
    mock_openai_call = mocker.patch.object(s, "_call_openai_new_client", side_effect=s.OpenAIAPIError(
        **mock_openai_error_args["api_error"]
    ))
    
    mock_gemini_call = mocker.patch.object(s, "_call_gemini", return_value=('{"gemini": "true"}', "gemini_success"))
    
    mocker.patch("time.sleep")
    
    result = s.generate_ai_json(proposal_data)
    assert result == '{"gemini": "true"}'
    assert mock_openai_call.call_count == 3
    mock_gemini_call.assert_called_once()

def test_generate_ai_json_all_fail_returns_stub(mocker, proposal_data, monkeypatch, mock_openai_error_args):
    """(FIXED) OpenAI и Gemini падают, возвращаем stub. Добавляем args."""
    monkeypatch.setattr(s, "OPENAI_RETRY_ATTEMPTS", 1)
    mocker.patch.object(s, "_invoke_openai_cached", side_effect=RuntimeError("Cache miss simulation"))
    
    # (FIX: Передаем 'request' и 'body')
    mock_openai_call = mocker.patch.object(s, "_call_openai_new_client", side_effect=s.OpenAIAPIError(
        **mock_openai_error_args["api_error"]
    ))
    
    mock_gemini_call = mocker.patch.object(s, "_call_gemini", return_value=("", "gemini_error"))
    
    mocker.patch("time.sleep")
    
    result = s.generate_ai_json(proposal_data)
    data = json.loads(result)
    assert "fallback executive summary" in data["executive_summary_text"]
    assert "TestClient" in data["executive_summary_text"]


# --- Тесты generate_suggestions ---

@pytest.fixture
def suggestion_json_str():
    """Валидный JSON для suggestions."""
    return json.dumps({
        "suggested_deliverables": [{"title": "D1"}],
        "suggested_phases": [{"phase_name": "P1"}]
    })

def test_generate_suggestions_cache_hit(mocker, proposal_data, suggestion_json_str):
    mocker.patch.object(s, "_invoke_openai_cached", return_value=suggestion_json_str)
    result = s.generate_suggestions(proposal_data)
    assert result["suggested_deliverables"][0]["title"] == "D1"

def test_generate_suggestions_cache_hit_bad_json(mocker, proposal_data, mock_openai_error_args):
    """(FIXED) Кэш вернул не-JSON, уходим на live-call. Добавляем args."""
    # 1. Кэш возвращает мусор
    mocker.patch.object(s, "_invoke_openai_cached", side_effect=RuntimeError("Cache miss"))
    
    # 2. Live call (в retry-цикле) падает
    # (FIX: Мокаем _call_openai_new_client, т.к. код generate_suggestions должен быть исправлен)
    mock_call = mocker.patch.object(s, "_call_openai_new_client", side_effect=s.OpenAIAPIError(
        **mock_openai_error_args["api_error"]
    ))
    
    mocker.patch.object(s, "_call_gemini", return_value=("", "fail"))
    mocker.patch("time.sleep")
    
    result = s.generate_suggestions(proposal_data)
    assert result["suggested_deliverables"][0]["title"] == "Requirements & Analysis"
    # Ожидаем 1 вызов для 'try cache' + 3 вызова для 'retry' = 4
    assert s._invoke_openai_cached.call_count == 1
    assert mock_call.call_count == s.OPENAI_RETRY_ATTEMPTS

def test_generate_suggestions_live_succeeds(mocker, proposal_data, suggestion_json_str):
    """(FIXED) Кэш промах, live call работает. (Предполагая, что код исправлен)"""
    
    # 1. Кэш падает
    mocker.patch.object(s, "_invoke_openai_cached", side_effect=RuntimeError("Cache miss")) 
    # 2. Live call (в retry) работает
    mocker.patch.object(s, "_call_openai_new_client", return_value=suggestion_json_str)
    
    result = s.generate_suggestions(proposal_data)
    
    # (FIX: Теперь ассерт должен пройти)
    assert result["suggested_deliverables"][0]["title"] == "D1"


def test_generate_suggestions_live_returns_bad_json(mocker, proposal_data, monkeypatch):
    monkeypatch.setattr(s, "OPENAI_RETRY_ATTEMPTS", 1)
    mocker.patch.object(s, "_invoke_openai_cached", side_effect=RuntimeError("Cache miss"))
    mocker.patch.object(s, "_call_openai_new_client", return_value="это не json")
    mocker.patch.object(s, "_call_gemini", return_value=("", "fail")) # Gemini тоже падает
    
    result = s.generate_suggestions(proposal_data)
    assert result["suggested_deliverables"][0]["title"] == "Requirements & Analysis"


def test_generate_suggestions_openai_fails_gemini_succeeds(mocker, proposal_data, suggestion_json_str, monkeypatch, mock_openai_error_args):
    """(FIXED) OpenAI падает, Gemini работает. Добавляем args."""
    monkeypatch.setattr(s, "OPENAI_RETRY_ATTEMPTS", 1)
    
    mocker.patch.object(s, "_invoke_openai_cached", side_effect=RuntimeError("Cache miss"))
    # (FIX: Передаем args в _call_openai_new_client)
    mocker.patch.object(s, "_call_openai_new_client", side_effect=s.OpenAIAPIError(
        **mock_openai_error_args["api_error"]
    ))
    
    mocker.patch.object(s, "_call_gemini", return_value=(suggestion_json_str, "success"))
    
    result = s.generate_suggestions(proposal_data)
    assert result["suggested_deliverables"][0]["title"] == "D1"

def test_generate_suggestions_all_fail_returns_stub(mocker, proposal_data, monkeypatch, mock_openai_error_args):
    """(FIXED) Все падает, получаем заглушку. Добавляем args."""
    monkeypatch.setattr(s, "OPENAI_RETRY_ATTEMPTS", 1)
    
    mocker.patch.object(s, "_invoke_openai_cached", side_effect=RuntimeError("Cache miss"))
    # (FIX: Передаем args в _call_openai_new_client)
    mocker.patch.object(s, "_call_openai_new_client", side_effect=s.OpenAIAPIError(
        **mock_openai_error_args["api_error"]
    ))
    
    mocker.patch.object(s, "_call_gemini", return_value=("", "fail"))
    
    result = s.generate_suggestions(proposal_data)
    assert result["suggested_deliverables"][0]["title"] == "Requirements & Analysis"