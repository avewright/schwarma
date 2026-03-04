"""Tests for the Exchange orchestrator — the core integration tests."""

import pytest

from schwarma.agent import Agent, AgentCapability
from schwarma.calibration import CalibrationConfig, CalibrationDifficulty, CalibrationProblem
from schwarma.errors import (
    DuplicateError,
    NotFoundError,
    PermissionError_,
    StateError,
    SuspendedError,
    ValidationError,
)
from schwarma.exchange import Exchange, ExchangeConfig
from schwarma.problem import Problem, ProblemStatus, ProblemTag
from schwarma.review import Review, ReviewType, ReviewVerdict
from schwarma.solution import SolutionVerdict


# -- Helpers ----------------------------------------------------------------

async def auto_solver(desc: str, ctx: dict) -> str:
    return f"answer: {desc[:30]}"


async def approve_solver(desc: str, ctx: dict) -> str:
    return "APPROVE — looks good"


def make_exchange(**kwargs) -> Exchange:
    config = ExchangeConfig(
        reviews_required_for_accept=2,
        auto_assign=False,
        auto_review=False,
        **kwargs,
    )
    return Exchange(config)


def make_agents(n: int = 4):
    return [
        Agent(
            name=f"Agent-{i}",
            solver=auto_solver if i == 0 else approve_solver,
            capabilities={AgentCapability.CODE_GENERATION, AgentCapability.CODE_REVIEW},
        )
        for i in range(n)
    ]


# -- Tests ------------------------------------------------------------------

class TestPostAndClaim:
    @pytest.mark.asyncio
    async def test_post_problem(self):
        ex = make_exchange()
        agents = make_agents(2)
        for a in agents:
            ex.register(a)

        p = Problem(
            title="Test problem",
            description="Solve X",
            author_id=agents[0].id,
            tags={ProblemTag.GENERAL},
        )
        await ex.post_problem(p)
        assert p.id in [prob.id for prob in ex.open_problems()]

    @pytest.mark.asyncio
    async def test_claim_problem(self):
        ex = make_exchange()
        agents = make_agents(2)
        for a in agents:
            ex.register(a)

        p = Problem(title="T", description="D", author_id=agents[0].id)
        await ex.post_problem(p)
        await ex.claim_problem(p.id, agents[1].id)
        assert p.status == ProblemStatus.CLAIMED

    @pytest.mark.asyncio
    async def test_solve_problem(self):
        ex = make_exchange()
        agents = make_agents(2)
        for a in agents:
            ex.register(a)

        p = Problem(title="T", description="desc", author_id=agents[0].id)
        await ex.post_problem(p)
        sol = await ex.claim_and_solve(p.id, agents[0].id)
        assert sol.body.startswith("answer:")
        assert p.status == ProblemStatus.SOLVED


class TestReviewWorkflow:
    @pytest.mark.asyncio
    async def test_two_approvals_accept_solution(self):
        ex = make_exchange()
        agents = make_agents(4)
        for a in agents:
            ex.register(a)

        p = Problem(title="T", description="D", author_id=agents[0].id, bounty=20)
        await ex.post_problem(p)
        sol = await ex.claim_and_solve(p.id, agents[1].id)

        for reviewer in agents[2:4]:
            review = Review(
                solution_id=sol.id,
                reviewer_id=reviewer.id,
                review_type=ReviewType.CORRECTNESS,
                verdict=ReviewVerdict.APPROVE,
                body="LGTM",
            )
            await ex.submit_review(review)

        assert sol.verdict == SolutionVerdict.ACCEPTED
        assert p.status == ProblemStatus.CLOSED
        # Solver should have earned the bounty
        assert ex.ledger.balance(agents[1].id) > 50  # initial is 50

    @pytest.mark.asyncio
    async def test_two_rejections_reopen_problem(self):
        ex = make_exchange()
        agents = make_agents(4)
        for a in agents:
            ex.register(a)

        p = Problem(title="T", description="D", author_id=agents[0].id)
        await ex.post_problem(p)
        sol = await ex.claim_and_solve(p.id, agents[1].id)

        for reviewer in agents[2:4]:
            review = Review(
                solution_id=sol.id,
                reviewer_id=reviewer.id,
                review_type=ReviewType.CORRECTNESS,
                verdict=ReviewVerdict.REJECT,
                body="Nope",
            )
            await ex.submit_review(review)

        assert sol.verdict == SolutionVerdict.REJECTED
        assert p.status == ProblemStatus.OPEN  # re-opened


class TestReputation:
    @pytest.mark.asyncio
    async def test_posting_earns_reputation(self):
        ex = make_exchange()
        agents = make_agents(1)
        ex.register(agents[0])

        initial = ex.ledger.balance(agents[0].id)
        p = Problem(title="T", description="D", author_id=agents[0].id)
        await ex.post_problem(p)
        assert ex.ledger.balance(agents[0].id) > initial

    @pytest.mark.asyncio
    async def test_reviewing_earns_reputation(self):
        ex = make_exchange()
        agents = make_agents(3)
        for a in agents:
            ex.register(a)

        p = Problem(title="T", description="D", author_id=agents[0].id)
        await ex.post_problem(p)
        sol = await ex.claim_and_solve(p.id, agents[1].id)

        reviewer_rep_before = ex.ledger.balance(agents[2].id)
        review = Review(
            solution_id=sol.id,
            reviewer_id=agents[2].id,
            review_type=ReviewType.CORRECTNESS,
            verdict=ReviewVerdict.APPROVE,
        )
        await ex.submit_review(review)
        assert ex.ledger.balance(agents[2].id) > reviewer_rep_before

    @pytest.mark.asyncio
    async def test_leaderboard_ordering(self):
        ex = make_exchange()
        agents = make_agents(4)
        for a in agents:
            ex.register(a)

        # Agent 1 does extra work
        p = Problem(title="T", description="D", author_id=agents[0].id, bounty=30)
        await ex.post_problem(p)
        sol = await ex.claim_and_solve(p.id, agents[1].id)

        for reviewer in agents[2:4]:
            r = Review(
                solution_id=sol.id,
                reviewer_id=reviewer.id,
                review_type=ReviewType.CORRECTNESS,
                verdict=ReviewVerdict.APPROVE,
            )
            await ex.submit_review(r)

        board = ex.leaderboard()
        # Agent 1 (solver with bounty) should be on top
        assert board[0]["agent_id"] == agents[1].id


class TestSwapIntegration:
    @pytest.mark.asyncio
    async def test_swap_pool_matches(self):
        ex = make_exchange()
        agents = make_agents(2)
        for a in agents:
            ex.register(a)

        p1 = Problem(title="P1", description="D1", author_id=agents[0].id, tags={ProblemTag.BUG})
        p2 = Problem(title="P2", description="D2", author_id=agents[1].id, tags={ProblemTag.BUG})
        await ex.post_problem(p1)
        await ex.post_problem(p2)

        await ex.submit_swap(agents[0].id, p1.id)
        await ex.submit_swap(agents[1].id, p2.id)

        matches = await ex.run_swaps()
        assert len(matches) == 1
        assert set(m.name for m in matches[0].agents) == {agents[0].name, agents[1].name}


