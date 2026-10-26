"""api/routers/course.py — Course info + lesson plan endpoints.

Backward-compatible endpoints (single-course KV store) are kept as-is.
New multi-subject endpoints are added under /subjects/*.
"""
from __future__ import annotations
import json
import tempfile
import shutil
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile, File
from api.schemas.course import (
    # Legacy
    CourseInfoOut, CourseInfoIn,
    CourseStructureOut, CourseStructureIn,
    ChapterOut, LessonPlanExtractionOut,
    # Multi-subject
    SubjectIn, SubjectOut, SubjectListItem,
    ActiveSubjectResponse, SetActiveSubjectIn,
    LpParseOut, ParsedUnitSchema, ChapterIn, MinorConfigSchema,
    ExamSchemeSchema, MarkingSchemesSchema,
)
from repositories.settings_repo import SettingsRepository
from repositories.subject_repo import SubjectRepository

router = APIRouter(prefix="/course", tags=["course"])
subjects_router = APIRouter(prefix="/subjects", tags=["subjects"])


# ══════════════════════════════════════════════════════════════════════════════
# LEGACY — single-course endpoints (backward compat)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/info", response_model=CourseInfoOut)
def get_course_info():
    repo = SettingsRepository()
    data = repo.get("course_info", {})
    return CourseInfoOut(**data) if data else CourseInfoOut()


@router.post("/info", response_model=CourseInfoOut)
def save_course_info(body: CourseInfoIn):
    repo = SettingsRepository()
    repo.set("course_info", body.model_dump())
    return CourseInfoOut(**body.model_dump())


@router.get("/structure", response_model=CourseStructureOut)
def get_course_structure():
    repo = SettingsRepository()
    data = repo.get("course_structure", {})
    if not data:
        return CourseStructureOut(chapters=[])
    chapters = [ChapterOut(**ch) for ch in data.get("chapters", [])]
    return CourseStructureOut(
        chapters=chapters,
        raw_text_length=data.get("raw_text_length", 0),
    )


@router.post("/structure", response_model=CourseStructureOut)
def save_course_structure(body: CourseStructureIn):
    repo = SettingsRepository()
    repo.set("course_structure", {
        "chapters": [ch.model_dump() for ch in body.chapters],
        "raw_text_length": 0,
    })
    return CourseStructureOut(chapters=body.chapters)


@router.post("/lesson-plan/extract", response_model=LessonPlanExtractionOut)
def extract_lesson_plan(file: UploadFile = File(...)):
    """Legacy LP extract endpoint — kept for backward compat."""
    suffix = Path(file.filename or "plan.pdf").suffix.lower()
    allowed = {".pdf", ".docx", ".txt", ".md", ".xlsx"}
    if suffix not in allowed:
        raise HTTPException(400, f"Unsupported format: {suffix}")

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = Path(tmp.name)

    try:
        from agents.intake_agent import CourseIntakeAgent
        agent = CourseIntakeAgent()
        result = agent.extract_lesson_plan(tmp_path)

        repo = SettingsRepository()
        repo.set("course_structure", {
            "chapters": [
                {"unit_number": ch.unit_number, "chapter_number": ch.chapter_number,
                 "title": ch.title, "topics": ch.topics}
                for ch in result.chapters
            ],
            "raw_text_length": len(result.raw_text),
            "isa_pattern": result.isa_pattern.to_dict() if result.isa_pattern else None,
            "esa_pattern": result.esa_pattern.to_dict() if result.esa_pattern else None,
        })
        if result.raw_text:
            repo.set("lesson_plan_text", result.raw_text[:50000])

        return LessonPlanExtractionOut(
            chapters=[ChapterOut(unit_number=ch.unit_number, chapter_number=ch.chapter_number,
                                 title=ch.title, topics=ch.topics) for ch in result.chapters],
            confidence=result.confidence,
            warnings=result.warnings,
            raw_text_preview=result.raw_text[:500],
        )
    finally:
        tmp_path.unlink(missing_ok=True)


