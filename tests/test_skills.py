"""Tests for schwarma.skills — SkillTracker, SkillRating, effective tier."""

import pytest
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from schwarma.agent import AgentCapability, ModelTier
from schwarma.skills import (
    DEFAULT_MU,
    DEFAULT_SIGMA,
    K_CONSERVATIVE,
    SkillConfig,
    SkillRating,
    SkillTracker,
)


# ---------------------------------------------------------------------------
# SkillRating unit tests
# ---------------------------------------------------------------------------

class TestSkillRating:
    def test_default_conservative_rating(self):
        r = SkillRating()
        expected = DEFAULT_MU - K_CONSERVATIVE * DEFAULT_SIGMA
        assert abs(r.conservative_rating - expected) < 0.001

    def test_high_mu_low_sigma_gives_high_conservative(self):
        r = SkillRating(mu=40.0, sigma=1.0)
        assert r.conservative_rating == pytest.approx(38.0, abs=0.1)

    def test_total_outcomes(self):
        r = SkillRating(wins=3, losses=2)
        assert r.total_outcomes == 5

    def test_to_dict_has_all_fields(self):
        r = SkillRating(mu=30.0, sigma=3.0, wins=5, losses=1)
        d = r.to_dict()
        assert "mu" in d
        assert "sigma" in d
        assert "conservative_rating" in d
        assert "wins" in d
        assert "losses" in d
        assert "last_active" in d


# ---------------------------------------------------------------------------
# SkillTracker — basic operations
# ---------------------------------------------------------------------------

