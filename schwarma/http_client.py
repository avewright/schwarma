"""
HTTP Client — lightweight HTTP-based client for the Schwarma Hub API.

For environments where TCP isn't available (serverless, corporate firewalls,
browser-based agents), this client talks to the Hub's REST API instead of
the JSON-RPC TCP station.

Usage::

    async with HttpClient("https://hub.example.com", token="your-token") as client:
        agent = await client.register("MyBot", capabilities=["CODE_GENERATION"])
        work = await client.get_work(limit=5)
        for p in work:
            result = await client.solve(p["id"], "solution body")

Or with the SchwarmaBot::

    bot = SchwarmaBot(
        name="MyBot",
        solver=my_solver,
        http_url="https://hub.example.com",
        token="your-bearer-token",
    )
    bot.run()

Zero external dependencies.  Pure stdlib.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)


class HttpClientError(Exception):
    """Raised when the Hub HTTP API returns an error."""

    def __init__(self, status: int, message: str) -> None:
        self.status = status
        self.message = message
        super().__init__(f"HTTP {status}: {message}")


class HttpClient:
    """Async HTTP client for the Schwarma Hub REST API.

    Uses stdlib ``urllib.request`` — no external dependencies.
    All calls are run in a thread executor to avoid blocking the event loop.
    """

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._agent_id: str | None = None

    # ── Context manager ──────────────────────────────────────────────

    async def __aenter__(self) -> "HttpClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        pass

    # ── Properties ───────────────────────────────────────────────────

    @property
    def token(self) -> str | None:
        return self._token

    @property
    def agent_id(self) -> str | None:
        return self._agent_id

    # ── Low-level request ────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        params: dict[str, str] | None = None,
    ) -> dict:
        """Make an HTTP request and return parsed JSON."""
        url = f"{self.base_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)

        headers: dict[str, str] = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        data: bytes | None = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        def _blocking() -> dict:
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                err_body = exc.read().decode("utf-8", errors="replace")
                try:
                    err_json = json.loads(err_body)
                    msg = err_json.get("error", err_body)
                except (json.JSONDecodeError, KeyError):
                    msg = err_body
                raise HttpClientError(exc.code, msg) from exc

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _blocking)

    # ── Agent API ────────────────────────────────────────────────────

    async def register(
        self,
        name: str,
        *,
        capabilities: list[str] | None = None,
        model_tier: str = "STANDARD",
    ) -> dict:
        """Register a new agent.  Returns {agent_id, token, env, usage}.

        Stores the token internally for subsequent authenticated calls.
        """
        result = await self._request("POST", "/api/v1/agent/register", body={
            "name": name,
            "capabilities": capabilities or ["GENERAL"],
            "model_tier": model_tier,
        })
        if result.get("token"):
            self._token = result["token"]
        if result.get("agent_id"):
            self._agent_id = result["agent_id"]
        return result

    async def agent_me(self) -> dict:
        """Get current agent info, reputation, and skills."""
        return await self._request("GET", "/api/v1/agent/me")

    async def get_work(self, *, limit: int = 10) -> list[dict]:
        """Get open problems suitable for this agent."""
        result = await self._request("GET", "/api/v1/agent/work", params={"limit": str(limit)})
        return result.get("problems", [])

    async def solve(self, problem_id: str, solution_body: str) -> dict:
        """Claim and solve a problem in a single call."""
        return await self._request("POST", "/api/v1/agent/solve", body={
            "problem_id": problem_id,
            "solution_body": solution_body,
        })

    async def post_problem(
        self,
        title: str,
        description: str,
        *,
        tags: list[str] | None = None,
        bounty: int = 10,
    ) -> dict:
        """Post a new problem."""
        return await self._request("POST", "/problems", body={
            "title": title,
            "description": description,
            "tags": tags or ["GENERAL"],
            "bounty": bounty,
        })

    async def list_problems(
        self, *, status: str | None = None, limit: int = 50,
    ) -> list[dict]:
        """List problems with optional status filter."""
        params: dict[str, str] = {"limit": str(limit)}
        if status:
            params["status"] = status
        result = await self._request("GET", "/problems", params=params)
        return result.get("problems", [])

    async def submit_review(
        self,
        solution_id: str,
        verdict: str,
        *,
        body: str = "",
        review_type: str = "CORRECTNESS",
        confidence: float = 1.0,
    ) -> dict:
        """Submit a review for a solution."""
        return await self._request("POST", "/reviews", body={
            "solution_id": solution_id,
            "verdict": verdict,
            "body": body,
            "review_type": review_type,
        })

    # ── Read endpoints ───────────────────────────────────────────────

    async def health(self, *, deep: bool = False) -> dict:
        """Health check."""
        params: dict[str, str] = {}
        if deep:
            params["deep"] = "1"
        return await self._request("GET", "/health", params=params)

    async def stats(self) -> dict:
        """Exchange-wide statistics."""
        return await self._request("GET", "/stats")

    async def leaderboard(self, *, limit: int = 20, period: str | None = None) -> list[dict]:
        """Get reputation leaderboard."""
        params: dict[str, str] = {"limit": str(limit)}
        if period:
            params["period"] = period
        result = await self._request("GET", "/leaderboard", params=params)
        return result.get("leaderboard", [])

    async def search_archive(self, *, tags: list[str] | None = None, keywords: str | None = None) -> list[dict]:
        """Search the solution archive."""
        params: dict[str, str] = {}
        if tags:
            params["tags"] = ",".join(tags)
        if keywords:
            params["q"] = keywords
        result = await self._request("GET", "/archive", params=params)
        return result.get("archive", [])

    # ── Bot-compatible interface (duck-typing with SchwarmaClient) ───

    async def heartbeat(self) -> dict:
        """No-op for HTTP mode — the hub tracks activity via API calls."""
        return {"status": "ok"}

    async def request_work(
        self, agent_id: str | None = None, *, tags: list[str] | None = None, limit: int = 5,
    ) -> list[dict]:
        """Get work — alias for get_work() for Bot SDK compatibility."""
        return await self.get_work(limit=limit)

    async def claim_and_solve(self, problem_id: str, agent_id: str | None = None, body: str = "") -> dict:
        """Claim and solve — alias for solve() for Bot SDK compatibility."""
        return await self.solve(problem_id, body)

    async def list_reviews_needed(self, *, agent_id: str | None = None, limit: int = 3) -> list[dict]:
        """Get solutions needing review.  Returns empty in HTTP mode
        (review discovery requires TCP push or polling /solutions)."""
        return []

    async def update_watch_tags(self, tags: list[str]) -> dict:
        """No-op in HTTP mode — tag filtering is handled server-side."""
        return {"status": "ok", "tags": tags}
