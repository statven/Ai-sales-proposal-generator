# backend/tests/test_backend.py
import pytest
import json
from datetime import date
from pydantic import ValidationError
from fastapi.testclient import TestClient
from unittest.mock import MagicMock, patch

from backend.app.models import ProposalInput

from backend.app.main import app

client = TestClient(app)

# ------------------- Tests for _extract_json_blob and helpers -------------------

from backend.app.ai_core import _extract_json_blob, _safe_stringify

def test_extract_json_blob_nested():
    text = 'Before some text {"key": 1, "nested": [1, 2, {"a": "b"}]} More text.'
    expected = '{"key": 1, "nested": [1, 2, {"a": "b"}]}'
    assert _extract_json_blob(text) == expected

def test_extract_json_blob_unclosed_end():
    text = 'JUNK {"key": "value", "incomplete": ['
    assert _extract_json_blob(text) == ""  # no complete JSON

# ------------------- Pydantic Validation -------------------

def test_proposal_input_validation_min_length():
    with pytest.raises(ValidationError):
        ProposalInput(
            client_company_name="A",  # too short
            provider_company_name="Provider",
        )

def test_proposal_input_validation_valid():
    data = {
        "client_company_name": "Acme Inc.",
        "provider_company_name": "Our Co",
        "project_goal": "Goal",
        "scope_description": "Scope",
        "tone": "Marketing",
        "deadline": date.today().isoformat()
    }
    proposal = ProposalInput(**data)
    assert proposal.client_company_name == "Acme Inc."
    assert proposal.tone == "Marketing"

# ------------------- LLM / AI robustness (use monkeypatch not mocker) -------------------

def test_process_ai_content_json_failure_and_fallback(monkeypatch):
    """
    Simulate first LLM call returns garbage, second returns JSON with missing keys.
    We'll patch ai_core.generate_ai_json (or openai service) to return two responses.
    """
    # Prepare two responses: first garbage, second valid JSON for missing keys
    raw_bad = "This is junk text. executive_summary_text: 'First try summary.' More text."
    raw_good = '{"solution_concept_text": "S2", "financial_justification_text": "F2", "executive_summary_text": "From regen"}'

    # Patch the openai_service.generate_ai_json used in ai_core.generate_ai_sections (call chain may vary)
    try:
        monkeypatch.setattr("backend.app.services.openai_service.generate_ai_json", lambda proposal, tone="Formal": raw_bad)
        # For regeneration, patch directly ai_core.generate_ai_json to return good on second call
    except Exception:
        # fallback: patch ai_core.generate_ai_json
        monkeypatch.setattr("backend.app.ai_core.generate_ai_json", lambda proposal, tone="Formal": raw_bad)

    # Now patch ai_core._call_model_async to return sequence:
    calls = {"n": 0}
    def fake_gen(proposal, tone="Formal"):
        calls["n"] += 1
        return raw_bad if calls["n"] == 1 else raw_good

    # monkeypatch the generate_ai_json used inside ai_core._call_model_async if exists
    monkeypatch.setattr("backend.app.ai_core.generate_ai_json", lambda proposal, tone="Formal": raw_bad)
    # Now directly call generate_ai_sections which contains regeneration logic.
    # To simulate regen, we will monkeypatch ai_core._call_model_async to return first then second.
    seq = {"i":0}
    async def fake_call_model_async(proposal, tone="Formal"):
        seq["i"] += 1
        return raw_bad if seq["i"] == 1 else raw_good

    monkeypatch.setattr("backend.app.ai_core._call_model_async", fake_call_model_async)

    # Call the coroutine
    import asyncio
    result = asyncio.get_event_loop().run_until_complete(
        __import__("backend.app.ai_core", fromlist=[""]).generate_ai_sections({"client_name":"X","provider_name":"Y"})
    )

    # result must be dict and contain values from regen for missing keys
    assert isinstance(result, dict)
    assert result.get("solution_concept_text") == "S2"
    assert result.get("financial_justification_text") == "F2"
    # executive summary should exist (either from first or second)
    assert result.get("executive_summary_text")

# ------------------- API endpoint tests (use TestClient, monkeypatch) -------------------

@patch("backend.app.doc_engine.render_docx_from_template")
@patch("backend.app.db.save_version", MagicMock(return_value=1))
def test_generate_proposal_happy_path(mock_render_docx, monkeypatch):
    # Patch AI core to return deterministic sections
    async def fake_generate_ai_sections(payload, tone="Formal"):
        return {
            "executive_summary_text": "AI Summary",
            "project_mission_text": "AI Mission",
            "solution_concept_text": "AI Solution",
            "project_methodology_text": "AI Methodology",
            "financial_justification_text": "AI Just",
            "payment_terms_text": "AI Pay",
            "development_note": "Dev Note",
            "licenses_note": "Lic Note",
            "support_note": "Support Note",
            "used_model": "fake-llm"
        }
    monkeypatch.setattr("backend.app.main.ai_core.generate_ai_sections", fake_generate_ai_sections)

    # render_docx_from_template should return BytesIO-like
    from io import BytesIO
    fake_bytes = BytesIO(b"FAKE_DOCX")
    mock_render_docx.return_value = fake_bytes

    payload = {
        "client_company_name": "ООО Test Клиент",
        "provider_company_name": "ИП Test Продавец",
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
            {"title": "D01", "description": "Desc1 is long enough", "acceptance_criteria": "A1"}
        ],
        "phases": [
            {"duration_weeks": 2, "tasks": "Task 1 is ok"}
        ]
    }

    resp = client.post("/api/v1/generate-proposal", json=payload)
    assert resp.status_code == 200
    assert resp.content == b"FAKE_DOCX"
    assert "application/vnd.openxmlformats-officedocument.wordprocessingml.document" in resp.headers["content-type"]

def test_suggest_happy(monkeypatch):
    fake = MagicMock()
    fake.generate_suggestions.return_value = {
        "suggested_deliverables": [{"title":"Ttl","description":"Desc long enough","acceptance":"A"}],
        "suggested_phases":[{"phase_name":"P","duration": "2 weeks", "tasks":"Tasks..."}]
    }
    monkeypatch.setattr("backend.app.main.openai_service", fake)
    pld = {"client_company_name":"AC","provider_company_name":"BC","project_goal":"G","scope":"S","tone":"Formal"}
    r = client.post("/api/v1/suggest", json=pld)
    assert r.status_code == 200
    data = r.json()
    assert "suggested_deliverables" in data
