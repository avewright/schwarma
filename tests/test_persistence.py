"""Tests for schwarma.persistence — save/load Exchange snapshots to disk."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import pytest

from schwarma.agent import Agent, AgentCapability, ModelTier
from schwarma.exchange import Exchange, ExchangeConfig
from schwarma.glob import GlobStatus
from schwarma.persistence import (
    load_snapshot,
    restore_from_dict,
    save_snapshot,
    snapshot_to_dict,
)
from schwarma.problem import Problem, ProblemStatus, ProblemTag
from schwarma.review import Review, ReviewType, ReviewVerdict
from schwarma.solution import Solution


# ── Helpers ──────────────────────────────────────────────────────────────

async def _dummy_solver(desc: str, ctx: dict) -> str:
    return "solved"


async def _setup_exchange() -> tuple[Exchange, dict]:
    """Create an exchange with agents, a problem, solution, and review."""
    cfg = ExchangeConfig(
        min_reputation_to_claim=0,
        enable_staking=False,
        reviews_required_for_accept=1,
        enable_content_guards=False,
        enable_effort_guards=False,
    )
    ex = Exchange(cfg)

    alice = Agent(name="Alice", solver=_dummy_solver, capabilities={AgentCapability.CODE_GENERATION})
    bob = Agent(name="Bob", solver=_dummy_solver, model_tier=ModelTier.PREMIUM)
    ex.register(alice)
    ex.register(bob)

    problem = Problem(
        title="Test Problem",
        description="Solve this test",
        author_id=alice.id,
        tags={ProblemTag.BUG},
        bounty=20,
        priority=5,
    )
    problem = await ex.post_problem(problem)
    await ex.claim_problem(problem.id, bob.id)
    solution = await ex.solve_problem(problem.id, bob.id, solution_body="answer body")
    review = Review(
        solution_id=solution.id,
        reviewer_id=alice.id,
        review_type=ReviewType.CORRECTNESS,
        verdict=ReviewVerdict.APPROVE,
        body="Looks good",
    )
    review = await ex.submit_review(review)

    return ex, {
        "alice": alice,
        "bob": bob,
        "problem": problem,
        "solution": solution,
        "review": review,
    }


# ── Save / Load ─────────────────────────────────────────────────────────


class TestSaveLoad:
    @pytest.mark.asyncio
    async def test_save_creates_file(self, tmp_path):
        ex, _ = await _setup_exchange()
        out = save_snapshot(ex, tmp_path / "state.json")
        assert out.exists()
        data = json.loads(out.read_text())
        assert "problems" in data
        assert "agents" in data

    @pytest.mark.asyncio
    async def test_save_creates_parent_dirs(self, tmp_path):
        ex, _ = await _setup_exchange()
        out = save_snapshot(ex, tmp_path / "sub" / "dir" / "state.json")
        assert out.exists()

    @pytest.mark.asyncio
    async def test_save_valid_json(self, tmp_path):
        ex, _ = await _setup_exchange()
        save_snapshot(ex, tmp_path / "state.json")
        # Should not raise
        data = json.loads((tmp_path / "state.json").read_text())
        assert isinstance(data, dict)

    @pytest.mark.asyncio
    async def test_roundtrip(self, tmp_path):
        ex, refs = await _setup_exchange()
        save_snapshot(ex, tmp_path / "state.json")
        ex2 = load_snapshot(tmp_path / "state.json")

        # Agents restored
        assert refs["alice"].id in ex2._agents
        assert refs["bob"].id in ex2._agents
        assert ex2._agents[refs["alice"].id].name == "Alice"
        assert ex2._agents[refs["bob"].id].model_tier == ModelTier.PREMIUM

        # Problem restored
        assert refs["problem"].id in ex2._problems
        p = ex2._problems[refs["problem"].id]
        assert p.title == "Test Problem"
        assert p.bounty == 20

        # Solution restored
        assert refs["solution"].id in ex2._solutions
        s = ex2._solutions[refs["solution"].id]
        assert s.body == "answer body"

        # Review restored
        assert refs["review"].id in ex2._reviews

    @pytest.mark.asyncio
    async def test_reputation_restored(self, tmp_path):
        ex, refs = await _setup_exchange()
        alice_bal = ex.ledger.balance(refs["alice"].id)
        bob_bal = ex.ledger.balance(refs["bob"].id)

        save_snapshot(ex, tmp_path / "state.json")
        ex2 = load_snapshot(tmp_path / "state.json")

        assert ex2.ledger.balance(refs["alice"].id) == alice_bal
        assert ex2.ledger.balance(refs["bob"].id) == bob_bal

    @pytest.mark.asyncio
    async def test_suspended_restored(self, tmp_path):
        ex, refs = await _setup_exchange()
        await ex.suspend_agent(refs["bob"].id)

        save_snapshot(ex, tmp_path / "state.json")
        ex2 = load_snapshot(tmp_path / "state.json")

        assert ex2.is_suspended(refs["bob"].id)

    @pytest.mark.asyncio
    async def test_custom_config_on_load(self, tmp_path):
        ex, _ = await _setup_exchange()
        save_snapshot(ex, tmp_path / "state.json")

        custom_cfg = ExchangeConfig(max_active_per_agent=99)
        ex2 = load_snapshot(tmp_path / "state.json", config=custom_cfg)
        assert ex2.config.max_active_per_agent == 99


# ── In-memory dict round-trip ────────────────────────────────────────────


class TestDictRoundtrip:
    @pytest.mark.asyncio
    async def test_snapshot_to_dict(self):
        ex, _ = await _setup_exchange()
        data = snapshot_to_dict(ex)
        assert isinstance(data, dict)
        assert "problems" in data

    @pytest.mark.asyncio
    async def test_restore_from_dict(self):
        ex, refs = await _setup_exchange()
        data = snapshot_to_dict(ex)
        ex2 = restore_from_dict(data)

        assert refs["alice"].id in ex2._agents
        assert refs["problem"].id in ex2._problems
        assert refs["solution"].id in ex2._solutions
        assert refs["review"].id in ex2._reviews

    @pytest.mark.asyncio
    async def test_empty_exchange(self):
        ex = Exchange()
        data = snapshot_to_dict(ex)
        ex2 = restore_from_dict(data)
        assert len(ex2._agents) == 0
        assert len(ex2._problems) == 0


# ── Edge cases ───────────────────────────────────────────────────────────


class TestEdgeCases:
    @pytest.mark.asyncio
    async def test_idempotent_restore(self, tmp_path):
        """Restoring twice doesn't duplicate entities."""
        ex, refs = await _setup_exchange()
        data = snapshot_to_dict(ex)
        ex2 = restore_from_dict(data)
        # Count before second restore
        agent_count = len(ex2._agents)
        problem_count = len(ex2._problems)
        # Re-restore same data — should be no-op
        from schwarma.persistence import _restore_full
        _restore_full(ex2, data)
        assert len(ex2._agents) == agent_count
        assert len(ex2._problems) == problem_count

    @pytest.mark.asyncio
    async def test_problem_status_preserved(self, tmp_path):
        """Problem statuses survive the round-trip."""
        ex, refs = await _setup_exchange()
        save_snapshot(ex, tmp_path / "state.json")
        ex2 = load_snapshot(tmp_path / "state.json")

        p = ex2._problems[refs["problem"].id]
        original = ex._problems[refs["problem"].id]
        assert p.status == original.status


