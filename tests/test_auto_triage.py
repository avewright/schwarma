"""Tests for auto-triage push and pull-based work discovery."""

from __future__ import annotations

import pytest

from schwarma.agent import Agent, AgentCapability
from schwarma.events import EventKind
from schwarma.exchange import Exchange, ExchangeConfig
from schwarma.problem import Problem, ProblemTag


# ── Helpers ──────────────────────────────────────────────────────────────

async def _dummy(desc: str, ctx: dict) -> str:
    return "solved"


def _cfg(**overrides) -> ExchangeConfig:
    defaults = dict(
        min_reputation_to_claim=0,
        enable_staking=False,
        enable_content_guards=False,
        enable_effort_guards=False,
        enable_similarity_check=False,
        auto_assign=True,
    )
    defaults.update(overrides)
    return ExchangeConfig(**defaults)


# ── Tests ────────────────────────────────────────────────────────────────


class TestAutoTriagePush:

    @pytest.mark.asyncio
    async def test_triage_push_to_inbox(self):
        """Posting a problem sends TRIAGE_ASSIGNED to candidate inboxes."""
        cfg = _cfg()
        ex = Exchange(cfg)
        author = Agent(name="Author", solver=_dummy)
        solver = Agent(name="Solver", solver=_dummy)
        ex.register(author)
        ex.register(solver)

        await ex.post_problem(Problem(
            title="Test problem",
            description="Something to solve",
            author_id=author.id,
        ))

        # Solver should have a triage notification
        msgs = ex.inbox(solver.id)
        triage_msgs = [m for m in msgs if m["kind"] == "TRIAGE_ASSIGNED"]
        assert len(triage_msgs) >= 1

    @pytest.mark.asyncio
    async def test_watch_tags_filter(self):
        """Agents with watch_tags only get pushes for matching problems."""
        cfg = _cfg()
        ex = Exchange(cfg)
        author = Agent(name="Author", solver=_dummy)
        bug_solver = Agent(name="BugSolver", solver=_dummy, watch_tags={ProblemTag.BUG})
        feature_solver = Agent(name="FeatureSolver", solver=_dummy, watch_tags={ProblemTag.FEATURE})
        general_solver = Agent(name="General", solver=_dummy)  # no preference
        for a in (author, bug_solver, feature_solver, general_solver):
            ex.register(a)

        # Post a BUG problem
        await ex.post_problem(Problem(
            title="Bug fix needed",
            description="Something crashes",
            author_id=author.id,
            tags=[ProblemTag.BUG],
        ))

        # Bug solver should get a push
        bug_msgs = [m for m in ex.inbox(bug_solver.id) if m["kind"] == "TRIAGE_ASSIGNED"]
        assert len(bug_msgs) >= 1

        # Feature solver should NOT get a push (wrong tags)
        feat_msgs = [m for m in ex.inbox(feature_solver.id) if m["kind"] == "TRIAGE_ASSIGNED"]
        assert len(feat_msgs) == 0

        # General solver (no watch_tags) should get a push
        gen_msgs = [m for m in ex.inbox(general_solver.id) if m["kind"] == "TRIAGE_ASSIGNED"]
        assert len(gen_msgs) >= 1

    @pytest.mark.asyncio
    async def test_suspended_agents_excluded(self):
        """Suspended agents don't receive triage pushes."""
        cfg = _cfg()
        ex = Exchange(cfg)
        author = Agent(name="Author", solver=_dummy)
        solver = Agent(name="Solver", solver=_dummy)
        ex.register(author)
        ex.register(solver)
        await ex.suspend_agent(solver.id, reason="testing")

        await ex.post_problem(Problem(
            title="Test problem",
            description="Something to solve",
            author_id=author.id,
        ))

        msgs = ex.inbox(solver.id)
        triage_msgs = [m for m in msgs if m["kind"] == "TRIAGE_ASSIGNED"]
        assert len(triage_msgs) == 0

    @pytest.mark.asyncio
    async def test_at_capacity_excluded(self):
        """Agents at max capacity don't get triage pushes."""
        cfg = _cfg(max_active_per_agent=1)
        ex = Exchange(cfg)
        author = Agent(name="Author", solver=_dummy)
        solver = Agent(name="Solver", solver=_dummy)
        ex.register(author)
        ex.register(solver)

        # Claim one problem to fill capacity
        p1 = await ex.post_problem(Problem(
            title="First", description="First problem", author_id=author.id,
        ))
        # Clear inbox from first triage
        ex.clear_inbox(solver.id)
        await ex.claim_problem(p1.id, solver.id)

        # Post another — solver is at capacity
        await ex.post_problem(Problem(
            title="Second", description="Second problem", author_id=author.id,
        ))

        msgs = ex.inbox(solver.id)
        triage_msgs = [m for m in msgs if m["kind"] == "TRIAGE_ASSIGNED"]
        assert len(triage_msgs) == 0

    @pytest.mark.asyncio
    async def test_circuit_open_agents_excluded(self):
        """Agents with open circuit breaker should not receive triage pushes."""
        cfg = _cfg(
            circuit_breaker_failure_threshold=1,
            circuit_breaker_cooldown_seconds=3600,
        )
        ex = Exchange(cfg)
        author = Agent(name="Author", solver=_dummy)
        solver = Agent(name="Solver", solver=_dummy)
        ex.register(author)
        ex.register(solver)

        ex._record_agent_failure(solver.id)  # open circuit immediately

        await ex.post_problem(Problem(
            title="Test problem",
            description="Something to solve",
            author_id=author.id,
        ))

        msgs = ex.inbox(solver.id)
        triage_msgs = [m for m in msgs if m["kind"] == "TRIAGE_ASSIGNED"]
        assert len(triage_msgs) == 0

    @pytest.mark.asyncio
    async def test_update_watch_tags(self):
        """update_watch_tags changes agent's triage preferences."""
        cfg = _cfg()
        ex = Exchange(cfg)
        agent = Agent(name="Agent", solver=_dummy)
        author = Agent(name="Author", solver=_dummy)
        ex.register(agent)
        ex.register(author)

        # Initially no watch_tags → gets all pushes
        assert agent.watch_tags == set()

        ex.update_watch_tags(agent.id, {ProblemTag.SECURITY})
        assert agent.watch_tags == {ProblemTag.SECURITY}

        # Post a BUG problem — agent watches SECURITY, should not get push
        await ex.post_problem(Problem(
            title="Bug fix", description="A bug", author_id=author.id,
            tags=[ProblemTag.BUG],
        ))
        triage_msgs = [m for m in ex.inbox(agent.id) if m["kind"] == "TRIAGE_ASSIGNED"]
        assert len(triage_msgs) == 0

    @pytest.mark.asyncio
    async def test_no_solver_queues_problem_for_graceful_degradation(self):
        """If no eligible solvers exist, problem is queued and reroute event emitted."""
        cfg = _cfg()
        ex = Exchange(cfg)
        author = Agent(name="Author", solver=_dummy)
        ex.register(author)

        ex.bus.enable_recording()
        p = await ex.post_problem(Problem(
            title="Queue me",
            description="No one can solve right now",
            author_id=author.id,
        ))

        assert p.id in ex._degraded_queue
        reroutes = [e for e in ex.bus.recorded_events if e.kind == EventKind.TRIAGE_REROUTED]
        assert len(reroutes) == 1
        assert reroutes[0].problem_id == p.id
        assert reroutes[0].payload.get("queued") is True


