# test_render.py
import json, traceback
from backend.app.doc_engine import render_docx_from_template
from backend.app.models import ProposalInput
from pathlib import Path

payload_path = Path("D:/programming/ai-sales-proposal-generator/payload.json")
template_path = Path("D:/programming/ai-sales-proposal-generator/docs/template.docx")
out_path = Path("D:/programming/ai-sales-proposal-generator/test_output.docx")

print("Template exists:", template_path.exists())
print("Payload exists:", payload_path.exists())

payload = json.load(open(payload_path, "r", encoding="utf-8"))

try:
    # Validate input (same as endpoint)
    p = ProposalInput(**payload)
    # Build context (same logic as endpoint)
    fin = p.financials or {}
    dev = getattr(fin, "development_cost", None) if fin else None
    lic = getattr(fin, "licenses_cost", None) if fin else None
    sup = getattr(fin, "support_cost", None) if fin else None
    total = sum(float(x or 0) for x in [dev, lic, sup])
    context = {
        "current_date": "",
        "client_company_name": p.client_name,
        "provider_company_name": p.provider_name,
        "expected_completion_date": str(p.deadline) if p.deadline else "",
        "development_cost": dev,
        "licenses_cost": lic,
        "support_cost": sup,
        "total_investment_cost": total,
        # minimal AI-stub texts to avoid empty values
        "executive_summary_text": "EXEC_SUM",
        "project_mission_text": "MISSION",
        "solution_concept_text": "SOLUTION",
        "project_methodology_text": "METHODOLOGY",
        "financial_justification_text": "FIN_JUST",
        "payment_terms_text": "PAYMENT",
        "development_note": "DEV_NOTE",
        "licenses_note": "LIC_NOTE",
        "support_note": "SUPPORT_NOTE",
    }
    # deliverables and phases
    context["deliverables_list"] = [{"title": d.title, "description": d.description, "acceptance": d.acceptance_criteria} for d in (p.deliverables or [])]
    context["phases_list"] = [{"phase_name": f"Phase {i+1}", "duration": f"{ph.duration_weeks} weeks", "tasks": ph.tasks} for i, ph in enumerate(p.phases or [])]

    print("Calling render_docx_from_template...")
    docx_io = render_docx_from_template(str(template_path), context)
    # save to disk
    with open(out_path, "wb") as f:
        f.write(docx_io.getvalue())
    print("Saved test output to:", out_path)
except Exception:
    traceback.print_exc()