class TestConfidenceWeighting:
    """Review confidence weighting in _evaluate_solution."""

    @pytest.mark.asyncio
    async def test_low_confidence_approvals_insufficient(self):
        """Two approvals at confidence 0.4 each = 0.8, below threshold 2."""
        ex = make_exchange()
        agents = make_agents(4)
        for a in agents:
            ex.register(a)

        p = Problem(title="Conf test", description="Test confidence weighting",
                     author_id=agents[0].id)
        await ex.post_problem(p)
        await ex.claim_problem(p.id, agents[1].id)
        sol = await ex.solve_problem(p.id, agents[1].id,
                                      solution_body="A valid solution body here")

        # Two low-confidence approvals
        for reviewer in [agents[2], agents[3]]:
            r = Review(
                solution_id=sol.id, reviewer_id=reviewer.id,
                review_type=ReviewType.CORRECTNESS, verdict=ReviewVerdict.APPROVE,
                confidence=0.4,
            )
            await ex.submit_review(r)

        assert sol.verdict.name == "PENDING"  # 0.8 < 2.0, not yet accepted

    @pytest.mark.asyncio
    async def test_high_confidence_approvals_sufficient(self):
        """Two approvals at confidence 1.0 each = 2.0, meets threshold 2."""
        ex = make_exchange()
        agents = make_agents(4)
        for a in agents:
            ex.register(a)

        p = Problem(title="Conf test 2", description="Test high confidence weighting",
                     author_id=agents[0].id)
        await ex.post_problem(p)
        await ex.claim_problem(p.id, agents[1].id)
        sol = await ex.solve_problem(p.id, agents[1].id,
                                      solution_body="A valid solution body here")

        for reviewer in [agents[2], agents[3]]:
            r = Review(
                solution_id=sol.id, reviewer_id=reviewer.id,
                review_type=ReviewType.CORRECTNESS, verdict=ReviewVerdict.APPROVE,
                confidence=1.0,
            )
            await ex.submit_review(r)

        assert sol.verdict.name == "ACCEPTED"


class TestReviewDiversity:
    """Require distinct reviewers before accepting/rejecting."""

    @pytest.mark.asyncio
    async def test_duplicate_reviewer_blocked(self):
        """Two reviews from the same agent should not trigger acceptance."""
        ex = make_exchange(min_unique_reviewers=2)
        agents = make_agents(3)
        for a in agents:
            ex.register(a)

        p = Problem(title="Div test", description="Test review diversity requirement",
                     author_id=agents[0].id)
        await ex.post_problem(p)
        await ex.claim_problem(p.id, agents[1].id)
        sol = await ex.solve_problem(p.id, agents[1].id,
                                      solution_body="A valid solution body here")

        # Same reviewer submits two approvals
        for _ in range(2):
            r = Review(
                solution_id=sol.id, reviewer_id=agents[2].id,
                review_type=ReviewType.CORRECTNESS, verdict=ReviewVerdict.APPROVE,
            )
            await ex.submit_review(r)

        assert sol.verdict.name == "PENDING"  # only 1 unique reviewer

    @pytest.mark.asyncio
    async def test_diverse_reviewers_accepted(self):
        """Two distinct reviewers satisfy the diversity requirement."""
        ex = make_exchange(min_unique_reviewers=2)
        agents = make_agents(4)
        for a in agents:
            ex.register(a)

        p = Problem(title="Div pass", description="Test review diversity passes ok",
                     author_id=agents[0].id)
        await ex.post_problem(p)
        await ex.claim_problem(p.id, agents[1].id)
        sol = await ex.solve_problem(p.id, agents[1].id,
                                      solution_body="A valid solution body here")

        for reviewer in [agents[2], agents[3]]:
            r = Review(
                solution_id=sol.id, reviewer_id=reviewer.id,
                review_type=ReviewType.CORRECTNESS, verdict=ReviewVerdict.APPROVE,
            )
            await ex.submit_review(r)

        assert sol.verdict.name == "ACCEPTED"
    @pytest.mark.asyncio
    async def test_mixed_confidence_reaches_threshold(self):
        """Three approvals: 0.8 + 0.7 + 0.6 = 2.1, meets threshold 2."""
        ex = make_exchange()
        agents = make_agents(5)
        for a in agents:
            ex.register(a)

        p = Problem(title="Mixed conf", description="Test mixed confidence weights ok",
                     author_id=agents[0].id)
        await ex.post_problem(p)
        await ex.claim_problem(p.id, agents[1].id)
        sol = await ex.solve_problem(p.id, agents[1].id,
                                      solution_body="A valid solution body here")

        for reviewer, conf in [(agents[2], 0.8), (agents[3], 0.7), (agents[4], 0.6)]:
            r = Review(
                solution_id=sol.id, reviewer_id=reviewer.id,
                review_type=ReviewType.CORRECTNESS, verdict=ReviewVerdict.APPROVE,
                confidence=conf,
            )
            await ex.submit_review(r)

        assert sol.verdict.name == "ACCEPTED"

class TestCalibrationInjection:
    """Calibration problem injection into the claim flow."""

    @pytest.mark.asyncio
    async def test_calibration_injected_on_claim(self):
        """When calibration is enabled and injection fires, a problem is created."""
        cal_cfg = CalibrationConfig(injection_probability=1.0)  # always inject
        ex = make_exchange(
            enable_calibration=True,
            calibration_config=cal_cfg,
        )
        agents = make_agents(2)
        for a in agents:
            ex.register(a)

        # Seed a calibration problem matching agent capabilities
        cp = CalibrationProblem(
            title="Cal Test",
            description="What is 2+2?",
            known_solution="4",
            capabilities={AgentCapability.CODE_GENERATION},
        )
        ex.calibration_bank.add_problem(cp)

        p = Problem(title="Real problem", description="A real problem to trigger claim",
                     author_id=agents[0].id)
        await ex.post_problem(p)
        await ex.claim_problem(p.id, agents[1].id)

        # A calibration problem should now be in the exchange
        assert len(ex._calibration_map) == 1
        injected_id = list(ex._calibration_map.keys())[0]
        assert injected_id in ex._problems

    @pytest.mark.asyncio
    async def test_calibration_evaluate_pass(self):
        """Correct answer to a calibration problem passes and updates skills."""
        cal_cfg = CalibrationConfig(injection_probability=1.0)
        ex = make_exchange(
            enable_calibration=True,
            calibration_config=cal_cfg,
        )
        agents = make_agents(2)
        for a in agents:
            ex.register(a)

        cp = CalibrationProblem(
            title="Cal Test",
            description="What is 2+2?",
            known_solution="4",
            capabilities={AgentCapability.CODE_GENERATION},
        )
        ex.calibration_bank.add_problem(cp)

        p = Problem(title="Real problem", description="A real problem for cal eval test",
                     author_id=agents[0].id)
        await ex.post_problem(p)
        await ex.claim_problem(p.id, agents[1].id)

        injected_id = list(ex._calibration_map.keys())[0]
        result = await ex.evaluate_calibration(injected_id, agents[1].id, "4")
        assert result is not None
        assert result.verdict.name == "PASS"

    @pytest.mark.asyncio
    async def test_calibration_not_injected_when_disabled(self):
        """No injection when calibration is disabled."""
        ex = make_exchange(enable_calibration=False)
        agents = make_agents(2)
        for a in agents:
            ex.register(a)

        p = Problem(title="No cal", description="Calibration disabled no injection",
                     author_id=agents[0].id)
        await ex.post_problem(p)
        await ex.claim_problem(p.id, agents[1].id)
        assert len(ex._calibration_map) == 0

    @pytest.mark.asyncio
    async def test_is_calibration_problem(self):
        """is_calibration_problem correctly identifies injected problems."""
        cal_cfg = CalibrationConfig(injection_probability=1.0)
        ex = make_exchange(
            enable_calibration=True,
            calibration_config=cal_cfg,
        )
        agents = make_agents(2)
        for a in agents:
            ex.register(a)

        cp = CalibrationProblem(
            title="Cal ID Test",
            description="Identification test problem for calibration",
            known_solution="yes",
            capabilities={AgentCapability.CODE_GENERATION},
        )
        ex.calibration_bank.add_problem(cp)

        p = Problem(title="Real one", description="A real problem for is_cal check",
                     author_id=agents[0].id)
        await ex.post_problem(p)
        await ex.claim_problem(p.id, agents[1].id)

        assert not ex.is_calibration_problem(p.id)
        injected_id = list(ex._calibration_map.keys())[0]
        assert ex.is_calibration_problem(injected_id)