class TestSkillTrackerBasic:
    def test_fresh_agent_gets_default_rating(self):
        tracker = SkillTracker()
        agent_id = uuid4()
        r = tracker.get_rating(agent_id, AgentCapability.DEBUGGING)
        assert r.mu == pytest.approx(DEFAULT_MU)
        assert r.sigma == pytest.approx(DEFAULT_SIGMA, abs=0.5)

    def test_record_win_increases_mu(self):
        tracker = SkillTracker()
        agent_id = uuid4()
        r_before = tracker.get_rating(agent_id, AgentCapability.DEBUGGING)
        tracker.record_outcome(
            agent_id, {AgentCapability.DEBUGGING}, won=True
        )
        r_after = tracker.get_rating(agent_id, AgentCapability.DEBUGGING)
        assert r_after.mu > r_before.mu

    def test_record_loss_decreases_mu(self):
        tracker = SkillTracker()
        agent_id = uuid4()
        # Start with a win so mu is high enough to show decrease
        tracker.record_outcome(agent_id, {AgentCapability.DEBUGGING}, won=True)
        r_before = tracker.get_rating(agent_id, AgentCapability.DEBUGGING)
        tracker.record_outcome(agent_id, {AgentCapability.DEBUGGING}, won=False)
        r_after = tracker.get_rating(agent_id, AgentCapability.DEBUGGING)
        assert r_after.mu < r_before.mu

    def test_win_reduces_sigma(self):
        tracker = SkillTracker()
        agent_id = uuid4()
        r_before = tracker.get_rating(agent_id, AgentCapability.DEBUGGING)
        tracker.record_outcome(agent_id, {AgentCapability.DEBUGGING}, won=True)
        r_after = tracker.get_rating(agent_id, AgentCapability.DEBUGGING)
        assert r_after.sigma < r_before.sigma

    def test_multiple_capabilities_updated(self):
        tracker = SkillTracker()
        agent_id = uuid4()
        caps = {AgentCapability.DEBUGGING, AgentCapability.CODE_REVIEW}
        tracker.record_outcome(agent_id, caps, won=True)
        for cap in caps:
            r = tracker.get_rating(agent_id, cap)
            assert r.wins == 1

    def test_difficulty_amplifies_update(self):
        tracker = SkillTracker()
        a1, a2 = uuid4(), uuid4()
        # Easy win → small update
        tracker.record_outcome(a1, {AgentCapability.DEBUGGING}, won=True, difficulty=0.5)
        # Hard win → bigger update
        tracker.record_outcome(a2, {AgentCapability.DEBUGGING}, won=True, difficulty=2.0)
        r1 = tracker.get_rating(a1, AgentCapability.DEBUGGING)
        r2 = tracker.get_rating(a2, AgentCapability.DEBUGGING)
        assert r2.mu > r1.mu

    def test_aggregate_rating_no_history(self):
        tracker = SkillTracker()
        agg = tracker.aggregate_rating(uuid4())
        expected = DEFAULT_MU - K_CONSERVATIVE * DEFAULT_SIGMA
        assert agg == pytest.approx(expected, abs=0.1)

    def test_aggregate_rating_with_two_caps(self):
        tracker = SkillTracker()
        agent_id = uuid4()
        # Win in debugging (1 outcome, weight=1)
        tracker.record_outcome(agent_id, {AgentCapability.DEBUGGING}, won=True)
        # Win twice in code review (2 outcomes, weight=2)
        tracker.record_outcome(agent_id, {AgentCapability.CODE_REVIEW}, won=True)
        tracker.record_outcome(agent_id, {AgentCapability.CODE_REVIEW}, won=True)
        agg = tracker.aggregate_rating(agent_id)
        # Should be somewhere above default conservative
        assert agg > DEFAULT_MU - K_CONSERVATIVE * DEFAULT_SIGMA

    def test_all_ratings_returns_copies(self):
        tracker = SkillTracker()
        agent_id = uuid4()
        tracker.record_outcome(agent_id, {AgentCapability.DEBUGGING}, won=True)
        ratings = tracker.all_ratings(agent_id)
        assert AgentCapability.DEBUGGING in ratings
        # Mutating the copy shouldn't affect internal state
        ratings[AgentCapability.DEBUGGING].mu = 999.0
        real = tracker.get_rating(agent_id, AgentCapability.DEBUGGING)
        assert real.mu != 999.0

    def test_summary_structure(self):
        tracker = SkillTracker()
        agent_id = uuid4()
        tracker.record_outcome(agent_id, {AgentCapability.DEBUGGING}, won=True)
        s = tracker.summary(agent_id)
        assert "agent_id" in s
        assert "aggregate_rating" in s
        assert "total_outcomes" in s
        assert "is_probationary" in s
        assert "capabilities" in s
        assert "DEBUGGING" in s["capabilities"]


# ---------------------------------------------------------------------------
# Effective tier derivation
# ---------------------------------------------------------------------------

class TestEffectiveTier:
    def test_specialized_always_honoured(self):
        tracker = SkillTracker()
        agent_id = uuid4()
        tier = tracker.effective_tier(agent_id, ModelTier.SPECIALIZED)
        assert tier == ModelTier.SPECIALIZED

    def test_probationary_caps_at_lightweight(self):
        tracker = SkillTracker(SkillConfig(probation_outcomes=3))
        agent_id = uuid4()
        # Only 2 outcomes — still probationary
        tracker.record_outcome(agent_id, {AgentCapability.DEBUGGING}, won=True)
        tracker.record_outcome(agent_id, {AgentCapability.DEBUGGING}, won=True)
        assert tracker.is_probationary(agent_id)
        tier = tracker.effective_tier(agent_id, ModelTier.PREMIUM)
        assert tier == ModelTier.LIGHTWEIGHT

    def test_after_probation_can_earn_tier(self):
        config = SkillConfig(
            probation_outcomes=2,
            min_outcomes_for_tier=2,
        )
        tracker = SkillTracker(config)
        agent_id = uuid4()
        # Win many times to build up rating
        for _ in range(10):
            tracker.record_outcome(agent_id, {AgentCapability.DEBUGGING}, won=True)
        tier = tracker.effective_tier(agent_id, ModelTier.PREMIUM)
        # After 10 wins, should have earned at least STANDARD
        assert tier.value >= ModelTier.STANDARD.value

    def test_effective_tier_capped_by_declared(self):
        """Even with great ratings, effective can't exceed declared."""
        config = SkillConfig(
            probation_outcomes=2,
            min_outcomes_for_tier=2,
        )
        tracker = SkillTracker(config)
        agent_id = uuid4()
        for _ in range(20):
            tracker.record_outcome(agent_id, {AgentCapability.DEBUGGING}, won=True)
        tier = tracker.effective_tier(agent_id, ModelTier.STANDARD)
        assert tier.value <= ModelTier.STANDARD.value

    def test_is_probationary_true_initially(self):
        tracker = SkillTracker()
        assert tracker.is_probationary(uuid4()) is True

    def test_is_probationary_false_after_enough(self):
        tracker = SkillTracker(SkillConfig(probation_outcomes=2))
        agent_id = uuid4()
        tracker.record_outcome(agent_id, {AgentCapability.DEBUGGING}, won=True)
        tracker.record_outcome(agent_id, {AgentCapability.DEBUGGING}, won=True)
        assert tracker.is_probationary(agent_id) is False


