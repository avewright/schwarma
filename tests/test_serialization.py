"""Tests for to_dict / from_dict round-trip serialization across core models."""

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from schwarma.events import Event, EventKind
from schwarma.problem import (
    FailureCategory,
    FailureReport,
    Problem,
    ProblemStatus,
    ProblemTag,
)
from schwarma.review import Review, ReviewType, ReviewVerdict
from schwarma.solution import (
    FixPackage,
    OutcomeRecord,
    OutcomeStatus,
    Solution,
    SolutionVerdict,
)


class TestProblemSerialization:
    def test_to_dict_basic(self):
        p = Problem(
            title="Test", description="A test problem",
            author_id=uuid4(),
        )
        d = p.to_dict()
        assert d["title"] == "Test"
        assert d["status"] == "OPEN"
        assert d["failure_report"] is None
        assert isinstance(d["id"], str)

    def test_to_dict_with_failure_report(self):
        fr = FailureReport(
            category=FailureCategory.RUNTIME_ERROR,
            error_message="KeyError: 'x'",
            file_path="app.py",
            line_number=10,
            severity=3,
        )
        p = Problem(
            title="Err", description="error problem",
            author_id=uuid4(), failure_report=fr,
        )
        d = p.to_dict()
        assert d["failure_report"]["category"] == "RUNTIME_ERROR"
        assert d["failure_report"]["line_number"] == 10

    def test_to_dict_tags(self):
        p = Problem(
            title="T", description="d",
            author_id=uuid4(),
            tags={ProblemTag.BUG, ProblemTag.SECURITY},
        )
        d = p.to_dict()
        assert set(d["tags"]) == {"BUG", "SECURITY"}


class TestSolutionSerialization:
    def test_to_dict_basic(self):
        s = Solution(problem_id=uuid4(), author_id=uuid4(), body="fix")
        d = s.to_dict()
        assert d["verdict"] == "PENDING"
        assert d["body"] == "fix"
        assert d["fix_package"] is None
        assert d["outcome"] is None

    def test_to_dict_with_fix_package(self):
        fp = FixPackage(diffs=["--- a\n+++ b"], summary="patch")
        s = Solution(
            problem_id=uuid4(), author_id=uuid4(),
            body="code", fix_package=fp,
        )
        d = s.to_dict()
        assert d["fix_package"]["diffs"] == ["--- a\n+++ b"]
        assert d["fix_package"]["summary"] == "patch"

    def test_to_dict_with_outcome(self):
        s = Solution(problem_id=uuid4(), author_id=uuid4(), body="fix")
        s.record_outcome(OutcomeStatus.CONFIRMED_FIX, ci_passed=True, tests_added=2)
        d = s.to_dict()
        assert d["outcome"]["status"] == "CONFIRMED_FIX"
        assert d["outcome"]["ci_passed"] is True
        assert d["outcome"]["tests_added"] == 2


class TestReviewSerialization:
    def test_to_dict(self):
        r = Review(
            solution_id=uuid4(),
            reviewer_id=uuid4(),
            review_type=ReviewType.CORRECTNESS,
            verdict=ReviewVerdict.APPROVE,
            body="LGTM",
            confidence=0.9,
        )
        d = r.to_dict()
        assert d["review_type"] == "CORRECTNESS"
        assert d["verdict"] == "APPROVE"
        assert d["confidence"] == 0.9
        assert d["body"] == "LGTM"


class TestEventSerialization:
    def test_to_dict(self):
        e = Event(
            kind=EventKind.PROBLEM_POSTED,
            source_agent_id=uuid4(),
            problem_id=uuid4(),
            payload={"extra": "data"},
        )
        d = e.to_dict()
        assert d["kind"] == "PROBLEM_POSTED"
        assert d["payload"] == {"extra": "data"}
        assert isinstance(d["timestamp"], str)

    def test_to_dict_with_none_fields(self):
        e = Event(kind=EventKind.AGENT_REGISTERED)
        d = e.to_dict()
        assert d["source_agent_id"] is None
        assert d["problem_id"] is None


# ======================================================================
# Round-trip tests: to_dict → from_dict produces equal objects
# ======================================================================


