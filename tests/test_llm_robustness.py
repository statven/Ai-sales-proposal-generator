# tests/test_llm_robustness.py
# ---- BEGIN TEST SHIM: stub optional runtime deps so tests import safely ----
# Place this at the very top so imports of backend.app.main won't fail on optional deps.
import sys
import types
import importlib
import re


# Lightweight stub for prometheus_client if missing
if "prometheus_client" not in sys.modules:
    prom = types.ModuleType("prometheus_client")
    def generate_latest(): return b""
    prom.generate_latest = generate_latest
    prom.CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"
    sys.modules["prometheus_client"] = prom

# Lightweight stub for sentry_sdk if missing
if "sentry_sdk" not in sys.modules:
    sentry = types.ModuleType("sentry_sdk")
    def init(*a, **k): pass
    sentry.init = init
    sys.modules["sentry_sdk"] = sentry

# Create a minimal observability module and ensure backend.app.observability resolves.
obs_full_name = "backend.app.observability"
if obs_full_name not in sys.modules:
    obs = types.ModuleType(obs_full_name)
    # plausible no-op functions/attributes that main.py or other modules might expect
    def setup_prometheus(app=None):
        return None
    def register_metrics(*a, **k):
        return None
    def dummy_middleware(app):
        return app
    obs.setup_prometheus = setup_prometheus
    obs.register_metrics = register_metrics
    obs.dummy_middleware = dummy_middleware
    sys.modules[obs_full_name] = obs

# Ensure package 'backend.app' has attributes the main module checks (sentry, observability).
try:
    pkg = importlib.import_module("backend.app")
    # attach our observability stub to the package namespace so `from backend.app import observability` works
    if not hasattr(pkg, "observability"):
        pkg.observability = sys.modules.get(obs_full_name)
    # ensure attr 'sentry' exists to avoid NameError in main.py checks like `if sentry:`
    if not hasattr(pkg, "sentry"):
        pkg.sentry = None
except Exception:
    # If importing backend.app right now is problematic, create minimal placeholder package entry in sys.modules
    if "backend.app" not in sys.modules:
        dummy_pkg = types.ModuleType("backend.app")
        dummy_pkg.observability = sys.modules.get(obs_full_name)
        dummy_pkg.sentry = None
        sys.modules["backend.app"] = dummy_pkg
    else:
        mod = sys.modules.get("backend.app")
        if mod and not hasattr(mod, "observability"):
            setattr(mod, "observability", sys.modules.get(obs_full_name))
        if mod and not hasattr(mod, "sentry"):
            setattr(mod, "sentry", None)
# ---- END TEST SHIM ----

import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from backend.app import main as main_mod

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "llm_outputs"

# helper checks
_html_script_re = re.compile(r"<\s*script", re.IGNORECASE)
_multiple_spaces_re = re.compile(r"\s{2,}")

def load_fixture_text(path: Path) -> str:
    """
    Robustly load fixture text. Try common encodings (utf-8, utf-8-sig, utf-16, latin-1).
    Normalize CRLF to LF for consistent assertions.
    """
    data = path.read_bytes()
    # try common encodings in order
    for enc in ("utf-8", "utf-8-sig", "utf-16", "utf-16-le", "utf-16-be", "latin-1"):
        try:
            text = data.decode(enc)
            # normalize line endings and trailing spaces
            text = text.replace("\r\n", "\n").replace("\r", "\n")
            return text
        except Exception:
            continue
    # fallback: replace undecodable bytes
    text = data.decode("latin-1", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def basic_placeholder_ctx():
    # context used to check placeholder replacement
    return {"client_company_name": "ClientCo", "provider_company_name": "ProvCo", "provider_name": "ProvCo2"}

@pytest.mark.parametrize("fixture_path", sorted(FIXTURES_DIR.glob("sample_*.txt")))
def test_sanitize_and_basic_checks(fixture_path, monkeypatch):
    """
    For each sample LLM output:
      - run _sanitize_ai_text (main) with a sample ctx
      - ensure result is a string
      - ensure script tags are removed (or at least not present)
      - ensure multiple spaces collapsed
      - placeholders are either replaced or left in some recognizable form (tolerant)
    """
    txt = load_fixture_text(fixture_path)
    ctx = basic_placeholder_ctx()

    # ensure that sanitizer doesn't trigger external calls: monkeypatch any openai or ai_core just in case
    monkeypatch.setattr(main_mod, "openai_service", MagicMock(), raising=False)
    monkeypatch.setattr(main_mod, "ai_core", MagicMock(), raising=False)

    out = main_mod._sanitize_ai_text(txt, ctx)
    assert isinstance(out, str)

    # no raw <script ...> tags allowed (basic safety)
    assert not _html_script_re.search(out), f"Found script tag in sanitized output for {fixture_path.name}"

    # multiple spaces collapsed (at least no runs of 2+ spaces)
    assert not _multiple_spaces_re.search(out), f"Multiple spaces left in {fixture_path.name}"

    # placeholders: either replaced with ctx values OR filler/preserved; tolerate both
    # check client and provider name presence in some form
    client_ok = ("ClientCo" in out) or ("client_company_name" in out) or ("[client_company_name]" in out)
    provider_ok = ("ProvCo" in out) or ("provider_company_name" in out) or ("{{provider_company_name}}" in out)
    assert client_ok and provider_ok, f"Placeholders not handled in {fixture_path.name}: {out[:120]!r}"

def test_doc_engine_parser_if_available(monkeypatch):
    """
    If ai_core exposes a parse function or doc_engine exposes helper to parse LLM output,
    try to call it with our fixtures. This is tolerant: only runs if function exists.
    We mock external services to prevent network calls.
    """
    # prevent network calls from any module
    monkeypatch.setattr(main_mod, "openai_service", MagicMock(), raising=False)

    # candidate functions to try in ai_core
    ai_core = None
    try:
        from backend.app import ai_core as ai_core
    except Exception:
        ai_core = None

    # Try a few common parser function names
    parser_fn = None
    if ai_core:
        for name in ("parse_ai_sections", "parse_sections", "parse_output"):
            if hasattr(ai_core, name):
                parser_fn = getattr(ai_core, name)
                break

    # if parser available, run a couple of fixtures through it and assert structure
    if parser_fn:
        sample_files = sorted(list(FIXTURES_DIR.glob("sample_0*.txt")))[:5]
        for p in sample_files:
            txt = load_fixture_text(p)
            # parser might expect text only
            try:
                parsed = parser_fn(txt)
            except TypeError:
                # maybe parser expects extra args; try only text
                parsed = parser_fn(txt)

            # parsed should be a dict or list-like structure that we can inspect
            assert parsed is not None
            assert isinstance(parsed, (dict, list)), f"Parser returned unexpected type {type(parsed)}"
            # if dict, expect keys like 'executive_summary_text' or similar; be tolerant
            if isinstance(parsed, dict):
                # At least one text-like value should be present
                values = [v for v in parsed.values() if isinstance(v, str)]
                assert values, f"No string values found in parser output for {p.name}"
