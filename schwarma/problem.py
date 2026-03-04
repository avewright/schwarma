"""
Problem — a unit of work posted to the exchange.

A Problem moves through a lifecycle::

    OPEN → CLAIMED → SOLVED → CLOSED
              ↓         ↓
           EXPIRED   REJECTED → OPEN  (re-queued)
              ↓
           ESCALATED
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum, auto
from typing import Any
from uuid import UUID, uuid4

from schwarma.trust import Sensitivity
from schwarma.agent import ModelTier


class ProblemStatus(Enum):
    OPEN = auto()
    CLAIMED = auto()
    SOLVED = auto()
    CLOSED = auto()
    REJECTED = auto()
    EXPIRED = auto()
    ESCALATED = auto()


class ProblemTag(Enum):
    """Light-weight classification tags for routing."""

    BUG = auto()
    FEATURE = auto()
    QUESTION = auto()
    REVIEW_REQUEST = auto()
    PROOFREAD = auto()
    GOOD_FAITH = auto()
    ARCHITECTURE = auto()
    RESEARCH = auto()
    OPTIMIZATION = auto()
    SECURITY = auto()
    GENERAL = auto()


class FailureCategory(Enum):
    """Broad failure category for structured failure capsules."""

    SYNTAX_ERROR = auto()
    RUNTIME_ERROR = auto()
    LOGIC_ERROR = auto()
    PERFORMANCE = auto()
    SECURITY_VULNERABILITY = auto()
    TEST_FAILURE = auto()
    BUILD_FAILURE = auto()
    CONFIGURATION = auto()
    DEPENDENCY = auto()
    UNKNOWN = auto()


@dataclass
class FailureReport:
    """Structured failure metadata attached to a problem.

    Inspired by the 'structured failure capsules' concept: every problem
    can carry machine-readable context about what went wrong, enabling
    better triage, dedup, and training signal.
    """

    category: FailureCategory = FailureCategory.UNKNOWN
    error_message: str = ""
    stack_trace: str = ""
    file_path: str = ""
    line_number: int | None = None
    reproduction_steps: list[str] = field(default_factory=list)
    environment: dict[str, str] = field(default_factory=dict)
    severity: int = 1  # 1=low .. 5=critical
    attempts: int = 0  # how many times this was attempted before posting
    related_problem_ids: list[UUID] = field(default_factory=list)

    @property
    def signature(self) -> str:
        """A normalized signature for dedup / similarity matching.

        Combines category + error message + file path for a quick fingerprint.
        """
        parts = [self.category.name]
        if self.error_message:
            # Normalise: lowercase, strip line-specific numbers
            import re
            norm = re.sub(r'\d+', 'N', self.error_message.lower().strip())
            parts.append(norm)
        if self.file_path:
            parts.append(self.file_path)
        return "|".join(parts)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FailureReport":
        """Reconstruct a FailureReport from a dict produced by ``to_dict``."""
        from uuid import UUID as _UUID
        return cls(
            category=FailureCategory[data["category"]],
            error_message=data.get("error_message", ""),
            stack_trace=data.get("stack_trace", ""),
            file_path=data.get("file_path", ""),
            line_number=data.get("line_number"),
            reproduction_steps=data.get("reproduction_steps", []),
            environment=data.get("environment", {}),
            severity=data.get("severity", 1),
            attempts=data.get("attempts", 0),
            related_problem_ids=[
                _UUID(uid) for uid in data.get("related_problem_ids", [])
            ],
        )


@dataclass
class Problem:
    """A task posted by an agent seeking help from the community."""

    title: str
    description: str
    author_id: UUID
    tags: set[ProblemTag] = field(default_factory=lambda: {ProblemTag.GENERAL})
    id: UUID = field(default_factory=uuid4)
    status: ProblemStatus = ProblemStatus.OPEN
    priority: int = 0  # higher = more urgent
    bounty: int = 10  # reputation reward for solver

    # Privacy / access control
    sensitivity: Sensitivity = Sensitivity.INTERNAL

    # Tier gating — minimum model tier to claim this problem
    min_solver_tier: ModelTier | None = None

    # Optional constraints
    required_capabilities: set | None = None  # AgentCapability set
    max_solvers: int = 1
    deadline: datetime | None = None

    # Bookkeeping
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    claimed_by: list[UUID] = field(default_factory=list)
    solution_ids: list[UUID] = field(default_factory=list)
    accepted_solution_id: UUID | None = None

    # Arbitrary context the posting agent can attach
    context: dict[str, Any] = field(default_factory=dict)

    # Structured failure metadata (optional)
    failure_report: FailureReport | None = None

    # Decomposition / dependency graph
    parent_id: UUID | None = None             # set when this is a sub-problem
    sub_problem_ids: list[UUID] = field(default_factory=list)  # children
    depends_on: list[UUID] = field(default_factory=list)       # must be CLOSED first

    # ------------------------------------------------------------------
    # Lifecycle helpers
    # ------------------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self.status == ProblemStatus.OPEN

    @property
    def is_expired(self) -> bool:
        if self.deadline and datetime.now(timezone.utc) > self.deadline:
            return True
        return self.status == ProblemStatus.EXPIRED

    def claim(self, agent_id: UUID) -> None:
        if not self.is_open:
            raise ValueError(f"Problem {self.id} is not open (status={self.status.name})")
        if len(self.claimed_by) >= self.max_solvers:
            raise ValueError(f"Problem {self.id} already has max solvers")
        self.claimed_by.append(agent_id)
        self.status = ProblemStatus.CLAIMED

    def add_solution(self, solution_id: UUID) -> None:
        self.solution_ids.append(solution_id)
        self.status = ProblemStatus.SOLVED

    def accept(self, solution_id: UUID) -> None:
        if solution_id not in self.solution_ids:
            raise ValueError("Solution not associated with this problem")
        self.accepted_solution_id = solution_id
        self.status = ProblemStatus.CLOSED

    def reject_and_reopen(self) -> None:
        """Reject current solutions and re-open the problem."""
        self.claimed_by.clear()
        self.accepted_solution_id = None
        self.status = ProblemStatus.OPEN

    def request_revision(self) -> None:
        """Put the problem back to CLAIMED so the solver can resubmit."""
        self.status = ProblemStatus.CLAIMED

    def escalate(self) -> None:
        self.status = ProblemStatus.ESCALATED

    def expire(self) -> None:
        self.status = ProblemStatus.EXPIRED

    # ------------------------------------------------------------------
    # Dunder
    # ------------------------------------------------------------------

    def __hash__(self) -> int:
        return hash(self.id)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Problem):
            return self.id == other.id
        return NotImplemented

    def __str__(self) -> str:
        tags = ", ".join(t.name for t in sorted(self.tags, key=lambda t: t.name))
        return f"Problem({self.title!r}, status={self.status.name}, tags=[{tags}])"

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict for storage / transport."""
        d: dict[str, Any] = {
            "id": str(self.id),
            "title": self.title,
            "description": self.description,
            "author_id": str(self.author_id),
            "tags": [t.name for t in self.tags],
            "status": self.status.name,
            "priority": self.priority,
            "bounty": self.bounty,
            "sensitivity": self.sensitivity.name,
            "min_solver_tier": self.min_solver_tier.name if self.min_solver_tier else None,
            "max_solvers": self.max_solvers,
            "deadline": self.deadline.isoformat() if self.deadline else None,
            "created_at": self.created_at.isoformat(),
            "claimed_by": [str(uid) for uid in self.claimed_by],
            "solution_ids": [str(uid) for uid in self.solution_ids],
            "accepted_solution_id": str(self.accepted_solution_id) if self.accepted_solution_id else None,
            "context": self.context,
        }
        if self.failure_report is not None:
            d["failure_report"] = {
                "category": self.failure_report.category.name,
                "error_message": self.failure_report.error_message,
                "stack_trace": self.failure_report.stack_trace,
                "file_path": self.failure_report.file_path,
                "line_number": self.failure_report.line_number,
                "reproduction_steps": self.failure_report.reproduction_steps,
                "environment": self.failure_report.environment,
                "severity": self.failure_report.severity,
                "attempts": self.failure_report.attempts,
                "related_problem_ids": [str(uid) for uid in self.failure_report.related_problem_ids],
            }
        else:
            d["failure_report"] = None
        # Decomposition / dependency graph
        d["parent_id"] = str(self.parent_id) if self.parent_id else None
        d["sub_problem_ids"] = [str(uid) for uid in self.sub_problem_ids]
        d["depends_on"] = [str(uid) for uid in self.depends_on]
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Problem":
        """Reconstruct a Problem from a dict produced by ``to_dict``."""
        from uuid import UUID as _UUID
        from datetime import datetime as _dt

        failure_report = None
        if data.get("failure_report"):
            failure_report = FailureReport.from_dict(data["failure_report"])

        deadline = None
        if data.get("deadline"):
            deadline = _dt.fromisoformat(data["deadline"])

        min_tier = None
        if data.get("min_solver_tier"):
            min_tier = ModelTier[data["min_solver_tier"]]

        p = cls(
            title=data["title"],
            description=data["description"],
            author_id=_UUID(data["author_id"]),
            tags={ProblemTag[t] for t in data.get("tags", ["GENERAL"])},
            bounty=data.get("bounty", 10),
            sensitivity=Sensitivity[data.get("sensitivity", "INTERNAL")],
            min_solver_tier=min_tier,
            max_solvers=data.get("max_solvers", 1),
            deadline=deadline,
            context=data.get("context", {}),
            failure_report=failure_report,
        )
        # Override generated fields
        p.id = _UUID(data["id"])
        p.status = ProblemStatus[data["status"]]
        p.priority = data.get("priority", 0)
        p.created_at = _dt.fromisoformat(data["created_at"])
        p.claimed_by = [_UUID(uid) for uid in data.get("claimed_by", [])]
        p.solution_ids = [_UUID(uid) for uid in data.get("solution_ids", [])]
        if data.get("accepted_solution_id"):
            p.accepted_solution_id = _UUID(data["accepted_solution_id"])
        # Decomposition / dependency graph
        if data.get("parent_id"):
            p.parent_id = _UUID(data["parent_id"])
        p.sub_problem_ids = [_UUID(uid) for uid in data.get("sub_problem_ids", [])]
        p.depends_on = [_UUID(uid) for uid in data.get("depends_on", [])]
        return p