# ══════════════════════════════════════════════════════════════════════════════
# MULTI-SUBJECT endpoints  /api/subjects/*
# ══════════════════════════════════════════════════════════════════════════════

def _dict_to_subject_out(d: dict) -> SubjectOut:
    from api.schemas.course import UnitDetail, ChapterDetail
    units = [
        UnitDetail(
            id=u.get("id"),
            unit_number=u["unit_number"],
            title=u["title"],
            chapters=[
                ChapterDetail(
                    id=ch.get("id"),
                    chapter_number=ch["chapter_number"],
                    title=ch["title"],
                    topics=ch.get("topics", []),
                )
                for ch in u.get("chapters", [])
            ],
        )
        for u in d.get("units", [])
    ]
    lp_meta = d.get("lp_metadata", {})
    raw_schemes = lp_meta.get("marking_schemes")
    marking_schemes_obj = None
    if raw_schemes:
        try:
            marking_schemes_obj = MarkingSchemesSchema(**raw_schemes)
        except Exception:
            pass

    return SubjectOut(
        id=d.get("id"),
        subject_id=d["subject_id"],
        course_name=d.get("course_name", ""),
        course_code=d.get("course_code", ""),
        department=d.get("department", ""),
        semester=d.get("semester", ""),
        academic_year=d.get("academic_year", ""),
        units=units,
        minor_configuration=d.get("minor_configuration", {}),
        lp_metadata=lp_meta,
        marking_schemes=marking_schemes_obj,
        created_at=d.get("created_at"),
        updated_at=d.get("updated_at"),
    )


def _dict_to_list_item(d: dict) -> SubjectListItem:
    return SubjectListItem(
        id=d.get("id"),
        subject_id=d["subject_id"],
        course_name=d.get("course_name", ""),
        course_code=d.get("course_code", ""),
        department=d.get("department", ""),
        semester=d.get("semester", ""),
        academic_year=d.get("academic_year", ""),
        minor_configuration=d.get("minor_configuration", {}),
        lp_metadata=d.get("lp_metadata", {}),
    )


# ── Active subject ─────────────────────────────────────────────────────────────

@subjects_router.get("/active", response_model=ActiveSubjectResponse)
def get_active_subject():
    repo = SubjectRepository()
    return ActiveSubjectResponse(active_subject_id=repo.get_active_subject_id())


@subjects_router.post("/active", response_model=ActiveSubjectResponse)
def set_active_subject(body: SetActiveSubjectIn):
    repo = SubjectRepository()
    # Verify subject exists
    subject = repo.get_subject_by_slug(body.subject_id)
    if not subject:
        raise HTTPException(404, f"Subject '{body.subject_id}' not found")
    repo.set_active_subject_id(body.subject_id)
    return ActiveSubjectResponse(active_subject_id=body.subject_id)


# ── LP Parse (parse only — does NOT save) ─────────────────────────────────────