# ---------------------------------------------------------------------------
# Skill decay
# ---------------------------------------------------------------------------

class TestSkillDecay:
    def test_decay_increases_sigma_over_time(self):
        tracker = SkillTracker(SkillConfig(decay_sigma_per_day=0.5))
        agent_id = uuid4()
        tracker.record_outcome(agent_id, {AgentCapability.DEBUGGING}, won=True)

        # Immediately after a win, sigma is reduced
        r_fresh = tracker.get_rating(agent_id, AgentCapability.DEBUGGING)
        sigma_after_win = r_fresh.sigma

        # Manually set last_active to 10 days ago on internal rating
        internal = tracker._ratings[agent_id][AgentCapability.DEBUGGING]
        internal.last_active = datetime.now(timezone.utc) - timedelta(days=10)

        r_decayed = tracker.get_rating(agent_id, AgentCapability.DEBUGGING)
        # Sigma should have increased from the post-win value
        assert r_decayed.sigma > sigma_after_win

    def test_sigma_capped_at_max(self):
        tracker = SkillTracker(SkillConfig(decay_sigma_per_day=100.0))
        agent_id = uuid4()
        tracker.record_outcome(agent_id, {AgentCapability.DEBUGGING}, won=True)
        rating = tracker._ratings[agent_id][AgentCapability.DEBUGGING]
        rating.last_active = datetime.now(timezone.utc) - timedelta(days=100)

        r = tracker.get_rating(agent_id, AgentCapability.DEBUGGING)
        assert r.sigma <= DEFAULT_SIGMA

    def test_global_decay_updates_all(self):
        tracker = SkillTracker(SkillConfig(decay_sigma_per_day=1.0))
        a1, a2 = uuid4(), uuid4()
        tracker.record_outcome(a1, {AgentCapability.DEBUGGING}, won=True)
        tracker.record_outcome(a2, {AgentCapability.CODE_REVIEW}, won=True)

        # Set both to old
        for agent_ratings in tracker._ratings.values():
            for r in agent_ratings.values():
                r.last_active = datetime.now(timezone.utc) - timedelta(days=5)

        count = tracker.apply_global_decay()
        assert count == 2


# ---------------------------------------------------------------------------
# Conservative rating accessor
# ---------------------------------------------------------------------------

class TestConservativeRating:
    def test_accessor_matches_get_rating(self):
        tracker = SkillTracker()
        agent_id = uuid4()
        tracker.record_outcome(agent_id, {AgentCapability.DEBUGGING}, won=True)
        cr = tracker.conservative_rating_for(agent_id, AgentCapability.DEBUGGING)
        r = tracker.get_rating(agent_id, AgentCapability.DEBUGGING)
        assert cr == pytest.approx(r.conservative_rating, abs=0.01)