class TestFailureReportRoundTrip:
    def test_round_trip_full(self):
        fr = FailureReport(
            category=FailureCategory.RUNTIME_ERROR,
            error_message="KeyError: 'x'",
            stack_trace="Traceback ...",
            file_path="app.py",
            line_number=42,
            reproduction_steps=["step1", "step2"],
            environment={"python": "3.12"},
            severity=4,
            attempts=3,
            related_problem_ids=[uuid4(), uuid4()],
        )
        # FailureReport is serialized inline by Problem.to_dict; build dict manually
        d = {
            "category": fr.category.name,
            "error_message": fr.error_message,
            "stack_trace": fr.stack_trace,
            "file_path": fr.file_path,
            "line_number": fr.line_number,
            "reproduction_steps": fr.reproduction_steps,
            "environment": fr.environment,
            "severity": fr.severity,
            "attempts": fr.attempts,
            "related_problem_ids": [str(uid) for uid in fr.related_problem_ids],
        }
        fr2 = FailureReport.from_dict(d)
        assert fr2.category == fr.category
        assert fr2.error_message == fr.error_message
        assert fr2.stack_trace == fr.stack_trace
        assert fr2.file_path == fr.file_path
        assert fr2.line_number == fr.line_number
        assert fr2.reproduction_steps == fr.reproduction_steps
        assert fr2.environment == fr.environment
        assert fr2.severity == fr.severity
        assert fr2.attempts == fr.attempts
        assert fr2.related_problem_ids == fr.related_problem_ids

    def test_round_trip_minimal(self):
        d = {"category": "LOGIC_ERROR"}
        fr2 = FailureReport.from_dict(d)
        assert fr2.category == FailureCategory.LOGIC_ERROR
        assert fr2.related_problem_ids == []


class TestProblemRoundTrip:
    def test_round_trip_basic(self):
        p = Problem(
            title="Test", description="desc", author_id=uuid4(),
            tags={ProblemTag.BUG, ProblemTag.SECURITY},
            bounty=25, priority=3,
        )
        d = p.to_dict()
        p2 = Problem.from_dict(d)
        assert p2.id == p.id
        assert p2.title == p.title
        assert p2.description == p.description
        assert p2.author_id == p.author_id
        assert p2.tags == p.tags
        assert p2.status == p.status
        assert p2.bounty == p.bounty
        assert p2.priority == p.priority
        assert p2.created_at == p.created_at

    def test_round_trip_with_failure_report(self):
        fr = FailureReport(
            category=FailureCategory.RUNTIME_ERROR,
            error_message="oof", severity=5,
        )
        p = Problem(
            title="Err", description="err desc",
            author_id=uuid4(), failure_report=fr,
        )
        d = p.to_dict()
        p2 = Problem.from_dict(d)
        assert p2.failure_report is not None
        assert p2.failure_report.category == FailureCategory.RUNTIME_ERROR
        assert p2.failure_report.severity == 5

    def test_round_trip_with_deadline(self):
        p = Problem(
            title="T", description="d", author_id=uuid4(),
            deadline=datetime(2025, 12, 31, tzinfo=timezone.utc),
        )
        d = p.to_dict()
        p2 = Problem.from_dict(d)
        assert p2.deadline == p.deadline

    def test_round_trip_claimed_and_solved(self):
        solver = uuid4()
        sol_id = uuid4()
        p = Problem(title="T", description="d", author_id=uuid4())
        p.claim(solver)
        p.add_solution(sol_id)
        d = p.to_dict()
        p2 = Problem.from_dict(d)
        assert p2.claimed_by == [solver]
        assert p2.solution_ids == [sol_id]
        assert p2.status == ProblemStatus.SOLVED


class TestFixPackageRoundTrip:
    def test_round_trip(self):
        fp = FixPackage(
            diffs=["--- a\n+++ b"],
            affected_files=["app.py"],
            test_cases=["test_fix"],
            validation_command="pytest",
            dependencies_added=["requests"],
            breaking_changes=True,
            summary="patch v2",
        )
        d = fp.__dict__  # FixPackage doesn't have to_dict, use dict literal
        fp2 = FixPackage.from_dict(d)
        assert fp2.diffs == fp.diffs
        assert fp2.affected_files == fp.affected_files
        assert fp2.test_cases == fp.test_cases
        assert fp2.validation_command == fp.validation_command
        assert fp2.dependencies_added == fp.dependencies_added
        assert fp2.breaking_changes is True
        assert fp2.summary == fp.summary


class TestOutcomeRecordRoundTrip:
    def test_round_trip_full(self):
        oc = OutcomeRecord(
            status=OutcomeStatus.CONFIRMED_FIX,
            reported_by=uuid4(),
            reported_at=datetime.now(timezone.utc),
            notes="works",
            ci_passed=True,
            tests_added=3,
            follow_up_problem_id=uuid4(),
        )
        # Build dict same way Solution.to_dict builds it
        d = {
            "status": oc.status.name,
            "reported_by": str(oc.reported_by),
            "reported_at": oc.reported_at.isoformat(),
            "notes": oc.notes,
            "ci_passed": oc.ci_passed,
            "tests_added": oc.tests_added,
            "follow_up_problem_id": str(oc.follow_up_problem_id),
        }
        oc2 = OutcomeRecord.from_dict(d)
        assert oc2.status == oc.status
        assert oc2.reported_by == oc.reported_by
        assert oc2.reported_at == oc.reported_at
        assert oc2.ci_passed is True
        assert oc2.tests_added == 3
        assert oc2.follow_up_problem_id == oc.follow_up_problem_id

    def test_round_trip_minimal(self):
        d = {"status": "UNKNOWN"}
        oc = OutcomeRecord.from_dict(d)
        assert oc.status == OutcomeStatus.UNKNOWN
        assert oc.reported_by is None


