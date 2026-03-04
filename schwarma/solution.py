"""
Solution — an agent's attempt to solve a posted Problem.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from typing import Any
from uuid import UUID, uuid4


class SolutionVerdict(Enum):
    """Outcome once a solution has been reviewed / judged."""

    PENDING = auto()
    ACCEPTED = auto()
    REJECTED = auto()
    NEEDS_REVISION = auto()


class OutcomeStatus(Enum):
    """Closed-loop resolution signal — did the fix actually work?"""

    UNKNOWN = auto()       # No follow-up data yet
    CONFIRMED_FIX = auto() # Author/CI confirms the fix resolved the issue
    PARTIAL_FIX = auto()   # Partially resolved
    NO_EFFECT = auto()     # Did not help
    REGRESSION = auto()    # Made things worse


@dataclass
class OutcomeRecord:
    """Tracks the real-world outcome after a solution is accepted.

    This closes the feedback loop: a solution can be ACCEPTED by review
    but still fail in production.  OutcomeRecord lets the problem author
    (or an automated CI system) report whether the fix actually helped.
    """

    status: OutcomeStatus = OutcomeStatus.UNKNOWN
    reported_by: UUID | None = None
    reported_at: datetime | None = None
    notes: str = ""
    ci_passed: bool | None = None  # True/False/None (not checked)
    tests_added: int = 0
    follow_up_problem_id: UUID | None = None  # if regression spawned new work

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OutcomeRecord":
        """Reconstruct an OutcomeRecord from a dict produced by ``to_dict``."""
        return cls(
            status=OutcomeStatus[data["status"]],
            reported_by=UUID(data["reported_by"]) if data.get("reported_by") else None,
            reported_at=(
                datetime.fromisoformat(data["reported_at"])
                if data.get("reported_at") else None
            ),
            notes=data.get("notes", ""),
            ci_passed=data.get("ci_passed"),
            tests_added=data.get("tests_added", 0),
            follow_up_problem_id=(
                UUID(data["follow_up_problem_id"])
                if data.get("follow_up_problem_id") else None
            ),
        )


@dataclass
class FixPackage:
    """Structured solution artifact — the deliverable of a solve.

    Goes beyond a raw ``body`` string: a fix package can carry diffs,
    affected files, test cases, and validation commands.  This provides
    richer training signal and enables downstream verification.
    """

    diffs: list[str] = field(default_factory=list)          # unified diff hunks
    affected_files: list[str] = field(default_factory=list)  # paths touched
    test_cases: list[str] = field(default_factory=list)      # new/modified tests
    validation_command: str = ""                             # e.g. "pytest tests/"
    dependencies_added: list[str] = field(default_factory=list)
    breaking_changes: bool = False
    summary: str = ""  # human-readable one-liner

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FixPackage":
        """Reconstruct a FixPackage from a dict produced by ``to_dict``."""
        return cls(
            diffs=data.get("diffs", []),
            affected_files=data.get("affected_files", []),
            test_cases=data.get("test_cases", []),
            validation_command=data.get("validation_command", ""),
            dependencies_added=data.get("dependencies_added", []),
            breaking_changes=data.get("breaking_changes", False),
            summary=data.get("summary", ""),
        )


@dataclass
class RevisionRound:
    """One round of revision feedback + solver response.

    Tracks the structured back-and-forth when a reviewer requests changes
    and the solver submits a revised solution body.
    """

    round_number: int
    reviewer_feedback: str
    reviewer_id: UUID
    revised_body: str = ""          # populated when solver resubmits
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class Solution:
    """An answer to a :class:`Problem`, submitted by an agent."""

    problem_id: UUID
    author_id: UUID
    body: str  # The actual answer / code / explanation
    id: UUID = field(default_factory=uuid4)
    verdict: SolutionVerdict = SolutionVerdict.PENDING
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    review_ids: list[UUID] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    # Structured fix artifacts (optional)
    fix_package: FixPackage | None = None

    # Closed-loop outcome tracking (optional)
    outcome: OutcomeRecord | None = None

    # Multi-round revision history
    revision_history: list[RevisionRound] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Verdict helpers
    # ------------------------------------------------------------------

    def accept(self) -> None:
        self.verdict = SolutionVerdict.ACCEPTED

    def reject(self) -> None:
        self.verdict = SolutionVerdict.REJECTED

    def request_revision(self) -> None:
        self.verdict = SolutionVerdict.NEEDS_REVISION

    def record_outcome(
        self,
        status: OutcomeStatus,
        *,
        reported_by: UUID | None = None,
        notes: str = "",
        ci_passed: bool | None = None,
        tests_added: int = 0,
        follow_up_problem_id: UUID | None = None,
    ) -> OutcomeRecord:
        """Record a closed-loop outcome for this solution."""
        self.outcome = OutcomeRecord(
            status=status,
            reported_by=reported_by,
            reported_at=datetime.now(timezone.utc),
            notes=notes,
            ci_passed=ci_passed,
            tests_added=tests_added,
            follow_up_problem_id=follow_up_problem_id,
        )
        return self.outcome

    @property
    def is_pending(self) -> bool:
        return self.verdict == SolutionVerdict.PENDING

    # ------------------------------------------------------------------
    # Dunder
    # ------------------------------------------------------------------

    def __hash__(self) -> int:
        return hash(self.id)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Solution):
            return self.id == other.id
        return NotImplemented

    def __str__(self) -> str:
        return (
            f"Solution(problem={self.problem_id}, author={self.author_id}, "
            f"verdict={self.verdict.name})"
        )

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict for storage / transport."""
        d: dict[str, Any] = {
            "id": str(self.id),
            "problem_id": str(self.problem_id),
            "author_id": str(self.author_id),
            "body": self.body,
            "verdict": self.verdict.name,
            "created_at": self.created_at.isoformat(),
            "review_ids": [str(uid) for uid in self.review_ids],
            "metadata": self.metadata,
        }
        if self.fix_package is not None:
            d["fix_package"] = {
                "diffs": self.fix_package.diffs,
                "affected_files": self.fix_package.affected_files,
                "test_cases": self.fix_package.test_cases,
                "validation_command": self.fix_package.validation_command,
                "dependencies_added": self.fix_package.dependencies_added,
                "breaking_changes": self.fix_package.breaking_changes,
                "summary": self.fix_package.summary,
            }
        else:
            d["fix_package"] = None
        if self.outcome is not None:
            d["outcome"] = {
                "status": self.outcome.status.name,
                "reported_by": str(self.outcome.reported_by) if self.outcome.reported_by else None,
                "reported_at": self.outcome.reported_at.isoformat() if self.outcome.reported_at else None,
                "notes": self.outcome.notes,
                "ci_passed": self.outcome.ci_passed,
                "tests_added": self.outcome.tests_added,
                "follow_up_problem_id": str(self.outcome.follow_up_problem_id) if self.outcome.follow_up_problem_id else None,
            }
        else:
            d["outcome"] = None
        d["revision_history"] = [
            {
                "round_number": rr.round_number,
                "reviewer_feedback": rr.reviewer_feedback,
                "reviewer_id": str(rr.reviewer_id),
                "revised_body": rr.revised_body,
                "timestamp": rr.timestamp.isoformat(),
            }
            for rr in self.revision_history
        ]
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Solution":
        """Reconstruct a Solution from a dict produced by ``to_dict``."""
        fix_package = None
        if data.get("fix_package"):
            fix_package = FixPackage.from_dict(data["fix_package"])

        outcome = None
        if data.get("outcome"):
            outcome = OutcomeRecord.from_dict(data["outcome"])

        s = cls(
            problem_id=UUID(data["problem_id"]),
            author_id=UUID(data["author_id"]),
            body=data["body"],
            fix_package=fix_package,
            outcome=outcome,
            metadata=data.get("metadata", {}),
        )
        s.id = UUID(data["id"])
        s.verdict = SolutionVerdict[data["verdict"]]
        s.created_at = datetime.fromisoformat(data["created_at"])
        s.review_ids = [UUID(uid) for uid in data.get("review_ids", [])]
        s.revision_history = [
            RevisionRound(
                round_number=rr["round_number"],
                reviewer_feedback=rr["reviewer_feedback"],
                reviewer_id=UUID(rr["reviewer_id"]),
                revised_body=rr.get("revised_body", ""),
            )
            for rr in data.get("revision_history", [])
        ]
        return s
