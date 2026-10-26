"""api/routers/papers.py — Generation, review & export endpoints. All queries are subject-scoped."""
from __future__ import annotations
from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import StreamingResponse
import io
from api.schemas.papers import (
    GenerateRequest, GenerateResponse, PaperSetOut, PaperListItem,
    PaperQuestionOut, QuestionStatusUpdate, ValidationReportOut, ValidationFindingOut,
)
from api.deps import get_subject_id
from repositories.database import get_session, PaperSetDB, PaperQuestionDB
from repositories.settings_repo import SettingsRepository

router = APIRouter(prefix="/papers", tags=["papers"])


@router.get("", response_model=list[PaperListItem])
def list_papers(subject_id: str = Depends(get_subject_id)):
    with get_session() as session:
        rows = (
            session.query(PaperSetDB)
            .filter(PaperSetDB.subject_id == subject_id)
            .order_by(PaperSetDB.generated_at.desc())
            .limit(50)
            .all()
        )
        return [
            PaperListItem(
                set_id=r.set_id,
                exam_type=r.exam_type,
                status=r.status,
                created_at=r.generated_at,
                question_count=session.query(PaperQuestionDB)
                    .filter(
                        PaperQuestionDB.paper_set_id == r.set_id,
                        PaperQuestionDB.subject_id == subject_id,
                    ).count(),
            )
            for r in rows
        ]


@router.post("/generate", response_model=GenerateResponse)
def generate_papers(body: GenerateRequest, subject_id: str = Depends(get_subject_id)):
    from agents.coordinator import CoordinatorAgent, GenerationRequest
    from models.enums import ExamType
    from repositories.subject_repo import SubjectRepository
    from repositories.paper_repo import PaperRepository

    # Use subject-scoped lesson plan text (key: lp_text:<subject_id>), fallback to global
    repo = SettingsRepository()
    lesson_plan_text = repo.get(f"lp_text:{subject_id}", "") or repo.get("lesson_plan_text", "")

    # Use the subject's own unit/chapter structure
    subj_repo = SubjectRepository()
    subject = subj_repo.get_subject_by_slug(subject_id)
    ucm: dict = {}
    if subject:
        for unit in subject.get("units", []):
            u = unit["unit_number"]
            ucm[u] = [
                {"unit_number": u, "chapter_number": ch["chapter_number"],
                 "title": ch["title"], "topics": ch.get("topics", [])}
                for ch in unit.get("chapters", [])
            ]
    else:
        # Fallback: global course structure
        course_structure = repo.get("course_structure", {})
        chapters = course_structure.get("chapters", [])
        for ch in chapters:
            u = ch.get("unit_number", 1)
            ucm.setdefault(u, []).append(ch)

    # Retrieve the already-saved marking scheme from CourseDB (no re-analysis of LP!)
    saved_schemes = {}
    if subject:
        saved_schemes = subject.get("lp_metadata", {}).get("marking_schemes") or {}

    req_exam = (body.exam_type or "").upper().strip()
    if "ISA-II" in req_exam or "ISA2" in req_exam or "MINOR 2" in req_exam or "MINOR-2" in req_exam:
        exam_key = "isa2"
        exam_subtype = "ISA-II"
        exam_type = ExamType.MINOR
    elif "END" in req_exam or "ESA" in req_exam or "SEE" in req_exam:
        exam_key = "esa"
        exam_subtype = "ESA"
        exam_type = ExamType.END_SEM
    else:
        # Default or Minor 1 / ISA-I
        exam_key = "isa1"
        exam_subtype = "ISA-I"
        exam_type = ExamType.MINOR

    # Selected saved scheme (deterministic from Course Setup)
    scheme_to_use = saved_schemes.get(exam_key) or saved_schemes.get(exam_subtype)

    request = GenerationRequest(
        exam_type=exam_type,
        subject_id=subject_id,
        num_sets=body.num_sets,
        l2_percent=body.l2_percent,
        l3_percent=body.l3_percent,
        selected_chapters=body.selected_chapters,
        random_seed=body.random_seed,
        tolerance_percent=body.tolerance_percent,
        academic_year=body.academic_year,
        department=body.department,
        lesson_plan_text=lesson_plan_text,
        unit_chapter_map=ucm,
        exclude_used_question_ids=body.exclude_used_question_ids,
        marking_scheme=scheme_to_use,
        exam_subtype=exam_subtype,
    )

    # Coordinator does not receive subject_id — it uses the repo's default.
    # After generation we patch subject_id on all generated sets.
    coordinator = CoordinatorAgent()
    result = coordinator.generate(request)

    set_ids = [s.set_id for s in result.complete_paper.sets] if result.success else []

    # Assign subject_id to the generated paper sets/questions/request
    if set_ids:
        PaperRepository().assign_subject_to_sets(set_ids, subject_id)

    reports = [_vr_out(vr) for vr in result.validation_reports]

    return GenerateResponse(
        success=result.success,
        paper_set_ids=set_ids,
        validation_reports=reports,
        errors=result.errors or [],
        warnings=result.warnings or [],
    )


