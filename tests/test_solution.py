"""Tests for the Solution model — verdict helpers, identity."""

from uuid import uuid4

import pytest

from schwarma.solution import (
    FixPackage, OutcomeRecord, OutcomeStatus,
    Solution, SolutionVerdict,
)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _make_solution(**kw) -> Solution:
    defaults = dict(
        problem_id=uuid4(),
        author_id=uuid4(),
        body="def fizzbuzz(): pass",
    )
    defaults.update(kw)
    return Solution(**defaults)


# ------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------

class TestSolutionCreation:
    def test_defaults(self):
        s = _make_solution()
        assert s.verdict == SolutionVerdict.PENDING
        assert s.is_pending
        assert s.body == "def fizzbuzz(): pass"
        assert s.review_ids == []

    def test_custom_body(self):
        s = _make_solution(body="custom answer")
        assert s.body == "custom answer"


class TestVerdictHelpers:
    def test_accept(self):
        s = _make_solution()
        s.accept()
        assert s.verdict == SolutionVerdict.ACCEPTED
        assert not s.is_pending

    def test_reject(self):
        s = _make_solution()
        s.reject()
        assert s.verdict == SolutionVerdict.REJECTED
        assert not s.is_pending

    def test_request_revision(self):
        s = _make_solution()
        s.request_revision()
        assert s.verdict == SolutionVerdict.NEEDS_REVISION
        assert not s.is_pending


class TestIdentity:
    def test_hashable(self):
        s = _make_solution()
        assert hash(s) == hash(s.id)

    def test_equality_by_id(self):
        s1 = _make_solution()
        s2 = _make_solution()
        assert s1 != s2

    def test_str(self):
        s = _make_solution()
        text = str(s)
        assert "PENDING" in text


class TestReviewTracking:
    def test_add_review_ids(self):
        s = _make_solution()
        r1, r2 = uuid4(), uuid4()
        s.review_ids.append(r1)
        s.review_ids.append(r2)
        assert len(s.review_ids) == 2


class TestOutcomeTracking:
    """Closed-loop outcome recording on solutions."""

    def test_default_outcome_is_none(self):
        s = _make_solution()
        assert s.outcome is None

    def test_record_confirmed_fix(self):
        s = _make_solution()
        reporter = uuid4()
        outcome = s.record_outcome(
            OutcomeStatus.CONFIRMED_FIX,
            reported_by=reporter,
            notes="CI green, deployed to prod",
            ci_passed=True,
            tests_added=3,
        )
        assert s.outcome is outcome
        assert outcome.status == OutcomeStatus.CONFIRMED_FIX
        assert outcome.ci_passed is True
        assert outcome.tests_added == 3
        assert outcome.reported_at is not None

    def test_record_regression(self):
        s = _make_solution()
        follow_up = uuid4()
        outcome = s.record_outcome(
            OutcomeStatus.REGRESSION,
            follow_up_problem_id=follow_up,
        )
        assert outcome.status == OutcomeStatus.REGRESSION
        assert outcome.follow_up_problem_id == follow_up

    def test_overwrite_outcome(self):
        s = _make_solution()
        s.record_outcome(OutcomeStatus.UNKNOWN)
        s.record_outcome(OutcomeStatus.CONFIRMED_FIX)
        assert s.outcome.status == OutcomeStatus.CONFIRMED_FIX


class TestFixPackage:
    """Structured solution artifacts."""

    def test_default_fix_package_is_none(self):
        s = _make_solution()
        assert s.fix_package is None

    def test_attach_fix_package(self):
        fp = FixPackage(
            diffs=["--- a/main.py\n+++ b/main.py\n@@ -1 +1 @@\n-old\n+new"],
            affected_files=["main.py"],
            test_cases=["test_main.py::test_fix"],
            validation_command="pytest tests/",
            summary="Fix off-by-one in loop",
        )
        s = _make_solution(fix_package=fp)
        assert s.fix_package is not None
        assert len(s.fix_package.diffs) == 1
        assert s.fix_package.breaking_changes is False
        assert s.fix_package.validation_command == "pytest tests/"

    def test_fix_package_dependencies(self):
        fp = FixPackage(
            dependencies_added=["requests>=2.0", "pydantic"],
            breaking_changes=True,
        )
        assert fp.breaking_changes is True
        assert len(fp.dependencies_added) == 2
