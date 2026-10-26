"""
agents/coordinator.py
Coordinator Agent — orchestrates the full paper generation workflow.
Maintains state, runs nodes in order, enforces approval gates.
"""

from __future__ import annotations

import logging
import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from models.blueprint import MinorBlueprint, EndSemBlueprint
from models.enums import ExamType, PaperSetStatus
from models.paper import CompletePaper, PaperSet
from models.validation import ValidationReport

from agents.blueprint_agent import BlueprintAgent
from agents.selection_agent import QuestionSelectionAgent
from agents.generation_agent import AIQuestionGenerationAgent
from agents.similarity_agent import SimilarityDuplicateAgent
from agents.composition_agent import PaperCompositionAgent
from agents.valuation_agent import ValuationAgent
from agents.validation_agent import ValidationAgent
from agents.export_agent import ExportAgent, ExportError
from repositories.audit_repo import AuditRepository
from repositories.paper_repo import PaperRepository
from services.llm_provider import LLMProviderProtocol, get_llm_provider
from config.settings import get_settings

logger = logging.getLogger(__name__)

SET_LABELS = ["SET-A", "SET-B", "SET-C", "SET-D", "SET-E"]


@dataclass
class GenerationRequest:
    """Input parameters for a complete paper generation run."""
    exam_type: ExamType
    subject_id: str = "default-subject"
    num_sets: int = 1
    l2_percent: int = 50
    l3_percent: int = 50
    selected_chapters: list[int] = field(default_factory=list)
    unit_allocations: Optional[list[dict]] = None
    random_seed: Optional[int] = None
    tolerance_percent: int = 5
    academic_year: str = ""
    course_name: str = "Generative AI"
    department: str = ""
    lesson_plan_text: str = ""
    unit_chapter_map: dict = field(default_factory=dict)
    exclude_used_question_ids: bool = True
    marking_scheme: Optional[dict] = None
    exam_subtype: Optional[str] = None


@dataclass
class GenerationResult:
    """Output of a complete generation run."""
    success: bool
    request_id: str = ""
    complete_paper: Optional[CompletePaper] = None
    blueprints: list = field(default_factory=list)
    validation_reports: list[ValidationReport] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    audit_trail: list[dict] = field(default_factory=list)


