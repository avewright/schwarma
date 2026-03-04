"""Tests for ModelTier — agent tiers, swap tier-matching, triage tier-scoring, exchange tier gating."""

from uuid import uuid4

import pytest

from schwarma.agent import Agent, AgentCapability, ModelTier
from schwarma.errors import PermissionError_
from schwarma.exchange import Exchange, ExchangeConfig
from schwarma.problem import Problem, ProblemTag
from schwarma.swap import SwapPool


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

async def _dummy_solver(desc, ctx):
    return "solution body that is long enough to pass effort guards easily"


def _make_agent(name: str, tier: ModelTier = ModelTier.STANDARD, **kw) -> Agent:
    defaults = dict(
        name=name,
        solver=_dummy_solver,
        capabilities={AgentCapability.CODE_GENERATION, AgentCapability.CODE_REVIEW},
        model_tier=tier,
    )
    defaults.update(kw)
    return Agent(**defaults)


def _make_problem(author_id, **kw) -> Problem:
    defaults = dict(
        title="Test",
        description="Describe the test problem in enough detail",
        author_id=author_id,
        tags={ProblemTag.FEATURE},
        bounty=10,
    )
    defaults.update(kw)
    return Problem(**defaults)


# ------------------------------------------------------------------
# ModelTier Enum
# ------------------------------------------------------------------

class TestModelTierEnum:
    def test_ordinal_values(self):
        assert ModelTier.LIGHTWEIGHT.value < ModelTier.STANDARD.value
        assert ModelTier.STANDARD.value < ModelTier.PREMIUM.value

    def test_default_tier_is_standard(self):
        agent = Agent(name="A", solver=_dummy_solver)
        assert agent.model_tier == ModelTier.STANDARD


# ------------------------------------------------------------------
# Swap Tier-Matching
# ------------------------------------------------------------------

class TestSwapTierMatching:
    def test_same_tier_matches(self):
        pool = SwapPool()
        a = _make_agent("Alice", ModelTier.PREMIUM)
        b = _make_agent("Bob", ModelTier.PREMIUM)
        pa = _make_problem(a.id, tags={ProblemTag.BUG})
        pb = _make_problem(b.id, tags={ProblemTag.FEATURE})

        pool.submit(a, pa)
        pool.submit(b, pb)

        match = pool.try_match()
        assert match is not None

    def test_adjacent_tier_matches(self):
        pool = SwapPool()
        a = _make_agent("Alice", ModelTier.STANDARD)
        b = _make_agent("Bob", ModelTier.PREMIUM)
        pa = _make_problem(a.id, tags={ProblemTag.BUG})
        pb = _make_problem(b.id, tags={ProblemTag.FEATURE})

        pool.submit(a, pa)
        pool.submit(b, pb)

        match = pool.try_match()
        assert match is not None  # gap=1, within default max_tier_gap

    def test_distant_tier_rejected(self):
        pool = SwapPool()
        a = _make_agent("Alice", ModelTier.LIGHTWEIGHT)
        b = _make_agent("Bob", ModelTier.PREMIUM)
        pa = _make_problem(a.id, tags={ProblemTag.BUG})
        pb = _make_problem(b.id, tags={ProblemTag.FEATURE})

        pool.submit(a, pa)
        pool.submit(b, pb)

        match = pool.try_match()
        assert match is None  # gap=2, exceeds default max_tier_gap=1

    def test_specialized_matches_any_tier(self):
        pool = SwapPool()
        a = _make_agent("Alice", ModelTier.SPECIALIZED)
        b = _make_agent("Bob", ModelTier.LIGHTWEIGHT)
        pa = _make_problem(a.id, tags={ProblemTag.BUG})
        pb = _make_problem(b.id, tags={ProblemTag.FEATURE})

        pool.submit(a, pa)
        pool.submit(b, pb)

        match = pool.try_match()
        assert match is not None

    def test_custom_max_tier_gap(self):
        pool = SwapPool(max_tier_gap=2)
        a = _make_agent("Alice", ModelTier.LIGHTWEIGHT)
        b = _make_agent("Bob", ModelTier.PREMIUM)
        pa = _make_problem(a.id, tags={ProblemTag.BUG})
        pb = _make_problem(b.id, tags={ProblemTag.FEATURE})

        pool.submit(a, pa)
        pool.submit(b, pb)

        match = pool.try_match()
        assert match is not None  # gap=2, within max_tier_gap=2


# ------------------------------------------------------------------
# Exchange Tier Gating
# ------------------------------------------------------------------

class TestExchangeTierGating:
    async def test_lightweight_blocked_from_premium_problem(self):
        config = ExchangeConfig(
            enable_content_guards=False,
            enable_effort_guards=False,
            enable_staking=False,
            auto_assign=False,
            auto_review=False,
        )
        ex = Exchange(config)

        author = _make_agent("Author", ModelTier.PREMIUM)
        solver = _make_agent("Solver", ModelTier.LIGHTWEIGHT)
        ex.register(author)
        ex.register(solver)

        p = _make_problem(author.id, min_solver_tier=ModelTier.PREMIUM)
        await ex.post_problem(p)

        with pytest.raises(PermissionError_, match="below minimum solver tier"):
            await ex.claim_problem(p.id, solver.id)

    async def test_premium_can_claim_premium_problem(self):
        config = ExchangeConfig(
            enable_content_guards=False,
            enable_effort_guards=False,
            enable_staking=False,
            auto_assign=False,
            auto_review=False,
            use_effective_tier=False,  # test declared tier, not skill-derived
        )
        ex = Exchange(config)

        author = _make_agent("Author", ModelTier.PREMIUM)
        solver = _make_agent("Solver", ModelTier.PREMIUM)
        ex.register(author)
        ex.register(solver)

        p = _make_problem(author.id, min_solver_tier=ModelTier.PREMIUM)
        await ex.post_problem(p)

        result = await ex.claim_problem(p.id, solver.id)
        assert result.status.name == "CLAIMED"

    async def test_specialized_bypasses_tier_requirement(self):
        config = ExchangeConfig(
            enable_content_guards=False,
            enable_effort_guards=False,
            enable_staking=False,
            auto_assign=False,
            auto_review=False,
        )
        ex = Exchange(config)

        author = _make_agent("Author", ModelTier.PREMIUM)
        solver = _make_agent("Solver", ModelTier.SPECIALIZED)
        ex.register(author)
        ex.register(solver)

        p = _make_problem(author.id, min_solver_tier=ModelTier.PREMIUM)
        await ex.post_problem(p)

        result = await ex.claim_problem(p.id, solver.id)
        assert result.status.name == "CLAIMED"

    async def test_no_tier_requirement_allows_any(self):
        config = ExchangeConfig(
            enable_content_guards=False,
            enable_effort_guards=False,
            enable_staking=False,
            auto_assign=False,
            auto_review=False,
        )
        ex = Exchange(config)

        author = _make_agent("Author", ModelTier.PREMIUM)
        solver = _make_agent("Solver", ModelTier.LIGHTWEIGHT)
        ex.register(author)
        ex.register(solver)

        p = _make_problem(author.id)  # no min_solver_tier
        await ex.post_problem(p)

        result = await ex.claim_problem(p.id, solver.id)
        assert result.status.name == "CLAIMED"
