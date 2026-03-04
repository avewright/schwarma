"""
CalibrationBank — reference problems with known-good solutions.

Calibration problems are injected into the work stream to independently
verify an agent's actual skill.  The agent doesn't know which problems are
calibration vs. real, so it can't game the system.

Design:
  • A CalibrationProblem wraps a Problem + known-good solution + difficulty.
  • The bank stores a pool of calibration problems per capability.
  • The Exchange can draw a calibration problem for an agent and later
    score the agent's solution against the known-good answer.
  • Results feed back into the SkillTracker as high-confidence data points.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from typing import Any, Callable, Sequence
from uuid import UUID, uuid4

from schwarma.agent import AgentCapability

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

class CalibrationDifficulty(Enum):
    """Rough difficulty bracket for calibration problems."""

    EASY = 1
    MEDIUM = 2
    HARD = 3


@dataclass
class CalibrationProblem:
    """A reference problem with a known-good solution."""

    title: str
    description: str
    known_solution: str
    capabilities: set[AgentCapability]
    difficulty: CalibrationDifficulty = CalibrationDifficulty.MEDIUM
    id: UUID = field(default_factory=uuid4)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __hash__(self) -> int:
        return hash(self.id)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, CalibrationProblem):
            return self.id == other.id
        return NotImplemented


class CalibrationVerdict(Enum):
    """Result of comparing an agent's answer to the known-good solution."""

    PASS = auto()
    FAIL = auto()
    PARTIAL = auto()


@dataclass
class CalibrationResult:
    """Record of a single calibration attempt."""

    agent_id: UUID
    calibration_problem_id: UUID
    verdict: CalibrationVerdict
    agent_answer: str
    score: float = 0.0          # 0.0 – 1.0
    evaluated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    id: UUID = field(default_factory=uuid4)


# Scorer signature: (agent_answer, known_solution) → (verdict, score)
ScorerFn = Callable[[str, str], tuple[CalibrationVerdict, float]]


def default_scorer(agent_answer: str, known_solution: str) -> tuple[CalibrationVerdict, float]:
    """Simple exact-match scorer (placeholder — real systems use LLM-as-judge)."""
    a = agent_answer.strip().lower()
    k = known_solution.strip().lower()
    if a == k:
        return CalibrationVerdict.PASS, 1.0
    # Check substring containment as partial credit
    if k in a or a in k:
        return CalibrationVerdict.PARTIAL, 0.5
    return CalibrationVerdict.FAIL, 0.0


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class CalibrationConfig:
    """Tuneable knobs for the calibration system."""

    # Probability that any given claim is replaced with a calibration problem
    injection_probability: float = 0.1

    # Max calibration problems per agent before we stop injecting
    max_per_agent: int = 20

    # Pass threshold — score must be >= this to count as a win
    pass_threshold: float = 0.6

    # Scorer function
    scorer: ScorerFn = field(default_factory=lambda: default_scorer)


# ---------------------------------------------------------------------------
# CalibrationBank
# ---------------------------------------------------------------------------