class CoordinatorAgent:
    """
    Agent node 9.1: Coordinator Agent.
    Orchestrates all other agents in a defined order.
    Faculty approval is a hard gate before export.
    """

    def __init__(self, llm_provider: Optional[LLMProviderProtocol] = None):
        if llm_provider is None:
            try:
                llm_provider = get_llm_provider()
            except Exception as exc:
                logger.warning(f"LLM provider unavailable: {exc}")
                from services.llm_provider import MockLLMProvider
                llm_provider = MockLLMProvider()

        self._llm = llm_provider
        self._blueprint_agent = BlueprintAgent()
        self._selection_agent = QuestionSelectionAgent()
        self._generation_agent = AIQuestionGenerationAgent(llm_provider)
        self._similarity_agent = SimilarityDuplicateAgent()
        self._composition_agent = PaperCompositionAgent()
        self._valuation_agent = ValuationAgent(llm_provider)
        self._validation_agent = ValidationAgent()
        self._export_agent = ExportAgent()
        self._audit_repo = AuditRepository()
        self._paper_repo = PaperRepository()
        self._settings = get_settings()

    def generate(self, request: GenerationRequest) -> GenerationResult:
        """
        Full paper generation pipeline.
        Returns GenerationResult with all sets and validation reports.
        """
        request_id = str(uuid.uuid4())
        result = GenerationResult(success=False, request_id=request_id)
        rng = random.Random(request.random_seed)

        self._audit("GENERATION_START", request_id=request_id, subject_id=request.subject_id, details={
            "exam_type": request.exam_type.value if hasattr(request.exam_type, "value") else request.exam_type,
            "num_sets": request.num_sets,
            "l2_percent": request.l2_percent,
        })

        # ── Step 1: Build blueprint ────────────────────────────────────────────
        try:
            blueprint = self._blueprint_agent.build(
                exam_type=request.exam_type,
                l2_percent=request.l2_percent,
                l3_percent=request.l3_percent,
                selected_chapters=request.selected_chapters,
                unit_allocations=request.unit_allocations,
                random_seed=request.random_seed,
                tolerance_percent=request.tolerance_percent,
                marking_scheme=request.marking_scheme,
                exam_subtype=request.exam_subtype,
            )
            result.blueprints.append(blueprint)
            self._audit("BLUEPRINT_BUILT", request_id=request_id, subject_id=request.subject_id, details={"type": str(type(blueprint).__name__)})
        except Exception as exc:
            result.errors.append(f"Blueprint error: {exc}")
            self._audit("BLUEPRINT_FAILED", request_id=request_id, subject_id=request.subject_id, details={"error": str(exc)})
            return result

        # ── Step 2: Get used question IDs for exclusion ────────────────────────
        used_globally: set[str] = set()
        if request.exclude_used_question_ids:
            used_globally = self._paper_repo.get_used_question_ids(subject_id=request.subject_id)

        # Track IDs used across sets in this run
        used_in_run: set[str] = set()
        generated_sets: list[PaperSet] = []
        generated_texts_all: list[str] = []

        # ── Step 3: Build each set ─────────────────────────────────────────────
        is_minor = request.exam_type in (ExamType.MINOR, "MINOR")

        # Generate a short timestamp suffix so each run produces unique IDs
        run_ts = datetime.utcnow().strftime("%H%M%S")
        if request.exam_subtype:
            exam_prefix = request.exam_subtype.replace("-", "").upper()
        else:
            exam_prefix = "MINOR" if is_minor else "ENDSEM"

        for set_idx in range(request.num_sets):
            label = SET_LABELS[set_idx] if set_idx < len(SET_LABELS) else chr(65 + set_idx)
            set_id = f"{exam_prefix}-{label}-{run_ts}"
            logger.info(f"Generating {set_id} ({set_idx+1}/{request.num_sets})")

            exclude_ids = list(used_globally | used_in_run)

            # ── Step 3a: Select bank questions ────────────────────────────────
            if is_minor:
                sel_result = self._selection_agent.select_for_minor(
                    blueprint, used_globally | used_in_run, rng, subject_id=request.subject_id
                )
            else:
                sel_result = self._selection_agent.select_for_end_sem(
                    blueprint, used_globally | used_in_run, rng, subject_id=request.subject_id
                )

            if not sel_result.success:
                result.errors.append(
                    f"{set_id}: Question selection failed — {sel_result.failure_reason}"
                )
                result.warnings.append(sel_result.feasibility_report)
                continue

            bank_questions = list(sel_result.selected.values())
            # Ensure subject_id isolation for bank questions
            bank_questions = [
                q for q in bank_questions
                if getattr(q, 'subject_id', request.subject_id) == request.subject_id
            ]
            for bq in bank_questions:
                used_in_run.add(bq.question_id)

            # ── Step 3b: Generate AI questions ────────────────────────────────
            bank_sample_texts = [q.question_text for q in bank_questions[:5]]
            used_texts_for_gen = generated_texts_all + [q.question_text for q in bank_questions]

            ai_questions = []
            if is_minor:
                ai_questions = self._generation_agent.generate_for_minor(
                    blueprint_bloom={"l2": blueprint.ai_l2, "l3": blueprint.ai_l3},
                    selected_chapters=request.selected_chapters or list(range(1, 8)),
                    lesson_plan_text=request.lesson_plan_text,
                    question_bank_sample=bank_sample_texts,
                    used_texts=used_texts_for_gen,
                    unit_chapter_map=request.unit_chapter_map,
                    subject_name=request.course_name,
                )
            else:
                # Build per-unit AI slots
                ai_slots = self._build_endsem_ai_slots(blueprint)
                ai_questions = self._generation_agent.generate_for_end_sem(
                    unit_ai_bloom=ai_slots,
                    lesson_plan_text=request.lesson_plan_text,
                    question_bank_sample=bank_sample_texts,
                    used_texts=used_texts_for_gen,
                    unit_chapter_map=request.unit_chapter_map,
                    subject_name=request.course_name,
                )

            generated_texts_all.extend(q.question_text for q in ai_questions)

            # ── Step 3c: Compose paper ────────────────────────────────────────
            if is_minor:
                paper_set = self._composition_agent.compose_minor(
                    set_id=set_id,
                    set_index=set_idx,
                    blueprint=blueprint,
                    bank_questions=bank_questions,
                    ai_questions=ai_questions,
                )
            else:
                bank_by_unit = self._group_by_unit(bank_questions)
                ai_by_unit = self._group_generated_by_unit(ai_questions)
                paper_set = self._composition_agent.compose_end_sem(
                    set_id=set_id,
                    set_index=set_idx,
                    blueprint=blueprint,
                    bank_questions_by_unit=bank_by_unit,
                    ai_questions_by_unit=ai_by_unit,
                )

            # ── Step 3d: Generate valuation schemes ───────────────────────────
            for pq in paper_set.questions:
                try:
                    scheme = self._valuation_agent.generate_scheme(pq, subject_id=request.subject_id)
                    pq.valuation_scheme = scheme
                except Exception as exc:
                    result.warnings.append(
                        f"{set_id}/{pq.slot_id}: Valuation failed — {exc}"
                    )

            # ── Step 3e: Validate ─────────────────────────────────────────────
            val_report = self._validation_agent.validate(paper_set, blueprint)
            paper_set.validation_status = val_report.status.value if hasattr(val_report.status, "value") else val_report.status
            paper_set.validation_findings = [f.model_dump() for f in val_report.findings]
            result.validation_reports.append(val_report)

            generated_sets.append(paper_set)
            self._audit("SET_GENERATED", request_id=request_id, paper_set_id=set_id, subject_id=request.subject_id,
                        details={"validation": val_report.status.value if hasattr(val_report.status, "value") else val_report.status})

        if not generated_sets:
            result.errors.append("No paper sets were generated successfully")
            return result

        # ── Step 4: Cross-set duplicate check ─────────────────────────────────
        if len(generated_sets) > 1:
            set_text_pairs = [
                (ps.set_id, [q.question_text for q in ps.questions])
                for ps in generated_sets
            ]
            cross_dups = self._similarity_agent.check_cross_set(set_text_pairs)
            blocking = [d for d in cross_dups if d.get("status") == "DUPLICATE"]
            if blocking:
                result.warnings.append(
                    f"Cross-set duplicate issues: {len(blocking)} blocking duplicates detected"
                )

        # ── Step 5: Assemble CompletePaper ────────────────────────────────────
        complete_paper = CompletePaper(
            request_id=request_id,
            exam_type=request.exam_type,
            course_name=request.course_name,
            academic_year=request.academic_year,
            sets=generated_sets,
            random_seed=request.random_seed,
        )

        # ── Step 6: Persist to database ───────────────────────────────────────
        try:
            self._paper_repo.save_request(
                paper=complete_paper,
                num_sets=request.num_sets,
                l2_pct=request.l2_percent,
                l3_pct=request.l3_percent,
                selected_chapters=request.selected_chapters,
                model_provider=self._settings.llm_provider,
                model_name=self._settings.resolved_llm_model,
                subject_id=request.subject_id,
            )
            for ps in generated_sets:
                self._paper_repo.save_set(ps, request_id, subject_id=request.subject_id)
        except Exception as exc:
            result.warnings.append(f"Database persistence warning: {exc}")

        result.success = True
        result.complete_paper = complete_paper
        self._audit("GENERATION_COMPLETE", request_id=request_id, subject_id=request.subject_id,
                    details={"sets_generated": len(generated_sets)})
        return result

    def approve_set(self, set_id: str, subject_id: Optional[str] = None) -> None:
        """Mark a paper set and all associated questions as APPROVED. Records approval in audit log."""
        self._paper_repo.approve_paper_set_and_questions(set_id, subject_id=subject_id)
        self._audit_repo.log(
            action="SET_APPROVED",
            subject_id=subject_id or "default-subject",
            paper_set_id=set_id,
            details={"approved_at": datetime.utcnow().isoformat()},
        )
        logger.info(f"Paper set {set_id} and all questions APPROVED")

    def export_approved(
        self,
        paper_set: PaperSet,
        validation_report: ValidationReport,
        academic_year: str = "",
        department: str = "",
        template_path: Optional[Path] = None,
    ) -> Path:
        """Export an approved paper. Raises ExportError if not approved."""
        return self._export_agent.export_paper(
            paper_set=paper_set,
            validation_report=validation_report,
            template_path=template_path,
            academic_year=academic_year,
            department=department,
            is_draft=False,
        )

    def export_draft(
        self,
        paper_set: PaperSet,
        validation_report: ValidationReport,
        academic_year: str = "",
        department: str = "",
        template_path: Optional[Path] = None,
    ) -> Path:
        """Export a draft preview with DRAFT watermark."""
        return self._export_agent.export_paper(
            paper_set=paper_set,
            validation_report=validation_report,
            template_path=template_path,
            academic_year=academic_year,
            department=department,
            is_draft=True,
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _group_by_unit(self, questions) -> dict:
        groups: dict[int, list] = {}
        for q in questions:
            groups.setdefault(q.unit_number, []).append(q)
        return groups

    def _group_generated_by_unit(self, questions) -> dict:
        groups: dict[int, list] = {}
        for q in questions:
            groups.setdefault(q.unit_number, []).append(q)
        return groups

    def _build_endsem_ai_slots(self, blueprint: EndSemBlueprint) -> list[dict]:
        """Build AI generation slot specs for End-Sem."""
        slots = []
        for ua in blueprint.unit_allocations:
            u = ua.unit_number
            ai_count = ua.source.ai_count
            ai_l2 = ai_count // 2
            ai_l3 = ai_count - ai_l2
            for i in range(ai_l2):
                slots.append({
                    "unit_number": u,
                    "chapter_number": ua.chapter_numbers[0] if ua.chapter_numbers else 1,
                    "chapter_name": f"Unit {u} Chapter",
                    "bloom_level": "L2",
                })
            for i in range(ai_l3):
                slots.append({
                    "unit_number": u,
                    "chapter_number": ua.chapter_numbers[0] if ua.chapter_numbers else 1,
                    "chapter_name": f"Unit {u} Chapter",
                    "bloom_level": "L3",
                })
        return slots

    def _audit(self, action: str, request_id: str = "", paper_set_id: str = "", details: dict = None, subject_id: str = "default-subject"):
        try:
            self._audit_repo.log(
                action=action,
                subject_id=subject_id,
                request_id=request_id,
                paper_set_id=paper_set_id or None,
                details=details or {},
            )
        except Exception as exc:
            logger.debug(f"Audit log failed (non-critical): {exc}")