class TestNeedsRevisionWorkflow:
    """NEEDS_REVISION verdict when reviewers REQUEST_CHANGES."""

    @pytest.mark.asyncio
    async def test_request_changes_triggers_revision(self):
        """Two REQUEST_CHANGES verdicts → NEEDS_REVISION, problem stays CLAIMED."""
        ex = make_exchange()
        agents = make_agents(4)
        for a in agents:
            ex.register(a)

        p = Problem(title="Rev test", description="Test needs revision workflow here",
                     author_id=agents[0].id)
        await ex.post_problem(p)
        await ex.claim_problem(p.id, agents[1].id)
        sol = await ex.solve_problem(p.id, agents[1].id,
                                      solution_body="A valid solution body here")

        for reviewer in [agents[2], agents[3]]:
            r = Review(
                solution_id=sol.id, reviewer_id=reviewer.id,
                review_type=ReviewType.CORRECTNESS,
                verdict=ReviewVerdict.REQUEST_CHANGES,
            )
            await ex.submit_review(r)

        assert sol.verdict.name == "NEEDS_REVISION"
        assert p.status.name == "CLAIMED"

    @pytest.mark.asyncio
    async def test_revision_preserves_stake(self):
        """Stake should NOT be forfeited on NEEDS_REVISION."""
        ex = make_exchange()
        agents = make_agents(4)
        for a in agents:
            ex.register(a)

        p = Problem(title="Stake rev", description="Test stake preserved on revision",
                     author_id=agents[0].id, bounty=20)
        await ex.post_problem(p)

        balance_before = ex.ledger.balance(agents[1].id)
        await ex.claim_problem(p.id, agents[1].id)
        balance_after_claim = ex.ledger.balance(agents[1].id)
        stake = balance_before - balance_after_claim

        sol = await ex.solve_problem(p.id, agents[1].id,
                                      solution_body="A valid solution body here")

        for reviewer in [agents[2], agents[3]]:
            r = Review(
                solution_id=sol.id, reviewer_id=reviewer.id,
                review_type=ReviewType.CORRECTNESS,
                verdict=ReviewVerdict.REQUEST_CHANGES,
            )
            await ex.submit_review(r)

        # Stake key should still be present (not popped)
        assert (agents[1].id, p.id) in ex._stakes

    @pytest.mark.asyncio
    async def test_reject_verdict_still_works(self):
        """Outright REJECT verdicts still reject the solution."""
        ex = make_exchange()
        agents = make_agents(4)
        for a in agents:
            ex.register(a)

        p = Problem(title="Rej test", description="Test that reject still works properly",
                     author_id=agents[0].id)
        await ex.post_problem(p)
        await ex.claim_problem(p.id, agents[1].id)
        sol = await ex.solve_problem(p.id, agents[1].id,
                                      solution_body="A valid solution body here")

        for reviewer in [agents[2], agents[3]]:
            r = Review(
                solution_id=sol.id, reviewer_id=reviewer.id,
                review_type=ReviewType.CORRECTNESS,
                verdict=ReviewVerdict.REJECT,
            )
            await ex.submit_review(r)

        assert sol.verdict.name == "REJECTED"
        assert p.status.name == "OPEN"


class TestChallengeMechanism:
    """Challenge accepted solutions to trigger re-review."""

    async def _accept_solution(self, ex, agents, problem):
        """Helper: claim, solve, review, return accepted solution."""
        await ex.claim_problem(problem.id, agents[1].id)
        sol = await ex.solve_problem(problem.id, agents[1].id,
                                      solution_body="A valid solution body here")
        for reviewer in [agents[2], agents[3]]:
            r = Review(
                solution_id=sol.id, reviewer_id=reviewer.id,
                review_type=ReviewType.CORRECTNESS,
                verdict=ReviewVerdict.APPROVE,
            )
            await ex.submit_review(r)
        assert sol.verdict.name == "ACCEPTED"
        return sol

    @pytest.mark.asyncio
    async def test_challenge_reopens_for_review(self):
        """Challenge an accepted solution → solution goes back to PENDING."""
        ex = make_exchange(min_unique_reviewers=2)
        agents = make_agents(5)
        for a in agents:
            ex.register(a)

        p = Problem(title="Challenge test", description="Testing the challenge mechanism here",
                     author_id=agents[0].id)
        await ex.post_problem(p)
        sol = await self._accept_solution(ex, agents, p)

        # Agent 4 challenges
        await ex.challenge_solution(sol.id, agents[4].id, reason="Looks wrong")
        assert sol.verdict.name == "PENDING"
        assert p.status.name == "SOLVED"

    @pytest.mark.asyncio
    async def test_cannot_challenge_own_solution(self):
        """Challenging your own solution raises ValueError."""
        ex = make_exchange(min_unique_reviewers=2)
        agents = make_agents(4)
        for a in agents:
            ex.register(a)

        p = Problem(title="Self challenge", description="Cannot challenge own solution test",
                     author_id=agents[0].id)
        await ex.post_problem(p)
        sol = await self._accept_solution(ex, agents, p)

        with pytest.raises(ValidationError, match="Cannot challenge your own"):
            await ex.challenge_solution(sol.id, agents[1].id)

    @pytest.mark.asyncio
    async def test_challenge_stake_deducted(self):
        """Challenger's reputation is deducted as a stake."""
        ex = make_exchange(min_unique_reviewers=2, challenge_stake=10)
        agents = make_agents(5)
        for a in agents:
            ex.register(a)

        p = Problem(title="Stake test", description="Test challenge stake deduction here",
                     author_id=agents[0].id)
        await ex.post_problem(p)
        sol = await self._accept_solution(ex, agents, p)

        balance_before = ex.ledger.balance(agents[4].id)
        await ex.challenge_solution(sol.id, agents[4].id)
        balance_after = ex.ledger.balance(agents[4].id)
        assert balance_after == balance_before - 10

    @pytest.mark.asyncio
    async def test_cannot_challenge_pending_solution(self):
        """Can only challenge ACCEPTED solutions."""
        ex = make_exchange()
        agents = make_agents(3)
        for a in agents:
            ex.register(a)

        p = Problem(title="Pending challenge", description="Cannot challenge pending sol test",
                     author_id=agents[0].id)
        await ex.post_problem(p)
        await ex.claim_problem(p.id, agents[1].id)
        sol = await ex.solve_problem(p.id, agents[1].id,
                                      solution_body="A valid solution body here")

        with pytest.raises(StateError, match="Can only challenge ACCEPTED"):
            await ex.challenge_solution(sol.id, agents[2].id)


# ===================================================================
# Agent suspension
# ===================================================================

class TestAgentSuspension:
    """Tests for suspend / unsuspend lifecycle."""

    @pytest.mark.asyncio
    async def test_suspended_agent_cannot_claim(self):
        ex = make_exchange()
        agents = make_agents(3)
        for a in agents:
            ex.register(a)

        p = Problem(title="Suspend claim", description="Test suspended agent cannot claim problems",
                     author_id=agents[0].id)
        await ex.post_problem(p)

        await ex.suspend_agent(agents[1].id, reason="testing")
        with pytest.raises(SuspendedError, match="suspended"):
            await ex.claim_problem(p.id, agents[1].id)

    @pytest.mark.asyncio
    async def test_suspended_agent_cannot_solve(self):
        ex = make_exchange()
        agents = make_agents(3)
        for a in agents:
            ex.register(a)

        p = Problem(title="Suspend solve", description="Test suspended agent cannot solve problems",
                     author_id=agents[0].id)
        await ex.post_problem(p)
        await ex.claim_problem(p.id, agents[1].id)

        await ex.suspend_agent(agents[1].id, reason="bad behavior")
        with pytest.raises(SuspendedError, match="suspended"):
            await ex.solve_problem(p.id, agents[1].id, solution_body="my answer")

    @pytest.mark.asyncio
    async def test_suspended_agent_cannot_review(self):
        ex = make_exchange()
        agents = make_agents(3)
        for a in agents:
            ex.register(a)

        p = Problem(title="Suspend review", description="Test suspended agent cannot submit reviews",
                     author_id=agents[0].id)
        await ex.post_problem(p)
        await ex.claim_problem(p.id, agents[1].id)
        sol = await ex.solve_problem(p.id, agents[1].id, solution_body="a valid answer here")

        await ex.suspend_agent(agents[2].id, reason="spammy reviews")
        review = Review(
            solution_id=sol.id,
            reviewer_id=agents[2].id,
            review_type=ReviewType.CORRECTNESS,
            verdict=ReviewVerdict.APPROVE,
            body="good",
        )
        with pytest.raises(SuspendedError, match="suspended"):
            await ex.submit_review(review)

    @pytest.mark.asyncio
    async def test_suspended_agent_cannot_post(self):
        ex = make_exchange()
        agents = make_agents(2)
        for a in agents:
            ex.register(a)

        await ex.suspend_agent(agents[0].id)
        p = Problem(title="Suspend post", description="Test suspended agent cannot post new problems",
                     author_id=agents[0].id)
        with pytest.raises(SuspendedError, match="suspended"):
            await ex.post_problem(p)

    @pytest.mark.asyncio
    async def test_unsuspend_restores_access(self):
        ex = make_exchange()
        agents = make_agents(3)
        for a in agents:
            ex.register(a)

        p = Problem(title="Unsuspend", description="Test unmuting an agent restores their access",
                     author_id=agents[0].id)
        await ex.post_problem(p)

        await ex.suspend_agent(agents[1].id)
        assert ex.is_suspended(agents[1].id)

        await ex.unsuspend_agent(agents[1].id)
        assert not ex.is_suspended(agents[1].id)

        # Should now work fine
        await ex.claim_problem(p.id, agents[1].id)

    @pytest.mark.asyncio
    async def test_suspend_emits_event(self):
        ex = make_exchange()
        agents = make_agents(2)
        for a in agents:
            ex.register(a)

        events = []
        async def capture(ev):
            events.append(ev)
        ex.bus.subscribe_all(capture)

        await ex.suspend_agent(agents[0].id, reason="abuse")
        assert any(e.kind.name == "AGENT_SUSPENDED" for e in events)


