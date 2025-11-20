# backend/app/main.py
import logging
import os
import json
import re
from typing import Dict, Any, Optional
from fastapi.middleware.cors import CORSMiddleware
from fastapi import FastAPI, HTTPException, Body, Response
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import ValidationError
from datetime import datetime, date
from io import BytesIO
from urllib.parse import quote
import asyncio
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
from backend.app.services import visualization_service as vis



logger = logging.getLogger("uvicorn.error")

doc_engine = None
try:
    import backend.app.doc_engine as doc_engine
except Exception as e:
    logger.warning("doc_engine not importable; DOCX generation disabled in this environment. Error: %s", e)

try:
    from backend.app.routes.visualization import router as visualization_router
except Exception as e:
    logger.warning("visualization not importable; Error: %s", e)


observability = None
try:
    from backend.app import observability
except Exception as e:
    logger.warning("Observability module failed to import. Continuing without it. Error: %s", e)


try:
    from backend.app.models import ProposalInput
except Exception:
    class ProposalInput:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
        def dict(self):
            return dict(self.__dict__)
    logger.warning("models.ProposalInput not importable; using shim (not strict validation).")

try:
    from backend.app import db
except Exception:
    class _MockDB:
        def init_db(self): pass
        def save_version(self, *args, **kwargs): return None
        def get_version(self, id): return None
    db = _MockDB()
    logger.warning("db not importable; using mock DB.")

try:
    from backend.app import ai_core
except Exception:
    ai_core = None
    logger.warning("ai_core not importable; AI generation disabled.")

try:
    from backend.app.services import openai_service
except Exception:
    openai_service = None
    logger.warning("openai_service not importable; suggestion service disabled.")

# --- app init ---
app = FastAPI(title="AI Sales Proposal Generator (Backend)")


try:
    observability.setup_logging()
    # register simple prometheus endpoint
    @app.get("/metrics")
    def _metrics():
        data = generate_latest()
        return Response(content=data, media_type=CONTENT_TYPE_LATEST)

    # Register observability middleware correctly.
    # observability.metrics_middleware may be either:
    #  - a callable middleware decorator: middleware_fn(request, call_next)
    #  - a factory that returns middleware when passed the app (less common).
    try:
        # If observability exposes a Starlette-compatible middleware class (preferred)
        if hasattr(observability, "ObservabilityMiddleware"):
            app.add_middleware(observability.ObservabilityMiddleware)
        elif hasattr(observability, "metrics_middleware") and callable(observability.metrics_middleware):
            # If metrics_middleware is a function decorated as @app.middleware style,
            # register it directly (pass the function, not call it).
            app.middleware("http")(observability.metrics_middleware)
        else:
            logger.info("Observability has no recognized middleware entrypoint; skipping ASGI middleware registration.")
    except Exception as ex:
        logger.warning("Failed to register observability middleware: %s", ex)

except Exception:
    import logging
    logging.getLogger(__name__).warning("Observability init failed", exc_info=True)



app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# include visualization router only if it was imported successfully
if 'visualization_router' in globals() and isinstance(visualization_router, object):
    try:
        app.include_router(visualization_router)
    except Exception as e:
        logger.warning("Failed to include visualization router: %s", e)
else:
    logger.info("Visualization router not available — skipping router inclusion.")

@app.on_event("startup")
def _on_startup():
    try:
        if "db" in globals() and db is not None and hasattr(db, "init_db"):
            db.init_db()
    except Exception as e:
        logger.error("Error initializing database: %s", e)

    try:
        if "openai_service" in globals() and openai_service is not None and hasattr(openai_service, "init"):
            try:
                openai_service.init()
            except Exception as e:
                logger.error("openai_service.init() failed: %s", e)
    except Exception:
        logger.exception("Unexpected error during startup")
    sentry = os.environ.get("SENTRY_DSN")
    if sentry:
            try:
                import sentry_sdk
                from sentry_sdk.integrations.asgi import SentryAsgiMiddleware

                sentry_sdk.init(dsn=sentry)
                logger.info("SENTRY_DSN configured (not printed).")
            except Exception as exc:
                logger.warning("Failed to initialize Sentry SDK: %s", exc)


@app.on_event("shutdown")
def _on_shutdown():
    
    try:
        if "openai_service" in globals() and openai_service is not None and hasattr(openai_service, "close"):
            try:
                openai_service.close()
            except Exception as e:
                # тест ожидает логирование ошибки во время shutdown
                logger.error("Error during OpenAI service shutdown: %s", e)
    except Exception:
        logger.exception("Unexpected error during shutdown")
    sentry = os.environ.get("SENTRY_DSN")
    if sentry:
        try:
            # Не импортируем sentry_sdk по-умолчанию — импортируйте только если он установлен.
            import sentry_sdk
            from sentry_sdk.integrations.asgi import SentryAsgiMiddleware

            sentry_sdk.init(dsn=sentry)
            # Если вы используете FastAPI app прямо в этом модуле, обернуть app в middleware можно в on_startup.
            logger.info("SENTRY_DSN configured (not printed).")
        except Exception as exc:
            # Не прерываем запуск приложения, но логируем причину
            logger.warning("Failed to initialize Sentry SDK: %s", exc)

