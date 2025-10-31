# backend/app/models.py
from __future__ import annotations
from pydantic import BaseModel, Field, field_validator
from typing import List, Optional
from datetime import date
from enum import Enum

class ToneEnum(str, Enum):
    Formal = "Formal"
    Marketing = "Marketing"
    Technical = "Technical"
    Friendly = "Friendly"
    # include Russian synonyms if incoming payload might use them
    Маркетинг = "Marketing"
    Маркетирование = "Marketing"

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
    # Primary canonical field names (used in code)
    client_name: str = Field(..., min_length=2, max_length=200, alias="client_company_name")
    provider_name: str = Field(..., min_length=2, max_length=200, alias="provider_company_name")

    # Accept both "scope" and older "scope_description" as alias
    project_goal: Optional[str] = Field("", max_length=1500)
    scope: Optional[str] = Field("", max_length=4000, alias="scope_description")

    # technologies can be list or comma string (normalize below)
    technologies: Optional[List[str]] = Field(default_factory=list)
    deadline: Optional[date] = None

    # tone: use enum for strict values; accepts English + Russian synonyms via ToneEnum
    tone: Optional[ToneEnum] = Field(ToneEnum.Formal)

    deliverables: Optional[List[Deliverable]] = Field(default_factory=list)
    phases: Optional[List[Phase]] = Field(default_factory=list)
    financials: Optional[Financials] = None

    # --- signature fields ---
    client_signature_name: Optional[str] = Field(None)
    client_signature_date: Optional[date] = None
    provider_signature_name: Optional[str] = Field(None)
    provider_signature_date: Optional[date] = None

    @field_validator("technologies", mode="before")
    @classmethod
    def _normalize_technologies(cls, v):
        if v is None:
            return []
        if isinstance(v, list):
            return [s.strip() for s in v if isinstance(s, str) and s.strip()]
        if isinstance(v, str):
            v_str = v.strip()
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

    class Config:
        # allow payloads to use field names (client_name) or aliases (client_company_name)
        allow_population_by_field_name = True
        # allow population using aliases (so incoming client_company_name maps to client_name)
        allow_population_by_field_alias = True
        # keep enum values as strings in .dict() / .json()
        use_enum_values = True
