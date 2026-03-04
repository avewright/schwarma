"""
SkillTracker — per-capability Bayesian skill ratings.

Inspired by TrueSkill / Glicko-2, each agent gets a (μ, σ) pair **per
capability** it has exercised.  μ is the estimated skill, σ is our
uncertainty about that estimate.

Key concepts:

  • **conservative_rating** = μ − k·σ  (the "floor" we're confident about)
  • **effective_tier** = the ModelTier derived from an agent's proven track
    record, replacing the self-declared ``model_tier`` as the gating value.
  • **σ-decay**: after a period of inactivity, σ drifts back up, reflecting
    our growing uncertainty.
  • **outcome recording**: on each ACCEPT/REJECT, we update the relevant
    capability ratings using a simplified Bayesian update.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from uuid import UUID

from schwarma.agent import AgentCapability, ModelTier

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_MU = 25.0          # TrueSkill default starting mean
DEFAULT_SIGMA = 25.0 / 3   # ≈ 8.33 — high initial uncertainty
K_CONSERVATIVE = 2.0        # multiplier for conservative_rating = μ − k·σ
MIN_SIGMA = 0.5             # uncertainty floor (never fully certain)
DECAY_SIGMA_PER_DAY = 0.05  # σ increases by this per inactive day
MAX_SIGMA = DEFAULT_SIGMA   # σ never exceeds starting value

# TrueSkill-style update parameters
BETA = DEFAULT_SIGMA / 2    # performance variance (≈ 4.17)
TAU = DEFAULT_SIGMA / 100   # dynamic factor (tiny re-inflation per update)

# Effective-tier thresholds on conservative_rating
TIER_THRESHOLDS: dict[ModelTier, float] = {
    ModelTier.LIGHTWEIGHT: 0.0,
    ModelTier.STANDARD: 20.0,
    ModelTier.PREMIUM: 30.0,
    # SPECIALIZED is never auto-derived — it stays as-is
}


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class SkillRating:
    """A single (μ, σ) rating for one agent × one capability."""

    mu: float = DEFAULT_MU
    sigma: float = DEFAULT_SIGMA
    wins: int = 0            # accepted solutions in this capability
    losses: int = 0          # rejected solutions in this capability
    last_active: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def conservative_rating(self) -> float:
        """Lower-bound estimate: μ − k·σ."""
        return self.mu - K_CONSERVATIVE * self.sigma

    @property
    def total_outcomes(self) -> int:
        return self.wins + self.losses

    def to_dict(self) -> dict:
        return {
            "mu": round(self.mu, 3),
            "sigma": round(self.sigma, 3),
            "conservative_rating": round(self.conservative_rating, 3),
            "wins": self.wins,
            "losses": self.losses,
            "last_active": self.last_active.isoformat(),
        }


@dataclass
class SkillConfig:
    """Tuneable knobs for the skill system."""

    default_mu: float = DEFAULT_MU
    default_sigma: float = DEFAULT_SIGMA
    k_conservative: float = K_CONSERVATIVE
    min_sigma: float = MIN_SIGMA
    decay_sigma_per_day: float = DECAY_SIGMA_PER_DAY
    max_sigma: float = MAX_SIGMA
    beta: float = BETA
    tau: float = TAU

    # How many outcomes before we trust the rating enough to derive a tier
    min_outcomes_for_tier: int = 5

    # Probationary period: new agents must complete this many tasks before
    # their effective tier can exceed LIGHTWEIGHT
    probation_outcomes: int = 3

    # Tier thresholds (conservative_rating → ModelTier)
    tier_thresholds: dict[ModelTier, float] = field(
        default_factory=lambda: dict(TIER_THRESHOLDS)
    )


# ---------------------------------------------------------------------------
# SkillTracker
# ---------------------------------------------------------------------------

class SkillTracker:
    """
    Tracks per-capability skill ratings for all agents.

    Usage::

        tracker = SkillTracker()
        tracker.record_outcome(agent_id, {AgentCapability.DEBUGGING}, won=True)
        tier = tracker.effective_tier(agent_id, declared_tier=ModelTier.PREMIUM)
        rating = tracker.conservative_rating(agent_id, AgentCapability.DEBUGGING)
    """

    def __init__(self, config: SkillConfig | None = None) -> None:
        self.config = config or SkillConfig()
        # agent_id → capability → SkillRating
        self._ratings: dict[UUID, dict[AgentCapability, SkillRating]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record_outcome(
        self,
        agent_id: UUID,
        capabilities: set[AgentCapability],
        *,
        won: bool,
        difficulty: float = 1.0,
    ) -> None:
        """Update skill ratings after a solution is accepted (won) or rejected.

        *capabilities* — the set of capabilities that were relevant to the
        problem.  All relevant ratings are updated.

        *difficulty* — multiplier for the update magnitude.  Values > 1 mean
        a harder problem (bigger reward/penalty); values < 1 mean easier.
        """
        for cap in capabilities:
            rating = self._get_or_create(agent_id, cap)
            self._apply_decay(rating)
            self._bayesian_update(rating, won=won, difficulty=difficulty)
            rating.last_active = datetime.now(timezone.utc)
            if won:
                rating.wins += 1
            else:
                rating.losses += 1

        logger.debug(
            "Skill update for %s: caps=%s won=%s difficulty=%.2f",
            agent_id, [c.name for c in capabilities], won, difficulty,
        )

    def get_rating(
        self,
        agent_id: UUID,
        capability: AgentCapability,
    ) -> SkillRating:
        """Return the current SkillRating for an agent × capability.

        Returns a default rating if the agent has no history for this cap.
        A copy is returned — callers cannot mutate internal state.
        """
        ratings = self._ratings.get(agent_id, {})
        rating = ratings.get(capability)
        if rating is None:
            return SkillRating(
                mu=self.config.default_mu,
                sigma=self.config.default_sigma,
            )
        # Apply decay before returning
        self._apply_decay(rating)
        return SkillRating(
            mu=rating.mu,
            sigma=rating.sigma,
            wins=rating.wins,
            losses=rating.losses,
            last_active=rating.last_active,
        )

    def conservative_rating_for(
        self,
        agent_id: UUID,
        capability: AgentCapability,
    ) -> float:
        """Quick accessor: conservative rating for a single capability."""
        return self.get_rating(agent_id, capability).conservative_rating

    def aggregate_rating(self, agent_id: UUID) -> float:
        """Weighted average of conservative ratings across all known capabilities.

        Weights are proportional to total outcomes (more data = more weight).
        Returns DEFAULT_MU − k·DEFAULT_SIGMA if the agent has no ratings.
        """
        ratings = self._ratings.get(agent_id, {})
        if not ratings:
            return self.config.default_mu - self.config.k_conservative * self.config.default_sigma

        total_weight = 0.0
        weighted_sum = 0.0
        for rating in ratings.values():
            self._apply_decay(rating)
            weight = max(1, rating.total_outcomes)
            weighted_sum += rating.conservative_rating * weight
            total_weight += weight

        return weighted_sum / total_weight if total_weight else 0.0

    def effective_tier(
        self,
        agent_id: UUID,
        declared_tier: ModelTier,
    ) -> ModelTier:
        """Derive a *proven* tier from the agent's skill track record.

        Rules:
          1. SPECIALIZED declared tier is always honoured (domain-expert flag).
          2. During probation (< probation_outcomes total), cap at LIGHTWEIGHT.
          3. After probation, derive tier from aggregate conservative rating,
             but **never exceed** the declared tier (you can't claim PREMIUM
             and then prove out to PREMIUM if you're actually STANDARD).
             Actually — the declared tier is a *ceiling*; the effective tier
             can be lower but not higher than declared.
          4. If the agent has fewer than min_outcomes_for_tier outcomes, the
             effective tier remains LIGHTWEIGHT regardless of declared.
        """
        if declared_tier == ModelTier.SPECIALIZED:
            return ModelTier.SPECIALIZED

        total = self._total_outcomes(agent_id)

        # Probationary: not enough data → LIGHTWEIGHT ceiling
        if total < self.config.probation_outcomes:
            return ModelTier.LIGHTWEIGHT

        # Not enough data to derive a tier → LIGHTWEIGHT
        if total < self.config.min_outcomes_for_tier:
            return ModelTier.LIGHTWEIGHT

        agg = self.aggregate_rating(agent_id)

        # Walk thresholds from highest to lowest
        for tier in (ModelTier.PREMIUM, ModelTier.STANDARD, ModelTier.LIGHTWEIGHT):
            threshold = self.config.tier_thresholds.get(tier, 0.0)
            if agg >= threshold:
                # Cap at declared tier
                if tier.value > declared_tier.value:
                    return declared_tier
                return tier

        return ModelTier.LIGHTWEIGHT

    def is_probationary(self, agent_id: UUID) -> bool:
        """True if the agent hasn't completed enough tasks to leave probation."""
        return self._total_outcomes(agent_id) < self.config.probation_outcomes

    def all_ratings(self, agent_id: UUID) -> dict[AgentCapability, SkillRating]:
        """Return all capability ratings for an agent (copies)."""
        raw = self._ratings.get(agent_id, {})
        result = {}
        for cap, rating in raw.items():
            self._apply_decay(rating)
            result[cap] = SkillRating(
                mu=rating.mu,
                sigma=rating.sigma,
                wins=rating.wins,
                losses=rating.losses,
                last_active=rating.last_active,
            )
        return result

    def apply_global_decay(self) -> int:
        """Apply σ-decay to ALL ratings.  Returns the number updated.

        Intended to be called periodically (e.g., daily maintenance).
        """
        count = 0
        for agent_ratings in self._ratings.values():
            for rating in agent_ratings.values():
                old_sigma = rating.sigma
                self._apply_decay(rating)
                if rating.sigma != old_sigma:
                    count += 1
        return count

    def summary(self, agent_id: UUID) -> dict:
        """Return a JSON-friendly summary of an agent's skill profile."""
        ratings = self.all_ratings(agent_id)
        return {
            "agent_id": str(agent_id),
            "aggregate_rating": round(self.aggregate_rating(agent_id), 3),
            "total_outcomes": self._total_outcomes(agent_id),
            "is_probationary": self.is_probationary(agent_id),
            "capabilities": {
                cap.name: r.to_dict() for cap, r in ratings.items()
            },
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_or_create(
        self,
        agent_id: UUID,
        capability: AgentCapability,
    ) -> SkillRating:
        if agent_id not in self._ratings:
            self._ratings[agent_id] = {}
        ratings = self._ratings[agent_id]
        if capability not in ratings:
            ratings[capability] = SkillRating(
                mu=self.config.default_mu,
                sigma=self.config.default_sigma,
            )
        return ratings[capability]

    def _total_outcomes(self, agent_id: UUID) -> int:
        ratings = self._ratings.get(agent_id, {})
        return sum(r.total_outcomes for r in ratings.values())

    def _apply_decay(self, rating: SkillRating) -> None:
        """Increase σ based on time since last activity."""
        now = datetime.now(timezone.utc)
        elapsed = (now - rating.last_active).total_seconds() / 86400  # days
        if elapsed <= 0:
            return

        new_sigma = rating.sigma + self.config.decay_sigma_per_day * elapsed
        rating.sigma = min(new_sigma, self.config.max_sigma)

    def _bayesian_update(
        self,
        rating: SkillRating,
        *,
        won: bool,
        difficulty: float,
    ) -> None:
        """Simplified TrueSkill-style update for a single rating.

        This is a lightweight version — we model the outcome as a 1v1
        match against a "reference opponent" at the difficulty level.

        The opponent's μ is set to the current tier threshold (i.e., the
        difficulty of the problem).  Win → μ goes up, σ goes down.
        Lose → μ goes down, σ goes down (but less).
        """
        cfg = self.config

        # Add dynamic factor (tiny re-inflation to allow drift)
        rating.sigma = min(
            math.sqrt(rating.sigma ** 2 + cfg.tau ** 2),
            cfg.max_sigma,
        )

        # Opponent strength — scaled by difficulty
        opponent_mu = cfg.default_mu * difficulty

        # Combined variance
        c = math.sqrt(2 * cfg.beta ** 2 + rating.sigma ** 2)

        # Win probability
        t = (rating.mu - opponent_mu) / c
        # Gaussian CDF approximation (logistic)
        win_prob = 1.0 / (1.0 + math.exp(-1.7 * t))

        # Update factor
        if won:
            v = (1.0 - win_prob)  # surprise factor
        else:
            v = -win_prob

        # Update magnitude scaled by uncertainty
        update = (rating.sigma ** 2 / c) * v * difficulty

        rating.mu += update

        # Reduce uncertainty (learning)
        w = (rating.sigma ** 2) / (c ** 2)
        rating.sigma *= math.sqrt(max(1.0 - w * abs(v), 0.2))

        # Enforce floor
        rating.sigma = max(rating.sigma, cfg.min_sigma)
