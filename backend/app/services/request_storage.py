# backend/app/services/request_storage.py
import os
import json
import logging
from datetime import datetime
from typing import Dict, Any, Optional
import hashlib

logger = logging.getLogger(__name__)

REQUESTS_DIR = r"D:\programming\ai-sales-proposal-generator\data\requests"
os.makedirs(REQUESTS_DIR, exist_ok=True)


def _hash_payload(payload: Dict[str, Any]) -> str:
    """Создаёт короткий хэш от payload для имени файла (без коллизий)"""
    payload_str = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload_str.encode("utf-8")).hexdigest()[:12]


def save_client_request(
    payload: Dict[str, Any],
    version_id: int,
    proposal_id: Optional[str] = None,
) -> str:
    """
    Сохраняет исходный запрос клиента в JSON-файл.
    Возвращает путь к сохранённому файлу.
    """
    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        hash_part = _hash_payload(payload)
        filename = f"request_v{version_id}_{timestamp}_{hash_part}.json"
        filepath = os.path.join(REQUESTS_DIR, filename)

        # Формируем чистый запрос (убираем служебные поля, оставляем только то, что ввёл клиент)
        clean_request = {
            "saved_at": datetime.now().isoformat(),
            "proposal_version_id": version_id,
            "proposal_id": proposal_id or "unknown",
            "client_company_name": payload.get("client_company_name") or payload.get("client_name"),
            "provider_company_name": payload.get("provider_company_name") or payload.get("provider_name"),
            "project_goal": payload.get("project_goal"),
            "scope": payload.get("scope") or payload.get("scope_description"),
            "technologies": payload.get("technologies"),
            "deadline": payload.get("deadline"),
            "team_size": payload.get("team_size"),
            "tone": payload.get("tone"),
            "deliverables": payload.get("deliverables"),
            "phases": payload.get("phases"),
            "financials": payload.get("financials"),
            # Добавляем любые пользовательские поля
            "custom_fields": {
                k: v for k, v in payload.items()
                if k not in {
                    "client_company_name", "provider_company_name", "project_goal", "scope",
                    "scope_description", "technologies", "deadline", "team_size", "tone",
                    "deliverables", "phases", "financials", "client_name", "provider_name"
                }
            }
        }

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(clean_request, f, ensure_ascii=False, indent=2)

        logger.info(f"Client request saved: {filepath}")
        return filepath

    except Exception as e:
        logger.exception(f"Failed to save client request: {e}")
        return ""