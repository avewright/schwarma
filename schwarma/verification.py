"""
Verification oracle protocol — objective automated solution verification.

Defines a protocol that external verification systems (test runners, sandboxes,
static analysers) can implement.  When an oracle is configured on the Exchange,
solutions are verified *before* the peer-review cycle begins.  An oracle pass
counts as one review; an oracle failure can optionally auto-reject.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from typing import Any, Protocol, runtime_checkable

from schwarma.problem import Problem
from schwarma.solution import Solution


class VerificationStatus(Enum):
    """Outcome of an oracle verification run."""

    PASSED = auto()
    FAILED = auto()
    ERROR = auto()   # oracle itself crashed / timed out
    SKIPPED = auto()  # oracle chose not to evaluate


@dataclass(frozen=True)
class VerificationResult:
    """Result returned by a :class:`VerificationOracle`."""

    status: VerificationStatus
    passed_tests: int = 0
    failed_tests: int = 0
    stdout: str = ""
    stderr: str = ""
    execution_time_s: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_pass(self) -> bool:
        return self.status == VerificationStatus.PASSED

    @property
    def is_fail(self) -> bool:
        return self.status == VerificationStatus.FAILED


@runtime_checkable
class VerificationOracle(Protocol):
    """Protocol that external verification systems implement.

    The exchange calls ``verify`` after a solution is submitted but before
    peer review begins.  Implementations might run a test suite in a sandbox,
    invoke a linter, or call an external grading API.
    """

    async def verify(
        self,
        solution: Solution,
        problem: Problem,
    ) -> VerificationResult:
        """Evaluate *solution* against *problem* and return a result."""
        ...
