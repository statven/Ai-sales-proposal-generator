# backend/app/db.py
import os
import json
from datetime import datetime, date # Import date for custom serialization
from typing import Optional, Dict, Any

from sqlalchemy import create_engine, Column, Integer, DateTime, Text, String
from sqlalchemy.orm import declarative_base, sessionmaker

DB_PATH = os.getenv("PROPOSAL_DB_PATH", os.path.join(os.getcwd(), "data", "proposals.db"))
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
ENGINE = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=ENGINE, autoflush=False, autocommit=False)
Base = declarative_base()

# FIX: Custom JSON encoder function to handle date/datetime objects
def _json_default(obj):
    """Fallback function for json.dumps to handle non-serializable objects."""
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    raise TypeError(f"Object of type {obj.__class__.__name__} is not JSON serializable")

class ProposalVersion(Base):
    __tablename__ = "proposal_versions"
    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    payload = Column(Text, nullable=False)          # JSON string
    ai_sections = Column(Text, nullable=True)       # JSON string
    used_model = Column(String(200), nullable=True)
    note = Column(String(500), nullable=True)

def init_db():
    Base.metadata.create_all(bind=ENGINE)
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
def save_version(payload: Dict[str, Any], ai_sections: Optional[Dict[str, Any]] = None, used_model: Optional[str] = None, note: Optional[str] = None) -> int:
    s = SessionLocal()
    try:
        pv = ProposalVersion(
            # FIX: Use custom default encoder
            payload=json.dumps(payload, ensure_ascii=False, default=_json_default),
            ai_sections=json.dumps(ai_sections or {}, ensure_ascii=False, default=_json_default),
            used_model=used_model,
            note=note
        )
        s.add(pv)
        s.commit()
        s.refresh(pv)
        return pv.id
    finally:
        s.close()

def get_version(version_id: int) -> Optional[Dict[str, Any]]:
    s = SessionLocal()
    try:
        pv = s.query(ProposalVersion).filter(ProposalVersion.id == version_id).first()
        if not pv:
            return None
        return {
            "id": pv.id,
            "created_at": pv.created_at.isoformat(),
            "payload": json.loads(pv.payload),
            "ai_sections": json.loads(pv.ai_sections or "{}"),
            "used_model": pv.used_model,
            "note": pv.note
        }
    finally:
        s.close()