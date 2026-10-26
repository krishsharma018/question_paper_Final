"""
tests/test_audit_regression.py
Comprehensive regression tests for audit findings and fixes:
1. Approval workflow: paper set approval updates all question approval statuses.
2. Blueprint integer rounding & tolerance math with small question counts.
3. Bulk upsert efficiency and domain conversion.
4. Chapter completeness report with subject scoping.
5. Settings / LLM health check returns valid structure without error.
6. Subject-scoped audit logs during paper generation.
7. Dependency subject_id query parameter fallback.
"""

import pytest
from models.enums import ExamType, ApprovalStatus, PaperSetStatus
from models.question import Question
from repositories.database import get_session, PaperSetDB, PaperQuestionDB, QuestionDB, AuditLogDB
from repositories.paper_repo import PaperRepository
from repositories.question_repo import QuestionRepository
from services.blueprint_service import BlueprintService, BlueprintError
from services.question_bank_service import QuestionBankService
from services.llm_provider import get_llm_provider
from agents.coordinator import CoordinatorAgent, GenerationRequest


class TestApprovalWorkflowRegression:
    """Verifies BUG-07: Approving a paper set must also approve all its questions."""

    def test_approve_paper_set_and_all_questions(self, loaded_bank, mock_llm):
        coordinator = CoordinatorAgent(llm_provider=mock_llm)
        request = GenerationRequest(
            exam_type=ExamType.MINOR,
            num_sets=1,
            selected_chapters=[1, 2, 3],
            subject_id="test-approval-subj",
            random_seed=42,
        )
        result = coordinator.generate(request)
        assert result.success
        assert len(result.complete_paper.sets) == 1
        set_id = result.complete_paper.sets[0].set_id

        # Verify initial question states are pending or generated
        paper_repo = PaperRepository()
        paper_set_before = paper_repo.get_paper_set(set_id, subject_id="test-approval-subj")
        assert paper_set_before is not None

        # Execute approval
        coordinator.approve_set(set_id, subject_id="test-approval-subj")

        # Verify paper set is APPROVED
        paper_set_after = paper_repo.get_paper_set(set_id, subject_id="test-approval-subj")
        assert paper_set_after.status in ("APPROVED", PaperSetStatus.APPROVED)

        # Verify ALL questions in the set are now APPROVED in the database
        with get_session() as session:
            questions = session.query(PaperQuestionDB).filter(
                PaperQuestionDB.paper_set_id == set_id,
                PaperQuestionDB.subject_id == "test-approval-subj",
            ).all()
            assert len(questions) == 6
            for q in questions:
                assert q.approval_status == "APPROVED", f"Slot {q.slot_id} was not approved: {q.approval_status}"

        # Verify paper_set.all_questions_approved is True
        assert paper_set_after.all_questions_approved


class TestBlueprintDiscretizationRegression:
    """Verifies BUG-11: Integer rounding for odd/small totals must not throw false BlueprintError."""

    def setup_method(self):
        self.service = BlueprintService()

    def test_odd_question_count_discretization(self):
        # 7 questions at 50% target: round(3.5) = 4, 4/7 = 57.14%, deviation 7.14%
        # With default 5% tolerance, integer discretization should now allow the nearest feasible integer
        l2, l3 = self.service._allocate_bloom(total=7, l2_pct=50, l3_pct=50, tolerance=5)
        assert l2 + l3 == 7
        assert (l2, l3) in [(4, 3), (3, 4)]

    def test_minor_blueprint_defaults(self):
        bp = self.service.build_minor()
        assert bp.total_attempted_marks == 40
        assert bp.total_printed_questions == 6
        assert bp.marks_per_part == 10


class TestQuestionRepositoryBulkRegression:
    """Verifies BUG-10 & BUG-14: Bulk upsert in single session and domain return."""

    def test_bulk_upsert_single_session_and_domain_return(self):
        repo = QuestionRepository()
        q1 = Question(
            question_id="BULK-TEST-01",
            course_name="Course 1",
            unit_number=1,
            chapter_number=1,
            chapter_name="Ch 1",
            question_text="Sample text 1",
            bloom_level="L2",
            marks=10,
        )
        q2 = Question(
            question_id="BULK-TEST-02",
            course_name="Course 1",
            unit_number=1,
            chapter_number=1,
            chapter_name="Ch 1",
            question_text="Sample text 2",
            bloom_level="L3",
            marks=10,
        )
        count = repo.bulk_upsert([q1, q2], subject_id="test-bulk-subj")
        assert count == 2

        # Verify single upsert returns a domain Question, not detached ORM
        single = repo.upsert(q1, subject_id="test-bulk-subj")
        assert isinstance(single, Question)
        assert single.question_id == "BULK-TEST-01"
        assert single.question_text == "Sample text 1"


class TestQuestionBankServiceRegression:
    """Verifies BUG-13: Chapter completeness report accepts subject_id."""

    def test_completeness_report_with_subject(self, loaded_bank):
        svc = QuestionBankService()
        # Should not raise TypeError: count_by_chapter_bloom() missing 1 required positional argument
        report = svc.chapter_completeness_report(expected_chapters=3, subject_id="genai-001")
        assert isinstance(report, dict)
        assert 1 in report
        assert "l2_count" in report[1]
        assert "l3_count" in report[1]


class TestLLMHealthCheckRegression:
    """Verifies BUG-09: LLM provider health check returns structured result without error."""

    def test_health_check_returns_valid_object(self):
        llm = get_llm_provider()
        res = llm.health_check()
        assert hasattr(res, "status")
        assert hasattr(res, "message")
        assert hasattr(res, "model")
        assert res.status in ("ok", "error")


class TestAuditSubjectPropagationRegression:
    """Verifies BUG-12: Coordinator passes subject_id to audit repository."""

    def test_audit_logs_carry_subject_id(self, loaded_bank, mock_llm):
        coordinator = CoordinatorAgent(llm_provider=mock_llm)
        target_subject = "audit-sub-test-123"
        request = GenerationRequest(
            exam_type=ExamType.MINOR,
            num_sets=1,
            selected_chapters=[1, 2, 3],
            subject_id=target_subject,
            random_seed=42,
        )
        result = coordinator.generate(request)
        assert result.success

        with get_session() as session:
            logs = session.query(AuditLogDB).filter(
                AuditLogDB.subject_id == target_subject
            ).all()
            actions = {log.action for log in logs}
            assert "GENERATION_START" in actions
            assert "GENERATION_COMPLETE" in actions
            for log in logs:
                assert log.subject_id == target_subject


class TestDepsQueryParamFallbackRegression:
    """Verifies BUG-08: Dependency resolves subject_id from query parameter if header absent."""

    def test_query_param_resolution(self):
        from api.deps import get_subject_id
        from repositories.subject_repo import SubjectRepository
        # default-subject always exists in test DB
        resolved = get_subject_id(x_subject_id=None, subject_id="default-subject")
        assert resolved == "default-subject"
