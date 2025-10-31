import pytest
from backend.app import ai_core

def test_extract_json_blob_and_safe_stringify():
    raw = 'JUNK {"executive_summary_text":"X","project_mission_text":""} JUNK'
    blob = ai_core._extract_json_blob(raw)
    assert '"executive_summary_text":' in blob
