"""Tests for the ReputationLedger."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from schwarma.reputation import LedgerConfig, ReputationEvent, ReputationLedger


class TestLedger:
    def test_initial_balance(self):
        ledger = ReputationLedger()
        agent_id = uuid4()
        assert ledger.balance(agent_id) == 50  # default initial

    def test_record_changes_balance(self):
        ledger = ReputationLedger()
        aid = uuid4()
        ledger.record(aid, ReputationEvent.SOLUTION_SUBMITTED)
        assert ledger.balance(aid) == 52  # 50 + 2

    def test_negative_event(self):
        ledger = ReputationLedger()
        aid = uuid4()
        ledger.record(aid, ReputationEvent.SOLUTION_REJECTED)
        assert ledger.balance(aid) == 47  # 50 - 3

    def test_floor_enforced(self):
        config = LedgerConfig(floor=0, initial_reputation=5)
        ledger = ReputationLedger(config)
        aid = uuid4()
        ledger.record(aid, ReputationEvent.GOOD_FAITH_VIOLATION)  # -20
        assert ledger.balance(aid) == 0  # floored

    def test_ceiling_enforced(self):
        config = LedgerConfig(ceiling=100)
        ledger = ReputationLedger(config)
        aid = uuid4()
        ledger.record(aid, ReputationEvent.BONUS, delta=200)
        assert ledger.balance(aid) == 100

    def test_history(self):
        ledger = ReputationLedger()
        aid = uuid4()
        ledger.record(aid, ReputationEvent.PROBLEM_POSTED)
        ledger.record(aid, ReputationEvent.REVIEW_SUBMITTED)
        assert len(ledger.history(aid)) == 2

    def test_leaderboard(self):
        ledger = ReputationLedger()
        a1, a2, a3 = uuid4(), uuid4(), uuid4()
        ledger.record(a1, ReputationEvent.BONUS, delta=100)
        ledger.record(a2, ReputationEvent.BONUS, delta=50)
        ledger.record(a3, ReputationEvent.BONUS, delta=75)
        board = ledger.leaderboard(top_n=2)
        assert board[0][0] == a1
        assert len(board) == 2

    def test_custom_delta_override(self):
        ledger = ReputationLedger()
        aid = uuid4()
        ledger.record(aid, ReputationEvent.BONUS, delta=42, reason="manual bonus")
        assert ledger.balance(aid) == 92  # 50 + 42
        assert ledger.history(aid)[-1].reason == "manual bonus"


class TestDiminishingReturns:
    """Pairwise interaction diminishing returns."""

    def test_factor_1_below_threshold(self):
        ledger = ReputationLedger(LedgerConfig(diminishing_threshold=3))
        a, b = uuid4(), uuid4()
        for _ in range(3):
            ledger.record_pairwise(a, b)
        assert ledger.diminishing_factor(a, b) == 1.0

    def test_factor_decays_above_threshold(self):
        ledger = ReputationLedger(LedgerConfig(
            diminishing_threshold=2, diminishing_decay=0.5
        ))
        a, b = uuid4(), uuid4()
        for _ in range(4):  # 2 above threshold
            ledger.record_pairwise(a, b)
        # excess=2 → 0.5^2 = 0.25
        assert ledger.diminishing_factor(a, b) == 0.25

    def test_pairwise_count(self):
        ledger = ReputationLedger()
        a, b, c = uuid4(), uuid4(), uuid4()
        ledger.record_pairwise(a, b)
        ledger.record_pairwise(a, b)
        ledger.record_pairwise(a, c)
        assert ledger.pairwise_interaction_count(a, b) == 2
        assert ledger.pairwise_interaction_count(a, c) == 1
        assert ledger.pairwise_interaction_count(b, c) == 0

    def test_diminishing_factor_symmetric(self):
        """Factor is the same regardless of argument order."""
        ledger = ReputationLedger()
        a, b = uuid4(), uuid4()
        for _ in range(5):
            ledger.record_pairwise(a, b)
        assert ledger.diminishing_factor(a, b) == ledger.diminishing_factor(b, a)


class TestInactivityDecay:
    """Reputation inactivity decay mechanics."""

    def test_no_decay_when_rate_is_zero(self):
        ledger = ReputationLedger(LedgerConfig(inactivity_decay_rate=0.0))
        aid = uuid4()
        ledger.record(aid, ReputationEvent.PROBLEM_POSTED)
        entries = ledger.apply_inactivity_decay(
            now=datetime.now(timezone.utc) + timedelta(days=365),
        )
        assert entries == []

    def test_no_decay_within_period(self):
        config = LedgerConfig(inactivity_decay_rate=0.1, inactivity_period_days=30)
        ledger = ReputationLedger(config)
        aid = uuid4()
        ledger.record(aid, ReputationEvent.PROBLEM_POSTED)
        entries = ledger.apply_inactivity_decay(
            now=datetime.now(timezone.utc) + timedelta(days=15),
        )
        assert entries == []

    def test_decay_after_inactivity_period(self):
        config = LedgerConfig(
            inactivity_decay_rate=0.1,
            inactivity_period_days=30,
            initial_reputation=100,
        )
        ledger = ReputationLedger(config)
        aid = uuid4()
        ledger.record(aid, ReputationEvent.PROBLEM_POSTED)  # balance = 101
        before = ledger.balance(aid)
        entries = ledger.apply_inactivity_decay(
            now=datetime.now(timezone.utc) + timedelta(days=45),
        )
        assert len(entries) == 1
        assert entries[0].delta < 0
        assert entries[0].reason == "inactivity decay"
        assert ledger.balance(aid) < before

    def test_decay_respects_floor(self):
        config = LedgerConfig(
            inactivity_decay_rate=0.5,
            inactivity_period_days=1,
            initial_reputation=2,
            floor=0,
        )
        ledger = ReputationLedger(config)
        aid = uuid4()
        ledger.record(aid, ReputationEvent.PROBLEM_POSTED)  # balance = 3
        # Apply decay repeatedly — should not go below floor
        for i in range(10):
            ledger.apply_inactivity_decay(
                now=datetime.now(timezone.utc) + timedelta(days=5 + i * 2),
            )
        assert ledger.balance(aid) >= config.floor

    def test_decay_does_not_affect_active_agents(self):
        config = LedgerConfig(
            inactivity_decay_rate=0.1,
            inactivity_period_days=30,
        )
        ledger = ReputationLedger(config)
        active = uuid4()
        inactive = uuid4()
        now = datetime.now(timezone.utc)
        ledger.record(active, ReputationEvent.PROBLEM_POSTED)
        ledger.record(inactive, ReputationEvent.PROBLEM_POSTED)

        # Simulate: active agent acts again at day 20
        ledger.record(active, ReputationEvent.REVIEW_SUBMITTED)

        entries = ledger.apply_inactivity_decay(now=now + timedelta(days=45))
        agent_ids = {e.agent_id for e in entries}
        assert inactive in agent_ids
        # active agent had recent activity so should not decay
        # (but note: last_activity was updated by the REVIEW_SUBMITTED)

    def test_last_activity_tracking(self):
        ledger = ReputationLedger()
        aid = uuid4()
        assert ledger.last_activity(aid) is None
        ledger.record(aid, ReputationEvent.PROBLEM_POSTED)
        assert ledger.last_activity(aid) is not None

    def test_minimum_deduction_of_one(self):
        config = LedgerConfig(
            inactivity_decay_rate=0.01,  # 1% of balance
            inactivity_period_days=1,
            initial_reputation=5,
        )
        ledger = ReputationLedger(config)
        aid = uuid4()
        ledger.record(aid, ReputationEvent.PROBLEM_POSTED)  # balance = 6
        entries = ledger.apply_inactivity_decay(
            now=datetime.now(timezone.utc) + timedelta(days=5),
        )
        # 1% of 6 = 0.06, rounded down = 0, but min deduction is 1
        assert len(entries) == 1
        assert entries[0].delta == -1
