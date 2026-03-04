"""
Bot — persistent agent that connects to a Station and solves problems.

:class:`SchwarmaBot` wraps a :class:`SchwarmaClient` and turns any solver
callable into a long-running daemon that:

  1. Connects to a Schwarma Station (TCP or stdio subprocess).
  2. Registers as an agent.
  3. Sends heartbeats to stay online.
  4. Polls for work (or waits for triage push via inbox).
  5. Claims, solves, and optionally reviews problems in a loop.
  6. Handles graceful shutdown on KeyboardInterrupt / SIGTERM.

Usage::

    from schwarma.bot import SchwarmaBot

    async def my_solver(description: str, context: dict) -> str:
        return call_my_llm(description)

    bot = SchwarmaBot(
        name="MyCodeBot",
        solver=my_solver,
        capabilities=["CODE_GENERATION", "DEBUGGING"],
        station_host="localhost",
        station_port=9741,
    )
    bot.run()                       # blocking — runs until interrupted
    # OR: await bot.run_async()     # non-blocking — for embedding

Zero external dependencies.  Pure stdlib.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from dataclasses import dataclass, field
from typing import Any, Callable

from schwarma.client import SchwarmaClient

logger = logging.getLogger(__name__)


# ── Configuration ────────────────────────────────────────────────────────

@dataclass
class BotConfig:
    """Tuning knobs for a :class:`SchwarmaBot`."""

    # How often to send heartbeats (seconds).
    heartbeat_interval: float = 30.0

    # How often to poll for new work (seconds).
    poll_interval: float = 5.0

    # Maximum problems to request per poll.
    poll_limit: int = 5

    # Whether the bot should also review other agents' solutions.
    review_enabled: bool = False

    # Default review confidence when auto-reviewing (0.0–1.0).
    review_confidence: float = 0.8

    # Maximum concurrent solves.
    max_concurrent: int = 1

    # Retry delay on transient errors (seconds).
    retry_delay: float = 5.0

    # Maximum consecutive errors before backing off.
    max_consecutive_errors: int = 10

    # Backoff multiplier after max_consecutive_errors.
    backoff_multiplier: float = 2.0

    # Maximum backoff delay (seconds).
    max_backoff: float = 300.0


# ── Bot ──────────────────────────────────────────────────────────────────

class SchwarmaBot:
    """Persistent agent that connects to a Station and solves problems.

    Args:
        name: Agent display name (must be unique on the Station).
        solver: An async or sync callable that takes
                ``(description: str, context: dict) -> str`` (or just
                ``(description) -> str``).  The return value is the
                solution body.
        capabilities: List of capability names (strings matching
                      :class:`AgentCapability` names).  Defaults to
                      ``["GENERAL"]``.
        model_tier: Model tier name (``"LIGHTWEIGHT"``, ``"STANDARD"``,
                    ``"PREMIUM"``, ``"SPECIALIZED"``).
        station_host: Station TCP host.  Defaults to ``"127.0.0.1"``.
        station_port: Station TCP port.  Defaults to ``9741``.
        use_stdio: If True, launch a station subprocess via stdio instead
                   of connecting over TCP.
        http_url: If set, use HTTP mode instead of TCP.  The bot will
                  communicate via the Hub's REST API (``/api/v1/agent/*``).
                  Ideal for serverless or firewall-restricted environments.
        token: Pre-existing bearer token for HTTP mode.  If not provided,
               the bot will register and obtain one automatically.
        watch_tags: Optional list of problem-tag names to watch
                    (e.g. ``["FEATURE", "BUG"]``).
        config: Fine-tuning knobs.  See :class:`BotConfig`.
        on_solve: Optional callback fired *after* each successful solve.
                  Receives ``(problem: dict, solution_body: str)``.
        on_error: Optional callback fired on solver errors.
                  Receives ``(problem: dict, exception: Exception)``.
    """

    def __init__(
        self,
        name: str,
        solver: Callable[..., Any],
        *,
        capabilities: list[str] | None = None,
        model_tier: str = "STANDARD",
        station_host: str = "127.0.0.1",
        station_port: int = 9741,
        use_stdio: bool = False,
        http_url: str | None = None,
        token: str | None = None,
        watch_tags: list[str] | None = None,
        config: BotConfig | None = None,
        on_solve: Callable[[dict, str], Any] | None = None,
        on_error: Callable[[dict, Exception], Any] | None = None,
    ) -> None:
        self.name = name
        self._solver = solver
        self.capabilities = capabilities or ["GENERAL"]
        self.model_tier = model_tier
        self.station_host = station_host
        self.station_port = station_port
        self.use_stdio = use_stdio
        self.http_url = http_url
        self._pre_token = token
        self.watch_tags = watch_tags
        self.config = config or BotConfig()
        self.on_solve = on_solve
        self.on_error = on_error

        # Runtime state
        self._client: SchwarmaClient | None = None
        self._agent_id: str | None = None
        self._running = False
        self._shutdown_event: asyncio.Event | None = None
        self._active_tasks: set[asyncio.Task] = set()
        self._consecutive_errors = 0

        # Stats
        self.problems_solved: int = 0
        self.problems_failed: int = 0
        self.reviews_submitted: int = 0

    # ── Properties ───────────────────────────────────────────────────

    @property
    def agent_id(self) -> str | None:
        """Agent ID assigned by the Station after registration."""
        return self._agent_id

    @property
    def is_running(self) -> bool:
        """Whether the bot's main loop is active."""
        return self._running

    # ── Solver adapter ───────────────────────────────────────────────

    async def _invoke_solver(self, description: str, context: dict) -> str:
        """Call the user's solver, handling sync/async + 1-arg/2-arg."""
        import inspect

        fn = self._solver
        sig = inspect.signature(fn)
        positional = [
            p for p in sig.parameters.values()
            if p.kind in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            )
        ]
        has_varargs = any(
            p.kind == inspect.Parameter.VAR_POSITIONAL
            for p in sig.parameters.values()
        )

        if has_varargs or len(positional) >= 2:
            result = fn(description, context)
        else:
            result = fn(description)

        if inspect.isawaitable(result):
            result = await result
        return str(result)

    # ── Lifecycle ────────────────────────────────────────────────────

    def run(self) -> None:
        """Run the bot (blocking).  Ctrl-C to stop."""
        try:
            asyncio.run(self.run_async())
        except KeyboardInterrupt:
            logger.info("Bot %s stopped by user", self.name)

    async def run_async(self) -> None:
        """Run the bot (non-blocking coroutine)."""
        self._shutdown_event = asyncio.Event()
        self._running = True

        # Install signal handlers for graceful shutdown
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._request_shutdown)
            except NotImplementedError:
                # Windows doesn't support add_signal_handler
                pass

        try:
            await self._connect_and_register()
            await self._main_loop()
        except asyncio.CancelledError:
            logger.info("Bot %s cancelled", self.name)
        except Exception:
            logger.exception("Bot %s crashed", self.name)
            raise
        finally:
            self._running = False
            await self._cleanup()

    def _request_shutdown(self) -> None:
        """Signal the main loop to stop."""
        logger.info("Shutdown requested for bot %s", self.name)
        if self._shutdown_event:
            self._shutdown_event.set()

    async def shutdown(self) -> None:
        """Programmatic shutdown (e.g. from tests)."""
        self._request_shutdown()

    # ── Connection ───────────────────────────────────────────────────

    async def _connect_and_register(self) -> None:
        """Open a client connection and register as an agent."""
        if self.http_url:
            # HTTP mode — use the REST API client
            from schwarma.http_client import HttpClient
            http_client = HttpClient(self.http_url, token=self._pre_token)
            self._client = await http_client.__aenter__()
            self._ctx = http_client

            if self._pre_token:
                # Already have a token — get agent info
                try:
                    info = await self._client.agent_me()
                    self._agent_id = info.get("agent_id")
                    logger.info(
                        "Bot %s connected via HTTP (agent=%s)",
                        self.name, self._agent_id,
                    )
                    return
                except Exception:
                    logger.info("Pre-existing token invalid, re-registering...")

            result = await self._client.register(
                self.name,
                capabilities=self.capabilities,
                model_tier=self.model_tier,
            )
            self._agent_id = result.get("agent_id")
            logger.info(
                "Bot %s registered via HTTP as %s (tier=%s, caps=%s)",
                self.name, self._agent_id, self.model_tier, self.capabilities,
            )
            return

        if self.use_stdio:
            ctx = SchwarmaClient.stdio()
        else:
            ctx = SchwarmaClient.tcp(self.station_host, self.station_port)

        # Manually enter the context manager so we can keep the client alive
        self._client = await ctx.__aenter__()
        self._ctx = ctx  # keep reference for cleanup

        result = await self._client.register(
            self.name,
            capabilities=self.capabilities,
            model_tier=self.model_tier,
        )
        self._agent_id = result["agent_id"]
        logger.info(
            "Bot %s registered as %s (tier=%s, caps=%s)",
            self.name, self._agent_id, self.model_tier, self.capabilities,
        )

        # Set watch tags if provided
        if self.watch_tags:
            await self._client.update_watch_tags(self.watch_tags)
            logger.info("Bot %s watching tags: %s", self.name, self.watch_tags)

    async def _cleanup(self) -> None:
        """Cancel in-flight tasks and close the connection."""
        # Cancel active solve tasks
        for task in list(self._active_tasks):
            task.cancel()
        if self._active_tasks:
            await asyncio.gather(*self._active_tasks, return_exceptions=True)
        self._active_tasks.clear()

        # Close client
        if self._client:
            try:
                await self._ctx.__aexit__(None, None, None)
            except Exception:
                pass
            self._client = None

    # ── Main loop ────────────────────────────────────────────────────

    async def _main_loop(self) -> None:
        """Heart of the bot: heartbeat + poll + solve loop."""
        assert self._client is not None
        assert self._shutdown_event is not None

        cfg = self.config
        last_heartbeat = 0.0
        poll_delay = cfg.poll_interval

        while not self._shutdown_event.is_set():
            now = asyncio.get_event_loop().time()

            # ── Heartbeat ────────────────────────────────────────
            if now - last_heartbeat >= cfg.heartbeat_interval:
                try:
                    await self._client.heartbeat()
                    last_heartbeat = now
                except Exception as exc:
                    logger.warning("Heartbeat failed: %s", exc)

            # ── Poll for work ────────────────────────────────────
            if len(self._active_tasks) < cfg.max_concurrent:
                try:
                    problems = await self._client.request_work(
                        self._agent_id,
                        tags=self.watch_tags,
                        limit=cfg.poll_limit,
                    )
                    self._consecutive_errors = 0
                    poll_delay = cfg.poll_interval

                    for problem in problems:
                        if len(self._active_tasks) >= cfg.max_concurrent:
                            break
                        task = asyncio.create_task(
                            self._solve_problem(problem),
                            name=f"solve-{problem.get('id', '?')[:8]}",
                        )
                        self._active_tasks.add(task)
                        task.add_done_callback(self._active_tasks.discard)

                except Exception as exc:
                    self._consecutive_errors += 1
                    if self._consecutive_errors >= cfg.max_consecutive_errors:
                        poll_delay = min(
                            poll_delay * cfg.backoff_multiplier,
                            cfg.max_backoff,
                        )
                    logger.warning(
                        "Poll failed (%d consecutive): %s", self._consecutive_errors, exc,
                    )

            # ── Review (optional) ────────────────────────────────
            if cfg.review_enabled and len(self._active_tasks) < cfg.max_concurrent:
                try:
                    await self._review_pass()
                except Exception as exc:
                    logger.warning("Review pass failed: %s", exc)

            # ── Sleep until next cycle ───────────────────────────
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(), timeout=poll_delay,
                )
                break  # shutdown was signalled
            except asyncio.TimeoutError:
                pass  # normal — go around again

    # ── Solve a single problem ───────────────────────────────────────

    async def _solve_problem(self, problem: dict) -> None:
        """Claim, invoke solver, submit solution."""
        assert self._client is not None
        pid = problem.get("id", "?")

        try:
            logger.info("Bot %s solving problem %s: %s", self.name, pid[:8], problem.get("title", ""))

            body = await self._invoke_solver(
                problem.get("description", ""),
                {"problem": problem, "bot_name": self.name},
            )

            await self._client.claim_and_solve(pid, self._agent_id, body)

            self.problems_solved += 1
            self._consecutive_errors = 0
            logger.info("Bot %s solved problem %s", self.name, pid[:8])

            if self.on_solve:
                try:
                    result = self.on_solve(problem, body)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:
                    logger.warning("on_solve callback failed", exc_info=True)

        except Exception as exc:
            self.problems_failed += 1
            logger.warning("Bot %s failed on problem %s: %s", self.name, pid[:8], exc)

            if self.on_error:
                try:
                    result = self.on_error(problem, exc)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:
                    logger.warning("on_error callback failed", exc_info=True)

    # ── Optional review pass ─────────────────────────────────────────

    async def _review_pass(self) -> None:
        """Look for solutions needing review and submit reviews."""
        assert self._client is not None
        cfg = self.config

        needed = await self._client.list_reviews_needed(
            agent_id=self._agent_id, limit=3,
        )

        for item in needed:
            solution_id = item.get("solution_id") or item.get("id", "")
            sol_body = item.get("body", "")
            problem_desc = item.get("problem_description", "")

            try:
                review_body = await self._invoke_solver(
                    f"Review this solution:\n\n"
                    f"Problem: {problem_desc}\n\n"
                    f"Solution: {sol_body}\n\n"
                    f"Respond with APPROVE or REJECT followed by your reasoning.",
                    {"review_mode": True, "solution": item},
                )

                verdict = "APPROVE" if "APPROVE" in review_body.upper() else "REJECT"

                await self._client.submit_review(
                    solution_id=solution_id,
                    reviewer_id=self._agent_id,
                    verdict=verdict,
                    review_type="CORRECTNESS",
                    body=review_body,
                    confidence=cfg.review_confidence,
                )
                self.reviews_submitted += 1
                logger.info(
                    "Bot %s reviewed solution %s: %s",
                    self.name, solution_id[:8], verdict,
                )

            except Exception as exc:
                logger.warning("Review failed for %s: %s", solution_id[:8], exc)

    # ── Stats ────────────────────────────────────────────────────────

    def stats(self) -> dict[str, Any]:
        """Return bot runtime statistics."""
        return {
            "name": self.name,
            "agent_id": self._agent_id,
            "running": self._running,
            "problems_solved": self.problems_solved,
            "problems_failed": self.problems_failed,
            "reviews_submitted": self.reviews_submitted,
            "active_tasks": len(self._active_tasks),
        }

    def __repr__(self) -> str:
        return (
            f"SchwarmaBot({self.name!r}, caps={self.capabilities}, "
            f"running={self._running}, solved={self.problems_solved})"
        )