@router.get("/{set_id}", response_model=PaperSetOut)
def get_paper(set_id: str, subject_id: str = Depends(get_subject_id)):
    with get_session() as session:
        row = session.query(PaperSetDB).filter(
            PaperSetDB.set_id == set_id,
            PaperSetDB.subject_id == subject_id,
        ).first()
        if not row:
            raise HTTPException(404, "Paper set not found")
        qs = session.query(PaperQuestionDB).filter(
            PaperQuestionDB.paper_set_id == set_id,
            PaperQuestionDB.subject_id == subject_id,
        ).all()
        questions = [_pq_out(q) for q in qs]
        return PaperSetOut(
            set_id=row.set_id,
            exam_type=row.exam_type,
            status=row.status,
            created_at=row.generated_at,
            question_count=len(questions),
            questions=questions,
        )


@router.patch("/{set_id}/questions/{slot_id}", response_model=PaperQuestionOut)
def update_question_status(
    set_id: str, slot_id: str, body: QuestionStatusUpdate,
    subject_id: str = Depends(get_subject_id),
):
    from repositories.paper_repo import PaperRepository
    from repositories.audit_repo import AuditRepository

    paper_repo = PaperRepository()
    audit_repo = AuditRepository()

    if body.edited_text:
        paper_repo.update_question_text(set_id, slot_id, body.edited_text, subject_id=subject_id)
        audit_repo.log("EDIT_QUESTION", subject_id=subject_id, paper_set_id=set_id,
                       details={"slot_id": slot_id})

    paper_repo.update_question_status(set_id, slot_id, body.status, subject_id=subject_id)
    audit_repo.log(f"QUESTION_{body.status}", subject_id=subject_id, paper_set_id=set_id,
                   details={"slot_id": slot_id})

    with get_session() as session:
        q = session.query(PaperQuestionDB).filter(
            PaperQuestionDB.paper_set_id == set_id,
            PaperQuestionDB.slot_id == slot_id,
            PaperQuestionDB.subject_id == subject_id,
        ).first()
        if not q:
            raise HTTPException(404, "Question slot not found")
        return _pq_out(q)


@router.post("/{set_id}/questions/{slot_id}/regenerate", response_model=PaperQuestionOut)
def regenerate_question(
    set_id: str, slot_id: str,
    subject_id: str = Depends(get_subject_id),
):
    """
    Regenerate a single question for a given paper set and slot.
    Preserves all existing slot constraints (marks, Bloom level, unit, chapter, course).
    Passes existing question text as an avoid/negative reference constraint.
    """
    from services.llm_provider import get_llm_provider
    from agents.generation_agent import AIQuestionGenerationAgent
    from models.enums import BloomLevel
    import json

    with get_session() as session:
        q = session.query(PaperQuestionDB).filter(
            PaperQuestionDB.paper_set_id == set_id,
            PaperQuestionDB.slot_id == slot_id,
            PaperQuestionDB.subject_id == subject_id,
        ).first()
        if not q:
            raise HTTPException(404, f"Question slot {slot_id} not found in paper set {set_id}")

        old_text = q.question_text
        bloom = BloomLevel.L3 if str(q.bloom_level).endswith("L3") else BloomLevel.L2

        try:
            llm = get_llm_provider()
            agent = AIQuestionGenerationAgent(llm)
            new_q = agent.regenerate_question(
                bloom_level=bloom,
                unit_number=q.unit_number,
                chapter_number=q.chapter_number,
                chapter_name=q.chapter_name or f"Chapter {q.chapter_number}",
                old_question_text=old_text,
            )

            if new_q and new_q.question_text:
                q.question_text = new_q.question_text
                q.bloom_justification = new_q.bloom_justification
                q.approval_status = "PENDING"
                if new_q.valuation_points:
                    q.valuation_points = json.dumps([vp.model_dump() for vp in new_q.valuation_points])
                session.commit()
                session.refresh(q)
                return _pq_out(q)
            else:
                raise HTTPException(500, "LLM failed to generate a replacement question.")
        except Exception as exc:
            raise HTTPException(500, f"Regeneration error: {str(exc)}")


@router.post("/{set_id}/approve")
def approve_paper(set_id: str, subject_id: str = Depends(get_subject_id)):
    from agents.coordinator import CoordinatorAgent
    from repositories.audit_repo import AuditRepository
    with get_session() as session:
        row = session.query(PaperSetDB).filter(
            PaperSetDB.set_id == set_id,
            PaperSetDB.subject_id == subject_id,
        ).first()
        if not row:
            raise HTTPException(404, "Paper set not found")
    coordinator = CoordinatorAgent()
    coordinator.approve_set(set_id, subject_id=subject_id)
    AuditRepository().log("APPROVE_SET", subject_id=subject_id, paper_set_id=set_id)
    return {"ok": True, "set_id": set_id, "status": "APPROVED"}