class CalibrationBank:
    """
    Pool of reference problems for skill verification.

    Usage::

        bank = CalibrationBank()
        bank.add_problem(CalibrationProblem(...))
        prob = bank.draw(agent_id, {AgentCapability.DEBUGGING})
        result = bank.evaluate(agent_id, prob.id, agent_answer="...")
    """

    def __init__(self, config: CalibrationConfig | None = None) -> None:
        self.config = config or CalibrationConfig()
        # capability → list of calibration problems
        self._problems: dict[AgentCapability, list[CalibrationProblem]] = {}
        # All problems by ID for quick lookup
        self._by_id: dict[UUID, CalibrationProblem] = {}
        # Track which calibrations each agent has seen: agent_id → set of cal_problem_ids
        self._seen: dict[UUID, set[UUID]] = {}
        # History of results
        self._results: list[CalibrationResult] = []

    # ------------------------------------------------------------------
    # Bank management
    # ------------------------------------------------------------------

    def add_problem(self, problem: CalibrationProblem) -> None:
        """Add a calibration problem to the bank."""
        self._by_id[problem.id] = problem
        for cap in problem.capabilities:
            self._problems.setdefault(cap, []).append(problem)
        logger.debug("CalibrationBank: added %s (%s)", problem.title, problem.id)

    def remove_problem(self, problem_id: UUID) -> None:
        """Remove a calibration problem."""
        problem = self._by_id.pop(problem_id, None)
        if problem is None:
            return
        for cap in problem.capabilities:
            cap_list = self._problems.get(cap, [])
            self._problems[cap] = [p for p in cap_list if p.id != problem_id]

    @property
    def problem_count(self) -> int:
        return len(self._by_id)

    def problems_for(self, capability: AgentCapability) -> list[CalibrationProblem]:
        """All calibration problems tagged with *capability*."""
        return list(self._problems.get(capability, []))

    # ------------------------------------------------------------------
    # Drawing
    # ------------------------------------------------------------------

    def draw(
        self,
        agent_id: UUID,
        capabilities: set[AgentCapability],
        *,
        difficulty: CalibrationDifficulty | None = None,
    ) -> CalibrationProblem | None:
        """Draw a calibration problem the agent hasn't seen yet.

        Returns ``None`` if no unseen problems are available for the
        requested capabilities.
        """
        seen = self._seen.get(agent_id, set())

        # Check max cap
        if len(seen) >= self.config.max_per_agent:
            return None

        candidates: list[CalibrationProblem] = []
        for cap in capabilities:
            for p in self._problems.get(cap, []):
                if p.id not in seen:
                    if difficulty is None or p.difficulty == difficulty:
                        candidates.append(p)

        # Deduplicate
        unique = list({p.id: p for p in candidates}.values())
        if not unique:
            return None

        chosen = random.choice(unique)

        # Record that this agent has seen it
        self._seen.setdefault(agent_id, set()).add(chosen.id)

        logger.debug(
            "CalibrationBank: drew %s for agent %s",
            chosen.title, agent_id,
        )
        return chosen

    def should_inject(self) -> bool:
        """Roll the dice — should we inject a calibration problem right now?"""
        return random.random() < self.config.injection_probability

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------

    def evaluate(
        self,
        agent_id: UUID,
        calibration_problem_id: UUID,
        agent_answer: str,
    ) -> CalibrationResult:
        """Score an agent's answer against the known-good solution.

        Returns a :class:`CalibrationResult` and stores it in history.
        """
        problem = self._by_id.get(calibration_problem_id)
        if problem is None:
            raise ValueError(f"Unknown calibration problem {calibration_problem_id}")

        verdict, score = self.config.scorer(agent_answer, problem.known_solution)

        result = CalibrationResult(
            agent_id=agent_id,
            calibration_problem_id=calibration_problem_id,
            verdict=verdict,
            agent_answer=agent_answer,
            score=score,
        )
        self._results.append(result)

        logger.info(
            "Calibration result: agent=%s problem=%s verdict=%s score=%.2f",
            agent_id, problem.title, verdict.name, score,
        )
        return result

    def is_pass(self, result: CalibrationResult) -> bool:
        """Does this result meet the pass threshold?"""
        return result.score >= self.config.pass_threshold

    # ------------------------------------------------------------------
    # History queries
    # ------------------------------------------------------------------

    def results_for_agent(self, agent_id: UUID) -> list[CalibrationResult]:
        """All calibration results for an agent, oldest first."""
        return [r for r in self._results if r.agent_id == agent_id]

    def pass_rate(self, agent_id: UUID) -> float:
        """Fraction of calibration attempts that passed.  0.0 if none."""
        results = self.results_for_agent(agent_id)
        if not results:
            return 0.0
        passes = sum(1 for r in results if self.is_pass(r))
        return passes / len(results)

    def agent_seen_count(self, agent_id: UUID) -> int:
        """How many calibration problems has this agent seen?"""
        return len(self._seen.get(agent_id, set()))

    @property
    def total_results(self) -> int:
        return len(self._results)
