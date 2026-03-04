"""Tests for the Agent model."""

import pytest

from schwarma.agent import Agent, AgentCapability
from uuid import uuid4


# -- Fixtures ---------------------------------------------------------------

async def dummy_solver(desc: str, ctx: dict) -> str:
    return f"solved: {desc}"


def make_agent(**kwargs) -> Agent:
    defaults = dict(name="TestAgent", solver=dummy_solver)
    defaults.update(kwargs)
    return Agent(**defaults)


# -- Tests ------------------------------------------------------------------

class TestAgentCapabilities:
    def test_has_capability(self):
        a = make_agent(capabilities={AgentCapability.DEBUGGING})
        assert a.has_capability(AgentCapability.DEBUGGING)
        assert not a.has_capability(AgentCapability.MATH)

    def test_general_matches_anything(self):
        a = make_agent(capabilities={AgentCapability.GENERAL})
        assert a.has_capability(AgentCapability.SECURITY_AUDIT)

    def test_has_any_capability(self):
        a = make_agent(capabilities={AgentCapability.DEBUGGING, AgentCapability.MATH})
        assert a.has_any_capability({AgentCapability.MATH, AgentCapability.RESEARCH})
        assert not a.has_any_capability({AgentCapability.DOCUMENTATION})


class TestAgentWorkTracking:
    def test_claim_and_release(self):
        a = make_agent()
        pid = uuid4()
        a.claim(pid)
        assert a.active_count == 1
        a.release(pid)
        assert a.active_count == 0
        assert a._total_solved == 1

    def test_multiple_claims(self):
        a = make_agent()
        ids = [uuid4() for _ in range(3)]
        for i in ids:
            a.claim(i)
        assert a.active_count == 3


class TestAgentSolve:
    @pytest.mark.asyncio
    async def test_solve_delegates_to_callback(self):
        a = make_agent()
        result = await a.solve("hello")
        assert result == "solved: hello"

    @pytest.mark.asyncio
    async def test_solve_supports_sync_single_arg_solver(self):
        def single_arg(desc: str) -> str:
            return f"sync: {desc}"

        a = make_agent(solver=single_arg)
        result = await a.solve("hello")
        assert result == "sync: hello"

    @pytest.mark.asyncio
    async def test_solve_supports_sync_two_arg_solver(self):
        def two_arg(desc: str, ctx: dict) -> str:
            return f"{desc}:{ctx.get('x', 0)}"

        a = make_agent(solver=two_arg)
        result = await a.solve("hello", {"x": 7})
        assert result == "hello:7"

    @pytest.mark.asyncio
    async def test_solve_supports_async_single_arg_solver(self):
        async def single_arg_async(desc: str) -> str:
            return f"async: {desc}"

        a = make_agent(solver=single_arg_async)
        result = await a.solve("hello")
        assert result == "async: hello"

    def test_invalid_solver_signature_raises(self):
        def bad(a, b, c):
            return "nope"

        with pytest.raises(TypeError):
            make_agent(solver=bad)


class TestAgentIdentity:
    def test_equality_by_id(self):
        a = make_agent()
        b = make_agent()
        assert a != b
        b.id = a.id
        assert a == b

    def test_hashable(self):
        a = make_agent()
        s = {a, a}
        assert len(s) == 1
