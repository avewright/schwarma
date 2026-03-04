"""Tests for the TriageRouter."""

from schwarma.agent import Agent, AgentCapability
from schwarma.problem import Problem, ProblemTag
from schwarma.triage import TriageConfig, TriageRouter, TriageStrategy


async def noop_solver(desc: str, ctx: dict) -> str:
    return ""


def make_agent(name: str, caps: set[AgentCapability]) -> Agent:
    return Agent(name=name, solver=noop_solver, capabilities=caps)


class TestTriageRouter:
    def test_capability_match_ranks_specialist_first(self):
        router = TriageRouter(config=TriageConfig(strategy=TriageStrategy.CAPABILITY_MATCH))
        debugger = make_agent("Debugger", {AgentCapability.DEBUGGING})
        writer = make_agent("Writer", {AgentCapability.DOCUMENTATION})
        generalist = make_agent("Gen", {AgentCapability.GENERAL})

        p = Problem(
            title="Bug",
            description="...",
            author_id=generalist.id,
            tags={ProblemTag.BUG},
        )
        ranked = router.rank(p, [debugger, writer, generalist], top_n=3)
        # Debugger should be first (direct capability match)
        assert ranked[0].name == "Debugger"

    def test_excludes_author(self):
        router = TriageRouter(config=TriageConfig(strategy=TriageStrategy.CAPABILITY_MATCH))
        a = make_agent("A", {AgentCapability.GENERAL})
        b = make_agent("B", {AgentCapability.GENERAL})

        p = Problem(title="T", description="D", author_id=a.id)
        ranked = router.rank(p, [a, b])
        assert a not in ranked

    def test_round_robin_rotates(self):
        router = TriageRouter(config=TriageConfig(strategy=TriageStrategy.ROUND_ROBIN))
        agents = [make_agent(f"A{i}", {AgentCapability.GENERAL}) for i in range(5)]
        author = make_agent("Author", {AgentCapability.GENERAL})

        p = Problem(title="T", description="D", author_id=author.id)
        first = router.rank(p, agents, top_n=2)
        second = router.rank(p, agents, top_n=2)
        # Should rotate, so the picks differ
        assert first != second or len(agents) <= 2

    def test_least_busy_prefers_idle(self):
        router = TriageRouter(config=TriageConfig(strategy=TriageStrategy.LEAST_BUSY))
        busy = make_agent("Busy", {AgentCapability.GENERAL})
        idle = make_agent("Idle", {AgentCapability.GENERAL})
        from uuid import uuid4
        for _ in range(3):
            busy.claim(uuid4())

        author = make_agent("Author", {AgentCapability.GENERAL})
        p = Problem(title="T", description="D", author_id=author.id)
        ranked = router.rank(p, [busy, idle])
        assert ranked[0].name == "Idle"
