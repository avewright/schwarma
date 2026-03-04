"""Tests for safeguards integrated into the Exchange.

Covers: trust gating, content guards, reputation staking, and behavior tracking.
"""

import pytest

from schwarma.agent import Agent, AgentCapability
from schwarma.errors import GuardBlockError, PermissionError_
from schwarma.exchange import Exchange, ExchangeConfig
from schwarma.guards import QualityConfig
from schwarma.problem import Problem, ProblemStatus, ProblemTag
from schwarma.review import Review, ReviewType, ReviewVerdict
from schwarma.solution import SolutionVerdict
from schwarma.trust import Sensitivity, TrustTier


# -- Helpers ----------------------------------------------------------------

async def good_solver(desc: str, ctx: dict) -> str:
    return f"Here is a thorough well-reasoned answer for: {desc[:40]}"


async def short_solver(desc: str, ctx: dict) -> str:
    return "no"


async def secret_solver(desc: str, ctx: dict) -> str:
    return "api_key = 'xk_test_ABCDEFGHIJKLMNOPQRSTUVWXYZ'"


async def approve_solver(desc: str, ctx: dict) -> str:
    return "APPROVE — looks good"


def make_exchange(**overrides) -> Exchange:
    defaults = dict(
        reviews_required_for_accept=2,
        auto_assign=False,
        auto_review=False,
        enable_staking=True,
        min_reputation_to_claim=10,
        stake_fraction=0.1,
    )
    defaults.update(overrides)
    config = ExchangeConfig(**defaults)
    return Exchange(config)


def make_agents() -> tuple[Agent, Agent, Agent, Agent]:
    """author, solver, reviewer1, reviewer2"""
    return (
        Agent(name="Author", solver=good_solver, capabilities={AgentCapability.GENERAL}),
        Agent(name="Solver", solver=good_solver, capabilities={AgentCapability.CODE_GENERATION}),
        Agent(name="Rev1", solver=approve_solver, capabilities={AgentCapability.CODE_REVIEW}),
        Agent(name="Rev2", solver=approve_solver, capabilities={AgentCapability.CODE_REVIEW}),
    )


# -- Trust gating tests -----------------------------------------------------

class TestTrustGating:
    @pytest.mark.asyncio
    async def test_basic_agent_can_claim_internal_problem(self):
        ex = make_exchange()
        author, solver, *_ = make_agents()
        for a in (author, solver):
            ex.register(a)

        p = Problem(title="T", description="A normal internal problem.",
                    author_id=author.id, sensitivity=Sensitivity.INTERNAL)
        await ex.post_problem(p)
        # Default tier is BASIC → can access INTERNAL
        await ex.claim_problem(p.id, solver.id)
        assert p.status == ProblemStatus.CLAIMED

    @pytest.mark.asyncio
    async def test_basic_agent_blocked_from_confidential(self):
        ex = make_exchange()
        author, solver, *_ = make_agents()
        for a in (author, solver):
            ex.register(a)

        p = Problem(title="T", description="Confidential data involved.",
                    author_id=author.id, sensitivity=Sensitivity.CONFIDENTIAL)
        await ex.post_problem(p)
        with pytest.raises(PermissionError_, match="cannot access"):
            await ex.claim_problem(p.id, solver.id)

    @pytest.mark.asyncio
    async def test_promoted_agent_can_access_confidential(self):
        ex = make_exchange()
        author, solver, *_ = make_agents()
        for a in (author, solver):
            ex.register(a)

        # Manually promote solver to TRUSTED
        ex.trust_gate.assign_tier(solver.id, TrustTier.TRUSTED)

        p = Problem(title="T", description="Confidential data involved.",
                    author_id=author.id, sensitivity=Sensitivity.CONFIDENTIAL)
        await ex.post_problem(p)
        await ex.claim_problem(p.id, solver.id)
        assert p.status == ProblemStatus.CLAIMED

    @pytest.mark.asyncio
    async def test_open_problems_for_filters_by_trust(self):
        ex = make_exchange()
        author, solver, *_ = make_agents()
        for a in (author, solver):
            ex.register(a)

        p_internal = Problem(title="Internal", description="A normal problem.",
                             author_id=author.id, sensitivity=Sensitivity.INTERNAL)
        p_secret = Problem(title="Secret", description="Top secret problem.",
                           author_id=author.id, sensitivity=Sensitivity.RESTRICTED)
        await ex.post_problem(p_internal)
        await ex.post_problem(p_secret)

        visible = ex.open_problems_for(solver.id)
        assert p_internal in visible
        assert p_secret not in visible


