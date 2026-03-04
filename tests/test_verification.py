"""Tests for the verification oracle protocol and Exchange integration."""

from uuid import uuid4

import pytest

from schwarma.agent import Agent, AgentCapability
from schwarma.exchange import Exchange, ExchangeConfig
from schwarma.problem import Problem
from schwarma.review import ReviewVerdict
from schwarma.solution import SolutionVerdict
from schwarma.verification import (
    VerificationOracle,
    VerificationResult,
    VerificationStatus,
)


# -- Helpers ---------------------------------------------------------------

async def auto_solver(desc: str, ctx: dict) -> str:
    return f"answer: {desc[:30]}"


async def approve_solver(desc: str, ctx: dict) -> str:
    return "APPROVE — looks good"


class PassingOracle:
    """Oracle that always passes."""

    async def verify(self, solution, problem) -> VerificationResult:
        return VerificationResult(
            status=VerificationStatus.PASSED,
            passed_tests=5,
            failed_tests=0,
            stdout="All tests passed",
        )


class FailingOracle:
    """Oracle that always fails."""

    async def verify(self, solution, problem) -> VerificationResult:
        return VerificationResult(
            status=VerificationStatus.FAILED,
            passed_tests=2,
            failed_tests=3,
            stderr="AssertionError in test_foo",
        )


class CrashingOracle:
    """Oracle that raises an exception."""

    async def verify(self, solution, problem) -> VerificationResult:
        raise RuntimeError("sandbox unavailable")


class SkippingOracle:
    """Oracle that skips verification."""

    async def verify(self, solution, problem) -> VerificationResult:
        return VerificationResult(status=VerificationStatus.SKIPPED)


def make_exchange(oracle=None, auto_reject=False) -> Exchange:
    config = ExchangeConfig(
        reviews_required_for_accept=2,
        auto_assign=False,
        auto_review=False,
        verification_oracle=oracle,
        oracle_auto_reject=auto_reject,
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


# -- Tests -----------------------------------------------------------------


class TestVerificationResult:
    def test_is_pass(self):
        r = VerificationResult(status=VerificationStatus.PASSED)
        assert r.is_pass is True
        assert r.is_fail is False

    def test_is_fail(self):
        r = VerificationResult(status=VerificationStatus.FAILED, failed_tests=2)
        assert r.is_fail is True
        assert r.is_pass is False

    def test_defaults(self):
        r = VerificationResult(status=VerificationStatus.ERROR)
        assert r.passed_tests == 0
        assert r.stdout == ""
        assert r.execution_time_s == 0.0


class TestOracleProtocol:
    def test_passing_oracle_is_verification_oracle(self):
        assert isinstance(PassingOracle(), VerificationOracle)

    def test_failing_oracle_is_verification_oracle(self):
        assert isinstance(FailingOracle(), VerificationOracle)


class TestOracleIntegration:
    @pytest.mark.asyncio
    async def test_passing_oracle_adds_approve_review(self):
        ex = make_exchange(oracle=PassingOracle())
        agents = make_agents(2)
        for a in agents:
            ex.register(a)

        p = Problem(title="Test", description="verify me", author_id=agents[0].id)
        await ex.post_problem(p)
        await ex.claim_problem(p.id, agents[1].id)
        sol = await ex.solve_problem(p.id, agents[1].id)

        # Oracle should have added a review
        assert len(sol.review_ids) == 1
        review = ex.get_review(sol.review_ids[0])
        assert review.verdict == ReviewVerdict.APPROVE
        assert review.confidence == 1.0
        assert review.metadata.get("oracle") is True

        # Oracle result should be in solution metadata
        assert sol.metadata["oracle_result"]["status"] == "PASSED"
        assert sol.metadata["oracle_result"]["passed_tests"] == 5

    @pytest.mark.asyncio
    async def test_failing_oracle_adds_reject_review(self):
        ex = make_exchange(oracle=FailingOracle())
        agents = make_agents(2)
        for a in agents:
            ex.register(a)

        p = Problem(title="Test", description="will fail", author_id=agents[0].id)
        await ex.post_problem(p)
        await ex.claim_problem(p.id, agents[1].id)
        sol = await ex.solve_problem(p.id, agents[1].id)

        # Oracle should have added a rejection review
        assert len(sol.review_ids) == 1
        review = ex.get_review(sol.review_ids[0])
        assert review.verdict == ReviewVerdict.REJECT
        assert "FAILED" in review.body

    @pytest.mark.asyncio
    async def test_failing_oracle_auto_reject(self):
        ex = make_exchange(oracle=FailingOracle(), auto_reject=True)
        agents = make_agents(2)
        for a in agents:
            ex.register(a)

        p = Problem(title="Test", description="auto-reject", author_id=agents[0].id)
        await ex.post_problem(p)
        await ex.claim_problem(p.id, agents[1].id)
        sol = await ex.solve_problem(p.id, agents[1].id)

        # Solution should be auto-rejected
        assert sol.verdict == SolutionVerdict.REJECTED

    @pytest.mark.asyncio
    async def test_crashing_oracle_does_not_break_solve(self):
        ex = make_exchange(oracle=CrashingOracle())
        agents = make_agents(2)
        for a in agents:
            ex.register(a)

        p = Problem(title="Test", description="oracle crashes", author_id=agents[0].id)
        await ex.post_problem(p)
        await ex.claim_problem(p.id, agents[1].id)

        # Should not raise despite oracle crashing
        sol = await ex.solve_problem(p.id, agents[1].id)
        assert sol.verdict == SolutionVerdict.PENDING
        assert "oracle_result" not in sol.metadata

    @pytest.mark.asyncio
    async def test_skipping_oracle_does_nothing(self):
        ex = make_exchange(oracle=SkippingOracle())
        agents = make_agents(2)
        for a in agents:
            ex.register(a)

        p = Problem(title="Test", description="skipped", author_id=agents[0].id)
        await ex.post_problem(p)
        await ex.claim_problem(p.id, agents[1].id)
        sol = await ex.solve_problem(p.id, agents[1].id)

        # Oracle ran but SKIPPED — no review added
        assert len(sol.review_ids) == 0
        assert sol.metadata["oracle_result"]["status"] == "SKIPPED"

    @pytest.mark.asyncio
    async def test_no_oracle_configured(self):
        ex = make_exchange(oracle=None)
        agents = make_agents(2)
        for a in agents:
            ex.register(a)

        p = Problem(title="Test", description="no oracle", author_id=agents[0].id)
        await ex.post_problem(p)
        await ex.claim_problem(p.id, agents[1].id)
        sol = await ex.solve_problem(p.id, agents[1].id)

        assert len(sol.review_ids) == 0
        assert "oracle_result" not in sol.metadata