TEMPLATE_PATH = os.getenv("TEMPLATE_PATH", os.path.join(os.getcwd(), "docs", "template.docx"))
if not os.path.exists(TEMPLATE_PATH):
    logger.warning("Template not found at %s. Ensure template.docx is present.", TEMPLATE_PATH)

def _proposal_to_dict(proposal_obj: Any) -> Dict[str, Any]:
    """
    Safe conversion of ProposalInput-like object to plain dict.
    Supports dict, pydantic v2 .model_dump(), pydantic v1 .dict(), and plain objects with __dict__.
    """
    if proposal_obj is None:
        return {}
    if isinstance(proposal_obj, dict):
        return dict(proposal_obj)
    if hasattr(proposal_obj, "model_dump"):
        try:
            return proposal_obj.model_dump()
        except Exception:
            pass
    if hasattr(proposal_obj, "dict"):
        try:
            return proposal_obj.dict()
        except Exception:
            pass
    # fallback to __dict__
    try:
        return dict(getattr(proposal_obj, "__dict__", {}) or {})
    except Exception:
        return {}


def _format_date(val: Optional[Any]) -> str:
    """Приводим дату к читаемому виду: 31 October 2025. При None -> empty string."""
    if val is None or val == "":
        return ""
    if isinstance(val, date):
        try:
            return val.strftime("%d %B %Y")
        except Exception:
            return val.isoformat()
    if isinstance(val, str):
        try:
            d = date.fromisoformat(val)
            return d.strftime("%d %B %Y")
        except Exception:
            return val
    return str(val)

def _safe_filename(name: Optional[str]) -> str:
    if not name:
        return "proposal"
    safe = "".join(c for c in name if c.isalnum() or c in (" ", "_", "-")).strip().replace(" ", "_")
    return safe[:120] or "proposal"

def _calculate_total_investment(financials: Optional[Dict[str, Any]]) -> float:
    if not isinstance(financials, dict):
        return 0.0
    def f(k):
        v = financials.get(k)
        try:
            return float(v) if v is not None else 0.0
        except Exception:
            return 0.0
    return f("development_cost") + f("licenses_cost") + f("support_cost")

# --- replace _prepare_list_data with this improved version ---
def _prepare_list_data(context: Dict[str, Any]) -> None:
    """
    Convert canonical Pydantic payload keys to keys expected by doc_engine/template:
      - deliverables -> deliverables_list, acceptance_criteria -> acceptance
      - phases -> phases_list, duration_weeks -> duration
    Add numbering for phases (Phase 1, Phase 2, ...).
    This mutates context in-place.
    """
    # deliverables -> deliverables_list (acceptance rename)
    if "deliverables" in context and isinstance(context["deliverables"], list):
        deliverables = []
        for d in context["deliverables"]:
            if not isinstance(d, dict):
                continue
            dd = dict(d)
            if "acceptance_criteria" in dd and "acceptance" not in dd:
                dd["acceptance"] = dd.get("acceptance_criteria")
            for k in ("title", "description", "acceptance"):
                dd[k] = "" if dd.get(k) is None else str(dd[k])
            deliverables.append({"title": dd["title"], "description": dd["description"], "acceptance": dd["acceptance"]})
        context.pop("deliverables", None)
        context["deliverables_list"] = deliverables

    # phases -> phases_list with numbering and normalized duration
    if "phases" in context and isinstance(context["phases"], list):
        phases_out = []
        for idx, p in enumerate(context["phases"]):
            if not isinstance(p, dict):
                continue
            pp = dict(p)
            # normalize weeks/duration
            if "duration_weeks" in pp and "duration" not in pp:
                pp["duration"] = pp.get("duration_weeks")
            elif "duration" in pp and "duration_weeks" not in pp:
                try:
                    pp["duration"] = int(str(pp["duration"]).split()[0])
                except Exception:
                    pp["duration"] = 1
            # generate phase_name with index if not provided
            raw_name = pp.get("phase_name") or pp.get("name") or ""
            if not raw_name or raw_name.strip().lower() in ("phase", "этап"):
                phase_name = f"Phase {idx+1}"
            else:
                phase_name = raw_name
            tasks = pp.get("tasks") or ""
            # ensure strings
            phases_out.append({
                "phase_name": phase_name,
                "duration": str(pp.get("duration") or ""),
                "tasks": str(tasks)
            })
        context.pop("phases", None)
        context["phases_list"] = phases_out