@subjects_router.post("/lp-parse", response_model=LpParseOut)
def parse_lp_for_subject(file: UploadFile = File(...)):
    """
    Upload an LP document, parse with Qwen, and return the extracted data.
    Data is NOT saved — the frontend shows it in a review/edit screen first.
    The client then calls POST /api/subjects to actually save.
    """
    suffix = Path(file.filename or "plan.pdf").suffix.lower()
    allowed = {".pdf", ".docx", ".txt", ".md", ".xlsx"}
    if suffix not in allowed:
        raise HTTPException(400, f"Unsupported file format: {suffix}. Allowed: {', '.join(allowed)}")

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = Path(tmp.name)

    try:
        from agents.intake_agent import CourseIntakeAgent
        agent = CourseIntakeAgent()
        result = agent.extract_lesson_plan(tmp_path, page_limit=60)

        # Convert UnitInfo objects to API schema
        units_out = []
        for u in result.units:
            chapters_out = [
                ChapterIn(
                    chapter_number=ch.chapter_number,
                    title=ch.title,
                    topics=ch.topics,
                )
                for ch in u.chapters
            ]
            units_out.append(ParsedUnitSchema(
                unit_number=u.unit_number,
                unit_name=u.unit_name,
                chapters=chapters_out,
            ))

        minor_cfg = MinorConfigSchema(
            minor1=result.minor_configuration.minor1,
            minor2=result.minor_configuration.minor2,
        )

        # Build marking schemes from extracted exam patterns
        def _to_scheme(pat) -> ExamSchemeSchema | None:
            if pat is None:
                return None
            return ExamSchemeSchema(
                exam_type=pat.exam_type,
                total_marks=pat.total_marks,
                duration=pat.duration,
                instructions=pat.instructions,
                total_questions=pat.total_questions,
                questions_to_attempt=pat.questions_to_attempt,
                marks_per_full_question=pat.marks_per_full_question,
                sub_question_pattern=pat.sub_question_pattern,
            )

        marking_schemes = MarkingSchemesSchema(
            isa1=_to_scheme(result.isa1_pattern),
            isa2=_to_scheme(result.isa2_pattern),
            esa=_to_scheme(result.esa_pattern),
        )
        # Only include marking_schemes if at least one was extracted
        has_schemes = any([marking_schemes.isa1, marking_schemes.isa2, marking_schemes.esa])

        return LpParseOut(
            course_name=result.course_name,
            course_code=result.course_code,
            department=result.department,
            semester=result.semester,
            academic_year=result.academic_year,
            units=units_out,
            minor_configuration=minor_cfg,
            marking_schemes=marking_schemes if has_schemes else None,
            confidence=result.confidence,
            warnings=result.warnings,
            raw_text_preview=result.raw_text[:500] if result.raw_text else "",
        )
    finally:
        tmp_path.unlink(missing_ok=True)


# ── CRUD ───────────────────────────────────────────────────────────────────────

@subjects_router.get("", response_model=list[SubjectListItem])
def list_subjects():
    repo = SubjectRepository()
    return [_dict_to_list_item(d) for d in repo.list_subjects()]


@subjects_router.post("", response_model=SubjectOut, status_code=201)
def create_subject(body: SubjectIn):
    repo = SubjectRepository()
    units_data = [
        {
            "unit_number": u.unit_number,
            "title": u.title or f"Unit {u.unit_number}",
            "chapters": [
                {
                    "chapter_number": ch.chapter_number,
                    "title": ch.title,
                    "topics": ch.topics,
                }
                for ch in u.chapters
            ],
        }
        for u in body.units
    ]
    minor_cfg = body.minor_configuration.model_dump() if body.minor_configuration else {}
    lp_meta   = body.lp_metadata.model_dump() if body.lp_metadata else {}
    # Merge marking_schemes into lp_metadata blob (no new DB column needed)
    if body.marking_schemes:
        lp_meta["marking_schemes"] = body.marking_schemes.model_dump()

    result = repo.create_subject(
        course_name=body.course_name,
        course_code=body.course_code,
        department=body.department,
        semester=body.semester,
        academic_year=body.academic_year,
        units=units_data,
        minor_configuration=minor_cfg,
        lp_metadata=lp_meta,
    )
    if not result:
        raise HTTPException(500, "Failed to create subject")

    # Auto-set as active if it's the first subject
    all_subjects = repo.list_subjects()
    if len(all_subjects) == 1:
        repo.set_active_subject_id(result["subject_id"])

    return _dict_to_subject_out(result)


@subjects_router.get("/{subject_id}", response_model=SubjectOut)
def get_subject(subject_id: str):
    repo = SubjectRepository()
    result = repo.get_subject_by_slug(subject_id)
    if not result:
        raise HTTPException(404, f"Subject '{subject_id}' not found")
    return _dict_to_subject_out(result)