class TestGlobPersistence:
    @pytest.mark.asyncio
    async def test_globs_and_glob_solutions_roundtrip(self, tmp_path):
        cfg = ExchangeConfig(
            min_reputation_to_claim=0,
            enable_staking=False,
            enable_content_guards=False,
            enable_effort_guards=False,
        )
        ex = Exchange(cfg)

        poster = Agent(name="Poster", solver=_dummy_solver, capabilities={AgentCapability.CODE_GENERATION})
        coordinator = Agent(name="Coord", solver=_dummy_solver, capabilities={AgentCapability.CODE_GENERATION})
        member = Agent(name="Member", solver=_dummy_solver, capabilities={AgentCapability.CODE_GENERATION})
        for a in (poster, coordinator, member):
            ex.register(a)

        problem = Problem(
            title="Glob Problem",
            description="Need coalition work",
            author_id=poster.id,
            tags={ProblemTag.ARCHITECTURE},
            bounty=30,
        )
        await ex.post_problem(problem)

        glob = await ex.form_glob(coordinator_id=coordinator.id, problem_id=problem.id, name="roundtrip-glob")
        await ex.join_glob(glob.id, member.id, subtask="research")
        await ex.submit_to_glob(glob.id, member.id, "member contribution")
        await ex.accept_glob_contribution(glob.id, coordinator.id, member.id)
        solution = await ex.assemble_glob_solution(glob.id, coordinator.id, "final assembly")

        assert glob.status == GlobStatus.DISSOLVED
        assert solution.id in ex._glob_solutions

        save_snapshot(ex, tmp_path / "state.json")
        ex2 = load_snapshot(tmp_path / "state.json")

        assert glob.id in ex2._globs
        assert ex2._globs[glob.id].status == GlobStatus.DISSOLVED
        assert len(ex2._globs[glob.id].memberships) == 2
        assert solution.id in ex2._glob_solutions
        gs2 = ex2._glob_solutions[solution.id]
        assert gs2.glob_id == glob.id
        assert "member contribution" in " ".join(gs2.member_contributions.values())


class TestGracefulDegradationPersistence:
    @pytest.mark.asyncio
    async def test_degraded_queue_roundtrip(self, tmp_path):
        cfg = ExchangeConfig(
            min_reputation_to_claim=0,
            enable_staking=False,
            auto_assign=True,
        )
        ex = Exchange(cfg)
        author = Agent(name="Author", solver=_dummy_solver)
        ex.register(author)

        p = await ex.post_problem(Problem(
            title="Queued",
            description="No solver available",
            author_id=author.id,
        ))
        assert p.id in ex._degraded_queue

        save_snapshot(ex, tmp_path / "state.json")
        ex2 = load_snapshot(tmp_path / "state.json")
        assert p.id in ex2._degraded_queue
