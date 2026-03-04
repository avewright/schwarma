"""
Review — evaluation of a Solution by a peer agent.

Review types:
  • CORRECTNESS  — does the solution actually solve the problem?
  • GOOD_FAITH   — is the solution a genuine attempt (not spam / sabotage)?
  • PROOFREADING — language, formatting, clarity checks
  • QUALITY      — code quality, best practices, etc.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from typing import Any
from uuid import UUID, uuid4


class ReviewType(Enum):
    CORRECTNESS = auto()
    GOOD_FAITH = auto()
    PROOFREADING = auto()
    QUALITY = auto()


class ReviewVerdict(Enum):
    APPROVE = auto()
    REJECT = auto()
    REQUEST_CHANGES = auto()
    ABSTAIN = auto()


@dataclass
class Review:
    """An evaluation of a :class:`Solution` by a reviewer agent."""

    solution_id: UUID
    reviewer_id: UUID
    review_type: ReviewType
    verdict: ReviewVerdict
    body: str = ""  # written feedback
    confidence: float = 1.0  # 0.0–1.0 self-reported confidence
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @property
    def is_positive(self) -> bool:
        return self.verdict == ReviewVerdict.APPROVE

    @property
    def is_negative(self) -> bool:
        return self.verdict in (ReviewVerdict.REJECT, ReviewVerdict.REQUEST_CHANGES)

    # ------------------------------------------------------------------
    # Dunder
    # ------------------------------------------------------------------

    def __hash__(self) -> int:
        return hash(self.id)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Review):
            return self.id == other.id
        return NotImplemented

    def __str__(self) -> str:
        return (
            f"Review(solution={self.solution_id}, type={self.review_type.name}, "
            f"verdict={self.verdict.name}, confidence={self.confidence:.1f})"
        )

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict for storage / transport."""
        return {
            "id": str(self.id),
            "solution_id": str(self.solution_id),
            "reviewer_id": str(self.reviewer_id),
            "review_type": self.review_type.name,
            "verdict": self.verdict.name,
            "body": self.body,
            "confidence": self.confidence,
            "created_at": self.created_at.isoformat(),
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Review":
        """Reconstruct a Review from a dict produced by ``to_dict``."""
        r = cls(
            solution_id=UUID(data["solution_id"]),
            reviewer_id=UUID(data["reviewer_id"]),
            review_type=ReviewType[data["review_type"]],
            verdict=ReviewVerdict[data["verdict"]],
            body=data.get("body", ""),
            confidence=data.get("confidence", 1.0),
            metadata=data.get("metadata", {}),
        )
        r.id = UUID(data["id"])
        r.created_at = datetime.fromisoformat(data["created_at"])
        return r
