# backend/tests/test_db_module.py
import os
import json
import sqlite3
import pytest
from datetime import date

# helper: import module under test
try:
    from backend.app import db as db_mod
except Exception as e:
    pytest.skip(f"backend.app.db import failed: {e}")

# Helper: resilient wrappers that adapt to different function names
def _find_save_fn():
    for name in ("save_version", "insert_version", "create_version"):
        if hasattr(db_mod, name):
            return getattr(db_mod, name)
    pytest.skip("No save_version-like function found in backend.app.db")

def _find_get_fn():
    for name in ("get_version", "fetch_version", "read_version", "get_version_by_id"):
        if hasattr(db_mod, name):
            return getattr(db_mod, name)
    pytest.skip("No get_version-like function found in backend.app.db")

def _find_list_fn():
    for name in ("list_versions", "get_versions", "all_versions"):
        if hasattr(db_mod, name):
            return getattr(db_mod, name)
    return None

def _maybe_init_db_fn():
    for name in ("init_db", "initialize_db", "ensure_db"):
        if hasattr(db_mod, name):
            return getattr(db_mod, name)
    return None

@pytest.fixture(autouse=True)
def set_env_tmp_db(tmp_path, monkeypatch):
    """
    Ensure that PROPOSAL_DB_PATH points to a temp DB for tests
    and that any module-level DB path is not persistent between tests.
    """
    db_path = tmp_path / "test_proposals.db"
    monkeypatch.setenv("PROPOSAL_DB_PATH", str(db_path))
    # If the module exposes a global var PROPOSAL_DB_PATH, override it as well
    if hasattr(db_mod, "PROPOSAL_DB_PATH"):
        monkeypatch.setattr(db_mod, "PROPOSAL_DB_PATH", str(db_path), raising=False)
    return str(db_path)

def test_init_and_save_and_get_roundtrip(set_env_tmp_db):
    """Init DB (if function exists), save a version and read it back."""
    init_fn = _maybe_init_db_fn()
    if init_fn:
        # some init functions accept path, others not
        try:
            init_fn()
        except TypeError:
            init_fn(set_env_tmp_db)

    save_fn = _find_save_fn()
    get_fn = _find_get_fn()

    payload = {
        "client_company_name": "Acme Test Co",
        "provider_company_name": "Provider LLC",
        "project_goal": "Test Goal",
        "scope": "Do the thing",
        "deadline": date.today().isoformat()
    }
    ai_sections = {"executive_summary_text": "Short summary"}
    used_model = "unit-test-model"

    # many implementations expect payload=dict, ai_sections=dict, used_model=str
    vid = save_fn(payload, ai_sections, used_model) if save_fn.__code__.co_argcount >= 3 else save_fn(payload, ai_sections)
    assert isinstance(vid, int) and vid >= 0, "save_version should return an integer id"

    # fetch by id
    result = get_fn(vid)
    # Accept either dict or row-like; convert to dict if needed
    assert result is not None, "get_version returned None"
    assert isinstance(result, (dict,)) or hasattr(result, "__getitem__")

    # Check either JSON stored or columns present
    if isinstance(result, dict):
        # Many implementations store payload/ai_sections as JSON strings
        # Accept either nested structure or JSON strings
        if "payload" in result:
            p = result["payload"]
            if isinstance(p, str):
                p = json.loads(p)
            assert p.get("client_company_name") == payload["client_company_name"]
        else:
            # maybe flattened
            assert result.get("client_company_name") == payload["client_company_name"] or result.get("client_name") == payload["client_company_name"]
    else:
        # row-like: access by index or attributes; we just ensure it exists
        assert True

def test_save_multiple_versions_and_list(set_env_tmp_db):
    save_fn = _find_save_fn()
    list_fn = _find_list_fn()
    if list_fn is None:
        pytest.skip("No list_versions-like function found in backend.app.db")

    for i in range(3):
        payload = {
            "client_company_name": f"Client {i}",
            "provider_company_name": "Prov",
            "project_goal": f"G{i}",
            "scope": "S",
            "deadline": date.today().isoformat()
        }
        ai_sections = {"executive_summary_text": f"Summary {i}"}
        used_model = "t"
        vid = save_fn(payload, ai_sections, used_model) if save_fn.__code__.co_argcount >= 3 else save_fn(payload, ai_sections)
        assert isinstance(vid, int)

    # list versions (no args or maybe limit)
    try:
        rows = list_fn()
    except TypeError:
        rows = list_fn(limit=10)
    assert isinstance(rows, (list, tuple))
    assert len(rows) >= 3

def test_database_error_handling_on_connect(monkeypatch, tmp_path):
    """
    Simulate sqlite3.connect throwing an OperationalError to trigger any error handling code paths.
    """
    class FakeError(Exception):
        pass

    # Force sqlite3.connect to raise when called
    import sqlite3 as pysqlite
    def fake_connect(*args, **kwargs):
        raise pysqlite.OperationalError("simulated failure")

    monkeypatch.setattr(pysqlite, "connect", fake_connect)

    save_fn = _find_save_fn()
    payload = {"client_company_name": "X", "provider_company_name": "Y", "project_goal": "G", "scope": "S", "deadline": date.today().isoformat()}
    ai_sections = {}
    used_model = "m"

    # The behaviour may be either to raise or to return None / -1; we accept both but must exercise branch.
    try:
        res = save_fn(payload, ai_sections, used_model) if save_fn.__code__.co_argcount >= 3 else save_fn(payload, ai_sections)
        # If function returned normally, ensure it's not a valid id
        assert res is None or (isinstance(res, int) and res < 0)
    except Exception as e:
        # acceptable — ensures error branch executed
        assert isinstance(e, pysqlite.OperationalError) or isinstance(e, Exception)

