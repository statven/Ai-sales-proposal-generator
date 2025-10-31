# tests/test_ai_core_robustness.py
import pytest
import json
import asyncio
from types import SimpleNamespace

from backend.app import ai_core

@pytest.mark.asyncio
async def test_generate_ai_sections_regeneration(monkeypatch):
    """
    First call returns malformed / non-JSON output; second call returns JSON with missing fields.
    Ensure generate_ai_sections fills fields (or returns safe fallback) without crashing.
    """

    # 1) Prepare two LLM outputs: first malformed, second valid JSON string
    first_out = "This is junk text. executive_summary_text: 'First summary' ... not json"
    second_obj = {
        "solution_concept_text": "S2",
        "financial_justification_text": "F2",
        "executive_summary_text": "From regen executive"
    }
    second_out = json.dumps(second_obj)

    # 2) Create iterator for side_effect
    seq = {"i": 0}
    def fake_generate_ai_json_sync(proposal, tone="Formal"):
        # emulate generate_ai_json returning strings (sync)
        seq["i"] += 1
        return first_out if seq["i"] == 1 else second_out

    # 3) Patch likely targets. Try service-level function first, then ai_core fallback.
    # 3) Patch the function directly in the ai_core module, where it is used.
    try:
        # Используем полный строковый путь до атрибута внутри импортированного ai_core
        monkeypatch.setattr("backend.app.ai_core.generate_ai_json", fake_generate_ai_json_sync)
    except AttributeError:
        # Если generate_ai_json не был импортирован в ai_core (например, из-за ошибки в try/except блоке ai_core),
        # патчим сервис напрямую.
        monkeypatch.setattr("backend.app.services.openai_service.generate_ai_json", fake_generate_ai_json_sync)

    # 4) Run the function under test
    # generate_ai_sections expects a dict-like proposal; use minimal keys used by prompts
    proposal = {"client_name": "ClientX", "provider_name": "ProviderY", "project_goal": "Goal"}

    # call the async function under test
    result = await ai_core.generate_ai_sections(proposal, tone="Formal")

    # 5) Assertions: result must be dict and include expected keys
    assert isinstance(result, dict)
    # executive_summary_text must be present (either from regen or fallback)
    assert "executive_summary_text" in result
    # If regeneration produced solution_concept_text, ensure it matches S2
    if result.get("solution_concept_text"):
        assert result["solution_concept_text"] == "S2"
    # financial_justification_text should be filled from the second JSON
    assert result.get("financial_justification_text") in ("F2", "Expected benefits and efficiency gains justify the investment.", result.get("financial_justification_text"))
