"""Tests for multi-round revision dialogue — request_revision / revise_solution."""

from uuid import uuid4

import pytest

from schwarma.agent import Agent, AgentCapability
from schwarma.errors import NotFoundError, PermissionError_, StateError
from schwarma.exchange import Exchange, ExchangeConfig
from schwarma.problem import Problem, ProblemStatus
from schwarma.solution import RevisionRound, SolutionVerdict


# -- Helpers ---------------------------------------------------------------

async def auto_solver(desc: str, ctx: dict) -> str:
    attempt = ctx.get("attempt", 1)
    feedback = ctx.get("revision_feedback", "")
    if feedback:
        return f"revised (attempt {attempt}): addressing {feedback[:20]}"
    return f"initial answer: {desc[:30]}"


async def approve_solver(desc: str, ctx: dict) -> str:
    return "APPROVE"


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


# -- Tests -----------------------------------------------------------------


class TestRequestRevision:
    @pytest.mark.asyncio
    async def test_basic_revision_request(self):
        ex = make_exchange()
        agents = make_agents(3)
        for a in agents:
            ex.register(a)

        p = Problem(title="Fix bug", description="broken", author_id=agents[0].id)
        await ex.post_problem(p)
        await ex.claim_problem(p.id, agents[1].id)
        sol = await ex.solve_problem(p.id, agents[1].id)

        await ex.request_revision(sol.id, agents[2].id, "needs more tests")

        assert sol.verdict == SolutionVerdict.NEEDS_REVISION
        assert len(sol.revision_history) == 1
        rr = sol.revision_history[0]
        assert rr.round_number == 1
        assert rr.reviewer_feedback == "needs more tests"
        assert rr.reviewer_id == agents[2].id
        assert p.status == ProblemStatus.CLAIMED

    @pytest.mark.asyncio
    async def test_max_rounds_exceeded_raises(self):
        ex = make_exchange(max_revision_rounds=2)
        agents = make_agents(3)
        for a in agents:
            ex.register(a)

        p = Problem(title="Task", description="d", author_id=agents[0].id)
        await ex.post_problem(p)
        await ex.claim_problem(p.id, agents[1].id)
        sol = await ex.solve_problem(p.id, agents[1].id)

        await ex.request_revision(sol.id, agents[2].id, "round 1")
        sol.revision_history[-1].revised_body = "v2"
        await ex.request_revision(sol.id, agents[2].id, "round 2")
        sol.revision_history[-1].revised_body = "v3"

        with pytest.raises(StateError, match="maximum"):
            await ex.request_revision(sol.id, agents[2].id, "round 3")

    @pytest.mark.asyncio
    async def test_revision_for_nonexistent_solution_raises(self):
        ex = make_exchange()
        with pytest.raises(NotFoundError):
            await ex.request_revision(uuid4(), uuid4(), "feedback")


class TestReviseSolution:
    @pytest.mark.asyncio
    async def test_revise_with_explicit_body(self):
        ex = make_exchange()
        agents = make_agents(3)
        for a in agents:
            ex.register(a)

        p = Problem(title="Task", description="d", author_id=agents[0].id)
        await ex.post_problem(p)
        await ex.claim_problem(p.id, agents[1].id)
        sol = await ex.solve_problem(p.id, agents[1].id)

        await ex.request_revision(sol.id, agents[2].id, "fix X")
        await ex.revise_solution(sol.id, agents[1].id, revised_body="fixed X")

        assert sol.body == "fixed X"
        assert sol.verdict == SolutionVerdict.PENDING
        assert sol.revision_history[-1].revised_body == "fixed X"

    @pytest.mark.asyncio
    async def test_revise_via_solver_callback(self):
        ex = make_exchange()
        agents = make_agents(3)
        for a in agents:
            ex.register(a)

        p = Problem(title="Task", description="some problem", author_id=agents[0].id)
        await ex.post_problem(p)
        await ex.claim_problem(p.id, agents[0].id)
        sol = await ex.solve_problem(p.id, agents[0].id)

        await ex.request_revision(sol.id, agents[2].id, "add error handling")
        await ex.revise_solution(sol.id, agents[0].id)  # uses callback

        # auto_solver includes attempt and feedback in the response
        assert "attempt 2" in sol.body
        assert "add error handling" in sol.body

    @pytest.mark.asyncio
    async def test_revise_wrong_author_raises(self):
        ex = make_exchange()
        agents = make_agents(3)
        for a in agents:
            ex.register(a)

        p = Problem(title="Task", description="d", author_id=agents[0].id)
        await ex.post_problem(p)
        await ex.claim_problem(p.id, agents[1].id)
        sol = await ex.solve_problem(p.id, agents[1].id)

        await ex.request_revision(sol.id, agents[2].id, "fix")
        with pytest.raises(PermissionError_):
            await ex.revise_solution(sol.id, agents[2].id, revised_body="nope")

    @pytest.mark.asyncio
    async def test_revise_without_request_raises(self):
        ex = make_exchange()
        agents = make_agents(2)
        for a in agents:
            ex.register(a)

        p = Problem(title="Task", description="d", author_id=agents[0].id)
        await ex.post_problem(p)
        await ex.claim_problem(p.id, agents[1].id)
        sol = await ex.solve_problem(p.id, agents[1].id)

        with pytest.raises(StateError, match="No revision"):
            await ex.revise_solution(sol.id, agents[1].id, revised_body="v2")

    @pytest.mark.asyncio
    async def test_revise_already_revised_round_raises(self):
        ex = make_exchange()
        agents = make_agents(3)
        for a in agents:
            ex.register(a)

        p = Problem(title="Task", description="d", author_id=agents[0].id)
        await ex.post_problem(p)
        await ex.claim_problem(p.id, agents[1].id)
        sol = await ex.solve_problem(p.id, agents[1].id)

        await ex.request_revision(sol.id, agents[2].id, "fix X")
        await ex.revise_solution(sol.id, agents[1].id, revised_body="v2")

        with pytest.raises(StateError, match="already has a revised body"):
            await ex.revise_solution(sol.id, agents[1].id, revised_body="v3")


class TestRevisionRoundTrip:
    """Test that revision_history survives serialization."""

    @pytest.mark.asyncio
    async def test_solution_with_revisions_round_trip(self):
        from schwarma.solution import Solution

        sol = Solution(problem_id=uuid4(), author_id=uuid4(), body="v1")
        sol.revision_history.append(
            RevisionRound(
                round_number=1,
                reviewer_feedback="add tests",
                reviewer_id=uuid4(),
                revised_body="v2",
            )
        )
        d = sol.to_dict()
        sol2 = Solution.from_dict(d)
        assert len(sol2.revision_history) == 1
        assert sol2.revision_history[0].round_number == 1
        assert sol2.revision_history[0].reviewer_feedback == "add tests"
        assert sol2.revision_history[0].revised_body == "v2"
