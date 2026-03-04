"""
TriageRouter — routes problems to the best-fit agents.

Strategies:
  • CAPABILITY_MATCH  — match problem tags/capabilities to agent capabilities
  • ROUND_ROBIN       — simple rotation for even load
  • REPUTATION_FIRST  — prefer agents with highest reputation
  • LEAST_BUSY        — prefer agents with fewest active claims
  • COMPOSITE         — weighted combination of the above
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Sequence
from uuid import UUID

from schwarma.agent import Agent, AgentCapability, ModelTier
from schwarma.problem import Problem, ProblemTag

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tag → Capability mapping (heuristic defaults; override via TriageConfig)
# ---------------------------------------------------------------------------

DEFAULT_TAG_CAP_MAP: dict[ProblemTag, set[AgentCapability]] = {
    ProblemTag.BUG: {AgentCapability.DEBUGGING, AgentCapability.CODE_REVIEW},
    ProblemTag.FEATURE: {AgentCapability.CODE_GENERATION, AgentCapability.ARCHITECTURE},
    ProblemTag.QUESTION: {AgentCapability.GENERAL, AgentCapability.RESEARCH},
    ProblemTag.REVIEW_REQUEST: {AgentCapability.CODE_REVIEW},
    ProblemTag.PROOFREAD: {AgentCapability.PROOFREADING},
    ProblemTag.GOOD_FAITH: {AgentCapability.GOOD_FAITH_CHECK},
    ProblemTag.ARCHITECTURE: {AgentCapability.ARCHITECTURE},
    ProblemTag.RESEARCH: {AgentCapability.RESEARCH},
    ProblemTag.OPTIMIZATION: {AgentCapability.CODE_GENERATION, AgentCapability.DEBUGGING},
    ProblemTag.SECURITY: {AgentCapability.SECURITY_AUDIT},
    ProblemTag.GENERAL: {AgentCapability.GENERAL},
}


class TriageStrategy(Enum):
    CAPABILITY_MATCH = auto()
    ROUND_ROBIN = auto()
    REPUTATION_FIRST = auto()
    LEAST_BUSY = auto()
    COMPOSITE = auto()


@dataclass
class TriageConfig:
    strategy: TriageStrategy = TriageStrategy.COMPOSITE
    tag_capability_map: dict[ProblemTag, set[AgentCapability]] = field(
        default_factory=lambda: dict(DEFAULT_TAG_CAP_MAP)
    )
    # Weights for COMPOSITE strategy (must sum to 1.0 roughly)
    w_capability: float = 0.4
    w_reputation: float = 0.3
    w_load: float = 0.2
    w_random: float = 0.1  # jitter to avoid herding


class TriageRouter:
    """Selects the best agents to handle a problem."""

    def __init__(
        self,
        config: TriageConfig | None = None,
        reputation_fn: "((UUID) -> int) | None" = None,
        skill_rating_fn: "((UUID, Problem) -> float) | None" = None,
        effective_tier_fn: "((Agent) -> ModelTier) | None" = None,
    ) -> None:
        self.config = config or TriageConfig()
        self._reputation_fn = reputation_fn or (lambda _: 50)
        self._skill_rating_fn = skill_rating_fn  # (agent_id, problem) → rating
        self._effective_tier_fn = effective_tier_fn  # (agent) → ModelTier
        self._round_robin_idx = 0

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def rank(
        self,
        problem: Problem,
        candidates: Sequence[Agent],
        top_n: int = 3,
    ) -> list[Agent]:
        """Return up to *top_n* agents ranked by suitability for *problem*.

        Agents who already claimed this problem are excluded.
        """
        available = [
            a for a in candidates
            if a.id not in problem.claimed_by and a.id != problem.author_id
        ]
        if not available:
            return []

        strategy = self.config.strategy

        if strategy == TriageStrategy.CAPABILITY_MATCH:
            scored = [(a, self._score_capability(problem, a)) for a in available]
        elif strategy == TriageStrategy.ROUND_ROBIN:
            return self._round_robin(available, top_n)
        elif strategy == TriageStrategy.REPUTATION_FIRST:
            scored = [(a, self._reputation_fn(a.id)) for a in available]
        elif strategy == TriageStrategy.LEAST_BUSY:
            scored = [(a, -a.active_count) for a in available]
        elif strategy == TriageStrategy.COMPOSITE:
            scored = [(a, self._composite_score(problem, a)) for a in available]
        else:
            scored = [(a, 0.0) for a in available]

        scored.sort(key=lambda pair: pair[1], reverse=True)
        return [a for a, _ in scored[:top_n]]

    # ------------------------------------------------------------------
    # Scoring helpers
    # ------------------------------------------------------------------

    def _required_capabilities(self, problem: Problem) -> set[AgentCapability]:
        """Derive required capabilities from problem tags + explicit requirements."""
        caps: set[AgentCapability] = set()
        if problem.required_capabilities:
            caps.update(problem.required_capabilities)
        for tag in problem.tags:
            caps.update(self.config.tag_capability_map.get(tag, set()))
        return caps or {AgentCapability.GENERAL}

    def _score_capability(self, problem: Problem, agent: Agent) -> float:
        needed = self._required_capabilities(problem)
        if not needed:
            return 1.0
        overlap = len(agent.capabilities & needed)
        return overlap / len(needed)

    def _composite_score(self, problem: Problem, agent: Agent) -> float:
        cfg = self.config
        cap_score = self._score_capability(problem, agent)
        rep_score = self._reputation_fn(agent.id) / 100.0  # normalise
        load_score = 1.0 / (1.0 + agent.active_count)
        jitter = random.random()

        # Skill bonus: if we have a skill rating function, blend it in
        skill_bonus = 0.0
        if self._skill_rating_fn is not None:
            # Normalise: default conservative rating is ~8.3, good is ~25+
            raw_skill = self._skill_rating_fn(agent.id, problem)
            skill_bonus = max(0.0, min(raw_skill / 30.0, 1.0)) * 0.2

        # Tier bonus: use effective tier if available, otherwise declared
        tier_bonus = 0.0
        effective = (
            self._effective_tier_fn(agent)
            if self._effective_tier_fn is not None
            else agent.model_tier
        )
        if problem.min_solver_tier is not None:
            if effective == ModelTier.SPECIALIZED:
                tier_bonus = 0.2  # always a reasonable match
            elif effective.value >= problem.min_solver_tier.value:
                tier_bonus = 0.2
            else:
                tier_bonus = -0.3  # discourage under-qualified agents

        return (
            cfg.w_capability * cap_score
            + cfg.w_reputation * rep_score
            + cfg.w_load * load_score
            + cfg.w_random * jitter
            + tier_bonus
            + skill_bonus
        )

    def _round_robin(self, available: list[Agent], top_n: int) -> list[Agent]:
        n = len(available)
        result: list[Agent] = []
        for i in range(min(top_n, n)):
            idx = (self._round_robin_idx + i) % n
            result.append(available[idx])
        self._round_robin_idx = (self._round_robin_idx + top_n) % max(n, 1)
        return result