@router.get("/{set_id}/validation", response_model=ValidationReportOut)
def get_validation(set_id: str, subject_id: str = Depends(get_subject_id)):
    from repositories.paper_repo import PaperRepository
    from agents.validation_agent import ValidationAgent
    from agents.blueprint_agent import BlueprintAgent
    from models.enums import ExamType

    paper_repo = PaperRepository()
    paper_set = paper_repo.get_paper_set(set_id, subject_id=subject_id)
    if not paper_set:
        raise HTTPException(404, "Paper set not found")

    blueprint_agent = BlueprintAgent()
    exam_type = ExamType.MINOR if paper_set.exam_type in ("MINOR", ExamType.MINOR) else ExamType.END_SEM
    blueprint = blueprint_agent.build(exam_type)
    report = ValidationAgent().validate(paper_set, blueprint)
    return _vr_out(report)


@router.get("/{set_id}/export/docx")
def export_docx(set_id: str, subject_id: str = Depends(get_subject_id)):
    from agents.export_agent import ExportAgent
    from repositories.paper_repo import PaperRepository

    paper_set = PaperRepository().get_paper_set(set_id, subject_id=subject_id)
    if not paper_set:
        raise HTTPException(404, "Paper set not found")

    if not paper_set.all_questions_approved and paper_set.status not in ("APPROVED", "EXPORTED"):
        raise HTTPException(
            status_code=400,
            detail="Download locked: All questions must be approved by faculty before downloading the question paper.",
        )

    export_agent = ExportAgent()
    docx_bytes = export_agent.export_docx_bytes(paper_set)
    return StreamingResponse(
        io.BytesIO(docx_bytes),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{set_id}.docx"'},
    )


@router.get("/{set_id}/export/pdf")
def export_pdf(set_id: str, subject_id: str = Depends(get_subject_id)):
    from services.pdf_service import generate_paper_pdf
    from repositories.paper_repo import PaperRepository

    paper_set = PaperRepository().get_paper_set(set_id, subject_id=subject_id)
    if not paper_set:
        raise HTTPException(404, "Paper set not found")

    if not paper_set.all_questions_approved and paper_set.status not in ("APPROVED", "EXPORTED"):
        raise HTTPException(
            status_code=400,
            detail="Download locked: All questions must be approved by faculty before downloading the question paper.",
        )

    pdf_bytes = generate_paper_pdf(paper_set)
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{set_id}.pdf"'},
    )


@router.get("/{set_id}/preview")
def preview_paper(set_id: str, subject_id: str = Depends(get_subject_id)):
    """Return rendered PDF stream with inline Disposition header for in-site paper preview."""
    from services.pdf_service import generate_paper_pdf
    from repositories.paper_repo import PaperRepository

    paper_set = PaperRepository().get_paper_set(set_id, subject_id=subject_id)
    if not paper_set:
        raise HTTPException(404, "Paper set not found")

    is_draft = paper_set.status not in ("APPROVED", "EXPORTED")
    pdf_bytes = generate_paper_pdf(paper_set, is_draft=is_draft)
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="PREVIEW_{set_id}.pdf"'},
    )


# ── Helpers ────────────────────────────────────────────────────────────────────

def _pq_out(q) -> PaperQuestionOut:
    return PaperQuestionOut(
        slot_id=q.slot_id,
        question_text=q.question_text,
        bloom_level=q.bloom_level.value if hasattr(q.bloom_level, "value") else q.bloom_level,
        marks=q.marks,
        source=q.source.value if hasattr(q.source, "value") else (q.source or ""),
        chapter_number=q.chapter_number or 0,
        chapter_name=q.chapter_name or "",
        unit_number=q.unit_number or 1,
        approval_status=q.approval_status.value if hasattr(q.approval_status, "value") else (q.approval_status or "PENDING"),
        model_answer=getattr(q, "model_answer", None),
    )


def _vr_out(vr) -> ValidationReportOut:
    findings = []
    for f in (vr.findings or []):
        findings.append(ValidationFindingOut(
            rule_id=f.rule_id,
            severity=f.severity,
            message=f.message,
            expected=str(f.expected) if f.expected is not None else None,
            actual=str(f.actual) if f.actual is not None else None,
            suggested_action=f.suggested_action,
            affected_questions=f.affected_questions or [],
        ))
    status = vr.status.value if hasattr(vr.status, "value") else str(vr.status)
    exam_type = vr.exam_type.value if hasattr(vr.exam_type, "value") else str(vr.exam_type)
    return ValidationReportOut(
        set_id=vr.set_id,
        exam_type=exam_type,
        status=status,
        findings=findings,
        source_bank_count=vr.source_bank_count,
        source_ai_count=vr.source_ai_count,
        source_bank_percent=vr.source_bank_percent,
        source_ai_percent=vr.source_ai_percent,
        bloom_l2_count=vr.bloom_l2_count,
        bloom_l3_count=vr.bloom_l3_count,
        bloom_l2_percent=vr.bloom_l2_percent,
        bloom_l3_percent=vr.bloom_l3_percent,
        total_printed=vr.total_printed,
        marks_total=vr.marks_total,
    )
