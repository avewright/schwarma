"""Tests for effective tier integration in the Exchange.

These tests validate the end-to-end flow: skill updates on accept/reject,
effective tier gating in claim, and triage/swap integration.
"""

import pytest
from uuid import uuid4

from schwarma.agent import Agent, AgentCapability, ModelTier
from schwarma.errors import PermissionError_
from schwarma.exchange import Exchange, ExchangeConfig
from schwarma.problem import Problem, ProblemTag
from schwarma.review import Review, ReviewType, ReviewVerdict
from schwarma.skills import SkillConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _dummy_solver(description: str, context: dict) -> str:
    return f"Solution for: {description[:50]}"


def _make_agent(name: str, tier: ModelTier = ModelTier.STANDARD) -> Agent:
    return Agent(name=name, solver=_dummy_solver, model_tier=tier)


def _make_problem(
    author_id,
    title: str = "Test",
    min_solver_tier: ModelTier | None = None,
    bounty: int = 10,
    tags: set[ProblemTag] | None = None,
) -> Problem:
    return Problem(
        title=title,
        description="Test problem",
        author_id=author_id,
        bounty=bounty,
        min_solver_tier=min_solver_tier,
        tags=tags or {ProblemTag.BUG},
    )


def _make_exchange(**overrides) -> Exchange:
    defaults = dict(
        enable_content_guards=False,
        enable_effort_guards=False,
        enable_staking=False,
        auto_assign=False,
        auto_review=False,
        enable_skill_tracking=True,
        use_effective_tier=True,
        enable_difficulty=True,
    )
    defaults.update(overrides)
    return Exchange(ExchangeConfig(**defaults))


async def _approve_solution(ex: Exchange, solution, reviewer):
    """Submit enough approvals to accept a solution.

    Creates additional disposable reviewers if quorum > 1 so that each
    review comes from a unique reviewer (required by the exchange).
    """
    reviewers = [reviewer]
    for i in range(ex.config.reviews_required_for_accept - 1):
        extra = Agent(
            name=f"_extra_reviewer_{i}",
            solver=_dummy_solver,
            capabilities={AgentCapability.CODE_REVIEW},
        )
        ex.register(extra)
        reviewers.append(extra)
    for r in reviewers:
        review = Review(
            solution_id=solution.id,
            reviewer_id=r.id,
            review_type=ReviewType.CORRECTNESS,
            verdict=ReviewVerdict.APPROVE,
            body="LGTM",
        )
        await ex.submit_review(review)


async def _reject_solution(ex: Exchange, solution, reviewer):
    """Submit enough rejections to reject a solution."""
    reviewers = [reviewer]
    for i in range(ex.config.reviews_required_for_accept - 1):
        extra = Agent(
            name=f"_extra_rejector_{i}",
            solver=_dummy_solver,
            capabilities={AgentCapability.CODE_REVIEW},
        )
        ex.register(extra)
        reviewers.append(extra)
    for r in reviewers:
        review = Review(
            solution_id=solution.id,
            reviewer_id=r.id,
            review_type=ReviewType.CORRECTNESS,
            verdict=ReviewVerdict.REJECT,
            body="Bad",
        )
        await ex.submit_review(review)


# ---------------------------------------------------------------------------
# Effective tier gating
# ---------------------------------------------------------------------------

class TestEffectiveTierGating:
    async def test_new_agent_is_probationary(self):
        """Fresh agent with no history should be probationary."""
        ex = _make_exchange()
        agent = _make_agent("New", ModelTier.PREMIUM)
        ex.register(agent)
        assert ex.is_probationary(agent.id) is True

    async def test_probationary_agent_blocked_from_premium_problem(self):
        """A probationary PREMIUM-declared agent can't claim PREMIUM problems."""
        ex = _make_exchange()
        author = _make_agent("Author")
        solver = _make_agent("Solver", ModelTier.PREMIUM)
        ex.register(author)
        ex.register(solver)
        p = _make_problem(author.id, min_solver_tier=ModelTier.PREMIUM)
        await ex.post_problem(p)
        with pytest.raises(PermissionError_, match="effective tier"):
            await ex.claim_problem(p.id, solver.id)

    async def test_specialized_bypass_even_when_probationary(self):
        ex = _make_exchange()
        author = _make_agent("Author")
        solver = _make_agent("Solver", ModelTier.SPECIALIZED)
        ex.register(author)
        ex.register(solver)
        p = _make_problem(author.id, min_solver_tier=ModelTier.PREMIUM)
        await ex.post_problem(p)
        result = await ex.claim_problem(p.id, solver.id)
        assert result.status.name == "CLAIMED"

    async def test_effective_tier_fallback_when_disabled(self):
        """With use_effective_tier=False, declared tier is used."""
        ex = _make_exchange(use_effective_tier=False)
        agent = _make_agent("A", ModelTier.PREMIUM)
        ex.register(agent)
        assert ex.get_effective_tier(agent.id) == ModelTier.PREMIUM


