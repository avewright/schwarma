"""Tests for the Problem model — lifecycle, status transitions, validation."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from schwarma.problem import FailureCategory, FailureReport, Problem, ProblemStatus, ProblemTag
from schwarma.trust import Sensitivity
from schwarma.agent import ModelTier


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _make_problem(**kw) -> Problem:
    defaults = dict(
        title="Test problem",
        description="Describe it",
        author_id=uuid4(),
    )
    defaults.update(kw)
    return Problem(**defaults)


# ------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------

class TestProblemCreation:
    def test_defaults(self):
        p = _make_problem()
        assert p.status == ProblemStatus.OPEN
        assert p.bounty == 10
        assert p.sensitivity == Sensitivity.INTERNAL
        assert p.is_open
        assert ProblemTag.GENERAL in p.tags
        assert p.min_solver_tier is None

    def test_custom_tags_and_bounty(self):
        p = _make_problem(tags={ProblemTag.BUG, ProblemTag.SECURITY}, bounty=50)
        assert ProblemTag.BUG in p.tags
        assert p.bounty == 50

    def test_min_solver_tier(self):
        p = _make_problem(min_solver_tier=ModelTier.PREMIUM)
        assert p.min_solver_tier == ModelTier.PREMIUM


class TestClaim:
    def test_claim_sets_status(self):
        p = _make_problem()
        agent_id = uuid4()
        p.claim(agent_id)
        assert p.status == ProblemStatus.CLAIMED
        assert agent_id in p.claimed_by

    def test_claim_not_open_raises(self):
        p = _make_problem()
        p.claim(uuid4())
        with pytest.raises(ValueError, match="not open"):
            p.claim(uuid4())

    def test_claim_max_solvers(self):
        p = _make_problem(max_solvers=2)
        p.claim(uuid4())
        p.status = ProblemStatus.OPEN  # reset for second claim
        p.claim(uuid4())
        assert len(p.claimed_by) == 2

    def test_claim_exceeds_max_solvers(self):
        p = _make_problem(max_solvers=1)
        p.claim(uuid4())
        p.status = ProblemStatus.OPEN  # try to claim again
        with pytest.raises(ValueError, match="max solvers"):
            p.claim(uuid4())


class TestAddSolution:
    def test_add_solution(self):
        p = _make_problem()
        sol_id = uuid4()
        p.add_solution(sol_id)
        assert sol_id in p.solution_ids
        assert p.status == ProblemStatus.SOLVED


class TestAccept:
    def test_accept_valid_solution(self):
        p = _make_problem()
        sol_id = uuid4()
        p.add_solution(sol_id)
        p.accept(sol_id)
        assert p.status == ProblemStatus.CLOSED
        assert p.accepted_solution_id == sol_id

    def test_accept_unknown_solution_raises(self):
        p = _make_problem()
        with pytest.raises(ValueError, match="not associated"):
            p.accept(uuid4())


class TestRejectAndReopen:
    def test_reject_and_reopen(self):
        p = _make_problem()
        agent_id = uuid4()
        p.claim(agent_id)
        p.reject_and_reopen()
        assert p.status == ProblemStatus.OPEN
        assert p.claimed_by == []
        assert p.accepted_solution_id is None


class TestEscalateAndExpire:
    def test_escalate(self):
        p = _make_problem()
        p.escalate()
        assert p.status == ProblemStatus.ESCALATED

    def test_expire(self):
        p = _make_problem()
        p.expire()
        assert p.status == ProblemStatus.EXPIRED

    def test_is_expired_by_deadline(self):
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        p = _make_problem(deadline=past)
        assert p.is_expired

    def test_not_expired_future_deadline(self):
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        p = _make_problem(deadline=future)
        assert not p.is_expired


class TestIdentity:
    def test_hashable(self):
        p = _make_problem()
        assert hash(p) == hash(p.id)

    def test_equality(self):
        p1 = _make_problem()
        p2 = _make_problem()
        assert p1 != p2


class TestFailureReport:
    """Structured failure metadata on a Problem."""

    def test_default_failure_report_is_none(self):
        p = _make_problem()
        assert p.failure_report is None

    def test_attach_failure_report(self):
        fr = FailureReport(
            category=FailureCategory.RUNTIME_ERROR,
            error_message="IndexError: list index out of range",
            file_path="main.py",
            line_number=42,
            severity=3,
        )
        p = _make_problem(failure_report=fr)
        assert p.failure_report is not None
        assert p.failure_report.category == FailureCategory.RUNTIME_ERROR
        assert p.failure_report.line_number == 42

    def test_signature_dedup(self):
        fr1 = FailureReport(
            category=FailureCategory.SYNTAX_ERROR,
            error_message="SyntaxError at line 10",
            file_path="foo.py",
        )
        fr2 = FailureReport(
            category=FailureCategory.SYNTAX_ERROR,
            error_message="SyntaxError at line 99",
            file_path="foo.py",
        )
        # Signatures should match after number normalisation
        assert fr1.signature == fr2.signature

    def test_signature_different_categories(self):
        fr1 = FailureReport(category=FailureCategory.RUNTIME_ERROR, error_message="err")
        fr2 = FailureReport(category=FailureCategory.LOGIC_ERROR, error_message="err")
        assert fr1.signature != fr2.signature

    def test_reproduction_steps(self):
        fr = FailureReport(
            reproduction_steps=["open file", "run test", "observe crash"],
        )
        assert len(fr.reproduction_steps) == 3

    def test_environment_metadata(self):
        fr = FailureReport(
            environment={"python": "3.12", "os": "linux"},
        )
        assert fr.environment["python"] == "3.12"