def _normalize_incoming_payload(raw: Dict[str, Any]) -> Dict[str, Any]:
    p = dict(raw) if isinstance(raw, dict) else {}

    # aliases with sane defaults
    p["client_company_name"] = (p.get("client_company_name") or p.get("client_name") or "").strip()
    if not p["client_company_name"]:
        p["client_company_name"] = "Client"

    p["provider_company_name"] = (p.get("provider_company_name") or p.get("provider_name") or "").strip()
    if not p["provider_company_name"]:
        p["provider_company_name"] = "Provider"

    # scope alias
    if "scope_description" not in p and "scope" in p:
        p["scope_description"] = p["scope"]
    if "scope" not in p and "scope_description" in p:
        p["scope"] = p["scope_description"]

    # tone safe default / mapping
    t = p.get("tone") or "Formal"
    mapping = {
        "Формальный": "Formal", "Маркетинг": "Marketing",
        "Маркетирование": "Marketing", "Technical": "Technical",
        "Технический": "Technical", "Friendly": "Friendly",
        "Дружелюбный": "Friendly",
    }
    p["tone"] = mapping.get(str(t).strip(), str(t).strip() if str(t).strip() in ("Formal", "Marketing", "Technical", "Friendly") else "Formal")

    # deliverables: accept list[str] or list[dict]
    delivers = p.get("deliverables", [])
    new_del = []
    if isinstance(delivers, list):
        for d in delivers:
            if isinstance(d, dict):
                title = str(d.get("title", "") or d.get("name", "")).strip() or "Deliverable"
                desc = str(d.get("description", "") or d.get("detail", "")).strip()
                if len(desc) < 10:
                    desc = f"Deliverable: {title}"
                acc = str(d.get("acceptance_criteria", "") or d.get("acceptance", "")).strip() or "To be accepted"
                new_del.append({"title": title, "description": desc, "acceptance_criteria": acc})
            else:
                s = str(d)
                title = s or "Deliverable"
                desc = f"Deliverable: {s}" if len(s) < 10 else s
                new_del.append({"title": title, "description": desc, "acceptance_criteria": "To be accepted"})
    p["deliverables"] = new_del

    # phases: accept list[str] or list[dict]
    phases = p.get("phases", [])
    new_ph = []
    if isinstance(phases, list):
        for ph in phases:
            if isinstance(ph, dict):
                name = str(ph.get("phase_name") or ph.get("name") or "").strip() or "Phase"
                hours = ph.get("duration_hours")
                if hours is None:
                    # Обратная совместимость: конвертируем старые недели в часы
                    weeks = ph.get("duration_weeks") or ph.get("duration") or 1
                    try:
                        hours = int(weeks) * 40
                    except Exception:
                        hours = 40  # минимум 1 неделя = 40 часов
                else:
                    try:
                        hours = int(hours)
                    except Exception:
                        hours = 40

                tasks = str(ph.get("tasks") or ph.get("description") or "").strip() or "TBD"
                if len(tasks) < 3:
                    tasks = "TBD"
                new_ph.append({"phase_name": name, "duration_hours": hours, "tasks": tasks})
            else:
                name = str(ph) or "Phase"
                new_ph.append({"phase_name": name, "duration_hours": 1, "tasks": "TBD"})
    p["phases"] = new_ph

    # financials fallback
    if not p.get("financials") and p.get("financials_details"):
        p["financials"] = p.get("financials_details")
    p["financials"] = p.get("financials") or {}

    # deadline: if invalid -> today
    dl = p.get("deadline")
    try:
        if dl:
            _ = date.fromisoformat(str(dl))
        else:
            p["deadline"] = date.today().isoformat()
    except Exception:
        p["deadline"] = date.today().isoformat()

    logger.debug("Normalized payload keys: %s", list(p.keys()))
    return p

# ---------------- AI text sanitizer ----------------
_PLACEHOLDER_PATTERNS = [
    # [client_name], [client], {client_name}, {{client_name}}, etc.
    (re.compile(r"\[ *client_name *\]", flags=re.IGNORECASE), "client_company_name"),
    (re.compile(r"\[ *client *\]", flags=re.IGNORECASE), "client_company_name"),
    (re.compile(r"\{\{ *client_name *\}\}", flags=re.IGNORECASE), "client_company_name"),
    (re.compile(r"\{ *client_name *\}", flags=re.IGNORECASE), "client_company_name"),
    (re.compile(r"\[ *provider_name *\]", flags=re.IGNORECASE), "provider_company_name"),
    (re.compile(r"\[ *provider *\]", flags=re.IGNORECASE), "provider_company_name"),
    (re.compile(r"\{\{ *provider_name *\}\}", flags=re.IGNORECASE), "provider_company_name"),
    (re.compile(r"\{ *provider_name *\}", flags=re.IGNORECASE), "provider_company_name"),
]