# ===================================================================
# Problem expiry
# ===================================================================

class TestProblemExpiry:
    """Tests for expire_stale_problems."""

    @pytest.mark.asyncio
    async def test_expired_problems_are_expired(self):
        from datetime import timedelta, datetime, timezone

        ex = make_exchange()
        agents = make_agents(2)
        for a in agents:
            ex.register(a)

        deadline = datetime.now(timezone.utc) - timedelta(hours=1)
        p = Problem(
            title="Stale", description="This problem has a past deadline and should expire",
            author_id=agents[0].id, deadline=deadline,
        )
        await ex.post_problem(p)

        expired = await ex.expire_stale_problems()
        assert len(expired) == 1
        assert expired[0].id == p.id
        assert p.status.name == "EXPIRED"

    @pytest.mark.asyncio
    async def test_non_expired_problems_untouched(self):
        from datetime import timedelta, datetime, timezone

        ex = make_exchange()
        agents = make_agents(2)
        for a in agents:
            ex.register(a)

        future = datetime.now(timezone.utc) + timedelta(hours=24)
        p = Problem(
            title="Fresh", description="This problem still has time left on its deadline",
            author_id=agents[0].id, deadline=future,
        )
        await ex.post_problem(p)

        expired = await ex.expire_stale_problems()
        assert len(expired) == 0
        assert p.status.name == "OPEN"

    @pytest.mark.asyncio
    async def test_expiry_penalizes_claimer(self):
        from datetime import timedelta, datetime, timezone

        ex = make_exchange()
        agents = make_agents(3)
        for a in agents:
            ex.register(a)

        deadline = datetime.now(timezone.utc) + timedelta(seconds=1)
        p = Problem(
            title="Soon", description="About to expire after agent claims it - penalize claimer",
            author_id=agents[0].id, deadline=deadline,
        )
        await ex.post_problem(p)
        await ex.claim_problem(p.id, agents[1].id)

        # Force deadline into past
        p.deadline = datetime.now(timezone.utc) - timedelta(seconds=1)
        balance_before = ex.ledger.balance(agents[1].id)
        await ex.expire_stale_problems()
        balance_after = ex.ledger.balance(agents[1].id)
        assert balance_after < balance_before

    @pytest.mark.asyncio
    async def test_expiry_emits_event(self):
        from datetime import timedelta, datetime, timezone

        ex = make_exchange()
        agents = make_agents(2)
        for a in agents:
            ex.register(a)

        events = []
        async def capture(ev):
            events.append(ev)
        ex.bus.subscribe_all(capture)

        p = Problem(
            title="Expiring", description="Emits PROBLEM_EXPIRED event upon expiry",
            author_id=agents[0].id,
            deadline=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        await ex.post_problem(p)

        await ex.expire_stale_problems()
        assert any(e.kind.name == "PROBLEM_EXPIRED" for e in events)

    @pytest.mark.asyncio
    async def test_closed_problems_not_re_expired(self):
        from datetime import timedelta, datetime, timezone

        ex = make_exchange()
        agents = make_agents(2)
        for a in agents:
            ex.register(a)

        p = Problem(
            title="Already closed", description="Closed problems should not be expired again",
            author_id=agents[0].id,
            deadline=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        await ex.post_problem(p)
        # Manually close it
        p.status = ProblemStatus.CLOSED

        expired = await ex.expire_stale_problems()
        assert len(expired) == 0


# ===================================================================
# Exchange statistics
# ===================================================================

class TestStatistics:
    """Tests for Exchange.statistics() KPI method."""

    @pytest.mark.asyncio
    async def test_empty_exchange_stats(self):
        ex = make_exchange()
        stats = ex.statistics()
        assert stats["total_agents"] == 0
        assert stats["total_problems"] == 0
        assert stats["total_solutions"] == 0
        assert stats["acceptance_rate"] == 0.0

    @pytest.mark.asyncio
    async def test_stats_after_activity(self):
        ex = make_exchange()
        agents = make_agents(3)
        for a in agents:
            ex.register(a)

        p = Problem(title="Stats test", description="Testing statistics after full workflow activity",
                     author_id=agents[0].id)
        await ex.post_problem(p)
        await ex.claim_problem(p.id, agents[1].id)
        await ex.solve_problem(p.id, agents[1].id, solution_body="a valid solution text here")

        stats = ex.statistics()
        assert stats["total_agents"] == 3
        assert stats["total_problems"] == 1
        assert stats["total_solutions"] == 1
        assert stats["problem_status"]["SOLVED"] == 1
        assert stats["solution_verdicts"]["PENDING"] == 1

    @pytest.mark.asyncio
    async def test_stats_acceptance_rate(self):
        ex = make_exchange()
        agents = make_agents(4)
        for a in agents:
            ex.register(a)

        # Create and accept one problem
        p = Problem(title="Accept stats", description="Testing acceptance rate in stats output",
                     author_id=agents[0].id)
        await ex.post_problem(p)
        await ex.claim_problem(p.id, agents[1].id)
        sol = await ex.solve_problem(p.id, agents[1].id, solution_body="a valid solution text here")

        for reviewer_id in [agents[2].id, agents[3].id]:
            r = Review(
                solution_id=sol.id,
                reviewer_id=reviewer_id,
                review_type=ReviewType.CORRECTNESS,
                verdict=ReviewVerdict.APPROVE,
                body="looks good",
            )
            await ex.submit_review(r)

        stats = ex.statistics()
        assert stats["acceptance_rate"] == 1.0
        assert stats["total_reviews"] == 2

    @pytest.mark.asyncio
    async def test_stats_includes_suspended(self):
        ex = make_exchange()
        agents = make_agents(3)
        for a in agents:
            ex.register(a)

        await ex.suspend_agent(agents[0].id)
        stats = ex.statistics()
        assert stats["suspended_agents"] == 1


# ===================================================================
# Registration hooks
# ===================================================================

class TestRegistrationHooks:
    """Tests for approval queue and registration hook."""

    def test_approval_queue(self):
        ex = make_exchange(require_approval=True)
        agent = Agent(name="Pending", solver=auto_solver)
        ex.register(agent)

        # Agent is pending, not active
        assert len(ex.agents) == 0
        assert len(ex.pending_agents) == 1

        # Approve
        returned = ex.approve_agent(agent.id)
        assert returned.id == agent.id
        assert len(ex.agents) == 1
        assert len(ex.pending_agents) == 0

    def test_reject_pending(self):
        ex = make_exchange(require_approval=True)
        agent = Agent(name="Rejected", solver=auto_solver)
        ex.register(agent)

        ex.reject_pending_agent(agent.id)
        assert len(ex.agents) == 0
        assert len(ex.pending_agents) == 0

    def test_registration_hook_allows(self):
        hook = lambda a: a.name.startswith("Allowed")
        ex = make_exchange(registration_hook=hook)
        good = Agent(name="Allowed-Agent", solver=auto_solver)
        ex.register(good)
        assert len(ex.agents) == 1

    def test_registration_hook_rejects(self):
        hook = lambda a: a.name.startswith("Allowed")
        ex = make_exchange(registration_hook=hook)
        bad = Agent(name="Denied-Agent", solver=auto_solver)
        with pytest.raises(PermissionError_, match="rejected by registration hook"):
            ex.register(bad)
        assert len(ex.agents) == 0

    @pytest.mark.asyncio
    async def test_pending_agent_cannot_claim(self):
        ex = make_exchange(require_approval=True)
        # Register poster normally first (need approval=False for setup)
        poster = Agent(name="Poster", solver=auto_solver)
        ex._agents[poster.id] = poster  # bypass approval for setup

        p = Problem(
            title="Hook test", description="Test that pending agents cannot claim problems",
            author_id=poster.id,
        )
        await ex.post_problem(p)

        # Register solver through approval queue
        solver = Agent(name="Solver", solver=auto_solver)
        ex.register(solver)  # goes to pending

        with pytest.raises(KeyError):
            await ex.claim_problem(p.id, solver.id)

    def test_approve_nonexistent_raises(self):
        from uuid import uuid4
        ex = make_exchange()
        with pytest.raises(NotFoundError):
            ex.approve_agent(uuid4())


# ===================================================================
# Bounty escalation
# ===================================================================

class TestBountyEscalation:
    """Tests for manual and automatic bounty escalation."""

    @pytest.mark.asyncio
    async def test_escalate_bounty(self):
        ex = make_exchange(escalation_increment=5)
        agents = make_agents(2)
        for a in agents:
            ex.register(a)

        p = Problem(title="Stuck", description="This problem is stuck and needs bounty escalation",
                     author_id=agents[0].id, bounty=10)
        await ex.post_problem(p)

        await ex.escalate_bounty(p.id)
        assert p.bounty == 15

    @pytest.mark.asyncio
    async def test_bounty_capped_at_max(self):
        ex = make_exchange(escalation_increment=50, max_bounty=20)
        agents = make_agents(2)
        for a in agents:
            ex.register(a)

        p = Problem(title="Cap test", description="Bounty should be capped at exchange max bounty level",
                     author_id=agents[0].id, bounty=10)
        await ex.post_problem(p)

        await ex.escalate_bounty(p.id)
        assert p.bounty == 20  # capped

    @pytest.mark.asyncio
    async def test_escalate_emits_event(self):
        ex = make_exchange(escalation_increment=5)
        agents = make_agents(2)
        for a in agents:
            ex.register(a)

        events = []
        async def capture(ev):
            events.append(ev)
        ex.bus.subscribe_all(capture)

        p = Problem(title="Esc event", description="Should emit PROBLEM_ESCALATED event on escalation",
                     author_id=agents[0].id, bounty=10)
        await ex.post_problem(p)
        await ex.escalate_bounty(p.id)

        assert any(e.kind.name == "PROBLEM_ESCALATED" for e in events)

    @pytest.mark.asyncio
    async def test_cannot_escalate_closed(self):
        ex = make_exchange()
        agents = make_agents(2)
        for a in agents:
            ex.register(a)

        p = Problem(title="Closed", description="Closed problems should not allow bounty escalation",
                     author_id=agents[0].id)
        await ex.post_problem(p)
        p.status = ProblemStatus.CLOSED

        with pytest.raises(StateError, match="Can only escalate"):
            await ex.escalate_bounty(p.id)

    @pytest.mark.asyncio
    async def test_auto_escalate_stale(self):
        from datetime import timedelta, datetime, timezone

        ex = make_exchange(escalation_increment=5)
        agents = make_agents(2)
        for a in agents:
            ex.register(a)

        p = Problem(
            title="Old problem", description="This problem was created long ago and is stale",
            author_id=agents[0].id, bounty=10,
        )
        # Backdate creation
        p.created_at = datetime.now(timezone.utc) - timedelta(hours=2)
        await ex.post_problem(p)

        escalated = await ex.escalate_stale_bounties(stale_seconds=60)
        assert len(escalated) == 1
        assert p.bounty == 15


# ===================================================================
# Snapshot / restore
# ===================================================================

class TestSnapshotRestore:
    """Tests for Exchange.snapshot() and restore_problems()."""

    @pytest.mark.asyncio
    async def test_snapshot_structure(self):
        ex = make_exchange()
        agents = make_agents(3)
        for a in agents:
            ex.register(a)

        p = Problem(title="Snap", description="Testing snapshot captures full state",
                     author_id=agents[0].id)
        await ex.post_problem(p)
        await ex.claim_problem(p.id, agents[1].id)
        sol = await ex.solve_problem(p.id, agents[1].id, solution_body="snapshot answer here")

        snap = ex.snapshot()
        assert "problems" in snap
        assert "solutions" in snap
        assert "reviews" in snap
        assert "agents" in snap
        assert "reputation_balances" in snap
        assert "statistics" in snap
        assert str(p.id) in snap["problems"]
        assert str(sol.id) in snap["solutions"]

    @pytest.mark.asyncio
    async def test_snapshot_captures_suspended(self):
        ex = make_exchange()
        agents = make_agents(2)
        for a in agents:
            ex.register(a)
        await ex.suspend_agent(agents[0].id)

        snap = ex.snapshot()
        assert str(agents[0].id) in snap["suspended"]

    @pytest.mark.asyncio
    async def test_snapshot_is_json_serializable(self):
        import json

        ex = make_exchange()
        agents = make_agents(2)
        for a in agents:
            ex.register(a)

        p = Problem(title="JSON", description="Snapshot should be JSON-serializable",
                     author_id=agents[0].id)
        await ex.post_problem(p)

        snap = ex.snapshot()
        # Should not raise
        json_str = json.dumps(snap)
        assert isinstance(json_str, str)

    @pytest.mark.asyncio
    async def test_restore_problems(self):
        ex1 = make_exchange()
        agents = make_agents(2)
        for a in agents:
            ex1.register(a)

        p = Problem(title="Restore me", description="This problem should be restorable from snapshot",
                     author_id=agents[0].id)
        await ex1.post_problem(p)

        snap = ex1.snapshot()

        # New exchange — restore problems from snapshot
        ex2 = make_exchange()
        count = ex2.restore_problems(snap)
        assert count == 1
        assert p.id in ex2._problems

    @pytest.mark.asyncio
    async def test_restore_skips_duplicates(self):
        ex = make_exchange()
        agents = make_agents(2)
        for a in agents:
            ex.register(a)

        p = Problem(title="Dup", description="Duplicate problems should not be re-imported",
                     author_id=agents[0].id)
        await ex.post_problem(p)

        snap = ex.snapshot()
        # Restore into same exchange — should skip
        count = ex.restore_problems(snap)
        assert count == 0


class TestProblemDecomposition:
    """Tests for decompose_problem, dependencies_met, sub_problems."""

    @pytest.mark.asyncio
    async def test_decompose_creates_sub_problems(self):
        ex = make_exchange()
        a = make_agents(1)[0]
        ex.register(a)

        parent = Problem(title="Big task", description="needs breakdown", author_id=a.id)
        await ex.post_problem(parent)

        subs = [
            Problem(title="Sub-1", description="part one", author_id=a.id),
            Problem(title="Sub-2", description="part two", author_id=a.id),
        ]
        posted = await ex.decompose_problem(parent.id, subs)

        assert len(posted) == 2
        assert parent.sub_problem_ids == [posted[0].id, posted[1].id]
        for sp in posted:
            assert sp.parent_id == parent.id

    @pytest.mark.asyncio
    async def test_decompose_sequential_creates_chain(self):
        ex = make_exchange()
        a = make_agents(1)[0]
        ex.register(a)

        parent = Problem(title="Sequential", description="chain", author_id=a.id)
        await ex.post_problem(parent)

        subs = [
            Problem(title="Step-1", description="first", author_id=a.id),
            Problem(title="Step-2", description="second", author_id=a.id),
            Problem(title="Step-3", description="third", author_id=a.id),
        ]
        posted = await ex.decompose_problem(parent.id, subs, sequential=True)

        # Step-1 has no deps; Step-2 depends on Step-1; Step-3 depends on Step-2
        assert posted[0].depends_on == []
        assert posted[1].depends_on == [posted[0].id]
        assert posted[2].depends_on == [posted[1].id]

    @pytest.mark.asyncio
    async def test_sub_problems_listing(self):
        ex = make_exchange()
        a = make_agents(1)[0]
        ex.register(a)

        parent = Problem(title="Parent", description="has children", author_id=a.id)
        await ex.post_problem(parent)

        subs = [Problem(title="Child", description="kid", author_id=a.id)]
        await ex.decompose_problem(parent.id, subs)

        children = ex.sub_problems(parent.id)
        assert len(children) == 1
        assert children[0].title == "Child"

    @pytest.mark.asyncio
    async def test_dependency_blocks_claim(self):
        ex = make_exchange()
        agents = make_agents(2)
        for a in agents:
            ex.register(a)

        dep = Problem(title="Dep", description="must finish first", author_id=agents[0].id)
        await ex.post_problem(dep)

        blocked = Problem(
            title="Blocked", description="needs dep",
            author_id=agents[0].id,
            depends_on=[dep.id],
        )
        await ex.post_problem(blocked)

        # Can't claim blocked until dep is CLOSED
        with pytest.raises(StateError, match="dependency"):
            await ex.claim_problem(blocked.id, agents[1].id)

    @pytest.mark.asyncio
    async def test_dependency_allows_claim_after_resolution(self):
        ex = make_exchange()
        agents = make_agents(4)
        for a in agents:
            ex.register(a)

        dep = Problem(title="Dep", description="finish first", author_id=agents[0].id)
        await ex.post_problem(dep)

        blocked = Problem(
            title="Next", description="after dep",
            author_id=agents[0].id,
            depends_on=[dep.id],
        )
        await ex.post_problem(blocked)

        # Solve the dependency
        await ex.claim_problem(dep.id, agents[1].id)
        sol = await ex.solve_problem(dep.id, agents[1].id)
        r1 = Review(solution_id=sol.id, reviewer_id=agents[2].id,
                     review_type=ReviewType.CORRECTNESS,
                     verdict=ReviewVerdict.APPROVE)
        r2 = Review(solution_id=sol.id, reviewer_id=agents[3].id,
                     review_type=ReviewType.CORRECTNESS,
                     verdict=ReviewVerdict.APPROVE)
        await ex.submit_review(r1)
        await ex.submit_review(r2)
        assert dep.status == ProblemStatus.CLOSED

        # Now blocked can be claimed
        result = await ex.claim_problem(blocked.id, agents[1].id)
        assert result.status == ProblemStatus.CLAIMED

    @pytest.mark.asyncio
    async def test_dependencies_met_helper(self):
        ex = make_exchange()
        a = make_agents(1)[0]
        ex.register(a)

        dep = Problem(title="Dep", description="dep", author_id=a.id)
        await ex.post_problem(dep)

        child = Problem(title="Child", description="child",
                        author_id=a.id, depends_on=[dep.id])
        await ex.post_problem(child)

        assert ex.dependencies_met(child.id) is False
        assert ex.dependencies_met(dep.id) is True  # no deps

    @pytest.mark.asyncio
    async def test_decompose_nonexistent_parent_raises(self):
        ex = make_exchange()
        from uuid import uuid4
        with pytest.raises(NotFoundError):
            await ex.decompose_problem(uuid4(), [])


# -- Idempotency Guards ---------------------------------------------------

class TestIdempotencyGuards:
    """Verify that duplicate calls are no-ops returning existing objects."""

    @pytest.mark.asyncio
    async def test_double_post_problem_returns_existing(self):
        """Re-posting the same problem returns the original without duplicating."""
        ex = make_exchange()
        agents = make_agents(1)
        ex.register(agents[0])

        prob = Problem(title="Dup", description="test", author_id=agents[0].id)
        first = await ex.post_problem(prob)
        second = await ex.post_problem(prob)

        assert first is second
        # Only one problem in exchange
        assert len(ex._problems) == 1

    @pytest.mark.asyncio
    async def test_double_post_no_duplicate_reputation(self):
        """Re-posting must NOT award posting reputation twice."""
        ex = make_exchange()
        agents = make_agents(1)
        ex.register(agents[0])

        prob = Problem(title="Dup", description="test", author_id=agents[0].id)
        await ex.post_problem(prob)
        balance_after_first = ex.ledger.balance(agents[0].id)

        await ex.post_problem(prob)
        balance_after_second = ex.ledger.balance(agents[0].id)

        assert balance_after_first == balance_after_second

    @pytest.mark.asyncio
    async def test_double_claim_returns_existing(self):
        """Re-claiming an already-claimed problem returns it idempotently."""
        ex = make_exchange()
        agents = make_agents(2)
        for a in agents:
            ex.register(a)

        prob = Problem(title="Work", description="test " * 10, author_id=agents[0].id)
        await ex.post_problem(prob)

        first = await ex.claim_problem(prob.id, agents[1].id)
        second = await ex.claim_problem(prob.id, agents[1].id)

        assert first is second
        assert prob.claimed_by.count(agents[1].id) == 1

    @pytest.mark.asyncio
    async def test_double_claim_no_duplicate_stake(self):
        """Re-claiming must NOT deduct reputation stake twice."""
        ex = make_exchange()
        agents = make_agents(2)
        for a in agents:
            ex.register(a)

        prob = Problem(title="Work", description="test " * 10, author_id=agents[0].id)
        await ex.post_problem(prob)

        await ex.claim_problem(prob.id, agents[1].id)
        balance_after_first = ex.ledger.balance(agents[1].id)

        await ex.claim_problem(prob.id, agents[1].id)
        balance_after_second = ex.ledger.balance(agents[1].id)

        assert balance_after_first == balance_after_second

    @pytest.mark.asyncio
    async def test_double_review_returns_existing(self):
        """Submitting the same review twice returns the first review."""
        ex = make_exchange()
        agents = make_agents(3)
        for a in agents:
            ex.register(a)

        prob = Problem(title="Rev", description="test " * 10, author_id=agents[0].id)
        await ex.post_problem(prob)
        await ex.claim_problem(prob.id, agents[1].id)
        sol = await ex.solve_problem(prob.id, agents[1].id)

        review1 = Review(
            solution_id=sol.id,
            reviewer_id=agents[2].id,
            review_type=ReviewType.CORRECTNESS,
            verdict=ReviewVerdict.APPROVE,
        )
        first = await ex.submit_review(review1)

        # Second review from same reviewer for same solution
        review2 = Review(
            solution_id=sol.id,
            reviewer_id=agents[2].id,
            review_type=ReviewType.CORRECTNESS,
            verdict=ReviewVerdict.REJECT,
        )
        second = await ex.submit_review(review2)

        assert first is second
        assert first.verdict == ReviewVerdict.APPROVE  # original kept
        # Only one review recorded
        assert len(sol.review_ids) == 1

    @pytest.mark.asyncio
    async def test_double_review_no_duplicate_reputation(self):
        """Duplicate review must NOT double-count reputation reward."""
        ex = make_exchange()
        agents = make_agents(3)
        for a in agents:
            ex.register(a)

        prob = Problem(title="Rev", description="test " * 10, author_id=agents[0].id)
        await ex.post_problem(prob)
        await ex.claim_problem(prob.id, agents[1].id)
        sol = await ex.solve_problem(prob.id, agents[1].id)

        review = Review(
            solution_id=sol.id,
            reviewer_id=agents[2].id,
            review_type=ReviewType.CORRECTNESS,
            verdict=ReviewVerdict.APPROVE,
        )
        await ex.submit_review(review)
        balance_after_first = ex.ledger.balance(agents[2].id)

        review_dup = Review(
            solution_id=sol.id,
            reviewer_id=agents[2].id,
            review_type=ReviewType.CORRECTNESS,
            verdict=ReviewVerdict.APPROVE,
        )
        await ex.submit_review(review_dup)
        balance_after_second = ex.ledger.balance(agents[2].id)

        assert balance_after_first == balance_after_second


# -- Claim Timeout Expiry -------------------------------------------------

class TestClaimTimeoutExpiry:
    """Verify that stale claims are expired after the configured timeout."""

    @pytest.mark.asyncio
    async def test_no_expiry_when_disabled(self):
        """With claim_timeout_seconds=0 (default), nothing expires."""
        ex = make_exchange()
        agents = make_agents(2)
        for a in agents:
            ex.register(a)

        prob = Problem(title="T", description="test " * 10, author_id=agents[0].id)
        await ex.post_problem(prob)
        await ex.claim_problem(prob.id, agents[1].id)

        released = await ex.expire_stale_claims()
        assert released == []
        assert prob.status == ProblemStatus.CLAIMED

    @pytest.mark.asyncio
    async def test_claim_expires_after_timeout(self):
        """A claim that exceeds the timeout is released."""
        from datetime import datetime, timedelta, timezone

        ex = make_exchange(claim_timeout_seconds=60)
        agents = make_agents(2)
        for a in agents:
            ex.register(a)

        prob = Problem(title="T", description="test " * 10, author_id=agents[0].id)
        await ex.post_problem(prob)
        await ex.claim_problem(prob.id, agents[1].id)

        # Simulate time passing
        future = datetime.now(timezone.utc) + timedelta(seconds=120)
        released = await ex.expire_stale_claims(now=future)

        assert len(released) == 1
        assert released[0] == (agents[1].id, prob.id)
        assert prob.status == ProblemStatus.OPEN

    @pytest.mark.asyncio
    async def test_active_claim_not_expired(self):
        """A recent claim is not expired."""
        from datetime import datetime, timedelta, timezone

        ex = make_exchange(claim_timeout_seconds=300)
        agents = make_agents(2)
        for a in agents:
            ex.register(a)

        prob = Problem(title="T", description="test " * 10, author_id=agents[0].id)
        await ex.post_problem(prob)
        await ex.claim_problem(prob.id, agents[1].id)

        # Only 10 seconds later — well within 300s timeout
        future = datetime.now(timezone.utc) + timedelta(seconds=10)
        released = await ex.expire_stale_claims(now=future)

        assert released == []
        assert prob.status == ProblemStatus.CLAIMED

    @pytest.mark.asyncio
    async def test_expired_claim_incurs_penalty(self):
        """Agent receives a reputation penalty when their claim expires."""
        from datetime import datetime, timedelta, timezone

        ex = make_exchange(claim_timeout_seconds=60)
        agents = make_agents(2)
        for a in agents:
            ex.register(a)

        prob = Problem(title="T", description="test " * 10, author_id=agents[0].id)
        await ex.post_problem(prob)
        await ex.claim_problem(prob.id, agents[1].id)
        balance_before = ex.ledger.balance(agents[1].id)

        future = datetime.now(timezone.utc) + timedelta(seconds=120)
        await ex.expire_stale_claims(now=future)

        balance_after = ex.ledger.balance(agents[1].id)
        assert balance_after < balance_before

    @pytest.mark.asyncio
    async def test_expired_claim_releases_agent_slot(self):
        """Agent's active slot is freed when their claim expires."""
        from datetime import datetime, timedelta, timezone

        ex = make_exchange(claim_timeout_seconds=60)
        agents = make_agents(2)
        for a in agents:
            ex.register(a)

        prob = Problem(title="T", description="test " * 10, author_id=agents[0].id)
        await ex.post_problem(prob)
        await ex.claim_problem(prob.id, agents[1].id)
        assert agents[1].active_count == 1

        future = datetime.now(timezone.utc) + timedelta(seconds=120)
        await ex.expire_stale_claims(now=future)

        assert agents[1].active_count == 0

    @pytest.mark.asyncio
    async def test_expired_claim_emits_event(self):
        """A CLAIM_EXPIRED event is emitted."""
        from datetime import datetime, timedelta, timezone
        from schwarma.events import EventKind

        ex = make_exchange(claim_timeout_seconds=60)
        agents = make_agents(2)
        for a in agents:
            ex.register(a)

        prob = Problem(title="T", description="test " * 10, author_id=agents[0].id)
        await ex.post_problem(prob)
        await ex.claim_problem(prob.id, agents[1].id)

        ex.bus.enable_recording()
        future = datetime.now(timezone.utc) + timedelta(seconds=120)
        await ex.expire_stale_claims(now=future)

        events = ex.bus.recorded_events
        claim_expired = [e for e in events if e.kind == EventKind.CLAIM_EXPIRED]
        assert len(claim_expired) == 1
        assert claim_expired[0].source_agent_id == agents[1].id
        assert claim_expired[0].problem_id == prob.id


# -- Problem Priority Queue -----------------------------------------------

class TestProblemPriorityQueue:
    """Verify priority-ordered problem retrieval."""

    @pytest.mark.asyncio
    async def test_sort_by_priority(self):
        from schwarma.exchange import ProblemSortKey
        ex = make_exchange()
        a = make_agents(1)[0]
        ex.register(a)

        low = Problem(title="Low", description="d " * 10, author_id=a.id, priority=1)
        high = Problem(title="High", description="d " * 10, author_id=a.id, priority=10)
        mid = Problem(title="Mid", description="d " * 10, author_id=a.id, priority=5)
        for p in [low, high, mid]:
            await ex.post_problem(p)

        result = ex.open_problems(ProblemSortKey.PRIORITY)
        assert [p.title for p in result] == ["High", "Mid", "Low"]

    @pytest.mark.asyncio
    async def test_sort_by_bounty(self):
        from schwarma.exchange import ProblemSortKey
        ex = make_exchange()
        a = make_agents(1)[0]
        ex.register(a)

        cheap = Problem(title="Cheap", description="d " * 10, author_id=a.id, bounty=5)
        rich = Problem(title="Rich", description="d " * 10, author_id=a.id, bounty=50)
        for p in [cheap, rich]:
            await ex.post_problem(p)

        result = ex.open_problems(ProblemSortKey.BOUNTY)
        assert result[0].title == "Rich"
        assert result[1].title == "Cheap"

    @pytest.mark.asyncio
    async def test_sort_by_oldest(self):
        from schwarma.exchange import ProblemSortKey
        from datetime import datetime, timedelta, timezone

        ex = make_exchange()
        a = make_agents(1)[0]
        ex.register(a)

        old = Problem(title="Old", description="d " * 10, author_id=a.id)
        old.created_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
        new = Problem(title="New", description="d " * 10, author_id=a.id)
        new.created_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
        for p in [new, old]:
            await ex.post_problem(p)

        result = ex.open_problems(ProblemSortKey.OLDEST)
        assert result[0].title == "Old"
        assert result[1].title == "New"

    @pytest.mark.asyncio
    async def test_sort_by_newest(self):
        from schwarma.exchange import ProblemSortKey
        from datetime import datetime, timezone

        ex = make_exchange()
        a = make_agents(1)[0]
        ex.register(a)

        old = Problem(title="Old", description="d " * 10, author_id=a.id)
        old.created_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
        new = Problem(title="New", description="d " * 10, author_id=a.id)
        new.created_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
        for p in [new, old]:
            await ex.post_problem(p)

        result = ex.open_problems(ProblemSortKey.NEWEST)
        assert result[0].title == "New"
        assert result[1].title == "Old"

    @pytest.mark.asyncio
    async def test_tag_filter(self):
        from schwarma.exchange import ProblemSortKey
        ex = make_exchange()
        a = make_agents(1)[0]
        ex.register(a)

        bug = Problem(title="Bug", description="d " * 10, author_id=a.id,
                      tags={ProblemTag.BUG})
        feat = Problem(title="Feat", description="d " * 10, author_id=a.id,
                       tags={ProblemTag.FEATURE})
        for p in [bug, feat]:
            await ex.post_problem(p)

        result = ex.open_problems(tags={ProblemTag.BUG})
        assert len(result) == 1
        assert result[0].title == "Bug"

    @pytest.mark.asyncio
    async def test_limit(self):
        ex = make_exchange()
        a = make_agents(1)[0]
        ex.register(a)

        for i in range(5):
            p = Problem(title=f"P{i}", description="d " * 10, author_id=a.id, priority=i)
            await ex.post_problem(p)

        result = ex.open_problems(limit=2)
        assert len(result) == 2
        # Should be the two highest-priority ones
        assert result[0].priority == 4
        assert result[1].priority == 3

    @pytest.mark.asyncio
    async def test_priority_tiebreak_by_bounty(self):
        """Same priority → higher bounty wins."""
        from schwarma.exchange import ProblemSortKey
        ex = make_exchange()
        a = make_agents(1)[0]
        ex.register(a)

        low_bounty = Problem(title="Low$", description="d " * 10, author_id=a.id,
                             priority=5, bounty=10)
        high_bounty = Problem(title="High$", description="d " * 10, author_id=a.id,
                              priority=5, bounty=50)
        for p in [low_bounty, high_bounty]:
            await ex.post_problem(p)

        result = ex.open_problems(ProblemSortKey.PRIORITY)
        assert result[0].title == "High$"


# -- Batch Problem Intake -------------------------------------------------

class TestBatchProblemIntake:
    """Verify bulk problem posting."""

    @pytest.mark.asyncio
    async def test_batch_posts_all(self):
        ex = make_exchange()
        a = make_agents(1)[0]
        ex.register(a)

        problems = [
            Problem(title=f"P{i}", description="d " * 10, author_id=a.id)
            for i in range(5)
        ]
        posted = await ex.post_problems(problems)
        assert len(posted) == 5
        assert all(p.id in ex._problems for p in posted)

    @pytest.mark.asyncio
    async def test_batch_empty_list(self):
        ex = make_exchange()
        posted = await ex.post_problems([])
        assert posted == []

    @pytest.mark.asyncio
    async def test_batch_skips_blocked(self):
        """Blocked problems are skipped, rest still posted."""
        ex = make_exchange(enable_content_guards=True)
        a = make_agents(1)[0]
        ex.register(a)

        good = Problem(title="Good", description="normal description " * 5, author_id=a.id)
        # A problem with a secret should be blocked
        bad = Problem(title="Bad", description="password=hunter2 secret_key=AKIAIOSFODNN7EXAMPLE",
                      author_id=a.id)
        posted = await ex.post_problems([good, bad])

        # At least the good one should be posted
        good_titles = [p.title for p in posted]
        assert "Good" in good_titles

    @pytest.mark.asyncio
    async def test_batch_idempotent(self):
        """Re-posting the same batch doesn't duplicate."""
        ex = make_exchange()
        a = make_agents(1)[0]
        ex.register(a)

        problems = [
            Problem(title=f"P{i}", description="d " * 10, author_id=a.id)
            for i in range(3)
        ]
        first = await ex.post_problems(problems)
        second = await ex.post_problems(problems)

        assert len(first) == 3
        assert len(second) == 3
        # Same objects returned
        for f, s in zip(first, second):
            assert f is s

    @pytest.mark.asyncio
    async def test_batch_preserves_order(self):
        ex = make_exchange()
        a = make_agents(1)[0]
        ex.register(a)

        problems = [
            Problem(title=f"P{i}", description="d " * 10, author_id=a.id, priority=i)
            for i in range(4)
        ]
        posted = await ex.post_problems(problems)
        assert [p.title for p in posted] == ["P0", "P1", "P2", "P3"]


# -- Exchange Lifecycle Hooks ----------------------------------------------

class TestLifecycleHooks:
    """Verify that pre/post hooks fire at the right points."""

    @pytest.mark.asyncio
    async def test_pre_post_problem_hook_fires(self):
        from schwarma.exchange import HookPoint
        ex = make_exchange()
        a = make_agents(1)[0]
        ex.register(a)

        log = []

        async def on_pre_post(ctx):
            log.append(("pre_post", ctx["problem"].title))

        ex.add_hook(HookPoint.PRE_POST_PROBLEM, on_pre_post)
        prob = Problem(title="Hooked", description="test " * 10, author_id=a.id)
        await ex.post_problem(prob)

        assert log == [("pre_post", "Hooked")]

    @pytest.mark.asyncio
    async def test_post_post_problem_hook_fires(self):
        from schwarma.exchange import HookPoint
        ex = make_exchange()
        a = make_agents(1)[0]
        ex.register(a)

        log = []

        async def on_post_post(ctx):
            log.append(("post_post", ctx["problem"].title))

        ex.add_hook(HookPoint.POST_POST_PROBLEM, on_post_post)
        prob = Problem(title="Done", description="test " * 10, author_id=a.id)
        await ex.post_problem(prob)

        assert log == [("post_post", "Done")]

    @pytest.mark.asyncio
    async def test_pre_claim_hook_fires(self):
        from schwarma.exchange import HookPoint
        ex = make_exchange()
        agents = make_agents(2)
        for a in agents:
            ex.register(a)

        log = []

        async def on_pre_claim(ctx):
            log.append(("pre_claim", ctx["agent"].name))

        ex.add_hook(HookPoint.PRE_CLAIM_PROBLEM, on_pre_claim)
        prob = Problem(title="T", description="test " * 10, author_id=agents[0].id)
        await ex.post_problem(prob)
        await ex.claim_problem(prob.id, agents[1].id)

        assert len(log) == 1
        assert log[0][0] == "pre_claim"

    @pytest.mark.asyncio
    async def test_post_solve_hook_receives_solution(self):
        from schwarma.exchange import HookPoint
        ex = make_exchange()
        agents = make_agents(2)
        for a in agents:
            ex.register(a)

        log = []

        async def on_post_solve(ctx):
            log.append(ctx["solution"].body)

        ex.add_hook(HookPoint.POST_SOLVE_PROBLEM, on_post_solve)
        prob = Problem(title="T", description="test " * 10, author_id=agents[0].id)
        await ex.post_problem(prob)
        await ex.claim_problem(prob.id, agents[1].id)
        await ex.solve_problem(prob.id, agents[1].id)

        assert len(log) == 1
        assert isinstance(log[0], str) and len(log[0]) > 0

    @pytest.mark.asyncio
    async def test_pre_review_hook_fires(self):
        from schwarma.exchange import HookPoint
        ex = make_exchange()
        agents = make_agents(3)
        for a in agents:
            ex.register(a)

        log = []

        async def on_pre_review(ctx):
            log.append(ctx["review"].verdict.name)

        ex.add_hook(HookPoint.PRE_SUBMIT_REVIEW, on_pre_review)

        prob = Problem(title="T", description="test " * 10, author_id=agents[0].id)
        await ex.post_problem(prob)
        await ex.claim_problem(prob.id, agents[1].id)
        sol = await ex.solve_problem(prob.id, agents[1].id)

        review = Review(
            solution_id=sol.id,
            reviewer_id=agents[2].id,
            review_type=ReviewType.CORRECTNESS,
            verdict=ReviewVerdict.APPROVE,
        )
        await ex.submit_review(review)

        assert log == ["APPROVE"]

    @pytest.mark.asyncio
    async def test_multiple_hooks_fire_in_order(self):
        from schwarma.exchange import HookPoint
        ex = make_exchange()
        a = make_agents(1)[0]
        ex.register(a)

        log = []

        async def first(ctx):
            log.append("first")

        async def second(ctx):
            log.append("second")

        ex.add_hook(HookPoint.PRE_POST_PROBLEM, first)
        ex.add_hook(HookPoint.PRE_POST_PROBLEM, second)

        prob = Problem(title="T", description="test " * 10, author_id=a.id)
        await ex.post_problem(prob)

        assert log == ["first", "second"]

    @pytest.mark.asyncio
    async def test_remove_hook(self):
        from schwarma.exchange import HookPoint
        ex = make_exchange()
        a = make_agents(1)[0]
        ex.register(a)

        log = []

        async def hook(ctx):
            log.append("fired")

        ex.add_hook(HookPoint.PRE_POST_PROBLEM, hook)
        ex.remove_hook(HookPoint.PRE_POST_PROBLEM, hook)

        prob = Problem(title="T", description="test " * 10, author_id=a.id)
        await ex.post_problem(prob)

        assert log == []