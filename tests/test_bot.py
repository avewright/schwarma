"""Tests for schwarma.bot — SchwarmaBot SDK."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from schwarma.bot import BotConfig, SchwarmaBot
from schwarma.client import SchwarmaClient


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------

AGENT_ID = str(uuid4())


def _make_fake_client(problems: list[dict] | None = None) -> AsyncMock:
    """Build a mock SchwarmaClient with typical responses."""
    client = AsyncMock()
    client.register = AsyncMock(return_value={
        "agent_id": AGENT_ID,
        "name": "TestBot",
        "capabilities": ["GENERAL"],
        "model_tier": "STANDARD",
        "token": "tok-abc",
    })
    client.heartbeat = AsyncMock(return_value={"ok": True})
    client.request_work = AsyncMock(return_value=problems or [])
    client.claim_and_solve = AsyncMock(return_value={
        "solution_id": str(uuid4()), "status": "SOLVED",
    })
    client.list_reviews_needed = AsyncMock(return_value=[])
    client.submit_review = AsyncMock(return_value={"ok": True})
    client.update_watch_tags = AsyncMock(return_value={"ok": True})
    client.close = AsyncMock()
    return client


def _make_bot(**kwargs) -> SchwarmaBot:
    """Create a bot with sensible test defaults."""
    defaults = dict(
        name="TestBot",
        solver=AsyncMock(return_value="solved it"),
        capabilities=["CODE_GENERATION"],
        station_host="127.0.0.1",
        station_port=9741,
        config=BotConfig(
            heartbeat_interval=0.05,
            poll_interval=0.05,
            max_concurrent=1,
        ),
    )
    defaults.update(kwargs)
    return SchwarmaBot(**defaults)


class _FakeCtx:
    """Fake async context manager that returns a mock client."""

    def __init__(self, client: AsyncMock):
        self._client = client

    async def __aenter__(self):
        return self._client

    async def __aexit__(self, *exc):
        pass


# ---------------------------------------------------------------------------
# Tests: construction & config
# ---------------------------------------------------------------------------

class TestBotConfig:

    def test_defaults(self):
        cfg = BotConfig()
        assert cfg.heartbeat_interval == 30.0
        assert cfg.poll_interval == 5.0
        assert cfg.review_enabled is False
        assert cfg.max_concurrent == 1

    def test_custom(self):
        cfg = BotConfig(heartbeat_interval=10, poll_interval=2, review_enabled=True)
        assert cfg.heartbeat_interval == 10
        assert cfg.poll_interval == 2
        assert cfg.review_enabled is True


class TestBotConstruction:

    def test_defaults(self):
        bot = _make_bot()
        assert bot.name == "TestBot"
        assert bot.capabilities == ["CODE_GENERATION"]
        assert bot.model_tier == "STANDARD"
        assert bot.station_port == 9741
        assert bot.is_running is False
        assert bot.agent_id is None
        assert bot.problems_solved == 0

    def test_custom_config(self):
        cfg = BotConfig(poll_interval=1.0, review_enabled=True)
        bot = _make_bot(config=cfg)
        assert bot.config.poll_interval == 1.0
        assert bot.config.review_enabled is True

    def test_watch_tags(self):
        bot = _make_bot(watch_tags=["BUG", "FEATURE"])
        assert bot.watch_tags == ["BUG", "FEATURE"]

    def test_repr(self):
        bot = _make_bot()
        r = repr(bot)
        assert "TestBot" in r
        assert "CODE_GENERATION" in r


# ---------------------------------------------------------------------------
# Tests: solver adapter
# ---------------------------------------------------------------------------

class TestSolverAdapter:

    async def test_async_two_arg(self):
        async def solver(desc, ctx):
            return f"answer: {desc}"

        bot = _make_bot(solver=solver)
        result = await bot._invoke_solver("hello", {})
        assert result == "answer: hello"

    async def test_sync_one_arg(self):
        def solver(desc):
            return f"sync: {desc}"

        bot = _make_bot(solver=solver)
        result = await bot._invoke_solver("hi", {})
        assert result == "sync: hi"

    async def test_sync_two_arg(self):
        def solver(desc, ctx):
            return f"{desc} + {ctx.get('extra', 'none')}"

        bot = _make_bot(solver=solver)
        result = await bot._invoke_solver("test", {"extra": "data"})
        assert result == "test + data"

    async def test_async_one_arg(self):
        async def solver(desc):
            return desc.upper()

        bot = _make_bot(solver=solver)
        result = await bot._invoke_solver("hello", {})
        assert result == "HELLO"


# ---------------------------------------------------------------------------
# Tests: connect & register
# ---------------------------------------------------------------------------

class TestConnectAndRegister:

    async def test_register_sets_agent_id(self):
        client = _make_fake_client()
        bot = _make_bot()

        with patch.object(SchwarmaClient, "tcp", return_value=_FakeCtx(client)):
            await bot._connect_and_register()

        assert bot._agent_id == AGENT_ID
        client.register.assert_awaited_once_with(
            "TestBot",
            capabilities=["CODE_GENERATION"],
            model_tier="STANDARD",
        )

    async def test_register_with_watch_tags(self):
        client = _make_fake_client()
        bot = _make_bot(watch_tags=["BUG", "FEATURE"])

        with patch.object(SchwarmaClient, "tcp", return_value=_FakeCtx(client)):
            await bot._connect_and_register()

        client.update_watch_tags.assert_awaited_once_with(["BUG", "FEATURE"])


# ---------------------------------------------------------------------------
# Tests: solve problem
# ---------------------------------------------------------------------------

class TestSolveProblem:

    async def test_successful_solve(self):
        client = _make_fake_client()
        solver = AsyncMock(return_value="my answer")
        bot = _make_bot(solver=solver)
        bot._client = client
        bot._agent_id = AGENT_ID

        problem = {"id": str(uuid4()), "title": "Test", "description": "Do something"}
        await bot._solve_problem(problem)

        assert bot.problems_solved == 1
        assert bot.problems_failed == 0
        client.claim_and_solve.assert_awaited_once()

    async def test_failed_solve(self):
        client = _make_fake_client()
        client.claim_and_solve.side_effect = Exception("network error")
        solver = AsyncMock(return_value="answer")
        bot = _make_bot(solver=solver)
        bot._client = client
        bot._agent_id = AGENT_ID

        problem = {"id": str(uuid4()), "title": "Test", "description": "Do something"}
        await bot._solve_problem(problem)

        assert bot.problems_solved == 0
        assert bot.problems_failed == 1

    async def test_on_solve_callback(self):
        client = _make_fake_client()
        on_solve = MagicMock()
        bot = _make_bot(solver=AsyncMock(return_value="answer"), on_solve=on_solve)
        bot._client = client
        bot._agent_id = AGENT_ID

        problem = {"id": str(uuid4()), "title": "Test", "description": "Desc"}
        await bot._solve_problem(problem)

        on_solve.assert_called_once()
        args = on_solve.call_args[0]
        assert args[0] == problem
        assert args[1] == "answer"

    async def test_on_error_callback(self):
        client = _make_fake_client()
        client.claim_and_solve.side_effect = RuntimeError("boom")
        on_error = MagicMock()
        bot = _make_bot(solver=AsyncMock(return_value="x"), on_error=on_error)
        bot._client = client
        bot._agent_id = AGENT_ID

        problem = {"id": str(uuid4()), "title": "T", "description": "D"}
        await bot._solve_problem(problem)

        on_error.assert_called_once()
        assert isinstance(on_error.call_args[0][1], RuntimeError)


# ---------------------------------------------------------------------------
# Tests: review pass
# ---------------------------------------------------------------------------

class TestReviewPass:

    async def test_review_approve(self):
        client = _make_fake_client()
        client.list_reviews_needed.return_value = [{
            "solution_id": str(uuid4()),
            "body": "def foo(): return 42",
            "problem_description": "Write foo",
        }]
        solver = AsyncMock(return_value="APPROVE — looks correct")
        bot = _make_bot(solver=solver, config=BotConfig(review_enabled=True))
        bot._client = client
        bot._agent_id = AGENT_ID

        await bot._review_pass()

        assert bot.reviews_submitted == 1
        client.submit_review.assert_awaited_once()
        call_kwargs = client.submit_review.call_args.kwargs
        assert call_kwargs["verdict"] == "APPROVE"

    async def test_review_reject(self):
        client = _make_fake_client()
        client.list_reviews_needed.return_value = [{
            "solution_id": str(uuid4()),
            "body": "idk",
            "problem_description": "Write foo",
        }]
        solver = AsyncMock(return_value="REJECT — low effort")
        bot = _make_bot(solver=solver, config=BotConfig(review_enabled=True))
        bot._client = client
        bot._agent_id = AGENT_ID

        await bot._review_pass()

        assert bot.reviews_submitted == 1
        call_kwargs = client.submit_review.call_args.kwargs
        assert call_kwargs["verdict"] == "REJECT"


# ---------------------------------------------------------------------------
# Tests: main loop (short-lived via shutdown)
# ---------------------------------------------------------------------------

class TestMainLoop:

    async def test_loop_polls_and_stops(self):
        """Bot polls once, finds a problem, solves it, then shuts down."""
        pid = str(uuid4())
        client = _make_fake_client(problems=[
            {"id": pid, "title": "Prob", "description": "Do it"},
        ])
        solver = AsyncMock(return_value="done")
        bot = _make_bot(solver=solver)
        bot._client = client
        bot._agent_id = AGENT_ID
        bot._shutdown_event = asyncio.Event()

        # Shut down after a brief delay
        async def _delayed_shutdown():
            await asyncio.sleep(0.15)
            bot._request_shutdown()

        asyncio.create_task(_delayed_shutdown())
        await bot._main_loop()

        assert bot.problems_solved >= 1 or client.request_work.await_count >= 1

    async def test_loop_heartbeats(self):
        """Bot sends heartbeats periodically."""
        client = _make_fake_client()
        bot = _make_bot(config=BotConfig(
            heartbeat_interval=0.02,
            poll_interval=0.02,
        ))
        bot._client = client
        bot._agent_id = AGENT_ID
        bot._shutdown_event = asyncio.Event()

        async def _delayed_shutdown():
            await asyncio.sleep(0.1)
            bot._request_shutdown()

        asyncio.create_task(_delayed_shutdown())
        await bot._main_loop()

        assert client.heartbeat.await_count >= 1

    async def test_consecutive_errors_backoff(self):
        """Errors increase the poll delay via backoff."""
        client = _make_fake_client()
        client.request_work.side_effect = ConnectionError("refused")
        bot = _make_bot(config=BotConfig(
            poll_interval=0.02,
            max_consecutive_errors=2,
            backoff_multiplier=2.0,
        ))
        bot._client = client
        bot._agent_id = AGENT_ID
        bot._shutdown_event = asyncio.Event()

        async def _delayed_shutdown():
            await asyncio.sleep(0.2)
            bot._request_shutdown()

        asyncio.create_task(_delayed_shutdown())
        await bot._main_loop()

        assert bot._consecutive_errors >= 2


# ---------------------------------------------------------------------------
# Tests: stats & shutdown
# ---------------------------------------------------------------------------

class TestBotStats:

    def test_stats_dict(self):
        bot = _make_bot()
        bot._agent_id = "abc-123"
        bot.problems_solved = 5
        bot.problems_failed = 1
        bot.reviews_submitted = 3

        s = bot.stats()
        assert s["name"] == "TestBot"
        assert s["agent_id"] == "abc-123"
        assert s["problems_solved"] == 5
        assert s["problems_failed"] == 1
        assert s["reviews_submitted"] == 3

    async def test_shutdown(self):
        bot = _make_bot()
        bot._shutdown_event = asyncio.Event()
        await bot.shutdown()
        assert bot._shutdown_event.is_set()
