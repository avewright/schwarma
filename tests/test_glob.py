"""Tests for schwarma/glob.py — Glob coalition system and reputation splitting."""

from __future__ import annotations

import pytest
from uuid import uuid4

from schwarma.glob import (
    Glob,
    GlobMembership,
    GlobRole,
    GlobStatus,
    GlobSolution,
    ContributionStatus,
    ReputationShare,
    split_reputation,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_glob(max_members: int = 4, coordinator_bonus: float = 0.10) -> tuple[Glob, list]:
    """Return a glob + list of UUIDs [coordinator_id, member1_id, member2_id, member3_id]."""
    ids = [uuid4() for _ in range(5)]
    g = Glob(
        problem_id=uuid4(),
        coordinator_id=ids[0],
        name="test-glob",
        max_members=max_members,
        coordinator_bonus=coordinator_bonus,
    )
    return g, ids


# ---------------------------------------------------------------------------
# GlobMembership — basic state machine
# ---------------------------------------------------------------------------

class TestGlobMembership:
    def test_submit_sets_submitted_status(self):
        m = GlobMembership(agent_id=uuid4(), glob_id=uuid4())
        m.submit("my answer")
        assert m.contribution_status == ContributionStatus.SUBMITTED
        assert m.contribution_text == "my answer"
        assert m.submitted_at is not None

    def test_accept_sets_accepted(self):
        m = GlobMembership(agent_id=uuid4(), glob_id=uuid4())
        m.submit("text")
        m.accept()
        assert m.contribution_status == ContributionStatus.ACCEPTED

    def test_reject_sets_rejected(self):
        m = GlobMembership(agent_id=uuid4(), glob_id=uuid4())
        m.submit("text")
        m.reject()
        assert m.contribution_status == ContributionStatus.REJECTED

    def test_serialisation_round_trip(self):
        m = GlobMembership(agent_id=uuid4(), glob_id=uuid4(), subtask="task A", weight=0.5)
        m.submit("answer")
        m.accept()
        restored = GlobMembership.from_dict(m.to_dict())
        assert restored.agent_id == m.agent_id
        assert restored.glob_id == m.glob_id
        assert restored.contribution_status == ContributionStatus.ACCEPTED
        assert restored.contribution_text == "answer"
        assert restored.weight == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Glob — lifecycle and membership management
# ---------------------------------------------------------------------------

class TestGlobLifecycle:
    def test_initial_status_is_forming(self):
        g, ids = _make_glob()
        assert g.status == GlobStatus.FORMING

    def test_add_coordinator_sets_role(self):
        g, ids = _make_glob()
        m = g.add_member(ids[0], subtask="orchestration")
        assert m.role == GlobRole.COORDINATOR

    def test_add_regular_member_sets_member_role(self):
        g, ids = _make_glob()
        g.add_member(ids[0])
        m = g.add_member(ids[1], subtask="subtask 1")
        assert m.role == GlobRole.MEMBER

    def test_adding_second_member_activates_glob(self):
        g, ids = _make_glob()
        g.add_member(ids[0])
        assert g.status == GlobStatus.FORMING
        g.add_member(ids[1])
        assert g.status == GlobStatus.ACTIVE

    def test_is_full_enforced(self):
        g, ids = _make_glob(max_members=2)
        g.add_member(ids[0])
        g.add_member(ids[1])
        assert g.is_full
        with pytest.raises(ValueError, match="full"):
            g.add_member(ids[2])

    def test_duplicate_member_raises(self):
        g, ids = _make_glob()
        g.add_member(ids[0])
        with pytest.raises(ValueError, match="already in glob"):
            g.add_member(ids[0])

    def test_dissolve_sets_status(self):
        g, ids = _make_glob()
        g.add_member(ids[0])
        g.add_member(ids[1])
        g.dissolve()
        assert g.status == GlobStatus.DISSOLVED
        assert g.dissolved_at is not None

    def test_disband_sets_status(self):
        g, ids = _make_glob()
        g.add_member(ids[0])
        g.disband()
        assert g.status == GlobStatus.DISBANDED

    def test_get_membership_by_agent(self):
        g, ids = _make_glob()
        g.add_member(ids[0])
        g.add_member(ids[1])
        m = g.get_membership(ids[1])
        assert m is not None
        assert m.agent_id == ids[1]

    def test_get_membership_unknown_agent_returns_none(self):
        g, _ = _make_glob()
        assert g.get_membership(uuid4()) is None

    def test_coordinator_membership_property(self):
        g, ids = _make_glob()
        g.add_member(ids[0])
        cm = g.coordinator_membership
        assert cm is not None
        assert cm.agent_id == ids[0]
        assert cm.role == GlobRole.COORDINATOR

    def test_cannot_add_members_to_dissolved_glob(self):
        g, ids = _make_glob()
        g.add_member(ids[0])
        g.dissolve()
        with pytest.raises(ValueError, match="DISSOLVED"):
            g.add_member(ids[1])


class TestGlobSerialisation:
    def test_round_trip_empty_memberships(self):
        g, ids = _make_glob()
        data = g.to_dict()
        restored = Glob.from_dict(data)
        assert restored.id == g.id
        assert restored.problem_id == g.problem_id
        assert restored.coordinator_id == g.coordinator_id
        assert restored.name == g.name
        assert restored.status == g.status
        assert restored.memberships == []

    def test_round_trip_with_members(self):
        g, ids = _make_glob()
        g.add_member(ids[0])
        g.add_member(ids[1], subtask="analysis", weight=2.0)
        restored = Glob.from_dict(g.to_dict())
        assert len(restored.memberships) == 2
        assert restored.status == GlobStatus.ACTIVE

    def test_str_representation(self):
        g, ids = _make_glob()
        s = str(g)
        assert "test-glob" in s
        assert "FORMING" in s

    def test_hash_and_equality(self):
        g, ids = _make_glob()
        globs = {g}
        assert g in globs
        g2, _ = _make_glob()
        assert g != g2


# ---------------------------------------------------------------------------
# GlobSolution serialisation
# ---------------------------------------------------------------------------

class TestGlobSolution:
    def test_round_trip(self):
        gs = GlobSolution(
            glob_id=uuid4(),
            problem_id=uuid4(),
            solution_id=uuid4(),
            assembled_by=uuid4(),
            assembly_notes="combined parts A+B",
            member_contributions={"agent-1": "part A", "agent-2": "part B"},
        )
        restored = GlobSolution.from_dict(gs.to_dict())
        assert restored.glob_id == gs.glob_id
        assert restored.assembly_notes == "combined parts A+B"
        assert restored.member_contributions == gs.member_contributions


# ---------------------------------------------------------------------------
# split_reputation — the core arithmetic
# ---------------------------------------------------------------------------

class TestSplitReputation:
    """Thorough arithmetic tests for reputation distribution."""

    def _setup_glob(self, n_members: int = 2, bonus: float = 0.10) -> tuple[Glob, list]:
        """Return active glob with coordinator + n_members, all contributions ACCEPTED."""
        g, ids = _make_glob(max_members=n_members + 2, coordinator_bonus=bonus)
        g.add_member(ids[0], subtask="orchestration", weight=1.0)
        g.coordinator_membership.accept()
        for i in range(1, n_members + 1):
            m = g.add_member(ids[i], subtask=f"subtask-{i}", weight=1.0)
            m.submit(f"answer {i}")
            m.accept()
        return g, ids

    def test_total_payout_equals_bounty(self):
        """Sum of all shares must exactly equal the bounty."""
        g, ids = self._setup_glob(n_members=3)
        shares = split_reputation(g, total_bounty=100)
        assert sum(s.delta for s in shares) == 100

    def test_coordinator_always_earns_bonus(self):
        """Coordinator earns at least coordinator_bonus * bounty."""
        g, ids = self._setup_glob(n_members=2, bonus=0.10)
        shares = split_reputation(g, total_bounty=100)
        coord_share = next(s for s in shares if s.agent_id == ids[0])
        assert coord_share.delta >= 10  # at least 10% bonus

    def test_equal_weights_split_evenly(self):
        """Two equal-weight members split the member pool equally."""
        g, ids = _make_glob(max_members=3, coordinator_bonus=0.0)
        g.add_member(ids[0], weight=1.0)
        g.coordinator_membership.accept()
        m1 = g.add_member(ids[1], weight=1.0)
        m2 = g.add_member(ids[2], weight=1.0)
        m1.submit("a"); m1.accept()
        m2.submit("b"); m2.accept()
        shares = split_reputation(g, total_bounty=100)
        # With 0% bonus, coordinator gets only the rounding remainder
        member_shares = [s for s in shares if s.agent_id in (ids[1], ids[2])]
        assert len(member_shares) == 2
        # Both should get ~50
        for s in member_shares:
            assert abs(s.delta - 50) <= 1

    def test_rejected_member_earns_nothing(self):
        """A member with REJECTED contribution status should earn zero."""
        g, ids = _make_glob(max_members=3, coordinator_bonus=0.10)
        g.add_member(ids[0])
        g.coordinator_membership.accept()
        m1 = g.add_member(ids[1], weight=1.0)
        m2 = g.add_member(ids[2], weight=1.0)
        m1.submit("good"); m1.accept()
        m2.submit("bad"); m2.reject()   # rejected — no payout
        shares = split_reputation(g, total_bounty=100)
        for s in shares:
            assert s.agent_id != ids[2], f"Rejected member should not earn a share; got {s}"

    def test_no_accepted_members_returns_empty_if_no_coordinator(self):
        """If glob has no memberships at all, returns empty list."""
        g, ids = _make_glob()
        shares = split_reputation(g, total_bounty=100)
        assert shares == []

    def test_weighted_split(self):
        """A member with twice the weight should earn roughly twice as much."""
        g, ids = _make_glob(max_members=3, coordinator_bonus=0.0)
        g.add_member(ids[0], weight=1.0)
        g.coordinator_membership.accept()
        m1 = g.add_member(ids[1], weight=2.0)
        m2 = g.add_member(ids[2], weight=1.0)
        m1.submit("a"); m1.accept()
        m2.submit("b"); m2.accept()
        shares = split_reputation(g, total_bounty=90)
        share_map = {s.agent_id: s.delta for s in shares}
        # ids[1] has weight 2, ids[2] has weight 1 → 2:1 ratio from member pool
        assert share_map[ids[1]] == pytest.approx(share_map[ids[2]] * 2, abs=1)

    def test_zero_bounty_all_zeros(self):
        """Zero bounty distributes zero to everyone."""
        g, ids = self._setup_glob(n_members=2)
        shares = split_reputation(g, total_bounty=0)
        assert all(s.delta == 0 for s in shares)

    def test_single_member_plus_coordinator(self):
        """One member + coordinator, total bounty 100."""
        g, ids = _make_glob(max_members=2, coordinator_bonus=0.20)
        g.add_member(ids[0])
        g.coordinator_membership.accept()
        m1 = g.add_member(ids[1], weight=1.0)
        m1.submit("answer"); m1.accept()
        shares = split_reputation(g, total_bounty=100)
        total = sum(s.delta for s in shares)
        assert total == 100

    def test_reason_strings_contain_glob_id(self):
        """Reason string references the glob id for auditability."""
        g, ids = self._setup_glob(n_members=1)
        shares = split_reputation(g, total_bounty=50)
        for s in shares:
            assert str(g.id) in s.reason

    def test_deduplication_coordinator_counted_once(self):
        """Coordinator appears exactly once in shares even with ACCEPTED contribution."""
        g, ids = _make_glob(max_members=2, coordinator_bonus=0.10)
        g.add_member(ids[0], weight=1.0)
        cm = g.coordinator_membership
        cm.submit("coord work"); cm.accept()
        m1 = g.add_member(ids[1], weight=1.0)
        m1.submit("member work"); m1.accept()
        shares = split_reputation(g, total_bounty=100)
        coord_entries = [s for s in shares if s.agent_id == ids[0]]
        assert len(coord_entries) == 1