# -- Content guard tests -----------------------------------------------------

class TestContentGuards:
    @pytest.mark.asyncio
    async def test_problem_with_secret_blocked(self):
        ex = make_exchange(enable_content_guards=True)
        author, *_ = make_agents()
        ex.register(author)

        p = Problem(
            title="Help",
            description="Use api_key='xk_test_ABCDEFGHIJKLMNOPQRSTUVWXYZ' to connect.",
            author_id=author.id,
        )
        with pytest.raises(GuardBlockError, match="blocked by content guard"):
            await ex.post_problem(p)

    @pytest.mark.asyncio
    async def test_clean_problem_passes(self):
        ex = make_exchange(enable_content_guards=True)
        author, *_ = make_agents()
        ex.register(author)

        p = Problem(
            title="Help with algorithm",
            description="How do I implement a binary search tree?",
            author_id=author.id,
        )
        result = await ex.post_problem(p)
        assert result.id == p.id

    @pytest.mark.asyncio
    async def test_solution_with_secret_blocked(self):
        ex = make_exchange(enable_content_guards=True)
        author, solver, *_ = make_agents()
        for a in (author, solver):
            ex.register(a)

        p = Problem(title="T", description="A good clean problem.", author_id=author.id)
        await ex.post_problem(p)
        await ex.claim_problem(p.id, solver.id)

        with pytest.raises(GuardBlockError, match="blocked by content guard"):
            await ex.solve_problem(
                p.id, solver.id,
                solution_body="password = 'SuperSecretPassword1234567890'",
            )

    @pytest.mark.asyncio
    async def test_short_solution_flagged_not_blocked(self):
        """Short solutions are flagged but not blocked (effort guard)."""
        ex = make_exchange(
            enable_content_guards=True,
            enable_effort_guards=True,
            quality_config=QualityConfig(min_length=20),
        )
        author, solver, *_ = make_agents()
        for a in (author, solver):
            ex.register(a)

        p = Problem(title="T", description="A good problem.", author_id=author.id)
        await ex.post_problem(p)
        await ex.claim_problem(p.id, solver.id)

        # Short solution is flagged but not blocked — should still go through
        sol = await ex.solve_problem(p.id, solver.id, solution_body="ok sure")
        assert sol.body == "ok sure"

    @pytest.mark.asyncio
    async def test_guards_disabled(self):
        ex = make_exchange(enable_content_guards=False, enable_effort_guards=False)
        author, solver, *_ = make_agents()
        for a in (author, solver):
            ex.register(a)

        p = Problem(
            title="T",
            description="api_key='xk_test_ABCDEFGHIJKLMNOPQRSTUVWXYZ'",
            author_id=author.id,
        )
        # With guards disabled, should not raise
        await ex.post_problem(p)
        assert p.id in [prob.id for prob in ex.open_problems()]


# -- Reputation gating & staking tests --------------------------------------

class TestReputationGating:
    @pytest.mark.asyncio
    async def test_low_reputation_cannot_claim(self):
        ex = make_exchange(min_reputation_to_claim=100)
        author, solver, *_ = make_agents()
        for a in (author, solver):
            ex.register(a)

        p = Problem(title="T", description="A problem.", author_id=author.id)
        await ex.post_problem(p)

        # Default rep is 50, minimum is 100
        with pytest.raises(PermissionError_, match="below minimum"):
            await ex.claim_problem(p.id, solver.id)


