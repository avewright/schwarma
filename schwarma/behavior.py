"""
Behavioral analysis — detecting anomalous agent patterns.

Monitors agent activity to identify:
  • Rubber-stamp reviewers (approve everything without genuine evaluation)
  • Collusion pairs (two agents suspiciously interacting with each other)
  • Unusual solve speed (too fast = probably not genuine)
  • Lopsided activity (only reviews, never solves, or vice versa)

This module doesn't *enforce* anything directly — it produces flags
that the Exchange or an operator can act on (suspend, weight-reduce,
investigate).
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AnomalyFlag:
    """A detected behavioral anomaly."""

    agent_id: UUID
    kind: str          # e.g. "rubber_stamp", "collusion", "speed"
    severity: float    # 0.0–1.0, higher = more suspicious
    detail: str = ""
    related_agent_id: UUID | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __str__(self) -> str:
        return f"Anomaly({self.kind}, agent={self.agent_id}, severity={self.severity:.2f})"


@dataclass
class BehaviorConfig:
    """Tuneable thresholds for anomaly detection."""

    # Rubber-stamp: flag if approval rate exceeds this after N reviews
    max_approval_rate: float = 0.92
    min_reviews_for_rate_check: int = 8

    # Collusion: flag if pairwise interaction count exceeds this
    max_pairwise_interactions: int = 5

    # Speed: flag if average solve time is under this
    min_solve_seconds: float = 5.0

    # Activity balance: flag if ratio of solves to reviews is extreme
    min_activity_ratio: float = 0.15  # at least 15% of total activity in each category


class BehaviorAnalyzer:
    """Tracks and analyzes agent behavior over time.

    Call ``record_*`` methods as events happen, then call ``analyze()`` or
    individual ``check_*`` methods to get anomaly flags.
    """

    def __init__(self, config: BehaviorConfig | None = None) -> None:
        self.config = config or BehaviorConfig()

        # Per-agent review verdicts: agent_id → list of (verdict_str, solution_author_id)
        self._review_verdicts: dict[UUID, list[tuple[str, UUID]]] = defaultdict(list)

        # Per-agent solve timestamps: agent_id → list of (claimed_at, solved_at)
        self._solve_times: dict[UUID, list[tuple[datetime, datetime]]] = defaultdict(list)

        # Pairwise interaction counts: frozenset({a, b}) → count
        self._pairwise: Counter[frozenset[UUID]] = Counter()

        # Activity counters
        self._solves: Counter[UUID] = Counter()
        self._reviews: Counter[UUID] = Counter()

        # Accumulated flags
        self._flags: list[AnomalyFlag] = []

    # ------------------------------------------------------------------
    # Recording events
    # ------------------------------------------------------------------

    def record_review(
        self,
        reviewer_id: UUID,
        solution_author_id: UUID,
        verdict: str,
    ) -> None:
        """Record that *reviewer_id* reviewed a solution by *solution_author_id*."""
        self._review_verdicts[reviewer_id].append((verdict, solution_author_id))
        self._reviews[reviewer_id] += 1
        pair = frozenset({reviewer_id, solution_author_id})
        self._pairwise[pair] += 1

    def record_solve(
        self,
        solver_id: UUID,
        problem_author_id: UUID,
        claimed_at: datetime,
        solved_at: datetime,
    ) -> None:
        """Record that *solver_id* solved a problem by *problem_author_id*."""
        self._solve_times[solver_id].append((claimed_at, solved_at))
        self._solves[solver_id] += 1
        pair = frozenset({solver_id, problem_author_id})
        self._pairwise[pair] += 1

    # ------------------------------------------------------------------
    # Analysis
    # ------------------------------------------------------------------

    def analyze(self, agent_id: UUID) -> list[AnomalyFlag]:
        """Run all checks for *agent_id* and return any new flags."""
        flags: list[AnomalyFlag] = []
        flags.extend(self.check_rubber_stamp(agent_id))
        flags.extend(self.check_collusion(agent_id))
        flags.extend(self.check_solve_speed(agent_id))
        flags.extend(self.check_activity_balance(agent_id))
        self._flags.extend(flags)
        return flags

    def analyze_all(self, agent_ids: list[UUID]) -> list[AnomalyFlag]:
        """Batch analysis for multiple agents."""
        all_flags: list[AnomalyFlag] = []
        for aid in agent_ids:
            all_flags.extend(self.analyze(aid))
        return all_flags

    def check_rubber_stamp(self, agent_id: UUID) -> list[AnomalyFlag]:
        """Detect reviewers who approve (almost) everything."""
        verdicts = self._review_verdicts.get(agent_id, [])
        if len(verdicts) < self.config.min_reviews_for_rate_check:
            return []

        approvals = sum(1 for v, _ in verdicts if v.upper() in ("APPROVE", "PASS"))
        rate = approvals / len(verdicts)

        if rate > self.config.max_approval_rate:
            return [AnomalyFlag(
                agent_id=agent_id,
                kind="rubber_stamp",
                severity=min(1.0, (rate - self.config.max_approval_rate) * 10),
                detail=f"Approval rate {rate:.0%} over {len(verdicts)} reviews",
            )]
        return []

    def check_collusion(self, agent_id: UUID) -> list[AnomalyFlag]:
        """Detect suspiciously frequent pairwise interactions."""
        flags: list[AnomalyFlag] = []
        threshold = self.config.max_pairwise_interactions
        for pair, count in self._pairwise.items():
            if agent_id in pair and count > threshold:
                other = next(iter(pair - {agent_id})) if len(pair) > 1 else agent_id
                if other == agent_id:
                    continue
                flags.append(AnomalyFlag(
                    agent_id=agent_id,
                    kind="collusion",
                    severity=min(1.0, (count - threshold) / threshold),
                    detail=f"{count} interactions with agent {other}",
                    related_agent_id=other,
                ))
        return flags

    def check_solve_speed(self, agent_id: UUID) -> list[AnomalyFlag]:
        """Detect suspiciously fast solve times."""
        times = self._solve_times.get(agent_id, [])
        if len(times) < 3:
            return []

        durations = [(s - c).total_seconds() for c, s in times]
        avg = sum(durations) / len(durations)

        if avg < self.config.min_solve_seconds:
            return [AnomalyFlag(
                agent_id=agent_id,
                kind="speed",
                severity=min(1.0, self.config.min_solve_seconds / max(avg, 0.1)),
                detail=f"Average solve time {avg:.1f}s (min {self.config.min_solve_seconds}s)",
            )]
        return []

    def check_activity_balance(self, agent_id: UUID) -> list[AnomalyFlag]:
        """Detect extreme imbalance between solving and reviewing."""
        solves = self._solves.get(agent_id, 0)
        reviews = self._reviews.get(agent_id, 0)
        total = solves + reviews

        if total < 5:
            return []

        ratio = min(solves, reviews) / total
        if ratio < self.config.min_activity_ratio:
            dominant = "reviewing" if reviews > solves else "solving"
            return [AnomalyFlag(
                agent_id=agent_id,
                kind="activity_imbalance",
                severity=min(1.0, (self.config.min_activity_ratio - ratio) * 5),
                detail=f"Mostly {dominant} ({solves} solves, {reviews} reviews)",
            )]
        return []

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    @property
    def flags(self) -> list[AnomalyFlag]:
        return list(self._flags)

    def flags_for(self, agent_id: UUID) -> list[AnomalyFlag]:
        return [f for f in self._flags if f.agent_id == agent_id]

    def approval_rate(self, agent_id: UUID) -> float | None:
        """Current approval rate, or None if no reviews recorded."""
        verdicts = self._review_verdicts.get(agent_id, [])
        if not verdicts:
            return None
        approvals = sum(1 for v, _ in verdicts if v.upper() in ("APPROVE", "PASS"))
        return approvals / len(verdicts)

    def pairwise_count(self, agent_a: UUID, agent_b: UUID) -> int:
        return self._pairwise[frozenset({agent_a, agent_b})]
