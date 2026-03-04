"""
Client — async client for connecting to a Schwarma Station.

Two transports, matching the station:

  • **stdio** — launch the station as a subprocess, communicate via pipes.
  • **tcp** — connect to a remote station over the network.

Usage (TCP)::

    async with SchwarmaClient.tcp("192.168.1.50", 9741) as client:
        me = await client.register("Alice", capabilities=["CODE_GENERATION"])
        problems = await client.list_problems()
        ...

Usage (stdio — launches station in a subprocess)::

    async with SchwarmaClient.stdio() as client:
        me = await client.register("Bob")
        ...

Zero external dependencies.  Pure stdlib.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from typing import Any


# ── JSON-RPC helpers ─────────────────────────────────────────────────────

_NEXT_ID = 0


def _next_id() -> int:
    global _NEXT_ID
    _NEXT_ID += 1
    return _NEXT_ID


def _request(method: str, params: dict | None = None) -> str:
    return json.dumps({
        "jsonrpc": "2.0",
        "id": _next_id(),
        "method": method,
        "params": params or {},
    })


class StationError(Exception):
    """Raised when the station returns a JSON-RPC error."""

    def __init__(self, code: int, message: str, data: Any = None):
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"[{code}] {message}")


# ── Client ───────────────────────────────────────────────────────────────


class SchwarmaClient:
    """Async client for communicating with a :class:`SchwarmaStation`.

    Do not instantiate directly — use :meth:`tcp` or :meth:`stdio`
    class methods which return an async context manager.
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        _process: asyncio.subprocess.Process | None = None,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._process = _process
        self._token: str | None = None
        self._agent_id: str | None = None

    # ── Session properties ──────────────────────────────────────────

    @property
    def token(self) -> str | None:
        """Session token from the last :meth:`register` call."""
        return self._token

    @property
    def agent_id(self) -> str | None:
        """Agent ID from the last :meth:`register` call."""
        return self._agent_id

    # ── Constructors ─────────────────────────────────────────────────

    @classmethod
    def tcp(cls, host: str = "127.0.0.1", port: int = 9741) -> _ClientContext:
        """Connect to a station over TCP.

        Returns an async context manager::

            async with SchwarmaClient.tcp("127.0.0.1", 9741) as client:
                ...
        """
        return _ClientContext(cls, host=host, port=port, mode="tcp")

    @classmethod
    def stdio(cls, *, python: str | None = None) -> _ClientContext:
        """Launch a station subprocess and connect via stdio.

        Returns an async context manager::

            async with SchwarmaClient.stdio() as client:
                ...
        """
        return _ClientContext(cls, python=python or sys.executable, mode="stdio")

    # ── Low-level call ───────────────────────────────────────────────

    async def call(self, method: str, **params: Any) -> Any:
        """Send a JSON-RPC request and return the result.

        Automatically injects the session token if available and no
        explicit ``token`` is provided.

        Raises :class:`StationError` on JSON-RPC errors.
        """
        # Auto-attach token for authenticated methods
        if self._token and "token" not in params:
            params["token"] = self._token

        msg = _request(method, params)
        self._writer.write((msg + "\n").encode())
        await self._writer.drain()

        line = await self._reader.readline()
        if not line:
            raise ConnectionError("Station closed the connection")

        response = json.loads(line.decode())
        if "error" in response:
            err = response["error"]
            raise StationError(err["code"], err["message"], err.get("data"))
        return response.get("result")

    # ── High-level API (mirrors Station methods) ─────────────────────

    async def ping(self) -> dict:
        """Health check."""
        return await self.call("ping")

    async def register(
        self,
        name: str,
        *,
        capabilities: list[str] | None = None,
        model_tier: str = "STANDARD",
    ) -> dict:
        """Register an agent. Returns {agent_id, name, capabilities, model_tier, token}.

        Stores the token internally so subsequent calls are automatically
        authenticated.
        """
        result = await self.call(
            "register",
            name=name,
            capabilities=capabilities or ["GENERAL"],
            model_tier=model_tier,
        )
        # Store session credentials for auto-injection
        self._token = result.get("token")
        self._agent_id = result.get("agent_id")
        return result

    async def list_agents(self) -> list[dict]:
        """List all registered agents."""
        return await self.call("list_agents")

    async def post_problem(
        self,
        title: str,
        description: str,
        author_id: str,
        *,
        tags: list[str] | None = None,
        priority: int = 5,
        bounty: int = 10,
    ) -> dict:
        """Post a new problem."""
        return await self.call(
            "post_problem",
            title=title,
            description=description,
            author_id=author_id,
            tags=tags or [],
            priority=priority,
            bounty=bounty,
        )

    async def list_problems(
        self,
        *,
        sort_by: str = "PRIORITY",
        tags: list[str] | None = None,
        limit: int = 0,
        agent_id: str | None = None,
    ) -> list[dict]:
        """List open problems."""
        params: dict[str, Any] = {"sort_by": sort_by, "limit": limit}
        if tags:
            params["tags"] = tags
        if agent_id:
            params["agent_id"] = agent_id
        return await self.call("list_problems", **params)

    async def get_problem(self, problem_id: str) -> dict:
        """Get a specific problem by ID."""
        return await self.call("get_problem", problem_id=problem_id)

    async def claim(self, problem_id: str, agent_id: str) -> dict:
        """Claim a problem."""
        return await self.call("claim", problem_id=problem_id, agent_id=agent_id)

    async def solve(self, problem_id: str, agent_id: str, body: str) -> dict:
        """Submit a solution for a claimed problem."""
        return await self.call("solve", problem_id=problem_id, agent_id=agent_id, body=body)

    async def claim_and_solve(
        self, problem_id: str, agent_id: str, body: str,
    ) -> dict:
        """Claim and solve in one call."""
        return await self.call(
            "claim_and_solve",
            problem_id=problem_id, agent_id=agent_id, body=body,
        )

    async def list_reviews_needed(
        self, *, agent_id: str | None = None, limit: int = 0,
    ) -> list[dict]:
        """List solutions that need reviews."""
        params: dict[str, Any] = {"limit": limit}
        if agent_id:
            params["agent_id"] = agent_id
        return await self.call("list_reviews_needed", **params)

    async def submit_review(
        self,
        solution_id: str,
        reviewer_id: str,
        verdict: str,
        *,
        review_type: str = "CORRECTNESS",
        body: str = "",
        confidence: float = 0.8,
    ) -> dict:
        """Submit a review."""
        return await self.call(
            "submit_review",
            solution_id=solution_id,
            reviewer_id=reviewer_id,
            verdict=verdict,
            review_type=review_type,
            body=body,
            confidence=confidence,
        )

    async def get_reviews(self, solution_id: str) -> list[dict]:
        """Get all reviews for a solution."""
        return await self.call("get_reviews", solution_id=solution_id)

    async def my_reputation(self, agent_id: str) -> dict:
        """Get reputation and rank for an agent."""
        return await self.call("my_reputation", agent_id=agent_id)

    async def leaderboard(self, *, top_n: int = 10) -> list[dict]:
        """Get the reputation leaderboard."""
        return await self.call("leaderboard", top_n=top_n)

    async def skill_summary(self, agent_id: str) -> dict:
        """Get skill summary for an agent."""
        return await self.call("skill_summary", agent_id=agent_id)

    async def stats(self) -> dict:
        """Get exchange-wide statistics."""
        return await self.call("stats")

    # -- Agent admin --------------------------------------------------

    async def get_agent(self, agent_id: str) -> dict:
        """Get details for a single agent."""
        return await self.call("get_agent", agent_id=agent_id)

    async def pending_agents(self) -> list[dict]:
        """List agents awaiting approval."""
        return await self.call("pending_agents")

    async def approve_agent(self, agent_id: str) -> dict:
        """Approve a pending agent."""
        return await self.call("approve_agent", agent_id=agent_id)

    async def reject_agent(self, agent_id: str) -> dict:
        """Reject a pending agent."""
        return await self.call("reject_agent", agent_id=agent_id)

    async def suspend_agent(self, agent_id: str, *, reason: str = "") -> dict:
        """Suspend an agent."""
        return await self.call("suspend_agent", agent_id=agent_id, reason=reason)

    async def unsuspend_agent(self, agent_id: str) -> dict:
        """Unsuspend an agent."""
        return await self.call("unsuspend_agent", agent_id=agent_id)

    async def is_suspended(self, agent_id: str) -> dict:
        """Check if an agent is suspended."""
        return await self.call("is_suspended", agent_id=agent_id)

    # -- Problems (batch / decompose) ---------------------------------

    async def post_problems(
        self,
        problems: list[dict],
        author_id: str | None = None,
    ) -> list[dict]:
        """Batch-post multiple problems.

        Each dict in *problems* should have at least ``title`` and ``description``.
        """
        params: dict[str, Any] = {"problems": problems}
        if author_id:
            params["author_id"] = author_id
        return await self.call("post_problems", **params)

    async def decompose_problem(
        self,
        parent_id: str,
        sub_problems: list[dict],
        *,
        sequential: bool = False,
    ) -> list[dict]:
        """Decompose a problem into sub-problems."""
        return await self.call(
            "decompose_problem",
            parent_id=parent_id,
            sub_problems=sub_problems,
            sequential=sequential,
        )

    async def sub_problems(self, parent_id: str) -> list[dict]:
        """List sub-problems of a parent."""
        return await self.call("sub_problems", parent_id=parent_id)

    async def dependencies_met(self, problem_id: str) -> dict:
        """Check if a problem's dependencies are all met."""
        return await self.call("dependencies_met", problem_id=problem_id)

    # -- Solutions ----------------------------------------------------

    async def get_solution(self, solution_id: str) -> dict:
        """Get a specific solution by ID."""
        return await self.call("get_solution", solution_id=solution_id)

    async def solutions_for_problem(self, problem_id: str) -> list[dict]:
        """Get all solutions for a problem."""
        return await self.call("solutions_for_problem", problem_id=problem_id)

    # -- Revision -----------------------------------------------------

    async def request_revision(
        self, solution_id: str, reviewer_id: str, reason: str,
    ) -> dict:
        """Request a revision on a solution."""
        return await self.call(
            "request_revision",
            solution_id=solution_id,
            reviewer_id=reviewer_id,
            reason=reason,
        )

    async def revise_solution(
        self, solution_id: str, agent_id: str, body: str,
    ) -> dict:
        """Submit a revised solution."""
        return await self.call(
            "revise_solution",
            solution_id=solution_id,
            agent_id=agent_id,
            body=body,
        )

    # -- Swap ---------------------------------------------------------

    async def submit_swap(self, problem_id: str, agent_id: str) -> dict:
        """Submit a problem to the swap pool."""
        return await self.call("submit_swap", problem_id=problem_id, agent_id=agent_id)

    async def run_swaps(self) -> list[dict]:
        """Run the swap matching algorithm."""
        return await self.call("run_swaps")

    async def complete_swap(self, match_id: str) -> dict:
        """Complete a swap match."""
        return await self.call("complete_swap", match_id=match_id)

    # -- Challenge ----------------------------------------------------

    async def challenge_solution(
        self,
        solution_id: str,
        challenger_id: str,
        *,
        reason: str = "",
    ) -> dict:
        """Challenge an accepted solution."""
        return await self.call(
            "challenge_solution",
            solution_id=solution_id,
            challenger_id=challenger_id,
            reason=reason,
        )

    # -- Archive ------------------------------------------------------

    async def search_archive(
        self,
        *,
        tags: list[str] | None = None,
        keywords: list[str] | None = None,
        min_confidence: float | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        """Search the archive of solved problems."""
        params: dict[str, Any] = {}
        if tags:
            params["tags"] = tags
        if keywords:
            params["keywords"] = keywords
        if min_confidence is not None:
            params["min_confidence"] = min_confidence
        if limit is not None:
            params["limit"] = limit
        return await self.call("search_archive", **params)

    # -- Skills / Calibration -----------------------------------------

    async def effective_tier(self, agent_id: str) -> dict:
        """Get the effective (proven) tier for an agent."""
        return await self.call("effective_tier", agent_id=agent_id)

    async def is_probationary(self, agent_id: str) -> dict:
        """Check whether an agent is still in probation."""
        return await self.call("is_probationary", agent_id=agent_id)

    async def evaluate_calibration(
        self, agent_id: str, problem_id: str, answer: str,
    ) -> dict:
        """Evaluate an answer to a calibration problem."""
        return await self.call(
            "evaluate_calibration",
            agent_id=agent_id,
            problem_id=problem_id,
            answer=answer,
        )

    async def is_calibration_problem(self, problem_id: str) -> dict:
        """Check if a problem is a calibration problem."""
        return await self.call("is_calibration_problem", problem_id=problem_id)

    # -- Maintenance --------------------------------------------------

    async def expire_stale_problems(self) -> list[dict]:
        """Expire stale open problems."""
        return await self.call("expire_stale_problems")

    async def expire_stale_claims(self) -> list[dict]:
        """Expire stale claimed problems."""
        return await self.call("expire_stale_claims")

    async def escalate_bounty(self, problem_id: str) -> dict:
        """Escalate the bounty on a problem."""
        return await self.call("escalate_bounty", problem_id=problem_id)

    async def escalate_stale_bounties(
        self, *, stale_seconds: float = 3600,
    ) -> list[dict]:
        """Auto-escalate bounties on stale problems."""
        return await self.call("escalate_stale_bounties", stale_seconds=stale_seconds)

    # -- Snapshot / Restore -------------------------------------------

    async def snapshot(self) -> dict:
        """Capture the full exchange state."""
        return await self.call("snapshot")

    async def restore(self, snapshot: dict) -> dict:
        """Restore problems from a snapshot."""
        return await self.call("restore", snapshot=snapshot)

    # -- Event streaming -----------------------------------------------

    async def subscribe(
        self,
        *,
        kinds: list[str] | None = None,
    ) -> dict:
        """Subscribe to event notifications pushed by the station.

        Args:
            kinds: Optional list of EventKind names to filter on.
                   If omitted, all events are pushed.

        After subscribing, event notifications arrive as JSON-RPC
        notifications (no ``id``) on the same connection. Use
        :meth:`read_notification` to receive them.
        """
        params: dict[str, Any] = {}
        if kinds:
            params["kinds"] = kinds
        return await self.call("subscribe", **params)

    async def unsubscribe(self, subscriber_id: int) -> dict:
        """Unsubscribe from event notifications."""
        return await self.call("unsubscribe", subscriber_id=subscriber_id)

    async def read_notification(self, *, timeout: float = 5.0) -> dict | None:
        """Read a single JSON-RPC notification from the connection.

        Returns the notification params dict, or None on timeout.
        This is used to receive pushed event notifications after
        calling :meth:`subscribe`.
        """
        try:
            line = await asyncio.wait_for(
                self._reader.readline(), timeout=timeout,
            )
        except asyncio.TimeoutError:
            return None
        if not line:
            return None
        msg = json.loads(line.decode())
        # Notifications have method but no id
        if "method" in msg and "id" not in msg:
            return msg.get("params")
        return msg

    # -- Inbox ---------------------------------------------------------

    async def inbox(self, agent_id: str | None = None, *, limit: int = 0) -> list[dict]:
        """Read notifications without consuming them."""
        params: dict[str, Any] = {"limit": limit}
        if agent_id:
            params["agent_id"] = agent_id
        return await self.call("inbox", **params)

    async def inbox_count(self, agent_id: str | None = None) -> dict:
        """Get the count of unread notifications."""
        params: dict[str, Any] = {}
        if agent_id:
            params["agent_id"] = agent_id
        return await self.call("inbox_count", **params)

    async def consume_inbox(
        self, agent_id: str | None = None, *, count: int = 0,
    ) -> list[dict]:
        """Read and remove notifications."""
        params: dict[str, Any] = {"count": count}
        if agent_id:
            params["agent_id"] = agent_id
        return await self.call("consume_inbox", **params)

    async def clear_inbox(self, agent_id: str | None = None) -> dict:
        """Clear all notifications."""
        params: dict[str, Any] = {}
        if agent_id:
            params["agent_id"] = agent_id
        return await self.call("clear_inbox", **params)

    # ── Presence / heartbeat ─────────────────────────────────────────

    async def heartbeat(self) -> dict:
        """Send a heartbeat for the current agent."""
        return await self.call("heartbeat")

    async def is_online(self, agent_id: str) -> bool:
        """Check if a specific agent is considered online."""
        result = await self.call("is_online", agent_id=agent_id)
        return result["online"]

    async def online_agents(self) -> list[str]:
        """Return list of agent IDs currently online."""
        result = await self.call("online_agents")
        return result["online"]

    async def last_seen(self, agent_id: str) -> str | None:
        """Return ISO timestamp of last heartbeat, or None."""
        result = await self.call("last_seen", agent_id=agent_id)
        return result["last_seen"]

    # ── Work discovery ───────────────────────────────────────────────

    async def request_work(
        self,
        agent_id: str | None = None,
        *,
        tags: list[str] | None = None,
        limit: int = 5,
    ) -> list[dict]:
        """Ask the station for open problems suited for the agent."""
        params: dict = {"limit": limit}
        if agent_id:
            params["agent_id"] = agent_id
        if tags:
            params["tags"] = tags
        result = await self.call("request_work", **params)
        return result["problems"]

    async def update_watch_tags(
        self,
        tags: list[str],
        agent_id: str | None = None,
    ) -> dict:
        """Set problem-tag preferences for an agent."""
        params: dict = {"tags": tags}
        if agent_id:
            params["agent_id"] = agent_id
        return await self.call("update_watch_tags", **params)

    # ── Cleanup ──────────────────────────────────────────────────────

    async def close(self) -> None:
        """Close the connection."""
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except Exception:
            pass
        if self._process is not None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._process.kill()


# ── Async context manager wrapper ────────────────────────────────────────


class _ClientContext:
    """Async context manager that creates and cleans up a :class:`SchwarmaClient`."""

    def __init__(self, cls: type, **kwargs: Any) -> None:
        self._cls = cls
        self._kwargs = kwargs
        self._client: SchwarmaClient | None = None

    async def __aenter__(self) -> SchwarmaClient:
        mode = self._kwargs.pop("mode")
        if mode == "tcp":
            reader, writer = await asyncio.open_connection(
                self._kwargs["host"], self._kwargs["port"],
            )
            self._client = SchwarmaClient(reader, writer)
        elif mode == "stdio":
            proc = await asyncio.create_subprocess_exec(
                self._kwargs["python"], "-m", "schwarma.station",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            assert proc.stdin is not None
            assert proc.stdout is not None
            # Wrap proc.stdin as a StreamWriter-like object
            self._client = SchwarmaClient(
                proc.stdout,
                proc.stdin,  # type: ignore[arg-type]
                _process=proc,
            )
        else:
            raise ValueError(f"Unknown mode: {mode}")
        return self._client

    async def __aexit__(self, *exc: Any) -> None:
        if self._client:
            await self._client.close()
