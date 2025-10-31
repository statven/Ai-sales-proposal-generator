# tests/test_ai_core_robustness.py
import pytest
import json
from unittest.mock import patch
from backend.app import ai_core

@pytest.mark.asyncio
async def test_generate_ai_sections_regeneration(monkeypatch):
    """
    First call returns malformed / non-JSON output; second call returns JSON with missing fields.
    Ensure generate_ai_sections fills fields (or returns safe fallback) without crashing.
    """
    # malformed initial output (no JSON)
    first = "This is junk text. executive_summary: 'First summary' ... not json"
    # second output includes a JSON with some fields (string)
    second = json.dumps({
        "solution_concept_text": "S2",
        "financial_justification_text": "F2",
        "executive_summary_text": "From regen executive"
    })

    async def fake_generate_ai_json(proposal, tone="Formal"):
        # this shouldn't be used because generate_ai_json is sync in original; ai_core uses to_thread
        return first

    # patch the openai service function used by ai_core.generate_ai_sections:
    # ai_core._call_model_async uses generate_ai_json via backend.app.services.openai_service.generate_ai_json or generate_ai_json in openai_service
    # We'll patch generate_ai_json used by ai_core.generate_ai_sections by patching the module backend.app.services.openai_service.generate_ai_json if present,
    # otherwise patch ai_core.generate_ai_json directly.
    target = "backend.app.services.openai_service.generate_ai_json"
    patched = False
    try:
        # first call -> first, second call -> second
        p = patch(target, side_effect=[first, second])
        p.start()
        patched = True
    except Exception:
        pass

    if not patched:
        # fallback: patch ai_core.generate_ai_json directly
        p = patch("backend.app.ai_core.generate_ai_json", side_effect=[first, second])
        p.start()

    try:
        proposal = {"client_name": "C", "provider_name": "P"}
        res = await ai_core.generate_ai_sections(proposal, tone="Formal")
        # result should be a dict and contain some keys
        assert isinstance(res, dict)
        # executive_summary_text should contain something (either from regen or safe fallback)
        assert "executive_summary_text" in res
        # If regen worked, solution_concept_text should equal S2 (otherwise fallback may differ)
        if res.get("solution_concept_text"):
            assert "S2" in res.get("solution_concept_text") or res.get("solution_concept_text") == "S2"
    finally:
        p.stop()
