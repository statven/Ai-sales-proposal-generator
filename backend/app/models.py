# backend/app/models.py
from __future__ import annotations
from typing import List, Optional
from datetime import date

from pydantic import BaseModel, Field, field_validator


class Deliverable(BaseModel):
    title: str = Field(..., min_length=3, max_length=200)
    description: str = Field(..., min_length=10, max_length=2000)
    acceptance_criteria: str = Field(..., min_length=3, max_length=1000)


class Phase(BaseModel):
    duration_weeks: int = Field(..., ge=1, le=52)
    tasks: str = Field(..., min_length=3, max_length=3000)  # newline-separated or markdown


class Financials(BaseModel):
    development_cost: Optional[float] = Field(None, ge=0)
    licenses_cost: Optional[float] = Field(None, ge=0)
    support_cost: Optional[float] = Field(None, ge=0)


class ProposalInput(BaseModel):
    client_name: str = Field(..., min_length=2, max_length=200)
    provider_name: str = Field(..., min_length=2, max_length=200)
    project_goal: Optional[str] = Field("", max_length=1500)
    scope: Optional[str] = Field("", max_length=4000)
    technologies: Optional[List[str]] = Field(default_factory=list)
    deadline: Optional[date] = None
    # use pattern instead of regex in Pydantic v2
    tone: Optional[str] = Field("Formal", pattern=r"^(Formal|Marketing)$")
    deliverables: Optional[List[Deliverable]] = Field(default_factory=list)
    phases: Optional[List[Phase]] = Field(default_factory=list)
    financials: Optional[Financials] = None

    # field validator for technologies: accept JSON-list-like or CSV string
    @field_validator("technologies", mode="before")
    @classmethod
    def _normalize_technologies(cls, v):
        if v is None:
            return []
        # if already a list, keep as-is (strip strings inside)
        if isinstance(v, list):
            return [s.strip() for s in v if isinstance(s, str) and s.strip()]
        # if comma-separated string
        if isinstance(v, str):
            # try to parse JSON array-like quickly
            v_str = v.strip()
            if v_str.startswith("[") and v_str.endswith("]"):
                try:
                    import json
                    parsed = json.loads(v_str)
                    if isinstance(parsed, list):
                        return [s.strip() for s in parsed if isinstance(s, str) and s.strip()]
                except Exception:
                    pass
            # fallback split by comma
            return [s.strip() for s in v_str.split(",") if s.strip()]
        # unknown type -> empty list (fail-safe)
        return []