class TestSolutionRoundTrip:
    def test_round_trip_basic(self):
        s = Solution(problem_id=uuid4(), author_id=uuid4(), body="fix it")
        d = s.to_dict()
        s2 = Solution.from_dict(d)
        assert s2.id == s.id
        assert s2.problem_id == s.problem_id
        assert s2.author_id == s.author_id
        assert s2.body == s.body
        assert s2.verdict == s.verdict
        assert s2.created_at == s.created_at
        assert s2.fix_package is None
        assert s2.outcome is None

    def test_round_trip_with_fix_package(self):
        fp = FixPackage(diffs=["--- a\n+++ b"], summary="patch")
        s = Solution(
            problem_id=uuid4(), author_id=uuid4(),
            body="code", fix_package=fp,
        )
        d = s.to_dict()
        s2 = Solution.from_dict(d)
        assert s2.fix_package is not None
        assert s2.fix_package.diffs == ["--- a\n+++ b"]
        assert s2.fix_package.summary == "patch"

    def test_round_trip_with_outcome(self):
        s = Solution(problem_id=uuid4(), author_id=uuid4(), body="fix")
        s.record_outcome(OutcomeStatus.PARTIAL_FIX, ci_passed=False, tests_added=1)
        d = s.to_dict()
        s2 = Solution.from_dict(d)
        assert s2.outcome is not None
        assert s2.outcome.status == OutcomeStatus.PARTIAL_FIX
        assert s2.outcome.ci_passed is False
        assert s2.outcome.tests_added == 1

    def test_round_trip_accepted(self):
        s = Solution(problem_id=uuid4(), author_id=uuid4(), body="fix")
        s.accept()
        rid = uuid4()
        s.review_ids.append(rid)
        d = s.to_dict()
        s2 = Solution.from_dict(d)
        assert s2.verdict == SolutionVerdict.ACCEPTED
        assert s2.review_ids == [rid]


class TestReviewRoundTrip:
    def test_round_trip(self):
        r = Review(
            solution_id=uuid4(),
            reviewer_id=uuid4(),
            review_type=ReviewType.CORRECTNESS,
            verdict=ReviewVerdict.APPROVE,
            body="LGTM",
            confidence=0.85,
            metadata={"source": "auto"},
        )
        d = r.to_dict()
        r2 = Review.from_dict(d)
        assert r2.id == r.id
        assert r2.solution_id == r.solution_id
        assert r2.reviewer_id == r.reviewer_id
        assert r2.review_type == r.review_type
        assert r2.verdict == r.verdict
        assert r2.body == r.body
        assert r2.confidence == r.confidence
        assert r2.created_at == r.created_at
        assert r2.metadata == r.metadata

    def test_round_trip_request_changes(self):
        r = Review(
            solution_id=uuid4(),
            reviewer_id=uuid4(),
            review_type=ReviewType.QUALITY,
            verdict=ReviewVerdict.REQUEST_CHANGES,
        )
        d = r.to_dict()
        r2 = Review.from_dict(d)
        assert r2.verdict == ReviewVerdict.REQUEST_CHANGES


class TestEventRoundTrip:
    def test_round_trip_full(self):
        e = Event(
            kind=EventKind.PROBLEM_POSTED,
            source_agent_id=uuid4(),
            target_agent_id=uuid4(),
            problem_id=uuid4(),
            solution_id=uuid4(),
            review_id=uuid4(),
            payload={"msg": "hello"},
        )
        d = e.to_dict()
        e2 = Event.from_dict(d)
        assert e2.kind == e.kind
        assert e2.timestamp == e.timestamp
        assert e2.source_agent_id == e.source_agent_id
        assert e2.target_agent_id == e.target_agent_id
        assert e2.problem_id == e.problem_id
        assert e2.solution_id == e.solution_id
        assert e2.review_id == e.review_id
        assert e2.payload == e.payload

    def test_round_trip_minimal(self):
        e = Event(kind=EventKind.AGENT_REGISTERED)
        d = e.to_dict()
        e2 = Event.from_dict(d)
        assert e2.kind == EventKind.AGENT_REGISTERED
        assert e2.source_agent_id is None
        assert e2.problem_id is None