# ---------------------------------------------------------------------------
# Skill updates on accept/reject
# ---------------------------------------------------------------------------

class TestSkillUpdatesOnOutcome:
    async def test_skill_updates_on_accept(self):
        ex = _make_exchange()
        author = _make_agent("Author")
        solver = _make_agent("Solver")
        reviewer = _make_agent("Reviewer")
        ex.register(author)
        ex.register(solver)
        ex.register(reviewer)

        p = _make_problem(author.id)
        await ex.post_problem(p)
        await ex.claim_problem(p.id, solver.id)
        sol = await ex.solve_problem(p.id, solver.id)
        await _approve_solution(ex, sol, reviewer)

        # Solver should have gained skill in DEBUGGING (from BUG tag)
        summary = ex.get_skill_summary(solver.id)
        assert summary["total_outcomes"] >= 1

    async def test_skill_updates_on_reject(self):
        ex = _make_exchange()
        author = _make_agent("Author")
        solver = _make_agent("Solver")
        reviewer = _make_agent("Reviewer")
        ex.register(author)
        ex.register(solver)
        ex.register(reviewer)

        p = _make_problem(author.id)
        await ex.post_problem(p)
        await ex.claim_problem(p.id, solver.id)
        sol = await ex.solve_problem(p.id, solver.id)
        await _reject_solution(ex, sol, reviewer)

        summary = ex.get_skill_summary(solver.id)
        assert summary["total_outcomes"] >= 1

    async def test_difficulty_tracked_on_claim(self):
        ex = _make_exchange()
        author = _make_agent("Author")
        solver = _make_agent("Solver")
        ex.register(author)
        ex.register(solver)

        p = _make_problem(author.id)
        await ex.post_problem(p)
        await ex.claim_problem(p.id, solver.id)

        rec = ex.difficulty.get_record(p.id)
        assert rec is not None
        assert rec.attempt_count == 1


# ---------------------------------------------------------------------------
# Effective tier earned through wins
# ---------------------------------------------------------------------------

class TestEffectiveTierEarned:
    async def test_many_wins_earn_higher_tier(self):
        """After many solutions accepted, effective tier should rise."""
        config = ExchangeConfig(
            enable_content_guards=False,
            enable_effort_guards=False,
            enable_staking=False,
            auto_assign=False,
            auto_review=False,
            enable_skill_tracking=True,
            use_effective_tier=True,
            skill_config=SkillConfig(
                probation_outcomes=2,
                min_outcomes_for_tier=2,
            ),
        )
        ex = Exchange(config)

        author = _make_agent("Author")
        solver = _make_agent("Solver", ModelTier.PREMIUM)
        reviewer = _make_agent("Reviewer")
        ex.register(author)
        ex.register(solver)
        ex.register(reviewer)

        # Run 8 successful solve cycles
        for i in range(8):
            p = _make_problem(author.id, title=f"P{i}")
            await ex.post_problem(p)
            await ex.claim_problem(p.id, solver.id)
            sol = await ex.solve_problem(p.id, solver.id)
            await _approve_solution(ex, sol, reviewer)

        tier = ex.get_effective_tier(solver.id)
        # After 8 wins, should be above LIGHTWEIGHT
        assert tier.value >= ModelTier.STANDARD.value
        assert not ex.is_probationary(solver.id)