class TestRequestWork:

    @pytest.mark.asyncio
    async def test_basic_request_work(self):
        """request_work returns open problems."""
        cfg = _cfg(auto_assign=False)
        ex = Exchange(cfg)
        author = Agent(name="Author", solver=_dummy)
        solver = Agent(name="Solver", solver=_dummy)
        ex.register(author)
        ex.register(solver)

        p = await ex.post_problem(Problem(
            title="Open problem",
            description="Needs a solver",
            author_id=author.id,
        ))

        work = ex.request_work(solver.id)
        assert len(work) >= 1
        assert any(w.id == p.id for w in work)

    @pytest.mark.asyncio
    async def test_excludes_own_problems(self):
        """request_work doesn't return the agent's own problems."""
        cfg = _cfg(auto_assign=False)
        ex = Exchange(cfg)
        author = Agent(name="Author", solver=_dummy)
        ex.register(author)

        await ex.post_problem(Problem(
            title="My problem",
            description="I posted this",
            author_id=author.id,
        ))

        work = ex.request_work(author.id)
        assert len(work) == 0

    @pytest.mark.asyncio
    async def test_tag_filter(self):
        """request_work respects tag filter."""
        cfg = _cfg(auto_assign=False)
        ex = Exchange(cfg)
        author = Agent(name="Author", solver=_dummy)
        solver = Agent(name="Solver", solver=_dummy)
        ex.register(author)
        ex.register(solver)

        await ex.post_problem(Problem(
            title="Bug", description="A bug", author_id=author.id,
            tags=[ProblemTag.BUG],
        ))
        await ex.post_problem(Problem(
            title="Feature", description="A feature", author_id=author.id,
            tags=[ProblemTag.FEATURE],
        ))

        work = ex.request_work(solver.id, tags={ProblemTag.BUG})
        assert len(work) == 1
        assert work[0].title == "Bug"

    @pytest.mark.asyncio
    async def test_suspended_gets_nothing(self):
        """Suspended agents get no work."""
        cfg = _cfg(auto_assign=False)
        ex = Exchange(cfg)
        author = Agent(name="Author", solver=_dummy)
        solver = Agent(name="Solver", solver=_dummy)
        ex.register(author)
        ex.register(solver)

        await ex.post_problem(Problem(
            title="Open", description="Open", author_id=author.id,
        ))
        await ex.suspend_agent(solver.id, reason="test")

        work = ex.request_work(solver.id)
        assert len(work) == 0

    @pytest.mark.asyncio
    async def test_at_capacity_gets_nothing(self):
        """Agents at capacity get no work from request_work."""
        cfg = _cfg(auto_assign=False, max_active_per_agent=0)
        ex = Exchange(cfg)
        author = Agent(name="Author", solver=_dummy)
        solver = Agent(name="Solver", solver=_dummy)
        ex.register(author)
        ex.register(solver)

        await ex.post_problem(Problem(
            title="Open", description="Open", author_id=author.id,
        ))

        work = ex.request_work(solver.id)
        assert len(work) == 0

    @pytest.mark.asyncio
    async def test_circuit_open_gets_no_work(self):
        """request_work returns nothing while circuit breaker is open."""
        cfg = _cfg(
            auto_assign=False,
            circuit_breaker_failure_threshold=1,
            circuit_breaker_cooldown_seconds=3600,
        )
        ex = Exchange(cfg)
        author = Agent(name="Author", solver=_dummy)
        solver = Agent(name="Solver", solver=_dummy)
        ex.register(author)
        ex.register(solver)

        await ex.post_problem(Problem(
            title="Open", description="Open", author_id=author.id,
        ))
        ex._record_agent_failure(solver.id)  # open circuit immediately

        work = ex.request_work(solver.id)
        assert len(work) == 0

    @pytest.mark.asyncio
    async def test_request_work_drains_degraded_queue(self):
        """When a solver requests work, queued degraded problems are drained."""
        cfg = _cfg(auto_assign=True)
        ex = Exchange(cfg)
        author = Agent(name="Author", solver=_dummy)
        ex.register(author)

        p = await ex.post_problem(Problem(
            title="Queued",
            description="Initially no solver present",
            author_id=author.id,
        ))
        assert p.id in ex._degraded_queue

        solver = Agent(name="Solver", solver=_dummy)
        ex.register(solver)

        work = ex.request_work(solver.id)
        assert any(w.id == p.id for w in work)
        assert p.id not in ex._degraded_queue
