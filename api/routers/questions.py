"""api/routers/questions.py — Question bank endpoints. All queries are subject-scoped."""
from __future__ import annotations
import tempfile, shutil
from pathlib import Path
from fastapi import APIRouter, HTTPException, UploadFile, File, Query, Depends
from api.schemas.questions import QuestionOut, QuestionUpdateIn, ImportResultOut, CompletenessOut
from api.deps import get_subject_id
from repositories.question_repo import QuestionRepository
from repositories.database import get_session, QuestionDB

router = APIRouter(prefix="/questions", tags=["questions"])


@router.get("", response_model=list[QuestionOut])
def list_questions(
    chapter: int | None = Query(None),
    bloom: str | None = Query(None),
    limit: int = Query(200, le=500),
    offset: int = Query(0),
    subject_id: str = Depends(get_subject_id),
):
    repo = QuestionRepository()
    questions = repo.get_all(subject_id=subject_id, chapter_number=chapter,
                             bloom_level=bloom, limit=limit, offset=offset)
    return [_q_out(q) for q in questions]


@router.get("/completeness", response_model=list[CompletenessOut])
def get_completeness(
    subject_id: str = Depends(get_subject_id),
):
    """Return completeness for each chapter in the active subject's Course Setup.

    Chapter list comes from SubjectRepository (Course Setup) for the active subject,
    so it is always subject-scoped and never hardcoded.
    Falls back to chapters that actually have questions if Course Setup has no chapters.
    """
    from repositories.subject_repo import SubjectRepository

    repo = QuestionRepository()
    counts = repo.count_by_chapter_bloom(subject_id=subject_id)

    # ── Determine chapter list from Course Setup ──────────────────────────
    subject_chapters: list[int] = []
    try:
        subj = SubjectRepository().get_subject_by_slug(subject_id)
        if subj:
            for unit in subj.get("units", []):
                for ch in unit.get("chapters", []):
                    ch_num = ch.get("chapter_number")
                    if ch_num is not None and ch_num not in subject_chapters:
                        subject_chapters.append(ch_num)
            subject_chapters.sort()
    except Exception:
        pass

    # ── Fallback: use chapters that have at least one question ────────────
    if not subject_chapters:
        seen = sorted({ch for (ch, _) in counts.keys()})
        subject_chapters = seen if seen else []

    result = []
    for ch in subject_chapters:
        l2 = counts.get((ch, "L2"), 0)
        l3 = counts.get((ch, "L3"), 0)
        result.append(CompletenessOut(
            chapter_number=ch, l2_count=l2, l3_count=l3,
            total=l2 + l3, l2_ok=l2 == 10, l3_ok=l3 == 10, complete=l2 == 10 and l3 == 10,
        ))
    return result


@router.post("/import", response_model=ImportResultOut)
def import_question_bank(
    file: UploadFile = File(...),
    subject_id: str = Depends(get_subject_id),
):
    """Upload a question bank file and import all valid questions into the active subject."""
    suffix = Path(file.filename or "bank.xlsx").suffix.lower()
    allowed = {".xlsx", ".xls", ".csv", ".json", ".docx", ".pdf"}
    if suffix not in allowed:
        raise HTTPException(400, f"Unsupported format: {suffix}")

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = Path(tmp.name)

    try:
        from services.question_bank_service import QuestionBankService
        svc = QuestionBankService()
        result = svc.load_from_file(tmp_path)
        count = 0
        if not result.has_fatal_errors:
            count = svc.import_to_database(result, subject_id=subject_id)

        return ImportResultOut(
            total_rows=result.total_rows,
            imported=count,
            warnings=result.warnings[:20],
            errors=[f"Row {e.row} [{e.field}]: {e.message}" for e in result.errors[:20]],
            chapter_completeness={k: dict(v) for k, v in result.chapter_completeness.items()},
        )
    finally:
        tmp_path.unlink(missing_ok=True)


@router.patch("/{question_id}", response_model=QuestionOut)
def update_question(
    question_id: str,
    body: QuestionUpdateIn,
    subject_id: str = Depends(get_subject_id),
):
    repo = QuestionRepository()
    q = repo.get_by_id(question_id, subject_id=subject_id)
    if not q:
        raise HTTPException(404, "Question not found")
    updates = body.model_dump(exclude_none=True)
    updated = repo.update(question_id, subject_id=subject_id, **updates)
    if not updated:
        raise HTTPException(404, "Question not found")
    return _q_out(updated)


@router.delete("/{question_id}")
def delete_question(
    question_id: str,
    subject_id: str = Depends(get_subject_id),
):
    repo = QuestionRepository()
    ok = repo.delete(question_id, subject_id=subject_id)
    if not ok:
        raise HTTPException(404, "Question not found")
    return {"ok": True}


def _q_out(q) -> QuestionOut:
    return QuestionOut(
        question_id=q.question_id,
        unit_number=q.unit_number,
        chapter_number=q.chapter_number,
        chapter_name=q.chapter_name or "",
        bloom_level=q.bloom_level.value if hasattr(q.bloom_level, "value") else q.bloom_level,
        question_text=q.question_text,
        marks=q.marks,
        model_answer=q.model_answer,
        difficulty=q.difficulty.value if hasattr(q.difficulty, "value") else (q.difficulty or "medium"),
        source=q.source.value if hasattr(q.source, "value") else (q.source or "QUESTION_BANK"),
        approval_status=q.approval_status.value if hasattr(q.approval_status, "value") else (q.approval_status or "APPROVED"),
    )
