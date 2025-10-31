# tests/test_suggestions_endpoint.py
import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock
from backend.app.main import app

client = TestClient(app)

def test_suggest_service_unavailable(monkeypatch):
    # ensure openai_service is None in module
    monkeypatch.setattr("backend.app.main.openai_service", None)
    resp = client.post("/api/v1/suggest", json={"client_name":"A", "provider_name":"B"})
    assert resp.status_code == 503

def test_suggest_happy(monkeypatch):
    fake = MagicMock()
    fake.generate_suggestions.return_value = {"suggested_deliverables": [{"title":"T","description":"D","acceptance":"A"}], "suggested_phases":[{"phase_name":"P","duration": "2 weeks", "tasks":"T"}]}
    monkeypatch.setattr("backend.app.main.openai_service", fake)
    pld = {"client_name":"A","provider_name":"B","project_goal":"G","scope":"S","tone":"Formal"}
    r = client.post("/api/v1/suggest", json=pld)
    assert r.status_code == 200
    data = r.json()
    assert "suggested_deliverables" in data