@subjects_router.put("/{subject_id}", response_model=SubjectOut)
def update_subject(subject_id: str, body: SubjectIn):
    repo = SubjectRepository()
    units_data = [
        {
            "unit_number": u.unit_number,
            "title": u.title or f"Unit {u.unit_number}",
            "chapters": [
                {
                    "chapter_number": ch.chapter_number,
                    "title": ch.title,
                    "topics": ch.topics,
                }
                for ch in u.chapters
            ],
        }
        for u in body.units
    ]
    minor_cfg = body.minor_configuration.model_dump() if body.minor_configuration else {}
    lp_meta   = body.lp_metadata.model_dump() if body.lp_metadata else {}
    # Merge marking_schemes into lp_metadata blob (no new DB column needed)
    if body.marking_schemes:
        lp_meta["marking_schemes"] = body.marking_schemes.model_dump()

    result = repo.update_subject(
        subject_id=subject_id,
        course_name=body.course_name,
        course_code=body.course_code,
        department=body.department,
        semester=body.semester,
        academic_year=body.academic_year,
        units=units_data,
        minor_configuration=minor_cfg,
        lp_metadata=lp_meta,
    )
    if not result:
        raise HTTPException(404, f"Subject '{subject_id}' not found")
    return _dict_to_subject_out(result)


@subjects_router.delete("/{subject_id}")
def delete_subject(subject_id: str):
    repo = SubjectRepository()

    # Block deleting the last remaining subject
    all_subjects = repo.list_subjects()
    if len(all_subjects) <= 1:
        raise HTTPException(409, "Cannot delete the last remaining subject")

    ok = repo.delete_subject(subject_id)
    if not ok:
        raise HTTPException(404, f"Subject '{subject_id}' not found")

    # Cascade: delete questions, papers, audit logs, uploaded files, context
    from repositories.question_repo import QuestionRepository
    from repositories.paper_repo import PaperRepository
    from repositories.database import AuditLogDB, UploadedFileDB, SubjectContextDB, get_session as _gs

    QuestionRepository().delete_by_subject(subject_id)
    PaperRepository().delete_by_subject(subject_id)

    with _gs() as session:
        session.query(AuditLogDB).filter(AuditLogDB.subject_id == subject_id).delete()
        session.query(UploadedFileDB).filter(UploadedFileDB.subject_id == subject_id).delete()
        session.query(SubjectContextDB).filter(SubjectContextDB.subject_id == subject_id).delete()

    try:
        from services.file_service import FileService
        FileService().delete_subject_files(subject_id)
    except Exception:
        pass

    # If deleted subject was active, assign a new active subject
    active = repo.get_active_subject_id()
    if active == subject_id:
        remaining = repo.list_subjects()
        new_active = remaining[0]["subject_id"] if remaining else None
        if new_active:
            repo.set_active_subject_id(new_active)
        else:
            from repositories.database import AppSettingDB, get_session
            with get_session() as session:
                row = session.query(AppSettingDB).filter(
                    AppSettingDB.key == "active_subject_id"
                ).first()
                if row:
                    session.delete(row)

    return {"ok": True, "deleted": subject_id}

# ── Subject Context endpoints ──────────────────────────────────────────────────

@subjects_router.get("/{subject_id}/context")
def get_subject_context(subject_id: str):
    """Return the stored UI context blob for subject_id."""
    repo = SubjectRepository()
    if not repo.get_subject_by_slug(subject_id):
        raise HTTPException(404, f"Subject '{subject_id}' not found")
    from repositories.context_repo import SubjectContextRepository
    return SubjectContextRepository().get(subject_id)


@subjects_router.put("/{subject_id}/context")
def put_subject_context(subject_id: str, body: dict):
    """Persist the UI context blob for subject_id (JSON, max 64 KB)."""
    repo = SubjectRepository()
    if not repo.get_subject_by_slug(subject_id):
        raise HTTPException(404, f"Subject '{subject_id}' not found")
    from repositories.context_repo import SubjectContextRepository
    try:
        SubjectContextRepository().put(subject_id, body)
    except ValueError as exc:
        raise HTTPException(413, str(exc))
    return {"ok": True}
