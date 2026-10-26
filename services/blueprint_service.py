"""
services/blueprint_service.py
Converts faculty UI criteria into immutable Blueprint objects.
Pure deterministic Python — no LLM involvement.
"""

from __future__ import annotations

import logging
import math
from typing import Optional

from models.blueprint import (
    MinorBlueprint,
    EndSemBlueprint,
    UnitAllocation,
    BloomAllocation,
    SourceAllocation,
)
from models.enums import ExamType

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

MINOR_TOTAL_PRINTED = 6
MINOR_BANK = 4
MINOR_AI = 2
MINOR_MARKS = 40

ENDSEM_TOTAL_PRINTED = 16
ENDSEM_BANK = 11
ENDSEM_AI = 5
ENDSEM_MARKS = 100

# Default unit allocations for End-Sem
DEFAULT_ENDSEM_UNITS = [
    {"unit": 1, "bank": 4, "ai": 2, "total": 6},
    {"unit": 2, "bank": 4, "ai": 2, "total": 6},
    {"unit": 3, "bank": 3, "ai": 1, "total": 4},
]


class BlueprintError(Exception):
    pass


class BlueprintService:
    """
    Builds blueprint objects from faculty parameters.
    Validates integer feasibility.
    """

    def build_minor(
        self,
        l2_percent: int = 50,
        l3_percent: int = 50,
        selected_chapters: Optional[list[int]] = None,
        random_seed: Optional[int] = None,
        tolerance_percent: int = 5,
        marking_scheme: Optional[dict] = None,
        exam_subtype: Optional[str] = "ISA-I",
    ) -> MinorBlueprint:
        """
        Build a validated MinorBlueprint. Consumes saved marking scheme if provided.
        """
        self._validate_bloom_percents(l2_percent, l3_percent)

        scheme = marking_scheme or {}
        total_q = scheme.get("total_questions", 3)
        q_attempt = scheme.get("questions_to_attempt", 2)
        total_marks = scheme.get("total_marks", MINOR_MARKS)
        sub_pattern = scheme.get("sub_question_pattern") or [10, 10]
        duration_str = scheme.get("duration", "")
        # Parse duration minutes if string like "75 minutes" or "60 mins"
        dur_mins = 75
        if duration_str:
            import re
            m = re.search(r"\d+", str(duration_str))
            if m:
                dur_mins = int(m.group(0))

        instruction = scheme.get("instructions") or f"Answer any {q_attempt} full questions. Each full question carries equal marks."
        marks_per_full = scheme.get("marks_per_full_question") or sum(sub_pattern)

        parts_count = len(sub_pattern) if sub_pattern else 2
        total_printed = total_q * parts_count

        l2_count, l3_count = self._allocate_bloom(
            total_printed, l2_percent, l3_percent, tolerance_percent
        )

        bank_count = max(1, round(total_printed * 0.67))
        ai_count = total_printed - bank_count

        # Source/bloom matrix for bank and AI subsets
        bank_l2, bank_l3 = self._allocate_bloom(bank_count, l2_percent, l3_percent, tolerance_percent)
        ai_l2, ai_l3 = l2_count - bank_l2, l3_count - bank_l3

        bp = MinorBlueprint(
            exam_type=ExamType.MINOR,
            exam_subtype=exam_subtype or "ISA-I",
            total_printed_questions=total_printed,
            total_attempted_marks=total_marks,
            duration_minutes=dur_mins,
            main_question_count=total_q,
            parts_per_question=parts_count,
            marks_per_part=sub_pattern[0] if sub_pattern else 10,
            sub_question_marks=sub_pattern,
            choice_instruction=instruction,
            source=SourceAllocation(bank_count=bank_count, ai_count=ai_count, total=total_printed),
            bloom=BloomAllocation(l2_count=l2_count, l3_count=l3_count, total=total_printed),
            bank_l2=bank_l2,
            bank_l3=bank_l3,
            ai_l2=ai_l2,
            ai_l3=ai_l3,
            selected_chapters=selected_chapters or [],
            l2_percent_target=l2_percent,
            l3_percent_target=l3_percent,
            tolerance_percent=tolerance_percent,
            random_seed=random_seed,
        )
        logger.info(
            f"MinorBlueprint ({exam_subtype}): bank={bank_count} AI={ai_count} "
            f"L2={l2_count} L3={l3_count} marks={total_marks} pattern={sub_pattern}"
        )
        return bp

    def build_end_sem(
        self,
        l2_percent: int = 50,
        l3_percent: int = 50,
        unit_allocations: Optional[list[dict]] = None,
        random_seed: Optional[int] = None,
        tolerance_percent: int = 5,
        marking_scheme: Optional[dict] = None,
        exam_subtype: Optional[str] = "ESA",
    ) -> EndSemBlueprint:
        """
        Build a validated EndSemBlueprint. Consumes saved marking scheme if provided.
        """
        self._validate_bloom_percents(l2_percent, l3_percent)

        scheme = marking_scheme or {}
        total_marks = scheme.get("total_marks", 100)
        sub_pattern = scheme.get("sub_question_pattern") or [10, 10]
        duration_str = scheme.get("duration", "")
        dur_mins = 180
        if duration_str:
            import re
            m = re.search(r"\d+", str(duration_str))
            if m:
                dur_mins = int(m.group(0))

        instruction = scheme.get("instructions") or "Answer any TWO full questions from Unit 1 & 2, and one from Unit 3."

        unit_allocs_cfg = unit_allocations or DEFAULT_ENDSEM_UNITS
        self._validate_unit_alloc(unit_allocs_cfg)

        l2_count, l3_count = self._allocate_bloom(
            ENDSEM_TOTAL_PRINTED, l2_percent, l3_percent, tolerance_percent
        )

        unit_objs: list[UnitAllocation] = []
        for cfg in unit_allocs_cfg:
            u = cfg["unit"]
            total = cfg["total"]
            bank = cfg["bank"]
            ai = cfg["ai"]
            u_l2, u_l3 = self._allocate_bloom(total, l2_percent, l3_percent, tolerance_percent)
            choice_map = {1: "Answer any TWO full questions.",
                          2: "Answer any TWO full questions.",
                          3: "Answer any ONE full question."}
            marks_map = {1: 40, 2: 40, 3: 20}
            attempt_map = {1: 4, 2: 4, 3: 2}
            unit_objs.append(UnitAllocation(
                unit_number=u,
                source=SourceAllocation(bank_count=bank, ai_count=ai, total=total),
                bloom=BloomAllocation(l2_count=u_l2, l3_count=u_l3, total=total),
                questions_to_print=total,
                questions_to_attempt=attempt_map.get(u, 4),
                marks_to_attempt=marks_map.get(u, 40),
                main_questions=3 if u in (1, 2) else 2,
                choice_instruction=choice_map.get(u, "Answer as instructed."),
            ))

        bp = EndSemBlueprint(
            exam_type=ExamType.END_SEM,
            exam_subtype=exam_subtype or "ESA",
            total_printed_questions=ENDSEM_TOTAL_PRINTED,
            total_attempted_marks=total_marks,
            duration_minutes=dur_mins,
            sub_question_marks=sub_pattern,
            choice_instruction=instruction,
            source=SourceAllocation(bank_count=ENDSEM_BANK, ai_count=ENDSEM_AI, total=ENDSEM_TOTAL_PRINTED),
            bloom=BloomAllocation(l2_count=l2_count, l3_count=l3_count, total=ENDSEM_TOTAL_PRINTED),
            unit_allocations=unit_objs,
            l2_percent_target=l2_percent,
            l3_percent_target=l3_percent,
            tolerance_percent=tolerance_percent,
            random_seed=random_seed,
        )
        logger.info(
            f"EndSemBlueprint ({exam_subtype}): bank={ENDSEM_BANK} AI={ENDSEM_AI} "
            f"L2={l2_count} L3={l3_count} marks={total_marks}"
        )
        return bp

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _validate_bloom_percents(self, l2: int, l3: int) -> None:
        if l2 + l3 != 100:
            raise BlueprintError(
                f"L2% + L3% must equal 100, got {l2} + {l3} = {l2 + l3}"
            )
        if not (0 <= l2 <= 100) or not (0 <= l3 <= 100):
            raise BlueprintError("Bloom percentages must be between 0 and 100")

    def _allocate_bloom(
        self, total: int, l2_pct: int, l3_pct: int, tolerance: int
    ) -> tuple[int, int]:
        """
        Compute nearest feasible integer Bloom allocation.
        Returns (l2_count, l3_count) that sum to total.
        Validates within tolerance.
        """
        l2_exact = total * l2_pct / 100
        l2_count = round(l2_exact)
        l3_count = total - l2_count

        if l2_count < 0 or l3_count < 0:
            raise BlueprintError(
                f"Cannot allocate negative counts: L2={l2_count} L3={l3_count}"
            )

        # Check within tolerance, accounting for integer discretization with small totals
        achieved_l2_pct = l2_count / total * 100
        achieved_l3_pct = l3_count / total * 100
        # Granularity of 1 question is (100 / total)%; rounding half-step is (50 / total)%
        effective_tolerance = max(float(tolerance), (50.0 / total) + 0.05)
        if abs(achieved_l2_pct - l2_pct) > effective_tolerance:
            raise BlueprintError(
                f"Cannot achieve L2={l2_pct}% within ±{tolerance}% tolerance "
                f"with {total} questions. Achieved L2={achieved_l2_pct:.1f}%."
            )
        return l2_count, l3_count

    def _validate_unit_alloc(self, unit_allocs: list[dict]) -> None:
        """Validate custom unit allocation sums."""
        total_bank = sum(u["bank"] for u in unit_allocs)
        total_ai = sum(u["ai"] for u in unit_allocs)
        if total_bank != ENDSEM_BANK:
            raise BlueprintError(
                f"Unit bank allocations must sum to {ENDSEM_BANK}, got {total_bank}"
            )
        if total_ai != ENDSEM_AI:
            raise BlueprintError(
                f"Unit AI allocations must sum to {ENDSEM_AI}, got {total_ai}"
            )
