"""Tests for the SwapPool."""

from schwarma.agent import Agent, AgentCapability
from schwarma.problem import Problem, ProblemTag
from schwarma.swap import SwapPool, SwapStatus


async def noop(desc: str, ctx: dict) -> str:
    return ""


def make_agent(name: str, caps: set[AgentCapability]) -> Agent:
    return Agent(name=name, solver=noop, capabilities=caps)


class TestSwapPool:
    def test_submit_and_count(self):
        pool = SwapPool()
        a = make_agent("A", {AgentCapability.DEBUGGING})
        p = Problem(title="P", description="D", author_id=a.id, tags={ProblemTag.BUG})
        pool.submit(a, p)
        assert pool.waiting_count == 1

    def test_match_compatible_pair(self):
        pool = SwapPool()
        a = make_agent("A", {AgentCapability.DEBUGGING, AgentCapability.CODE_GENERATION})
        b = make_agent("B", {AgentCapability.CODE_GENERATION, AgentCapability.CODE_REVIEW})

        pa = Problem(title="PA", description="D", author_id=a.id, tags={ProblemTag.BUG})
        pb = Problem(title="PB", description="D", author_id=b.id, tags={ProblemTag.FEATURE})

        pool.submit(a, pa)
        pool.submit(b, pb)

        match = pool.try_match()
        assert match is not None
        assert pool.waiting_count == 0

    def test_no_match_same_agent(self):
        pool = SwapPool()
        a = make_agent("A", {AgentCapability.GENERAL})

        p1 = Problem(title="P1", description="D", author_id=a.id)
        p2 = Problem(title="P2", description="D", author_id=a.id)
        pool.submit(a, p1)
        pool.submit(a, p2)

        match = pool.try_match()
        assert match is None

    def test_cancel(self):
        pool = SwapPool()
        a = make_agent("A", {AgentCapability.GENERAL})
        p = Problem(title="P", description="D", author_id=a.id)
        entry = pool.submit(a, p)
        pool.cancel(entry.id)
        assert pool.waiting_count == 0
        assert entry.status == SwapStatus.CANCELLED

    def test_complete_match(self):
        pool = SwapPool()
        a = make_agent("A", {AgentCapability.GENERAL})
        b = make_agent("B", {AgentCapability.GENERAL})
        pa = Problem(title="PA", description="D", author_id=a.id)
        pb = Problem(title="PB", description="D", author_id=b.id)
        pool.submit(a, pa)
        pool.submit(b, pb)

        match = pool.try_match()
        assert match is not None
        pool.complete(match.id)
        assert match.completed
        assert match.entry_a.status == SwapStatus.COMPLETED

    def test_match_all_greedy(self):
        pool = SwapPool()
        agents = [make_agent(f"A{i}", {AgentCapability.GENERAL}) for i in range(4)]
        for a in agents:
            p = Problem(title=f"P-{a.name}", description="D", author_id=a.id)
            pool.submit(a, p)

        matches = pool.match_all()
        assert len(matches) == 2
        assert pool.waiting_count == 0
