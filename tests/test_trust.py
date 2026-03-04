"""Tests for trust tiers, sensitivity, and the TrustGate."""

from uuid import uuid4

from schwarma.trust import Sensitivity, TrustGate, TrustPolicy, TrustTier
from schwarma.problem import Problem, ProblemTag


class TestTrustTierOrdering:
    def test_tier_ordering(self):
        assert TrustTier.UNTRUSTED < TrustTier.BASIC < TrustTier.TRUSTED < TrustTier.PRIVILEGED

    def test_sensitivity_ordering(self):
        assert Sensitivity.PUBLIC < Sensitivity.INTERNAL < Sensitivity.CONFIDENTIAL < Sensitivity.RESTRICTED


class TestTrustGateAccess:
    def test_default_tier_is_basic(self):
        gate = TrustGate()
        agent_id = uuid4()
        assert gate.get_tier(agent_id) == TrustTier.BASIC

    def test_basic_can_access_public_and_internal(self):
        gate = TrustGate()
        agent_id = uuid4()
        assert gate.can_access(agent_id, Sensitivity.PUBLIC)
        assert gate.can_access(agent_id, Sensitivity.INTERNAL)

    def test_basic_cannot_access_confidential(self):
        gate = TrustGate()
        agent_id = uuid4()
        assert not gate.can_access(agent_id, Sensitivity.CONFIDENTIAL)

    def test_untrusted_cannot_access_internal(self):
        gate = TrustGate()
        agent_id = uuid4()
        gate.assign_tier(agent_id, TrustTier.UNTRUSTED)
        assert gate.can_access(agent_id, Sensitivity.PUBLIC)
        assert not gate.can_access(agent_id, Sensitivity.INTERNAL)

    def test_privileged_can_access_everything(self):
        gate = TrustGate()
        agent_id = uuid4()
        gate.assign_tier(agent_id, TrustTier.PRIVILEGED)
        for sensitivity in Sensitivity:
            assert gate.can_access(agent_id, sensitivity)

    def test_manual_tier_assignment(self):
        gate = TrustGate()
        agent_id = uuid4()
        gate.assign_tier(agent_id, TrustTier.TRUSTED)
        assert gate.get_tier(agent_id) == TrustTier.TRUSTED


class TestTrustPromotion:
    def test_promote_basic_to_trusted(self):
        gate = TrustGate()
        agent_id = uuid4()
        new_tier = gate.maybe_promote(agent_id, reputation=120)
        assert new_tier == TrustTier.TRUSTED

    def test_promote_to_privileged(self):
        gate = TrustGate()
        agent_id = uuid4()
        new_tier = gate.maybe_promote(agent_id, reputation=250)
        assert new_tier == TrustTier.PRIVILEGED

    def test_no_promotion_if_rep_too_low(self):
        gate = TrustGate()
        agent_id = uuid4()
        new_tier = gate.maybe_promote(agent_id, reputation=60)
        assert new_tier == TrustTier.BASIC  # stays at default

    def test_promotion_never_downgrades(self):
        gate = TrustGate()
        agent_id = uuid4()
        gate.assign_tier(agent_id, TrustTier.TRUSTED)
        # Even with low rep, should stay TRUSTED
        new_tier = gate.maybe_promote(agent_id, reputation=10)
        assert new_tier == TrustTier.TRUSTED

    def test_custom_thresholds(self):
        policy = TrustPolicy(thresholds={
            TrustTier.UNTRUSTED: 0,
            TrustTier.BASIC: 10,
            TrustTier.TRUSTED: 50,
            TrustTier.PRIVILEGED: 100,
        })
        gate = TrustGate(policy)
        agent_id = uuid4()
        gate.assign_tier(agent_id, TrustTier.UNTRUSTED)
        assert gate.maybe_promote(agent_id, reputation=50) == TrustTier.TRUSTED


class TestFilterVisible:
    def _make_problem(self, sensitivity: Sensitivity) -> Problem:
        return Problem(
            title=f"{sensitivity.name} problem",
            description="...",
            author_id=uuid4(),
            sensitivity=sensitivity,
        )

    def test_basic_sees_public_and_internal(self):
        gate = TrustGate()
        agent_id = uuid4()
        problems = [
            self._make_problem(Sensitivity.PUBLIC),
            self._make_problem(Sensitivity.INTERNAL),
            self._make_problem(Sensitivity.CONFIDENTIAL),
            self._make_problem(Sensitivity.RESTRICTED),
        ]
        visible = gate.filter_visible(agent_id, problems)
        assert len(visible) == 2  # PUBLIC + INTERNAL

    def test_privileged_sees_all(self):
        gate = TrustGate()
        agent_id = uuid4()
        gate.assign_tier(agent_id, TrustTier.PRIVILEGED)
        problems = [self._make_problem(s) for s in Sensitivity]
        visible = gate.filter_visible(agent_id, problems)
        assert len(visible) == len(Sensitivity)

    def test_untrusted_sees_only_public(self):
        gate = TrustGate()
        agent_id = uuid4()
        gate.assign_tier(agent_id, TrustTier.UNTRUSTED)
        problems = [self._make_problem(s) for s in Sensitivity]
        visible = gate.filter_visible(agent_id, problems)
        assert len(visible) == 1
        assert visible[0].sensitivity == Sensitivity.PUBLIC