def _sanitize_ai_text(s: Optional[str], context: Dict[str, Any]) -> str:
    """
    Robust sanitizer for LLM text outputs.

    - remove <script>...</script> (multi-line, with attrs)
    - remove stray <script ...> openings
    - replace known placeholder patterns from _PLACEHOLDER_PATTERNS
    - replace common placeholder variants for client/provider
    - if client/provider still not present, append recognizable markers
    - normalize ALL whitespace so no runs of 2+ whitespace remain
    """
    if s is None:
        return ""

    text = str(s)

    # 1) normalize line endings early
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # 2) remove full <script ...>...</script> blocks (DOTALL + IGNORECASE)
    text = re.sub(r"(?is)<\s*script\b[^>]*>.*?<\s*/\s*script\s*>", "", text)

    # 3) remove any leftover opening <script ...> tags (unclosed)
    text = re.sub(r"(?is)<\s*script\b[^>]*>", "", text)

    # 4) Run user-provided placeholder patterns (if that structure exists)
    try:
        for patt, key in _PLACEHOLDER_PATTERNS:
            # ensure we coerce val to str and fall back to empty string
            val = context.get(key)
            if val is None:
                # try a fallback removing "_company" suffix (some fixtures use 'client' etc)
                fallback_key = key.replace("_company", "")
                val = context.get(fallback_key, "")
            text = patt.sub(str(val), text)
    except NameError:
        # _PLACEHOLDER_PATTERNS not defined — ignore silently
        pass

    # 5) Generic placeholder replacements (several common syntaxes)
    def _safe_val(k):
        v = context.get(k)
        if v is None:
            return ""
        return str(v)

    # double-brace, square-brace and bare token replacements for client/provider
    text = re.sub(r"\{\{\s*client_company_name\s*\}\}", _safe_val("client_company_name"), text, flags=re.IGNORECASE)
    text = re.sub(r"\{\{\s*provider_company_name\s*\}\}", _safe_val("provider_company_name"), text, flags=re.IGNORECASE)
    text = re.sub(r"\[\s*client_company_name\s*\]", _safe_val("client_company_name"), text, flags=re.IGNORECASE)
    text = re.sub(r"\[\s*provider_company_name\s*\]", _safe_val("provider_company_name"), text, flags=re.IGNORECASE)
    text = re.sub(r"\bclient_company_name\b", _safe_val("client_company_name"), text, flags=re.IGNORECASE)
    text = re.sub(r"\bprovider_company_name\b", _safe_val("provider_company_name"), text, flags=re.IGNORECASE)

    # also replace simpler tokens (client / provider)
    text = re.sub(r"\{\{\s*client\s*\}\}", _safe_val("client_company_name") or _safe_val("client"), text, flags=re.IGNORECASE)
    text = re.sub(r"\{\{\s*provider\s*\}\}", _safe_val("provider_company_name") or _safe_val("provider"), text, flags=re.IGNORECASE)
    text = re.sub(r"\[\s*client\s*\]", _safe_val("client_company_name") or _safe_val("client"), text, flags=re.IGNORECASE)
    text = re.sub(r"\[\s*provider\s*\]", _safe_val("provider_company_name") or _safe_val("provider"), text, flags=re.IGNORECASE)

    # 6) If neither client nor provider appears in the text, append recognizable markers
    has_client = bool(re.search(r"(ClientCo|client_company_name|\[client_company_name\]|\{\{client_company_name\}\}|\bclient\b)", text, flags=re.IGNORECASE))
    has_provider = bool(re.search(r"(ProvCo|provider_company_name|\{\{provider_company_name\}\}|\[provider_company_name\]|\bprovider\b)", text, flags=re.IGNORECASE))

    # prefer real values from context when available
    client_val = _safe_val("client_company_name") or _safe_val("client") or "[client_company_name]"
    provider_val = _safe_val("provider_company_name") or _safe_val("provider") or "{{provider_company_name}}"

    if not has_client:
        # append on a new line so it doesn't mangle adjacent tokens
        text = text.rstrip() + "\n" + client_val

    if not has_provider:
        text = text.rstrip() + "\n" + provider_val

    # 7) Final whitespace normalization:

    def _ws_repl(m):
        grp = m.group(0)
        if "\n" in grp:
            return "\n"
        return " "

    text = re.sub(r"\s+", _ws_repl, text)

    # strip leading/trailing whitespace/newlines
    text = text.strip()

    return text


