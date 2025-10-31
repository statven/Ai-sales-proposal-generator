# backend/app/models.py
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator
from typing import List, Optional
from datetime import date

class Deliverable(BaseModel):
    title: str = Field(..., min_length=3, max_length=200)
    description: str = Field(..., min_length=10, max_length=2000)
    acceptance_criteria: str = Field(..., min_length=3, max_length=1000)

class Phase(BaseModel):
    duration_weeks: int = Field(..., ge=1, le=52)
    tasks: str = Field(..., min_length=3, max_length=3000)

class Financials(BaseModel):
    development_cost: Optional[float] = Field(None, ge=0)
    licenses_cost: Optional[float] = Field(None, ge=0)
    support_cost: Optional[float] = Field(None, ge=0)

class ProposalInput(BaseModel):
    """
    Основная Pydantic-модель для входящего payload'а.
    Поле `tone` нормализуется и валидируется (принимаются русские и английские варианты).
    """
    # --- New Fields Added ---
    proposal_title: Optional[str] = Field("", max_length=255)
    proposal_date: Optional[date] = None # Added missing field
    valid_until_date: Optional[date] = None # Added missing field
    # ------------------------

    client_company_name: str = Field(..., min_length=2, max_length=200)
    provider_company_name: str = Field(..., min_length=2, max_length=200)
    project_goal: Optional[str] = Field("", max_length=1500)
    scope: Optional[str] = Field("", max_length=4000)
    technologies: Optional[List[str]] = Field(default_factory=list)
    deadline: Optional[date] = None

    # tone: нормализуется в один из: Formal, Marketing, Technical, Friendly
    tone: Optional[str] = Field("Formal", description="One of Formal|Marketing|Technical|Friendly (case-insensitive). Russian synonyms accepted.")

    deliverables: Optional[List[Deliverable]] = Field(default_factory=list)
    phases: Optional[List[Phase]] = Field(default_factory=list)
    financials: Optional[Financials] = None

    # --- поля для подписей ---
    client_signature_name: Optional[str] = ""
    client_signature_date: Optional[date] = None
    provider_signature_name: Optional[str] = ""
    provider_signature_date: Optional[date] = None

    # ------------- Validators -------------

    @field_validator("technologies", mode="before")
    @classmethod
    def _normalize_technologies(cls, v):
        """
        Accept:
         - list of strings
         - comma-separated string
         - JSON-like list string
        """
        if v is None:
            return []
        if isinstance(v, list):
            return [s.strip() for s in v if isinstance(s, str) and s.strip()]
        if isinstance(v, str):
            v_str = v.strip()
            # try to parse a JSON array string first
            if v_str.startswith("[") and v_str.endswith("]"):
                try:
                    import json
                    parsed = json.loads(v_str)
                    if isinstance(parsed, list):
                        return [s.strip() for s in parsed if isinstance(s, str) and s.strip()]
                except Exception:
                    pass
            return [s.strip() for s in v_str.split(",") if s.strip()]
        return []

    @field_validator("tone", mode="before")
    @classmethod
    def _normalize_tone_before(cls, v):
        """
        Normalize many common inputs (English & Russian) to canonical English tokens.
        Called before other validations.
        """
        if v is None:
            return "Formal"
        s = str(v).strip()
        if not s:
            return "Formal"
        mapping = {
            # English
            "formal": "Formal",
            "marketing": "Marketing",
            "technical": "Technical",
            "friendly": "Friendly",
            # Russian common synonyms
            "формальный": "Formal",
            "форматный": "Formal",
            "формал": "Formal",
            "маркетинг": "Marketing",
            "маркетирование": "Marketing",
            "маркетинговый": "Marketing",
            "технический": "Technical",
            "техничный": "Technical",
            "technical": "Technical",
            "friendly": "Friendly",
            "дружелюбный": "Friendly",
            "дружественный": "Friendly",
        }
        key = s.lower()
        return mapping.get(key, s)

    @field_validator("tone")
    @classmethod
    def _validate_tone_allowed(cls, v):
        """
        Validate final normalized tone value is one of allowed tokens.
        """
        allowed = {"Formal", "Marketing", "Technical", "Friendly"}
        if v not in allowed:
            raise ValueError(f"tone must be one of {sorted(allowed)} (accepted synonyms in RU/EN). Received: {v!r}")
        return v