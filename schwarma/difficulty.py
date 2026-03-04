"""
DifficultyEstimator — empirical difficulty scoring for problems.

Instead of relying solely on author-declared difficulty, we track
empirical signals:

  • **rejection_count**: how many solutions were rejected before one
    was accepted?  More rejections → harder problem.
  • **attempt_count**: how many agents attempted?
  • **solve_time**: median time from claim to accepted solution.
  • **solver_tier**: what tier of model ended up solving it?

These signals combine into a single ``difficulty_score`` (0.0–3.0) that
feeds back into the SkillTracker as the ``difficulty`` parameter for
Bayesian updates — so solving a hard problem gives a bigger μ boost.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from schwarma.agent import ModelTier

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class DifficultyConfig:
    """Tuneable knobs for the difficulty estimator."""

    # Weight of each signal in the composite score
    w_rejections: float = 0.4
    w_attempts: float = 0.2
    w_solve_time: float = 0.2
    w_solver_tier: float = 0.2

    # Normalization constants
    max_rejections: int = 10         # beyond this, we saturate
    max_attempts: int = 10
    reference_solve_seconds: float = 300.0  # 5 min baseline

    # The difficulty score is clamped to [min_difficulty, max_difficulty]
    min_difficulty: float = 0.3
    max_difficulty: float = 3.0


# ---------------------------------------------------------------------------
# Data tracking
# ---------------------------------------------------------------------------

@dataclass
class ProblemDifficultyRecord:
    """Accumulated signals for a single problem."""

    problem_id: UUID
    rejection_count: int = 0
    attempt_count: int = 0
    solve_times: list[float] = field(default_factory=list)  # seconds
    solver_tier: ModelTier | None = None   # tier of the accepted solver
    accepted: bool = False
    first_claimed_at: datetime | None = None
    accepted_at: datetime | None = None

    @property
    def median_solve_time(self) -> float | None:
        if not self.solve_times:
            return None
        sorted_times = sorted(self.solve_times)
        mid = len(sorted_times) // 2
        if len(sorted_times) % 2 == 0:
            return (sorted_times[mid - 1] + sorted_times[mid]) / 2
        return sorted_times[mid]

    def to_dict(self) -> dict[str, Any]:
        return {
            "problem_id": str(self.problem_id),
            "rejection_count": self.rejection_count,
            "attempt_count": self.attempt_count,
            "median_solve_time": self.median_solve_time,
            "solver_tier": self.solver_tier.name if self.solver_tier else None,
            "accepted": self.accepted,
        }


# ---------------------------------------------------------------------------
# DifficultyEstimator
# ---------------------------------------------------------------------------

class DifficultyEstimator:
    """
    Tracks empirical difficulty for problems and computes a difficulty score.

    Usage::

        estimator = DifficultyEstimator()
        estimator.record_attempt(problem_id)
        estimator.record_rejection(problem_id)
        estimator.record_acceptance(problem_id, solver_tier=ModelTier.PREMIUM, solve_seconds=120)
        score = estimator.difficulty_score(problem_id)  # → 0.3 – 3.0
    """

    def __init__(self, config: DifficultyConfig | None = None) -> None:
        self.config = config or DifficultyConfig()
        self._records: dict[UUID, ProblemDifficultyRecord] = {}

    # ------------------------------------------------------------------
    # Recording signals
    # ------------------------------------------------------------------

    def record_attempt(self, problem_id: UUID) -> None:
        """An agent attempted (claimed) this problem."""
        rec = self._get_or_create(problem_id)
        rec.attempt_count += 1
        if rec.first_claimed_at is None:
            rec.first_claimed_at = datetime.now(timezone.utc)

    def record_rejection(self, problem_id: UUID) -> None:
        """A solution was rejected for this problem."""
        rec = self._get_or_create(problem_id)
        rec.rejection_count += 1

    def record_acceptance(
        self,
        problem_id: UUID,
        *,
        solver_tier: ModelTier | None = None,
        solve_seconds: float | None = None,
    ) -> None:
        """A solution was accepted for this problem."""
        rec = self._get_or_create(problem_id)
        rec.accepted = True
        rec.accepted_at = datetime.now(timezone.utc)
        if solver_tier is not None:
            rec.solver_tier = solver_tier
        if solve_seconds is not None:
            rec.solve_times.append(solve_seconds)

    def record_solve_time(self, problem_id: UUID, seconds: float) -> None:
        """Record a solve duration (claim→solution) regardless of verdict."""
        rec = self._get_or_create(problem_id)
        rec.solve_times.append(seconds)

    # ------------------------------------------------------------------
    # Scoring
    # ------------------------------------------------------------------

    def difficulty_score(self, problem_id: UUID) -> float:
        """Compute the empirical difficulty score for a problem.

        Returns a value in [min_difficulty, max_difficulty] (default 0.3–3.0).
        If no data exists, returns 1.0 (neutral difficulty).
        """
        rec = self._records.get(problem_id)
        if rec is None:
            return 1.0  # unknown → neutral

        cfg = self.config

        # 1. Rejection signal: more rejections → harder
        rej_norm = min(rec.rejection_count / max(cfg.max_rejections, 1), 1.0)

        # 2. Attempt signal: more attempts → harder
        att_norm = min(rec.attempt_count / max(cfg.max_attempts, 1), 1.0)

        # 3. Solve time signal: longer → harder
        time_norm = 0.5  # default (neutral) if no data
        median = rec.median_solve_time
        if median is not None:
            # ratio to reference; cap at 3x
            time_norm = min(median / cfg.reference_solve_seconds, 3.0) / 3.0

        # 4. Solver tier signal: if a PREMIUM model solved it, it's probably
        #    harder; if LIGHTWEIGHT solved it, probably easy
        tier_norm = 0.5
        if rec.solver_tier is not None and rec.solver_tier != ModelTier.SPECIALIZED:
            tier_norm = (rec.solver_tier.value - 1) / 2.0  # 0.0, 0.5, 1.0

        raw = (
            cfg.w_rejections * rej_norm
            + cfg.w_attempts * att_norm
            + cfg.w_solve_time * time_norm
            + cfg.w_solver_tier * tier_norm
        )

        # Scale to [min, max]
        score = cfg.min_difficulty + raw * (cfg.max_difficulty - cfg.min_difficulty)
        return max(cfg.min_difficulty, min(cfg.max_difficulty, score))

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_record(self, problem_id: UUID) -> ProblemDifficultyRecord | None:
        return self._records.get(problem_id)

    def hardest_problems(self, top_n: int = 10) -> list[tuple[UUID, float]]:
        """Return the top-N hardest problems by difficulty score."""
        scored = [
            (pid, self.difficulty_score(pid))
            for pid in self._records
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_n]

    @property
    def tracked_count(self) -> int:
        return len(self._records)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get_or_create(self, problem_id: UUID) -> ProblemDifficultyRecord:
        if problem_id not in self._records:
            self._records[problem_id] = ProblemDifficultyRecord(problem_id=problem_id)
        return self._records[problem_id]