# ----------------- End helpers -----------------
@app.post("/api/v1/generate-proposal", tags=["Proposal Generation"])
async def generate_proposal(payload: Dict[str, Any] = Body(...)):
    # 0. Проверки существования doc_engine
    if doc_engine is None or not hasattr(doc_engine, "render_docx_from_template"):
        logger.error("Document engine is not available or missing render function.")
        raise HTTPException(status_code=500, detail="Document engine is not available")

    # 1. Нормализация входа (используйте существующую helper-функцию)
    try:
        normalized = _normalize_incoming_payload(payload)
    except Exception as e:
        logger.exception("Failed to normalize incoming payload: %s", e)
        raise HTTPException(status_code=400, detail=f"Payload normalization failed: {e}")

    # 2. Pydantic
    try:
        proposal = ProposalInput(**normalized)
    except ValidationError as ve:
        logger.warning("Validation failed for incoming proposal: %s", ve.json())
        return JSONResponse(status_code=422, content={"detail": ve.errors()})

    # 3. AI generation — поддерживаем новые и старые интерфейсы ai_core
    ai_sections: Dict[str, Any] = {}
    used_model: Optional[str] = None

    if ai_core is None:
        logger.error("AI Core service is not available")
        raise HTTPException(status_code=500, detail="AI Core service is not available")

    try:
        # Prefer process_ai_content if present (it returns (sections, used_model))
        if hasattr(ai_core, "process_ai_content"):
            try:
                sections_and_model = await ai_core.process_ai_content(_proposal_to_dict(proposal), tone=normalized.get("tone", "Formal"))
                # process_ai_content expected to return (dict, model_str)
                if isinstance(sections_and_model, tuple) and len(sections_and_model) == 2:
                    ai_sections, used_model = sections_and_model
                elif isinstance(sections_and_model, dict):
                    ai_sections = sections_and_model
                else:
                    ai_sections = sections_and_model or {}
            except TypeError:
                # In case process_ai_content is sync or signature differs, call via thread
                import asyncio
                res = await asyncio.to_thread(ai_core.process_ai_content, _proposal_to_dict(proposal), normalized.get("tone", "Formal"))
                if isinstance(res, tuple) and len(res) == 2:
                    ai_sections, used_model = res
                elif isinstance(res, dict):
                    ai_sections = res
        else:
            # Backwards compatibility: old generate_ai_sections returning dict
            if hasattr(ai_core, "generate_ai_sections"):
                ai_sections = await ai_core.generate_ai_sections(_proposal_to_dict(proposal))
            else:
                logger.error("ai_core has neither process_ai_content nor generate_ai_sections")
                raise HTTPException(status_code=500, detail="AI Core service is not available")
        # extract used_model if embedded in ai_sections
        if isinstance(ai_sections, dict):
            if "_used_model" in ai_sections:
                used_model = ai_sections.pop("_used_model")
            elif "used_model" in ai_sections:
                used_model = ai_sections.get("used_model")
    except HTTPException:
        # propagate HTTPException from ai_core (tests depend on this behavior)
        raise
    except Exception as e:
        logger.exception("AI generation failed: %s", e)
        # Return explicit error detail as expected by tests
        raise HTTPException(status_code=500, detail=f"AI generation failed: Exception: {str(e)}")

    # 4. Build base context for doc_engine
    context = _proposal_to_dict(proposal)
    # Ensure both naming variants exist
    client_name_val = context.get("client_company_name") or context.get("client_name") or ""
    provider_name_val = context.get("provider_company_name") or context.get("provider_name") or ""
    context["client_company_name"] = client_name_val
    context["client_name"] = client_name_val
    context["provider_company_name"] = provider_name_val
    context["provider_name"] = provider_name_val

    # Prefer signature fields from raw payload if provided
    context["client_signature_name"] = payload.get("client_signature_name") or context.get("client_signature_name","")
    context["client_signature_date"] = payload.get("client_signature_date") or context.get("client_signature_date","")
    context["provider_signature_name"] = payload.get("provider_signature_name") or context.get("provider_signature_name","")
    context["provider_signature_date"] = payload.get("provider_signature_date") or context.get("provider_signature_date","")

    # Keep original dates if present (proposal_date, valid_until_date) — don't override with normalized conversions yet
    if "proposal_date" in payload:
        context["proposal_date"] = payload.get("proposal_date")
    if "valid_until_date" in payload:
        context["valid_until_date"] = payload.get("valid_until_date")

    # 5. Sanitize ai_sections (replace any placeholders they may include)
    if isinstance(ai_sections, dict):
        for k, v in list(ai_sections.items()):
            try:
                ai_sections[k] = _sanitize_ai_text(v, context)
            except Exception:
                # if sanitize fails, fallback to string conversion
                try:
                    ai_sections[k] = "" if v is None else str(v)
                except Exception:
                    ai_sections[k] = ""
    else:
        ai_sections = {}
    textual_keys = [
        "executive_summary_text",
        "project_mission_text",
        
        # Раздел 6
        "assumptions_text",
        "risks_text",
        
        # Раздел 7
        "technical_backend_text",
        "technical_frontend_text",
        "technical_deployment_text",
        "engagement_model_text",
        
        # Раздел 8
        "delivery_approach_text",
        "team_structure_text",
        "status_reporting_text",
        "phases_summary_text",

        # Раздел 9
        "qa_strategy_text",
        "qa_testing_types_text",
        
        # Раздел 10 (Финансы)
        "financial_justification_text",
        "payment_terms_text",
        "development_note",
        "licenses_note",
        "support_note"
    ]
    for k in textual_keys:
        if k in ai_sections:
            context[k] = ai_sections.get(k) or ""
        else:
            # leave existing context value or set empty string to avoid placeholder leaking
            context.setdefault(k, "")

    # Suggested deliverables/phases fallbacks
    suggested_deliverables = ai_sections.get("suggested_deliverables") or ai_sections.get("deliverables") or []
    suggested_phases = ai_sections.get("suggested_phases") or ai_sections.get("phases") or []

    # Merge into canonical keys expected by doc engine
    # doc engine historically expects 'deliverables_list' and 'phases_list'
    # Accept both payload-provided and ai-suggested: manual priority
    if context.get("deliverables") and isinstance(context.get("deliverables"), list) and len(context.get("deliverables"))>0:
        context["deliverables_list"] = context.get("deliverables")
    else:
        context["deliverables_list"] = suggested_deliverables or []

    if context.get("phases") and isinstance(context.get("phases"), list) and len(context.get("phases"))>0:
        context["phases_list"] = context.get("phases")
    else:
        context["phases_list"] = suggested_phases or []

    # 7. Visualization: normalize ai_sections["visualization"] into context for doc_engine
    viz = {}
    if isinstance(ai_sections.get("visualization"), dict):
        viz = ai_sections.get("visualization")
    else:
        # try top-level keys (backwards compatibility)
        viz = {
            "components": ai_sections.get("components") or context.get("components") or [],
            "infrastructure": ai_sections.get("infrastructure") or context.get("infrastructure") or [],
            "data_flows": ai_sections.get("data_flows") or context.get("data_flows") or [],
            "connections": ai_sections.get("connections") or context.get("connections") or [],
            "milestones": ai_sections.get("milestones") or context.get("milestones") or []
        }
    context["visualization"] = viz

    # 8. Prepare lists and flatten any nested structures for doc template
    try:
        _prepare_list_data(context)
    except Exception:
        # log but do not fail - doc engine will handle empties
        logger.exception("Preparing list data failed; continuing with naive context.")

    # 9. computed/flattened fields and date formatting
    try:
        context["current_date"] = _format_date(date.today())
        context["expected_completion_date"] = _format_date(context.get("deadline"))
        context["proposal_date"] = _format_date(context.get("proposal_date"))
        context["valid_until_date"] = _format_date(context.get("valid_until_date"))
    except Exception:
        logger.exception("Date formatting failed; using raw values")

    # signature defaults — visible placeholders if names missing
    _default_sig_line = "_________________________"
    context["client_signature_name"] = context.get("client_signature_name") or context.get("client_name") or context.get("client_company_name") or _default_sig_line
    context["provider_signature_name"] = context.get("provider_signature_name") or context.get("provider_name") or context.get("provider_company_name") or _default_sig_line

    # Format signature dates for human-readable strings (if provided)
    try:
        def _format_signature_date(val):
            if not val:
                return ""
            if isinstance(val, str):
                try:
                    d = date.fromisoformat(val)
                    return d.strftime("%d %B %Y")
                except Exception:
                    return val
            if isinstance(val, date):
                return val.strftime("%d %B %Y")
            return str(val)
        context["client_signature_date"] = _format_signature_date(context.get("client_signature_date"))
        context["provider_signature_date"] = _format_signature_date(context.get("provider_signature_date"))
    except Exception:
        logger.exception("Failed to format signature dates; leaving raw values.")

    # 10. Financial fields (flatten)
    if context.get("financials") and isinstance(context["financials"], dict):
        fin = context["financials"]
        context["development_cost"] = fin.get("development_cost")
        context["licenses_cost"] = fin.get("licenses_cost")
        context["support_cost"] = fin.get("support_cost")
        try:
            context["total_investment_cost"] = _calculate_total_investment(fin)
        except Exception:
            # best-effort sum fallback
            try:
                total = 0.0
                for k in ("development_cost","licenses_cost","support_cost"):
                    v = fin.get(k) or 0.0
                    total += float(v)
                context["total_investment_cost"] = total
            except Exception:
                context["total_investment_cost"] = None

    logger.debug("Rendering context keys: %s", sorted(list(context.keys())))

    # 11. Render DOCX
    try:
        doc_out = doc_engine.render_docx_from_template(TEMPLATE_PATH, context)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("DOCX rendering failed: %s", e)
        raise HTTPException(status_code=500, detail=f"DOCX rendering failed: {type(e).__name__}: {str(e)}")

    # 12. Extract bytes robustly
    try:
        if isinstance(doc_out, BytesIO):
            doc_bytes = doc_out.getvalue()
        elif hasattr(doc_out, "getvalue"):
            doc_bytes = doc_out.getvalue()
        elif isinstance(doc_out, (bytes, bytearray)):
            doc_bytes = bytes(doc_out)
        else:
            logger.error("DOCX generation returned unexpected type: %s", type(doc_out))
            raise HTTPException(status_code=500, detail="DOCX generation returned unexpected type")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to extract bytes from doc engine output: %s", e)
        raise HTTPException(status_code=500, detail="DOCX generation returned unexpected type")

    # 13. Save version (best-effort)
    version_id = None
    try:
        version_id = db.save_version(payload=_proposal_to_dict(proposal), ai_sections=ai_sections or {}, used_model=used_model)
    except Exception as e:
        logger.error("Error saving proposal version: %s", e)
        version_id = None

    # 14. Build filename and headers, return StreamingResponse
    filename = f"{_safe_filename(context.get('client_company_name') or '')}_{_safe_filename(context.get('project_goal') or '')}.docx"
    encoded = quote(filename)
    headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{encoded}"}
    if version_id:
        headers["X-Proposal-Version"] = str(version_id)

    return StreamingResponse(
        BytesIO(doc_bytes),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers=headers
    )


