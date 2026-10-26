"""
services/question_bank_service.py
Parses, normalizes and validates question-bank uploads.
Supports: Excel (.xlsx/.xls), CSV, JSON, DOCX, PDF.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from models.enums import BloomLevel, ApprovalStatus, QuestionSource, QuestionType, DifficultyLevel
from models.question import Question
from models.valuation import ValuationPoint
from repositories.question_repo import QuestionRepository

logger = logging.getLogger(__name__)

# Column name aliases for normalized field mapping
COLUMN_ALIASES: dict[str, list[str]] = {
    "question_id": ["question_id", "id", "qid", "q_id"],
    "unit_number": ["unit_number", "unit", "unit_no"],
    "chapter_number": ["chapter_number", "chapter", "ch_no", "chapter_no"],
    "chapter_name": ["chapter_name", "chapter_title"],
    "question_text": ["question_text", "question", "text", "q_text"],
    "bloom_level": ["bloom_level", "bloom", "cognitive_level", "level"],
    "marks": ["marks", "mark"],
    "model_answer": ["model_answer", "answer", "key_answer"],
    "difficulty": ["difficulty", "difficulty_level"],
}


@dataclass
class ValidationError:
    row: int
    field: str
    message: str
    is_fatal: bool = True


@dataclass
class ImportResult:
    total_rows: int = 0
    imported: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[ValidationError] = field(default_factory=list)
    chapter_completeness: dict[int, dict[str, int]] = field(default_factory=dict)
    questions: list[Question] = field(default_factory=list)

    @property
    def has_fatal_errors(self) -> bool:
        return any(e.is_fatal for e in self.errors)

    @property
    def error_summary(self) -> str:
        lines = []
        for e in self.errors:
            lines.append(f"Row {e.row}: [{e.field}] {e.message}")
        return "\n".join(lines)


class QuestionBankService:
    """
    Parses question-bank files (Excel, CSV, JSON, DOCX)
    and validates against the schema rules.
    """

    def __init__(self):
        self._repo = QuestionRepository()

    # ── Public API ────────────────────────────────────────────────────────────

    def load_from_file(self, path: Path) -> ImportResult:
        """
        Load and validate a question bank from file.
        Does NOT write to the database — caller decides.
        Supported: .xlsx, .xls, .csv, .json, .docx, .pdf
        """
        suffix = path.suffix.lower()
        if suffix in {".xlsx", ".xls"}:
            df = self._read_excel(path)
        elif suffix == ".csv":
            df = pd.read_csv(path, dtype=str)
        elif suffix == ".json":
            df = self._read_json(path)
        elif suffix == ".docx":
            df = self._read_docx(path)
        elif suffix == ".pdf":
            df = self._read_pdf(path)
        else:
            raise ValueError(f"Unsupported format: {suffix}")

        return self._validate_dataframe(df, path.name)

    def import_to_database(self, result: ImportResult, subject_id: str) -> int:
        """
        Persist validated questions to database for the given subject.
        Only imports questions without fatal errors.
        """
        if not result.questions:
            return 0
        return self._repo.bulk_upsert(result.questions, subject_id=subject_id)

    def chapter_completeness_report(
        self, expected_chapters: int = 7, subject_id: str = "default-subject"
    ) -> dict[int, dict[str, Any]]:
        """
        Return per-chapter counts of L2 and L3 questions.
        Flags chapters with fewer or more than 10 questions per level.
        """
        counts = self._repo.count_by_chapter_bloom(subject_id=subject_id)
        report: dict[int, dict[str, Any]] = {}
        for ch in range(1, expected_chapters + 1):
            l2 = counts.get((ch, "L2"), 0)
            l3 = counts.get((ch, "L3"), 0)
            report[ch] = {
                "l2_count": l2,
                "l3_count": l3,
                "total": l2 + l3,
                "l2_ok": l2 == 10,
                "l3_ok": l3 == 10,
                "complete": l2 == 10 and l3 == 10,
            }
        return report

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _read_excel(self, path: Path) -> pd.DataFrame:
        return pd.read_excel(path, dtype=str)

    def _read_json(self, path: Path) -> pd.DataFrame:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return pd.DataFrame(data)
        if isinstance(data, dict) and "questions" in data:
            return pd.DataFrame(data["questions"])
        raise ValueError("JSON must be a list of question objects or {questions: [...]}")

    def _read_docx(self, path: Path) -> pd.DataFrame:
        """Extract questions from a Word document (table or paragraph-based)."""
        try:
            from docx import Document
        except ImportError:
            raise RuntimeError("python-docx required for DOCX parsing")

        doc = Document(str(path))
        rows: list[dict] = []

        # Try tables first
        for table in doc.tables:
            if not table.rows:
                continue
            # Detect header row
            header = [c.text.strip().lower() for c in table.rows[0].cells]
            has_cols = any(h in ("question_text", "question", "text", "q_text") for h in header)
            if has_cols:
                for row in table.rows[1:]:
                    record = {header[i]: cell.text.strip() for i, cell in enumerate(row.cells)}
                    rows.append(record)

        if rows:
            return pd.DataFrame(rows)

        # Fallback: paragraph extraction (same as PDF text mode)
        full_text = "\n".join(p.text for p in doc.paragraphs)
        return self._parse_freeform_text(full_text)

    def _read_pdf(self, path: Path) -> pd.DataFrame:
        """
        Extract questions from a PDF.
        Strategy 1: pdfplumber table extraction (handles tabular/Excel-like PDFs).
        Strategy 2: LLM structured extraction (handles freeform paragraph PDFs).
        """
        try:
            import pdfplumber
        except ImportError:
            raise RuntimeError(
                "pdfplumber is required for PDF upload. Run: pip install pdfplumber"
            )

        # Chapter → Unit mapping for this question bank
        # (matches the course structure: Unit 1=Ch1-2, Unit 2=Ch3-5, Unit 3=Ch6-7)
        _CHAPTER_UNIT = {1: 1, 2: 1, 3: 2, 4: 2, 5: 2, 6: 3, 7: 3}
        _CH_HEADER = re.compile(r"Chapter\s+(\d+)\s*:\s*(.+)", re.IGNORECASE)

        rows: list[dict] = []
        full_text_parts: list[str] = []

        with pdfplumber.open(str(path)) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text() or ""
                full_text_parts.append(page_text)

                # ── Extract chapter number + name from page header ─────────
                ch_num, ch_name, unit_num = None, "", 1
                for line in page_text.split("\n"):
                    m = _CH_HEADER.search(line.strip())
                    if m:
                        ch_num = int(m.group(1))
                        # Join with next line if name seems truncated (< 10 chars)
                        ch_name = m.group(2).strip()
                        unit_num = _CHAPTER_UNIT.get(ch_num, 1)
                        break

                if ch_num is None:
                    # Try to guess chapter from page index
                    ch_num = len(rows) // 20 + 1  # rough fallback
                    unit_num = _CHAPTER_UNIT.get(ch_num, 1)
                    ch_name = f"Chapter {ch_num}"

                # ── Extract tables — each table is one bloom level ─────────
                for table in (page.extract_tables() or []):
                    if not table or len(table) < 2:
                        continue

                    # Detect bloom level from column header
                    header_cells = [str(c or "").strip() for c in table[0]]
                    header_joined = " ".join(header_cells).lower()

                    if "l2" in header_joined or "understanding" in header_joined:
                        bloom = "L2"
                    elif "l3" in header_joined or "apply" in header_joined or "analyz" in header_joined:
                        bloom = "L3"
                    else:
                        # Skip non-question tables (e.g. title tables)
                        continue

                    # Find the column index that holds question text
                    # (the column whose header contains "questions" or "level")
                    q_col_idx = 1  # default: second column
                    for ci, hdr in enumerate(header_cells):
                        if "question" in hdr.lower() or "level" in hdr.lower():
                            q_col_idx = ci

                    # Extract each data row
                    for row_idx, data_row in enumerate(table[1:], start=1):
                        if not data_row or all(not c for c in data_row):
                            continue
                        q_text = str(data_row[q_col_idx] or "").strip()
                        # Multi-line cells: pdfplumber joins with \n
                        q_text = " ".join(q_text.split())
                        if len(q_text) < 10:
                            continue

                        rows.append({
                            "question_id": f"CH{ch_num:02d}-{bloom}-{row_idx:02d}",
                            "unit_number": str(unit_num),
                            "chapter_number": str(ch_num),
                            "chapter_name": ch_name,
                            "bloom_level": bloom,
                            "question_text": q_text,
                            "marks": "10",
                            "model_answer": "",
                        })

        if rows:
            logger.info(
                f"[QBService] Page-by-page structured extraction: "
                f"{len(rows)} questions from {path.name}"
            )
            return pd.DataFrame(rows)

        # ── Fallback: LLM extraction ───────────────────────────────────────
        full_text = "\n".join(full_text_parts)
        logger.info(f"Structured extraction found nothing — using LLM extraction for {path.name}")
        return self._extract_questions_with_llm(full_text, path.name)



    def _extract_questions_with_llm(self, text: str, source_name: str) -> pd.DataFrame:
        """
        Use LLM with structured output to extract all questions from freeform text.
        Sends semantically relevant chunks ranked by sentence-transformer embeddings.
        """
        from pydantic import BaseModel, Field

        class _QuestionItem(BaseModel):
            question_id: str = Field(description="Unique ID e.g. CH01-L2-01")
            unit_number: int = Field(description="Unit number 1-3")
            chapter_number: int = Field(description="Chapter number 1-7")
            chapter_name: str = Field(description="Chapter title")
            bloom_level: str = Field(description="L2 or L3")
            question_text: str = Field(description="Full question text")
            marks: int = Field(default=10)

        class _QuestionBankSchema(BaseModel):
            questions: list[_QuestionItem] = Field(
                description="All questions extracted from the document"
            )

        # Select relevant chunks via embeddings
        relevant_text = self._select_relevant_chunks(text, max_chars=8000)

        try:
            from services.llm_provider import get_llm_provider
            llm = get_llm_provider()

            system_prompt = (
                "You are an expert academic document parser specializing in question banks. "
                "Extract EVERY question from the document. "
                "Identify each question's: chapter number, chapter name, unit number, "
                "Bloom's taxonomy level (L2=Understanding, L3=Applying/Analyzing), "
                "and full question text. "
                "Do NOT paraphrase — copy the exact question text. "
                "Assign globally unique question_id values like CH01-L2-01, CH01-L2-02, CH01-L3-01, etc."
            )

            user_prompt = (
                "Extract ALL questions from this question bank document. "
                "The document has chapters (Chapter 1 through 7) each with L2 and L3 level questions "
                "numbered 1-10. Extract every single question.\n\n"
                "DOCUMENT:\n---\n"
                f"{relevant_text}\n"
                "---\n\n"
                "Return ALL questions found. A complete bank has 140 questions (10 L2 + 10 L3 × 7 chapters)."
            )

            # Split into chunks if text is large — process in batches
            chunk_size = 6000
            all_questions: list[_QuestionItem] = []

            # Process in overlapping chunks to avoid missing questions at chunk boundaries
            text_chunks = [text[i:i+chunk_size] for i in range(0, len(text), chunk_size - 500)]

            for chunk_idx, chunk in enumerate(text_chunks):
                chunk_prompt = (
                    f"Extract ALL questions from this SECTION ({chunk_idx+1}/{len(text_chunks)}) "
                    "of the question bank. Copy exact question text.\n\n"
                    "DOCUMENT SECTION:\n---\n"
                    f"{chunk}\n"
                    "---"
                )
                try:
                    response: _QuestionBankSchema = llm.generate_structured(
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": chunk_prompt},
                        ],
                        response_model=_QuestionBankSchema,
                        temperature=0.1,
                    )
                    all_questions.extend(response.questions)
                    logger.info(
                        f"[QBService] Chunk {chunk_idx+1}/{len(text_chunks)}: "
                        f"{len(response.questions)} questions extracted"
                    )
                except Exception as exc:
                    logger.warning(f"[QBService] Chunk {chunk_idx+1} extraction failed: {exc}")

            if not all_questions:
                raise ValueError("LLM returned no questions")

            # Deduplicate by question_text (strip whitespace, lowercase)
            seen_texts: set[str] = set()
            unique_questions = []
            for q in all_questions:
                key = " ".join(q.question_text.lower().split())[:120]
                if key not in seen_texts:
                    seen_texts.add(key)
                    unique_questions.append(q)

            logger.info(
                f"[QBService] LLM extracted {len(unique_questions)} unique questions "
                f"from {source_name}"
            )

            return pd.DataFrame([
                {
                    "question_id": q.question_id,
                    "unit_number": str(q.unit_number),
                    "chapter_number": str(q.chapter_number),
                    "chapter_name": q.chapter_name,
                    "bloom_level": q.bloom_level,
                    "question_text": q.question_text,
                    "marks": str(q.marks),
                    "model_answer": "",
                }
                for q in unique_questions
            ])

        except Exception as exc:
            logger.error(f"[QBService] LLM extraction failed: {exc}")
            raise ValueError(
                f"Could not extract questions from PDF using AI: {exc}. "
                "Try uploading as Excel (.xlsx) or CSV format instead."
            )

    def _select_relevant_chunks(self, text: str, max_chars: int = 8000) -> str:
        """
        Rank paragraphs by semantic similarity to 'question bank' query using embeddings.
        Falls back to returning the full text if sentence-transformers is unavailable.
        """
        import re
        paragraphs = [p.strip() for p in re.split(r"\n{2,}", text) if len(p.strip()) > 20]
        if not paragraphs:
            return text[:max_chars]

        try:
            from sentence_transformers import SentenceTransformer
            import numpy as np

            query = "question bloom level L2 L3 chapter unit marks"
            model = SentenceTransformer("all-MiniLM-L6-v2")
            query_emb = model.encode([query], normalize_embeddings=True)[0]
            para_embs = model.encode(paragraphs, normalize_embeddings=True, batch_size=32)
            scores = para_embs @ query_emb
            ranked = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)
            selected_idx = sorted({idx for idx, _ in ranked[:30]} | set(range(min(5, len(paragraphs)))))
            result = "\n\n".join(paragraphs[i] for i in selected_idx)
            return result[:max_chars]
        except Exception:
            return text[:max_chars]

    def _normalize_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        """Rename dataframe columns using alias mapping."""
        col_map = {}
        existing = {c.lower().strip(): c for c in df.columns}
        for canonical, aliases in COLUMN_ALIASES.items():
            for alias in aliases:
                if alias.lower() in existing:
                    col_map[existing[alias.lower()]] = canonical
                    break
        return df.rename(columns=col_map)

    def _validate_dataframe(self, df: pd.DataFrame, source_name: str) -> ImportResult:
        result = ImportResult(total_rows=len(df))
        df = self._normalize_columns(df)
        df = df.fillna("")

        seen_ids: set[str] = set()
        seen_texts: set[str] = set()
        questions: list[Question] = []

        for idx, row in df.iterrows():
            row_num = int(idx) + 2  # 1-indexed, +1 for header
            q_errors: list[ValidationError] = []

            # question_id
            q_id = str(row.get("question_id", "")).strip()
            if not q_id:
                q_errors.append(ValidationError(row_num, "question_id", "Missing question ID", True))
                q_id = f"AUTO-ROW{row_num}"
            elif q_id in seen_ids:
                q_errors.append(ValidationError(row_num, "question_id", f"Duplicate ID: {q_id}", True))
            seen_ids.add(q_id)

            # question_text
            q_text = str(row.get("question_text", "")).strip()
            if not q_text:
                q_errors.append(ValidationError(row_num, "question_text", "Empty question text", True))
            else:
                normalized = re.sub(r"\s+", " ", q_text.lower())
                if normalized in seen_texts:
                    q_errors.append(ValidationError(row_num, "question_text",
                                                     "Exact duplicate question text detected", True))
                seen_texts.add(normalized)

            # unit_number
            try:
                unit_num = int(float(str(row.get("unit_number", "")).strip()))
                if unit_num not in (1, 2, 3):
                    q_errors.append(ValidationError(row_num, "unit_number",
                                                     f"Unit must be 1-3, got {unit_num}", True))
            except (ValueError, TypeError):
                q_errors.append(ValidationError(row_num, "unit_number", "Missing or invalid unit number", True))
                unit_num = 1

            # chapter_number
            try:
                ch_num = int(float(str(row.get("chapter_number", "")).strip()))
            except (ValueError, TypeError):
                q_errors.append(ValidationError(row_num, "chapter_number", "Missing or invalid chapter number", True))
                ch_num = 1

            # chapter_name
            ch_name = str(row.get("chapter_name", "")).strip()
            if not ch_name:
                q_errors.append(ValidationError(row_num, "chapter_name",
                                                 "Missing chapter name", False))
                ch_name = f"Chapter {ch_num}"

            # bloom_level
            bloom_raw = str(row.get("bloom_level", "")).strip().upper()
            bloom_raw = bloom_raw.replace("LEVEL", "").replace(" ", "")
            if bloom_raw not in ("L2", "L3"):
                q_errors.append(ValidationError(
                    row_num, "bloom_level",
                    f"Invalid Bloom level '{bloom_raw}'. Only L2/L3 allowed.", True
                ))
                bloom_level = BloomLevel.L2
            else:
                bloom_level = BloomLevel(bloom_raw)

            # marks
            try:
                marks = int(float(str(row.get("marks", "10")).strip()))
                if marks != 10:
                    q_errors.append(ValidationError(row_num, "marks",
                                                     f"Marks must be 10, got {marks}", True))
            except (ValueError, TypeError):
                marks = 10

            # model_answer (warning only)
            model_answer_raw = str(row.get("model_answer", "")).strip()
            if not model_answer_raw:
                result.warnings.append(
                    f"Row {row_num}: No model answer provided for {q_id} (non-fatal)"
                )
                model_answer_raw = None

            # difficulty
            diff_raw = str(row.get("difficulty", "medium")).strip().lower()
            try:
                difficulty = DifficultyLevel(diff_raw)
            except ValueError:
                difficulty = DifficultyLevel.MEDIUM

            # Accumulate errors
            result.errors.extend(q_errors)
            fatal_in_row = any(e.is_fatal for e in q_errors)

            if not fatal_in_row and q_text:
                try:
                    q = Question(
                        question_id=q_id,
                        unit_number=unit_num,
                        chapter_number=ch_num,
                        chapter_name=ch_name,
                        question_text=q_text,
                        bloom_level=bloom_level,
                        marks=10,
                        difficulty=difficulty,
                        model_answer=model_answer_raw if model_answer_raw else None,
                        source=QuestionSource.QUESTION_BANK,
                        approval_status=ApprovalStatus.APPROVED,
                    )
                    questions.append(q)
                    result.imported += 1
                except Exception as exc:
                    result.errors.append(
                        ValidationError(row_num, "schema", str(exc), True)
                    )

        result.questions = questions
        result.chapter_completeness = self._build_completeness(questions)
        return result

    def _build_completeness(
        self, questions: list[Question]
    ) -> dict[int, dict[str, int]]:
        """Count L2/L3 per chapter."""
        comp: dict[int, dict[str, int]] = {}
        for q in questions:
            ch = q.chapter_number
            bl = q.bloom_level.value if hasattr(q.bloom_level, "value") else q.bloom_level
            if ch not in comp:
                comp[ch] = {"L2": 0, "L3": 0}
            comp[ch][bl] = comp[ch].get(bl, 0) + 1
        return comp
