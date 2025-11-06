# backend/app/ai_core.py
import json
import logging
from typing import Dict, Any, List, Tuple, Optional
import asyncio
import os
from datetime import date

# Предполагаем, что generate_ai_json импортируется
try:
    from backend.app.services.openai_service import generate_ai_json
except Exception:
    generate_ai_json = None

logger = logging.getLogger("uvicorn.error")


EXPECTED_KEYS: List[str] = [
    "executive_summary_text",
    "project_mission_text",
    "solution_concept_text",
    "project_methodology_text",
    "financial_justification_text",
    "payment_terms_text",
    "development_note",
    "licenses_note",
    "support_note",
    # new keys used for visualization
    "components",
    "milestones",
]



# (FIX 1: Более надежный экстрактор JSON)
# backend/app/ai_core.py

# (FIX 1: Исправлена логика _extract_json_blob)
def _extract_json_blob(text: str) -> str:
    """
    Extract the first balanced JSON object {...} or array [...] substring.
    Returns '' if no balanced JSON object/array is found.
    """
    if not text or not isinstance(text, str):
        return ""

    n = len(text)
    stack = []
    start_index = -1
    
    i = 0
    while i < n:
        char = text[i]
        if char == '{':
            # (FIX) Корректно пропускаем '{{'
            if i + 1 < n and text[i + 1] == '{':
                i += 1 # Пропускаем второй {
                i += 1 # Переходим к следующему символу
                continue 
            
            stack.append(char)
            start_index = i
            break # Нашли начало, переходим к парсингу
        elif char == '[':
            stack.append(char)
            start_index = i
            break # Нашли начало, переходим к парсингу
        
        i += 1 # (FIX) Убеждаемся, что i инкрементируется

    if start_index == -1:
        return "" # Не найдено начало JSON

    # Ищем сбалансированную структуру
    for i in range(start_index + 1, n):
        char = text[i]
        
        if char == '{' or char == '[':
            stack.append(char)
        elif char == '}':
            if not stack or stack[-1] != '{':
                # (FIX) Несбалансированная структура
                return "" 
            stack.pop()
        elif char == ']':
            if not stack or stack[-1] != '[':
                # (FIX) Несбалансированная структура
                return "" 
            stack.pop()
            
        if not stack:
            # Стек пуст, мы нашли конец
            return text[start_index : i + 1]
            
    return "" # Несбалансированная структура (не закрыто)

# (FIX 2: Исправлена логика _proposal_to_dict для .__dict__ fallback)
def _proposal_to_dict(proposal_obj: Any) -> Dict[str, Any]:
    """
    Безопасное преобразование объекта ProposalInput (или его мока) в dict.
    """
    if proposal_obj is None:
        return {}
    if isinstance(proposal_obj, dict):
        return dict(proposal_obj)
    
    # Pydantic v2
    if hasattr(proposal_obj, "model_dump"):
        try:
            return proposal_obj.model_dump()
        except Exception:
            pass # Пробуем другие методы
            
    # Pydantic v1
    if hasattr(proposal_obj, "dict"):
        try:
            return proposal_obj.dict()
        except Exception:
            pass # Пробуем другие методы

    # Fallback для моков (как в test_generate_proposal_no_pydantic_dict_method)
    if hasattr(proposal_obj, "__dict__"):
        try:
            # (FIX) Используем vars() для получения __dict__ чистого объекта,
            # а не MagicMock
            return vars(proposal_obj)
        except Exception:
            pass
            
    logger.warning("Could not convert proposal_obj to dict, returning empty dict.")
    return {}