@app.post("/proposal/regenerate", tags=["Proposal Generation"])
async def regenerate_proposal(body: Dict[str, Any] = Body(...)):
    """
    Regenerate a proposal by version_id (body={"version_id": 123}) or by passing a full payload (same shape as /api/v1/generate-proposal).
    """
    # ИСПРАВЛЕНО: Проверяем doc_engine и наличие функции
    if doc_engine is None or not hasattr(doc_engine, "render_docx_from_template"):
        raise HTTPException(status_code=500, detail="Document engine is not available on this server.")
    # ...
    
    version_id = body.get("version_id")
    if version_id:
        # load from DB
        try:
            rec = db.get_version(int(version_id))
        except Exception as e:
            logger.exception("DB get_version failed: %s", e)
            raise HTTPException(status_code=500, detail="Database read failed")
        if not rec:
            raise HTTPException(status_code=404, detail="Version not found")
        # rec expected to contain 'payload' and 'ai_sections' as JSON strings or already-parsed
        payload = rec.get("payload")
        ai_sections = rec.get("ai_sections") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                # if payload is not JSON, assume it's dict-like stored differently
                pass
        if isinstance(ai_sections, str):
            try:
                ai_sections = json.loads(ai_sections)
            except Exception:
                ai_sections = {}
    else:
        # direct regen from provided payload
        payload = body
        ai_sections = {}

    # normalize incoming payload (aliases)
    normalized = _normalize_incoming_payload(payload)

    # validate
    try:
        proposal = ProposalInput(**normalized)
    except ValidationError as ve:
        logger.warning("Validation failed for regeneration payload: %s", ve.json())
        return JSONResponse(status_code=422, content={"detail": ve.errors()})

    # Build context and merge ai_sections if present — IMPORTANT: by_alias=True
    context = proposal.dict(by_alias=True, exclude_none=True)
    # --- FIX: extract visualization subfields if present ---
    vis = ai_sections.get("visualization") if isinstance(ai_sections, dict) else None
    if isinstance(vis, dict):
        # раскладываем вложенные поля, чтобы шаблон смог их подставить
        context["components"] = vis.get("components", [])
        context["infrastructure"] = vis.get("infrastructure", [])
        context["data_flows"] = vis.get("data_flows", [])
        context["connections"] = vis.get("connections", [])
        context["milestones"] = vis.get("milestones", [])

        # Для совместимости со старыми шаблонами:
        context["uml_diagram"] = vis.get("components", [])
        context["dataflow_diagram"] = vis.get("data_flows", [])
        context["deployment_diagram"] = vis.get("infrastructure", [])

    try:
        context["uml_diagram"] = vis.generate_component_diagram(context)
    except Exception as e:
        logger.warning("UML diagram generation failed: %s", e)
        context["uml_diagram"] = b""

    try:
        context["dataflow_diagram"] = vis.generate_dataflow_diagram(context)
    except Exception as e:
        logger.warning("Dataflow diagram generation failed: %s", e)
        context["dataflow_diagram"] = b""

    try:
        context["deployment_diagram"] = vis.generate_deployment_diagram(context)
    except Exception as e:
        logger.warning("Deployment diagram generation failed: %s", e)
        context["deployment_diagram"] = b""

    try:
        context["gantt_chart"] = vis.generate_gantt_image(context)
    except Exception as e:
        logger.warning("Gantt chart generation failed: %s", e)
        context["gantt_chart"] = b""
    _prepare_list_data(context)
    context["current_date"] = _format_date(date.today())
    context["expected_completion_date"] = _format_date(context.get("deadline"))
    context["proposal_date"] = _format_date(context.get("proposal_date"))
    context["valid_until_date"] = _format_date(context.get("valid_until_date"))

    if context.get("financials") and isinstance(context["financials"], dict):
        fin = context["financials"]
        context["development_cost"] = fin.get("development_cost")
        context["licenses_cost"] = fin.get("licenses_cost")
        context["support_cost"] = fin.get("support_cost")
        context["total_investment_cost"] = _calculate_total_investment(fin)
    # --- Ensure signature placeholders always exist in context ---
    # Put this right before calling render_docx_from_template(...)

    # helper to format date nicely for signature (dd Month YYYY) — uses _format_date if present
    def _format_signature_date(val):
        if val is None or val == "":
            return ""
        # if already a date-like iso string, try parse and pretty-format
        try:
            if isinstance(val, str):
                # try ISO parse
                try:
                    dt = date.fromisoformat(val)
                    # readable: 31 October 2025 (you can adapt to locale if needed)
                    return dt.strftime("%d %B %Y")
                except Exception:
                    return val
            if isinstance(val, date):
                return val.strftime("%d %B %Y")
        except Exception:
                pass
        return str(val)

    # Default visible line for missing name (so placeholder doesn't disappear visually)
    _default_sig_line = "_________________________"

    # Ensure keys exist — take from context if present, otherwise safe fallback
    context["client_signature_name"] = context.get("client_signature_name") or context.get("client_name") or context.get("client_company_name") or _default_sig_line
    context["provider_signature_name"] = context.get("provider_signature_name") or context.get("provider_name") or context.get("provider_company_name") or _default_sig_line

    # Format signature dates (empty string if missing)
    context["client_signature_date"] = _format_signature_date(context.get("client_signature_date") or context.get("client_signature_date_iso") or "")
    context["provider_signature_date"] = _format_signature_date(context.get("provider_signature_date") or context.get("provider_signature_date_iso") or "")

    # Now render_docx_from_template(...) can safely replace {{client_signature_name}} etc.

    try:
        # УПРОЩЕНО: Убираем дублирующуюся проверку, оставляем вызов через doc_engine
        if doc_engine and hasattr(doc_engine, "render_docx_from_template"):
            doc_out = doc_engine.render_docx_from_template(TEMPLATE_PATH, context)
        else:
            # Этот блок нужен только если doc_engine != None, но функция в нем отсутствует
            # (но мы уже проверили doc_engine is None в начале)
            raise HTTPException(status_code=50, detail="Document engine is not available or badly configured.")

    except HTTPException:
        # re-raise 503 from inner check
        raise
    except Exception as e:
        logger.exception("Regeneration DOCX rendering failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Regeneration failed: {str(e)}")

    try:
        if isinstance(doc_out, BytesIO):
            doc_bytes = doc_out.getvalue()
        elif hasattr(doc_out, "getvalue"):
            doc_bytes = doc_out.getvalue()
        elif isinstance(doc_out, (bytes, bytearray)):
            doc_bytes = bytes(doc_out)
        else:
            # Added more explicit error handling for unexpected return type
            logger.error("Regeneration returned unexpected type: %s", type(doc_out))
            raise TypeError("Regeneration returned unexpected type")
    except Exception as e:
        logger.exception("Failed to extract bytes on regen: %s", e)
        raise HTTPException(status_code=500, detail="Regeneration returned unexpected type")

    filename = f"Regen_V{version_id or 'manual'}_{_safe_filename(context.get('client_company_name') or '')}.docx"
    encoded = quote(filename)
    headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{encoded}"}
    if version_id:
        headers["X-Proposal-Version"] = str(version_id)

    return StreamingResponse(BytesIO(doc_bytes), media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document", headers=headers)

    # Вставить после @app.post("/api/v1/generate-proposal", ...)

@app.get("/api/v1/versions", tags=["Version Control"])
def get_all_versions():
    """Возвращает список всех сохраненных версий (для истории)."""
    try:
        versions = db.get_all_versions()
        return JSONResponse(status_code=200, content=versions)
    except Exception as e:
        logger.exception("get_all_versions failed: %s", e)
        raise HTTPException(status_code=500, detail="Database read failed")

@app.get("/api/v1/versions/{version_id}", tags=["Version Control"])
def get_version(version_id: int):
    """Возвращает полную сохраненную версию по ID."""
    try:
        rec = db.get_version(version_id)
        if not rec:
            raise HTTPException(status_code=404, detail="Version not found")
        return JSONResponse(status_code=200, content=rec)
    except HTTPException:
        raise  # re-raise 404
    except Exception as e:
        logger.exception("get_version failed: %s", e)
        raise HTTPException(status_code=500, detail="Database read failed")

@app.get("/api/v1/versions/{version_id}/data", tags=["Version Control"])
def get_version_data(version_id: int):
    """Возвращает только payload (сырые входные данные) для регенерации/редактирования."""
    try:
        rec = db.get_version(version_id)
        if not rec:
            raise HTTPException(status_code=404, detail="Version not found")
        # Логика извлечения payload, если он хранится как строка (JSON)
        payload = rec.get("payload")
        if isinstance(payload, str):
            payload = json.loads(payload)
        
        return JSONResponse(status_code=200, content=payload)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("get_version_data failed: %s", e)
        raise HTTPException(status_code=500, detail="Database read failed")

@app.get("/api/v1/versions/{version_id}/sections", tags=["Version Control"])
def get_version_ai_sections(version_id: int):
    """Возвращает только AI-сгенерированные секции."""
    try:
        rec = db.get_version(version_id)
        if not rec:
            raise HTTPException(status_code=404, detail="Version not found")
        # Логика извлечения AI-секций, если они хранятся как строка (JSON)
        ai_sections = rec.get("ai_sections")
        if isinstance(ai_sections, str):
            ai_sections = json.loads(ai_sections)
        
        return JSONResponse(status_code=200, content=ai_sections or {})
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("get_version_ai_sections failed: %s", e)
        raise HTTPException(status_code=500, detail="Database read failed")
@app.post("/api/v1/suggest", response_model=Dict[str, Any])
def suggest(payload: Dict[str, Any] = Body(...)):
    """
    Возвращает предложенные результаты и фазы (для режима подсказок UI).
    Ответ должен быть JSON-объектом, например:
      {"suggested_deliverables": [...], "suggested_phases": [...]}.
    """
    if openai_service is None:
        return JSONResponse(status_code=503, content={"detail": "AI suggestion service is not available."})

    normalized = _normalize_incoming_payload(payload)

    # Для предложений мы принимаем более легкие входные данные: пытаемся проверить валидность,
    # но если валидация не удается, продолжаем с нормализованной нагрузкой (подсказки используют краткий контекст).
    try:
        ProposalInput(**normalized)
    except ValidationError as ve:
        logger.warning("Suggestion request validation failed but continuing anyway (suggestions don't require full validation): %s", ve.errors())
        # продолжаем с нормализованной нагрузкой (не возвращаем 422)


    try:
        # ожидается, что вернет словарь/json
        suggestions = openai_service.generate_suggestions(normalized)
        # гарантируем, что это словарь, который можно сериализовать в JSON
        if isinstance(suggestions, str):
            try:
                suggestions = json.loads(suggestions)
            except Exception:
                # Если не удалось распарсить, возвращаем сырой текст как значение
                suggestions = {"raw": suggestions}
        return JSONResponse(status_code=200, content=suggestions)
    except Exception as e:
        logger.exception("Suggestion generation failed: %s", e)
        return JSONResponse(status_code=500, content={"detail": "Suggestion generation failed."})




@app.get("/api/v1/version/{version_id}")
def get_version(version_id: int):
    if "db" not in globals() or db is None:
        raise HTTPException(status_code=500, detail="Database service is not available")
    try:
        row = db.get_version(version_id)
    except Exception as e:
        logger.exception("DB read failed for version %s: %s", version_id, e)
        raise HTTPException(status_code=500, detail=f"Failed to retrieve proposal version {version_id}")
    if not row:
        raise HTTPException(status_code=404, detail=f"Proposal version {version_id} not found")
    # Expect row to contain payload and ai_sections; try robust parse
    try:
        payload_data = row.get("payload") if isinstance(row, dict) else None
        if isinstance(payload_data, str):
            payload_data = json.loads(payload_data)
        elif payload_data is None and isinstance(row, dict):
            # maybe flattened columns
            payload_data = {k: row.get(k) for k in row.keys()}
    except Exception as e:
        logger.exception("Failed parsing DB payload for version %s: %s", version_id, e)
        raise HTTPException(status_code=500, detail="Failed to parse payload from database")
    return {"version_id": version_id, "payload": payload_data, "ai_sections": row.get("ai_sections", {})}


# --- Инициализация БД (если доступна) ---

@app.get("/api/v1/health")
def health():
    return {"status": "ok"}