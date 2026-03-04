"""Tests for solutions_needing_review on Exchange."""

import pytest
from schwarma import (
    Agent,
    AgentCapability,
    Exchange,
    ExchangeConfig,
    Problem,
    ProblemTag,
    Review,
    ReviewType,
    ReviewVerdict,
)


async def _solver(desc, ctx):
    return f"solution for {desc[:30]}"


def make_exchange() -> Exchange:
    return Exchange(ExchangeConfig(
        reviews_required_for_accept=2,
        auto_assign=False,
        auto_review=False,
        enable_content_guards=False,
        enable_staking=False,
    ))


def make_agent(name: str) -> Agent:
    return Agent(
        name=name,
        solver=_solver,
        capabilities={AgentCapability.CODE_GENERATION, AgentCapability.CODE_REVIEW},
    )


class TestSolutionsNeedingReview:
    @pytest.mark.asyncio
    async def test_returns_pending_solutions(self):
        ex = make_exchange()
        alice, bob, carol = make_agent("Alice"), make_agent("Bob"), make_agent("Carol")
        for a in (alice, bob, carol):
            ex.register(a)

        p = Problem(title="T", description="D", author_id=carol.id, tags={ProblemTag.FEATURE})
        await ex.post_problem(p)
        await ex.claim_problem(p.id, alice.id)
        sol = await ex.solve_problem(p.id, alice.id)

        needed = ex.solutions_needing_review()
        assert len(needed) == 1
        assert needed[0].id == sol.id

    @pytest.mark.asyncio
    async def test_excludes_own_solutions(self):
        ex = make_exchange()
        alice, bob = make_agent("Alice"), make_agent("Bob")
        for a in (alice, bob):
            ex.register(a)

        p = Problem(title="T", description="D", author_id=bob.id)
        await ex.post_problem(p)
        await ex.claim_problem(p.id, alice.id)
        await ex.solve_problem(p.id, alice.id)

        # Alice should not see her own solution
        needed = ex.solutions_needing_review(alice.id)
        assert len(needed) == 0

        # Bob should see it
        needed = ex.solutions_needing_review(bob.id)
        assert len(needed) == 1

    @pytest.mark.asyncio
    async def test_excludes_already_reviewed(self):
        ex = make_exchange()
        alice, bob, carol = make_agent("Alice"), make_agent("Bob"), make_agent("Carol")
        for a in (alice, bob, carol):
            ex.register(a)

        p = Problem(title="T", description="D", author_id=carol.id)
        await ex.post_problem(p)
        await ex.claim_problem(p.id, alice.id)
        sol = await ex.solve_problem(p.id, alice.id)

        # Bob reviews
        r = Review(solution_id=sol.id, reviewer_id=bob.id,
                   review_type=ReviewType.CORRECTNESS, verdict=ReviewVerdict.APPROVE)
        await ex.submit_review(r)

        # Bob should no longer see it
        needed = ex.solutions_needing_review(bob.id)
        assert len(needed) == 0

        # Carol should still see it (only 1 of 2 required reviews done)
        needed = ex.solutions_needing_review(carol.id)
        assert len(needed) == 1

    @pytest.mark.asyncio
    async def test_excludes_fully_reviewed(self):
        ex = make_exchange()
        alice, bob, carol, dan = (
            make_agent("Alice"), make_agent("Bob"),
            make_agent("Carol"), make_agent("Dan"),
        )
        for a in (alice, bob, carol, dan):
            ex.register(a)

        p = Problem(title="T", description="D", author_id=dan.id)
        await ex.post_problem(p)
        await ex.claim_problem(p.id, alice.id)
        sol = await ex.solve_problem(p.id, alice.id)

        for reviewer in (bob, carol):
            r = Review(solution_id=sol.id, reviewer_id=reviewer.id,
                       review_type=ReviewType.CORRECTNESS,
                       verdict=ReviewVerdict.APPROVE, confidence=1.0)
            await ex.submit_review(r)

        # Solution now has 2 reviews (= required) — no longer needs review
        needed = ex.solutions_needing_review(dan.id)
        assert len(needed) == 0

    @pytest.mark.asyncio
    async def test_respects_limit(self):
        ex = make_exchange()
        alice, bob = make_agent("Alice"), make_agent("Bob")
        for a in (alice, bob):
            ex.register(a)

        for i in range(5):
            p = Problem(title=f"P{i}", description="D", author_id=bob.id)
            await ex.post_problem(p)
            await ex.claim_problem(p.id, alice.id)
            await ex.solve_problem(p.id, alice.id)

        needed = ex.solutions_needing_review(bob.id, limit=2)
        assert len(needed) == 2

    @pytest.mark.asyncio
    async def test_without_agent_id_returns_all(self):
        """When no agent_id is given, returns all pending solutions."""
        ex = make_exchange()
        alice, bob = make_agent("Alice"), make_agent("Bob")
        for a in (alice, bob):
            ex.register(a)

        for i in range(3):
            p = Problem(title=f"P{i}", description="D", author_id=bob.id)
            await ex.post_problem(p)
            await ex.claim_problem(p.id, alice.id)
            await ex.solve_problem(p.id, alice.id)

        # No agent filter — return all
        needed = ex.solutions_needing_review()
        assert len(needed) == 3