def _safe_stringify(value: Any) -> str:
    """Безопасное преобразование в строку."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    # (FIX 2: Упрощено)
    try:
        # Используем ensure_ascii=False для поддержки UTF-8
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        # Fallback для несериализуемых объектов
        return str(value)


async def generate_ai_sections_safe(proposal: Dict[str, Any]) -> Dict[str, str]:
    """Возвращает безопасный (fallback) текст."""
    # (FIX 4: Упрощаем try/except, он здесь не нужен, 
    # так как proposal уже должен быть dict)
    client = "Client"
    if proposal:
        client_name = proposal.get("client_name") or proposal.get("client_company_name")
        client = str(client_name) if client_name else "Client"

    safe = {
        "executive_summary_text": f"This proposal for {client} outlines a phased plan to meet the goals specified.",
        "project_mission_text": "Deliver a reliable, maintainable solution that provides measurable business value.",
        "solution_concept_text": "A modular services architecture with reliable third-party integrations.",
        "project_methodology_text": "Agile with two-week sprints, CI/CD, testing and regular demos.",
        "financial_justification_text": "Expected efficiency gains and revenue uplift justify the investment.",
        "payment_terms_text": "50% upfront, 50% on delivery. Valid for 30 days.",
        "development_note": "Estimate includes development, QA, and DevOps.",
        "licenses_note": "Includes required SaaS licenses and hosting.",
        "support_note": "Includes 3 months of post-launch support.",
    }
    return safe


async def _call_model_async(proposal: Dict[str, Any], tone: str = "Formal") -> str:
    """
    Вызывает синхронный `generate_ai_json` в пуле потоков.
    """
    # (FIX 5: Тестируем эту ветку, мокая generate_ai_json = None)
    if generate_ai_json is None:
        logger.warning("_call_model_async: generate_ai_json (openai_service) is not available.")
        return "" # Возвращаем пустую строку, чтобы вызвать fallback

    try:
        # (FIX 6: Гарантируем, что proposal является dict перед передачей в to_thread)
        proposal_dict = _proposal_to_dict(proposal)
        
        # Запускаем синхронную функцию в потоке
        res = await asyncio.to_thread(generate_ai_json, proposal_dict, tone)
        
        # (FIX 7: Тестируем эту ветку, мокая возврат байтов)
        if isinstance(res, bytes):
            try:
                return res.decode("utf-8", errors="ignore")
            except Exception as e:
                logger.warning("Failed to decode bytes from AI response: %s", e)
                return str(res) # Fallback на str()
        
        return str(res) if res is not None else ""
        
    except Exception as e:
        # (FIX 8: Тестируем эту ветку, мокая generate_ai_json с side_effect=Exception)
        logger.exception("generate_ai_json (via to_thread) raised exception: %s", e)
        return "" # Возвращаем пустую строку, чтобы вызвать fallback


async def generate_ai_sections(proposal: dict, tone: str = "Formal") -> dict:
    """
    Надежная обертка для получения структурированных AI-секций.
    Стратегия:
      1) Вызвать модель (которая внутри себя делает 3 попытки).
      2) Попытаться извлечь JSON.
      3) Если не удалось -> вернуть generate_ai_sections_safe.
    """
    
    def try_parse_string_to_dict(s: str) -> Optional[Dict[str, Any]]:
        """Пытается распарсить JSON из строки."""
        if not s or not isinstance(s, str):
            return None
            
        blob = _extract_json_blob(s)
        if blob:
            try:
                data = json.loads(blob)
                return data if isinstance(data, dict) else None
            except json.JSONDecodeError:
                pass # Пробуем распарсить всю строку

        # Fallback: пробуем распарсить всю строку, если она похожа на JSON
        s_stripped = s.strip()
        if s_stripped.startswith("{") and s_stripped.endswith("}"):
            try:
                data = json.loads(s_stripped)
                return data if isinstance(data, dict) else None
            except json.JSONDecodeError:
                pass
                
        return None # Не удалось распарсить


def normalize_values(d: Dict[str, Any]) -> Dict[str, Any]:
    """Гарантирует, что все значения являются безопасными строками/примитивами или сохраняет вложенные структуры."""
    out: Dict[str, Any] = {}
    if not d:
        return out

    for k, v in d.items():
        if v is None:
            out[k] = ""
        elif isinstance(v, (str, int, float, bool)):
            out[k] = v
        else:
            # Preserve structured data (lists/dicts) for components/milestones
            if isinstance(v, (list, dict)):
                out[k] = v
            else:
                out[k] = _safe_stringify(v)
    return out


    # 1) Вызываем модель (включает 3 retries)
    raw_response = ""
    try:
        raw_response = await _call_model_async(proposal, tone=tone)
    except Exception as e:
        logger.exception("AI call failed unexpectedly: %s", e)
        raw_response = "" # Переходим к safe fallback

    # 2) Пытаемся распарсить
    parsed_json = try_parse_string_to_dict(raw_response)

    if parsed_json:
        return normalize_values(parsed_json)

    # 3) Не удалось распарсить -> используем safe fallback
    logger.warning("Failed to parse JSON from AI response. Returning safe fallback.")
    safe_sections = await generate_ai_sections_safe(proposal)
    
    # (FIX 11: Если AI вернул не-JSON, но полезный текст, 
    # используем его как executive_summary_text)
    if raw_response and len(raw_response) > 50: # (произвольный порог)
         safe_sections["executive_summary_text"] = raw_response.strip()

    return safe_sections



async def process_ai_content(proposal: Dict[str, Any], tone: str = "Formal") -> Tuple[Dict[str, str], str]:
    """
    Тонкая обертка-оркестратор.
      - Вызывает generate_ai_sections
      - Возвращает (sections_dict, used_model_string)
    """
    used_model = os.getenv("OPENAI_MODEL") or "openai" # По умолчанию
    
    try:
        sections = await generate_ai_sections(proposal, tone)
        
        # (FIX 13: Тестируем эту ветку)
        # Если модель не определена в sections, используем env var
        # Если она определена (например, _used_model из openai_service), она будет в sections
        if "used_model" in sections:
            used_model = str(sections.get("used_model", used_model))
        
        return sections, used_model
        
    except Exception as e:
        # (FIX 14: Тестируем эту ветку, мокая generate_ai_sections с side_effect=Exception)
        logger.exception("process_ai_content: AI generation failed unexpectedly: %s", e)
        safe = await generate_ai_sections_safe(proposal)
        return safe, "fallback_safe"