class TestStaking:
    @pytest.mark.asyncio
    async def test_stake_deducted_on_claim(self):
        ex = make_exchange(enable_staking=True, stake_fraction=0.2)
        author, solver, *_ = make_agents()
        for a in (author, solver):
            ex.register(a)

        p = Problem(title="T", description="A problem.", author_id=author.id, bounty=20)
        await ex.post_problem(p)
        rep_before = ex.ledger.balance(solver.id)
        await ex.claim_problem(p.id, solver.id)
        rep_after = ex.ledger.balance(solver.id)

        stake = max(1, int(20 * 0.2))  # = 4
        assert rep_after == rep_before - stake

    @pytest.mark.asyncio
    async def test_stake_refunded_on_accept(self):
        ex = make_exchange(enable_staking=True, stake_fraction=0.1)
        author, solver, r1, r2 = make_agents()
        for a in (author, solver, r1, r2):
            ex.register(a)

        p = Problem(title="T", description="A good problem.", author_id=author.id, bounty=20)
        await ex.post_problem(p)
        await ex.claim_problem(p.id, solver.id)
        sol = await ex.solve_problem(p.id, solver.id)

        # Approve solution
        for reviewer in (r1, r2):
            review = Review(
                solution_id=sol.id,
                reviewer_id=reviewer.id,
                review_type=ReviewType.CORRECTNESS,
                verdict=ReviewVerdict.APPROVE,
            )
            await ex.submit_review(review)

        assert sol.verdict == SolutionVerdict.ACCEPTED
        # Solver should have: initial + posting_bonus + submission_bonus + stake_refund + bounty - stake
        # Net is positive
        balance = ex.ledger.balance(solver.id)
        assert balance > 50  # well above initial

    @pytest.mark.asyncio
    async def test_stake_forfeited_on_reject(self):
        ex = make_exchange(enable_staking=True, stake_fraction=0.1)
        author, solver, r1, r2 = make_agents()
        for a in (author, solver, r1, r2):
            ex.register(a)

        p = Problem(title="T", description="A good problem.", author_id=author.id, bounty=20)
        await ex.post_problem(p)

        rep_before_claim = ex.ledger.balance(solver.id)
        await ex.claim_problem(p.id, solver.id)
        sol = await ex.solve_problem(p.id, solver.id)

        # Reject solution
        for reviewer in (r1, r2):
            review = Review(
                solution_id=sol.id,
                reviewer_id=reviewer.id,
                review_type=ReviewType.CORRECTNESS,
                verdict=ReviewVerdict.REJECT,
            )
            await ex.submit_review(review)

        assert sol.verdict == SolutionVerdict.REJECTED
        # Solver lost stake + rejection penalty, no bounty
        balance = ex.ledger.balance(solver.id)
        assert balance < rep_before_claim

    @pytest.mark.asyncio
    async def test_staking_disabled(self):
        ex = make_exchange(enable_staking=False)
        author, solver, *_ = make_agents()
        for a in (author, solver):
            ex.register(a)

        p = Problem(title="T", description="A problem.", author_id=author.id, bounty=20)
        await ex.post_problem(p)
        rep_before = ex.ledger.balance(solver.id)
        await ex.claim_problem(p.id, solver.id)
        # No stake deducted
        assert ex.ledger.balance(solver.id) == rep_before


# -- Behavior tracking integration ------------------------------------------

class TestBehaviorIntegration:
    @pytest.mark.asyncio
    async def test_review_tracked_in_behavior_analyzer(self):
        ex = make_exchange()
        author, solver, r1, r2 = make_agents()
        for a in (author, solver, r1, r2):
            ex.register(a)

        p = Problem(title="T", description="A good clean problem.", author_id=author.id)
        await ex.post_problem(p)
        sol = await ex.claim_and_solve(p.id, solver.id)

        review = Review(
            solution_id=sol.id,
            reviewer_id=r1.id,
            review_type=ReviewType.CORRECTNESS,
            verdict=ReviewVerdict.APPROVE,
        )
        await ex.submit_review(review)

        # Behavior analyzer should have recorded the review
        assert ex.behavior.approval_rate(r1.id) == 1.0
        assert ex.behavior.pairwise_count(r1.id, solver.id) == 1

    @pytest.mark.asyncio
    async def test_trust_auto_promotion_on_review(self):
        """Reviewer earns rep → trust tier auto-promotion."""
        ex = make_exchange()
        author, solver, r1, r2 = make_agents()
        for a in (author, solver, r1, r2):
            ex.register(a)

        # Pump up reviewer's reputation so maybe_promote can trigger
        # Default TRUSTED threshold is 120 — give them a big boost
        from schwarma.reputation import ReputationEvent
        ex.ledger.record(r1.id, ReputationEvent.BONUS, delta=80)

        p = Problem(title="T", description="A good clean problem.", author_id=author.id)
        await ex.post_problem(p)
        sol = await ex.claim_and_solve(p.id, solver.id)

        review = Review(
            solution_id=sol.id,
            reviewer_id=r1.id,
            review_type=ReviewType.CORRECTNESS,
            verdict=ReviewVerdict.APPROVE,
        )
        await ex.submit_review(review)

        # After bonus (80) + initial (50) + review (3) = 133 → should be TRUSTED
        assert ex.trust_gate.get_tier(r1.id) == TrustTier.TRUSTED
