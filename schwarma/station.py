"""
Station — JSON-RPC 2.0 server wrapping a Schwarma Exchange.

The Station is the network entry-point that lets **external** agents
(different processes, machines, or continents) participate in the same
Exchange.  It speaks newline-delimited JSON-RPC over two transports:

  • **stdio** — stdin/stdout, sub-millisecond, same machine.
    Ideal for MCP-style tool integrations.
  • **tcp** — raw TCP socket, LAN or internet.
    Ideal for distributed multi-user collaboration.

Usage (stdio)::

    station = SchwarmaStation()
    asyncio.run(station.serve_stdio())

Usage (tcp)::

    station = SchwarmaStation()
    asyncio.run(station.serve_tcp("0.0.0.0", 9741))

Agents connect and issue JSON-RPC calls::

    --> {"jsonrpc":"2.0","id":1,"method":"register","params":{"name":"Alice","capabilities":["CODE_GENERATION"]}}
    <-- {"jsonrpc":"2.0","id":1,"result":{"agent_id":"..."}}

Zero external dependencies.  Pure stdlib.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine
from uuid import UUID

from schwarma.agent import Agent, AgentCapability, ModelTier
from schwarma.events import Event, EventKind
from schwarma.exchange import Exchange, ExchangeConfig, ProblemSortKey
from schwarma.problem import Problem, ProblemTag
from schwarma.review import Review, ReviewType, ReviewVerdict
from schwarma.scheduler import Scheduler, SchedulerConfig
from schwarma.solution import SolutionVerdict

logger = logging.getLogger(__name__)

# ── JSON-RPC 2.0 constants ──────────────────────────────────────────────

JSONRPC_VERSION = "2.0"
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
APPLICATION_ERROR = -32000  # Schwarma-specific errors
AUTH_REQUIRED = -32001  # Token missing or invalid


# ── Helpers ──────────────────────────────────────────────────────────────


class _AuthError(Exception):
    """Raised when token authentication fails."""
    pass


def _ok(id: Any, result: Any) -> dict:
    return {"jsonrpc": JSONRPC_VERSION, "id": id, "result": result}


def _err(id: Any, code: int, message: str, data: Any = None) -> dict:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": JSONRPC_VERSION, "id": id, "error": error}


def _uuid(value: str) -> UUID:
    """Parse a UUID string, raising ValueError on bad input."""
    return UUID(value)


def _serialize(obj: Any) -> Any:
    """Recursively make an object JSON-safe."""
    if isinstance(obj, UUID):
        return str(obj)
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_serialize(v) for v in obj]
    if hasattr(obj, "to_dict"):
        return _serialize(obj.to_dict())
    if hasattr(obj, "name") and hasattr(obj, "value"):
        # Enum
        return obj.name
    return obj


# A no-op solver for externally-driven agents.
async def _external_solver(desc: str, ctx: dict) -> str:  # pragma: no cover
    raise RuntimeError(
        "This agent is externally driven — use the 'solve' station method "
        "to push a solution body."
    )


# ── Station ──────────────────────────────────────────────────────────────


class SchwarmaStation:
    """JSON-RPC 2.0 server wrapping a :class:`Exchange`.

    Exposes every meaningful Exchange operation as a callable RPC method.
    Transport-agnostic: call :meth:`handle` with a raw JSON string and
    get a JSON string back.  Or use :meth:`serve_stdio` /
    :meth:`serve_tcp` for built-in transports.
    """

    def __init__(
        self,
        exchange: Exchange | None = None,
        config: ExchangeConfig | None = None,
        *,
        require_auth: bool = True,
        scheduler_config: SchedulerConfig | None = None,
    ) -> None:
        self.exchange = exchange or Exchange(config or ExchangeConfig())
        self.require_auth = require_auth
        self._sessions: dict[str, UUID] = {}  # token → agent_id
        self._methods: dict[str, Callable[..., Coroutine]] = {}
        # Event streaming: maps writer id → (writer, subscribed event kinds | None for all)
        self._subscribers: dict[int, tuple[asyncio.StreamWriter, set[EventKind] | None]] = {}
        # Background scheduler for periodic maintenance
        self.scheduler = Scheduler(self.exchange, scheduler_config)
        self._register_methods()
        # Wire up event bus → push notifications to subscribers
        self.exchange.bus.subscribe_all(self._broadcast_event)

    # ── Method registry ──────────────────────────────────────────────

    def _register_methods(self) -> None:
        """Map JSON-RPC method names → async handler functions."""
        self._methods = {
            # Agent management
            "register":              self._m_register,
            "list_agents":           self._m_list_agents,
            "get_agent":             self._m_get_agent,
            "pending_agents":        self._m_pending_agents,
            "approve_agent":         self._m_approve_agent,
            "reject_agent":          self._m_reject_agent,
            "suspend_agent":         self._m_suspend_agent,
            "unsuspend_agent":       self._m_unsuspend_agent,
            "is_suspended":          self._m_is_suspended,
            # Problems
            "post_problem":          self._m_post_problem,
            "post_problems":         self._m_post_problems,
            "list_problems":         self._m_list_problems,
            "get_problem":           self._m_get_problem,
            "decompose_problem":     self._m_decompose_problem,
            "sub_problems":          self._m_sub_problems,
            "dependencies_met":      self._m_dependencies_met,
            # Claim / Solve
            "claim":                 self._m_claim,
            "solve":                 self._m_solve,
            "claim_and_solve":       self._m_claim_and_solve,
            "get_solution":          self._m_get_solution,
            "solutions_for_problem": self._m_solutions_for_problem,
            # Reviews
            "list_reviews_needed":   self._m_list_reviews_needed,
            "submit_review":         self._m_submit_review,
            "get_reviews":           self._m_get_reviews,
            # Revision
            "request_revision":      self._m_request_revision,
            "revise_solution":       self._m_revise_solution,
            # Swap
            "submit_swap":           self._m_submit_swap,
            "run_swaps":             self._m_run_swaps,
            "complete_swap":         self._m_complete_swap,
            # Challenge
            "challenge_solution":    self._m_challenge_solution,
            # Archive
            "search_archive":        self._m_search_archive,
            # Reputation / Skills
            "my_reputation":         self._m_my_reputation,
            "leaderboard":           self._m_leaderboard,
            "skill_summary":         self._m_skill_summary,
            "effective_tier":        self._m_effective_tier,
            "is_probationary":       self._m_is_probationary,
            # Calibration
            "evaluate_calibration":  self._m_evaluate_calibration,
            "is_calibration_problem": self._m_is_calibration_problem,
            # Maintenance
            "expire_stale_problems": self._m_expire_stale_problems,
            "expire_stale_claims":   self._m_expire_stale_claims,
            "escalate_bounty":       self._m_escalate_bounty,
            "escalate_stale_bounties": self._m_escalate_stale_bounties,
            # Snapshot / Restore
            "snapshot":              self._m_snapshot,
            "restore":               self._m_restore,
            # Event streaming
            "subscribe":             self._m_subscribe,
            "unsubscribe":           self._m_unsubscribe,
            # Inbox
            "inbox":                 self._m_inbox,
            "inbox_count":           self._m_inbox_count,
            "consume_inbox":         self._m_consume_inbox,
            "clear_inbox":           self._m_clear_inbox,
            # Presence / heartbeat
            "heartbeat":             self._m_heartbeat,
            "is_online":             self._m_is_online,
            "online_agents":         self._m_online_agents,
            "last_seen":             self._m_last_seen,
            # Meta
            "ping":                  self._m_ping,
            "stats":                 self._m_stats,
        }

    # ── Auth helpers ─────────────────────────────────────────────────

    def _resolve_agent(self, params: dict) -> UUID:
        """Extract and validate agent identity from params.

        When ``require_auth`` is True, a ``token`` param is mandatory
        and must match the ``agent_id`` (or the ``agent_id`` is inferred
        from the token when omitted).

        Returns the verified agent UUID.
        """
        token = params.get("token")

        if self.require_auth:
            if not token:
                raise _AuthError("'token' is required — use the token from register")
            agent_id = self._sessions.get(token)
            if agent_id is None:
                raise _AuthError("Invalid or expired token")
            # If caller also provided agent_id, make sure it matches
            explicit_id = (
                params.get("agent_id") or params.get("author_id")
                or params.get("reviewer_id") or params.get("challenger_id")
            )
            if explicit_id and _uuid(explicit_id) != agent_id:
                raise _AuthError("Token does not match the provided agent_id")
            return agent_id

        # require_auth=False: fall back to explicit agent_id
        if token:
            agent_id = self._sessions.get(token)
            if agent_id is not None:
                return agent_id
        # Try explicit id fields
        for key in ("agent_id", "author_id", "reviewer_id", "challenger_id"):
            if key in params:
                return _uuid(params[key])
        raise ValueError("One of 'token', 'agent_id', 'author_id', or 'reviewer_id' is required")

    # ── Core dispatch ────────────────────────────────────────────────

    async def handle(self, raw: str, *, _writer: asyncio.StreamWriter | None = None) -> str:
        """Process a single JSON-RPC request and return the response string.

        This is the transport-agnostic core.  Feed it a raw JSON line,
        get a JSON line back.

        When called from the TCP transport, ``_writer`` is injected so
        that ``subscribe`` can register the connection for event push.
        """
        try:
            request = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return json.dumps(_err(None, PARSE_ERROR, "Parse error"))

        req_id = request.get("id")

        # Validate structure
        if request.get("jsonrpc") != JSONRPC_VERSION:
            return json.dumps(_err(req_id, INVALID_REQUEST, "Missing or bad jsonrpc version"))
        method_name = request.get("method")
        if not isinstance(method_name, str):
            return json.dumps(_err(req_id, INVALID_REQUEST, "Missing method"))
        params = request.get("params", {})
        if not isinstance(params, dict):
            return json.dumps(_err(req_id, INVALID_PARAMS, "params must be an object"))

        # Inject writer for subscribe method
        if _writer is not None:
            params["_writer"] = _writer

        handler = self._methods.get(method_name)
        if handler is None:
            return json.dumps(_err(req_id, METHOD_NOT_FOUND, f"Unknown method: {method_name}"))

        try:
            result = await handler(params)
            return json.dumps(_ok(req_id, _serialize(result)))
        except _AuthError as exc:
            return json.dumps(_err(req_id, AUTH_REQUIRED, str(exc)))
        except (KeyError, ValueError) as exc:
            return json.dumps(_err(req_id, INVALID_PARAMS, str(exc)))
        except Exception as exc:
            logger.exception("Unhandled error in method %s", method_name)
            return json.dumps(_err(req_id, APPLICATION_ERROR, str(exc)))

    # ── Transport: stdio ─────────────────────────────────────────────

    async def serve_stdio(self) -> None:
        """Read JSON-RPC requests from stdin, write responses to stdout.

        One request per line, newline-delimited.  Runs until EOF.
        """
        while True:
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            response = await self.handle(line)
            sys.stdout.write(response + "\n")
            sys.stdout.flush()

    # ── Transport: TCP ───────────────────────────────────────────────

    async def serve_tcp(
        self,
        host: str = "127.0.0.1",
        port: int = 9741,
    ) -> None:
        """Listen on a TCP socket, newline-delimited JSON-RPC.

        Each connected client gets its own read loop.  All clients
        share the same Exchange (that's the point).
        """
        async def _handle_client(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            addr = writer.get_extra_info("peername")
            logger.info("Client connected: %s", addr)
            sub_id = id(writer)
            try:
                while True:
                    data = await reader.readline()
                    if not data:
                        break
                    response = await self.handle(
                        data.decode().strip(), _writer=writer,
                    )
                    writer.write((response + "\n").encode())
                    await writer.drain()
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Error handling client %s", addr)
            finally:
                self.remove_subscriber(sub_id)
                writer.close()
                await writer.wait_closed()
                logger.info("Client disconnected: %s", addr)

        server = await asyncio.start_server(_handle_client, host, port)
        addrs = ", ".join(str(s.getsockname()) for s in server.sockets)
        logger.info("Schwarma Station listening on %s", addrs)
        print(f"Schwarma Station listening on {addrs}", file=sys.stderr)
        async with server:
            await self.scheduler.start()
            try:
                await server.serve_forever()
            finally:
                await self.scheduler.stop()

    # ── RPC method implementations ───────────────────────────────────

    async def _m_ping(self, params: dict) -> dict:
        """Health check."""
        return {"pong": True, "agents": len(self.exchange._agents)}

    async def _m_stats(self, params: dict) -> dict:
        """Exchange-wide statistics."""
        return self.exchange.statistics()

    # -- Agents -------------------------------------------------------

    async def _m_register(self, params: dict) -> dict:
        """Register an agent.

        params: {name, capabilities?: [str], model_tier?: str, metadata?: dict}
        """
        name = params.get("name")
        if not name:
            raise ValueError("'name' is required")

        caps_raw = params.get("capabilities", ["GENERAL"])
        capabilities = set()
        for c in caps_raw:
            try:
                capabilities.add(AgentCapability[c])
            except KeyError:
                raise ValueError(f"Unknown capability: {c}")

        tier_raw = params.get("model_tier", "STANDARD")
        try:
            model_tier = ModelTier[tier_raw]
        except KeyError:
            raise ValueError(f"Unknown model_tier: {tier_raw}")

        agent = Agent(
            name=name,
            solver=_external_solver,
            capabilities=capabilities,
            model_tier=model_tier,
            metadata=params.get("metadata", {}),
        )
        self.exchange.register(agent)

        # Issue session token
        token = secrets.token_urlsafe(32)
        self._sessions[token] = agent.id

        return {
            "agent_id": str(agent.id),
            "name": agent.name,
            "capabilities": [c.name for c in agent.capabilities],
            "model_tier": agent.model_tier.name,
            "token": token,
        }

    async def _m_list_agents(self, params: dict) -> list:
        """List registered agents."""
        agents = list(self.exchange._agents.values())
        return [
            {
                "agent_id": str(a.id),
                "name": a.name,
                "capabilities": [c.name for c in a.capabilities],
                "model_tier": a.model_tier.name,
                "active_claims": a.active_count,
                "reputation": self.exchange.ledger.balance(a.id),
            }
            for a in agents
        ]

    # -- Problems -----------------------------------------------------

    async def _m_post_problem(self, params: dict) -> dict:
        """Post a new problem.

        params: {title, description, author_id|token, tags?: [str], priority?: int, bounty?: int}
        """
        title = params.get("title")
        description = params.get("description")
        if not title or not description:
            raise ValueError("'title' and 'description' are required")

        author_id = self._resolve_agent(params)

        tags_raw = params.get("tags", [])
        tags = set()
        for t in tags_raw:
            try:
                tags.add(ProblemTag[t])
            except KeyError:
                raise ValueError(f"Unknown tag: {t}")

        problem = Problem(
            title=title,
            description=description,
            author_id=author_id,
            tags=tags,
            priority=params.get("priority", 5),
            bounty=params.get("bounty", 10),
        )
        await self.exchange.post_problem(problem)
        return problem.to_dict()

    async def _m_list_problems(self, params: dict) -> list:
        """List open problems.

        params: {sort_by?: str, tags?: [str], limit?: int, agent_id?: str}
        """
        sort_raw = params.get("sort_by", "PRIORITY")
        try:
            sort_by = ProblemSortKey[sort_raw]
        except KeyError:
            raise ValueError(f"Unknown sort_by: {sort_raw}")

        tags = None
        if "tags" in params:
            tags = set()
            for t in params["tags"]:
                try:
                    tags.add(ProblemTag[t])
                except KeyError:
                    raise ValueError(f"Unknown tag: {t}")

        limit = params.get("limit", 0)

        # agent_id is optional — filters problems for a specific agent
        agent_id = None
        if "agent_id" in params or "token" in params:
            try:
                agent_id = self._resolve_agent(params)
            except (_AuthError, ValueError):
                # If no agent_id and token is absent/invalid, just skip filtering
                if "agent_id" in params:
                    raise
        if agent_id:
            problems = self.exchange.open_problems_for(
                agent_id, sort_by, tags=tags, limit=limit,
            )
        else:
            problems = self.exchange.open_problems(
                sort_by, tags=tags, limit=limit,
            )
        return [p.to_dict() for p in problems]

    async def _m_get_problem(self, params: dict) -> dict:
        """Get a specific problem.

        params: {problem_id}
        """
        pid = _uuid(params["problem_id"])
        return self.exchange.get_problem(pid).to_dict()

    # -- Claim / Solve ------------------------------------------------

    async def _m_claim(self, params: dict) -> dict:
        """Claim a problem.

        params: {problem_id, agent_id|token}
        """
        pid = _uuid(params["problem_id"])
        aid = self._resolve_agent(params)
        await self.exchange.claim_problem(pid, aid)
        return {"claimed": True, "problem_id": str(pid), "agent_id": str(aid)}

    async def _m_solve(self, params: dict) -> dict:
        """Submit a solution body for a claimed problem.

        params: {problem_id, agent_id|token, body}
        """
        pid = _uuid(params["problem_id"])
        aid = self._resolve_agent(params)
        body = params.get("body")
        if not body:
            raise ValueError("'body' is required — push your solution text here")
        solution = await self.exchange.solve_problem(
            pid, aid, solution_body=body,
        )
        return solution.to_dict()

    async def _m_claim_and_solve(self, params: dict) -> dict:
        """Claim + solve in one call.

        params: {problem_id, agent_id|token, body}
        """
        pid = _uuid(params["problem_id"])
        aid = self._resolve_agent(params)
        body = params.get("body")
        if not body:
            raise ValueError("'body' is required")
        await self.exchange.claim_problem(pid, aid)
        solution = await self.exchange.solve_problem(
            pid, aid, solution_body=body,
        )
        return solution.to_dict()

    # -- Reviews ------------------------------------------------------

    async def _m_list_reviews_needed(self, params: dict) -> list:
        """List solutions that need reviews.

        params: {agent_id|token?: str, limit?: int}
        """
        aid = None
        if "agent_id" in params or "token" in params:
            try:
                aid = self._resolve_agent(params)
            except (_AuthError, ValueError):
                if "agent_id" in params:
                    raise
        limit = params.get("limit", 0)
        solutions = self.exchange.solutions_needing_review(aid, limit=limit)
        result = []
        for sol in solutions:
            problem = self.exchange.get_problem(sol.problem_id)
            result.append({
                "solution": sol.to_dict(),
                "problem": {
                    "id": str(problem.id),
                    "title": problem.title,
                    "description": problem.description,
                    "tags": [t.name for t in problem.tags],
                },
            })
        return result

    async def _m_submit_review(self, params: dict) -> dict:
        """Submit a review for a solution.

        params: {solution_id, reviewer_id, verdict, review_type?: str,
                 body?: str, confidence?: float}
        """
        sid = _uuid(params["solution_id"])
        rid = self._resolve_agent(params)

        verdict_raw = params.get("verdict")
        try:
            verdict = ReviewVerdict[verdict_raw]
        except (KeyError, TypeError):
            raise ValueError(
                f"'verdict' must be one of: {', '.join(v.name for v in ReviewVerdict)}"
            )

        rtype_raw = params.get("review_type", "CORRECTNESS")
        try:
            review_type = ReviewType[rtype_raw]
        except KeyError:
            raise ValueError(f"Unknown review_type: {rtype_raw}")

        review = Review(
            solution_id=sid,
            reviewer_id=rid,
            review_type=review_type,
            verdict=verdict,
            body=params.get("body", ""),
            confidence=params.get("confidence", 0.8),
        )
        result = await self.exchange.submit_review(review)
        return result.to_dict()

    async def _m_get_reviews(self, params: dict) -> list:
        """Get all reviews for a solution.

        params: {solution_id}
        """
        sid = _uuid(params["solution_id"])
        reviews = self.exchange.reviews_for_solution(sid)
        return [r.to_dict() for r in reviews]

    # -- Reputation / Skills ------------------------------------------

    async def _m_my_reputation(self, params: dict) -> dict:
        """Get an agent's reputation and rank.

        params: {agent_id|token}
        """
        aid = self._resolve_agent(params)
        balance = self.exchange.ledger.balance(aid)
        board = self.exchange.leaderboard()
        rank = None
        for i, entry in enumerate(board, 1):
            if entry.get("agent_id") == aid or entry.get("name") == self.exchange._agents[aid].name:
                rank = i
                break
        return {
            "agent_id": str(aid),
            "reputation": balance,
            "rank": rank,
            "total_agents": len(self.exchange._agents),
        }

    async def _m_leaderboard(self, params: dict) -> list:
        """Get the reputation leaderboard.

        params: {top_n?: int}
        """
        top_n = params.get("top_n", 10)
        return self.exchange.leaderboard(top_n=top_n)

    async def _m_skill_summary(self, params: dict) -> dict:
        """Get skill summary for an agent.

        params: {agent_id|token}
        """
        aid = self._resolve_agent(params)
        return self.exchange.get_skill_summary(aid)

    async def _m_effective_tier(self, params: dict) -> dict:
        """Get the effective (proven) tier for an agent.

        params: {agent_id|token}
        """
        aid = self._resolve_agent(params)
        tier = self.exchange.get_effective_tier(aid)
        return {"agent_id": str(aid), "effective_tier": tier.name}

    async def _m_is_probationary(self, params: dict) -> dict:
        """Check whether an agent is still in probation.

        params: {agent_id|token}
        """
        aid = self._resolve_agent(params)
        return {"agent_id": str(aid), "probationary": self.exchange.is_probationary(aid)}

    # -- Agent admin --------------------------------------------------

    async def _m_get_agent(self, params: dict) -> dict:
        """Get details for a single agent.

        params: {agent_id}
        """
        aid = _uuid(params["agent_id"])
        a = self.exchange.get_agent(aid)
        return {
            "agent_id": str(a.id),
            "name": a.name,
            "capabilities": [c.name for c in a.capabilities],
            "model_tier": a.model_tier.name,
            "active_claims": a.active_count,
            "reputation": self.exchange.ledger.balance(a.id),
        }

    async def _m_pending_agents(self, params: dict) -> list:
        """List agents awaiting approval."""
        return [
            {
                "agent_id": str(a.id),
                "name": a.name,
                "capabilities": [c.name for c in a.capabilities],
                "model_tier": a.model_tier.name,
            }
            for a in self.exchange.pending_agents
        ]

    async def _m_approve_agent(self, params: dict) -> dict:
        """Approve a pending agent.

        params: {agent_id}
        """
        aid = _uuid(params["agent_id"])
        agent = self.exchange.approve_agent(aid)
        return {"agent_id": str(agent.id), "approved": True}

    async def _m_reject_agent(self, params: dict) -> dict:
        """Reject a pending agent.

        params: {agent_id}
        """
        aid = _uuid(params["agent_id"])
        self.exchange.reject_pending_agent(aid)
        return {"agent_id": str(aid), "rejected": True}

    async def _m_suspend_agent(self, params: dict) -> dict:
        """Suspend an agent.

        params: {agent_id, reason?: str}
        """
        aid = _uuid(params["agent_id"])
        reason = params.get("reason", "Suspended via station")
        await self.exchange.suspend_agent(aid, reason=reason)
        return {"agent_id": str(aid), "suspended": True}

    async def _m_unsuspend_agent(self, params: dict) -> dict:
        """Unsuspend an agent.

        params: {agent_id}
        """
        aid = _uuid(params["agent_id"])
        await self.exchange.unsuspend_agent(aid)
        return {"agent_id": str(aid), "suspended": False}

    async def _m_is_suspended(self, params: dict) -> dict:
        """Check if an agent is suspended.

        params: {agent_id}
        """
        aid = _uuid(params["agent_id"])
        return {"agent_id": str(aid), "suspended": self.exchange.is_suspended(aid)}

    # -- Problems (batch / decompose) ---------------------------------

    async def _m_post_problems(self, params: dict) -> list:
        """Batch-post multiple problems.

        params: {problems: [{title, description, author_id|token, tags?, priority?, bounty?}]}
        """
        raw_list = params.get("problems")
        if not raw_list:
            raise ValueError("'problems' list is required")
        author_id = self._resolve_agent(params)
        problems = []
        for raw in raw_list:
            tags = set()
            for t in raw.get("tags", []):
                try:
                    tags.add(ProblemTag[t])
                except KeyError:
                    raise ValueError(f"Unknown tag: {t}")
            problems.append(Problem(
                title=raw["title"],
                description=raw["description"],
                author_id=author_id,
                tags=tags,
                priority=raw.get("priority", 5),
                bounty=raw.get("bounty", 10),
            ))
        posted = await self.exchange.post_problems(problems)
        return [p.to_dict() for p in posted]

    async def _m_decompose_problem(self, params: dict) -> list:
        """Decompose a problem into sub-problems.

        params: {parent_id, sub_problems: [{title, description, tags?, priority?, bounty?}],
                 sequential?: bool, agent_id|token}
        """
        parent_id = _uuid(params["parent_id"])
        author_id = self._resolve_agent(params)
        sequential = params.get("sequential", False)
        raw_list = params.get("sub_problems")
        if not raw_list:
            raise ValueError("'sub_problems' list is required")
        subs = []
        for raw in raw_list:
            tags = set()
            for t in raw.get("tags", []):
                try:
                    tags.add(ProblemTag[t])
                except KeyError:
                    raise ValueError(f"Unknown tag: {t}")
            subs.append(Problem(
                title=raw["title"],
                description=raw["description"],
                author_id=author_id,
                tags=tags,
                priority=raw.get("priority", 5),
                bounty=raw.get("bounty", 10),
            ))
        posted = await self.exchange.decompose_problem(
            parent_id, subs, sequential=sequential,
        )
        return [p.to_dict() for p in posted]

    async def _m_sub_problems(self, params: dict) -> list:
        """List sub-problems of a parent.

        params: {parent_id}
        """
        pid = _uuid(params["parent_id"])
        return [p.to_dict() for p in self.exchange.sub_problems(pid)]

    async def _m_dependencies_met(self, params: dict) -> dict:
        """Check if a problem's dependencies are all met.

        params: {problem_id}
        """
        pid = _uuid(params["problem_id"])
        return {"problem_id": str(pid), "met": self.exchange.dependencies_met(pid)}

    # -- Solutions ----------------------------------------------------

    async def _m_get_solution(self, params: dict) -> dict:
        """Get a specific solution by ID.

        params: {solution_id}
        """
        sid = _uuid(params["solution_id"])
        return self.exchange.get_solution(sid).to_dict()

    async def _m_solutions_for_problem(self, params: dict) -> list:
        """Get all solutions for a problem.

        params: {problem_id}
        """
        pid = _uuid(params["problem_id"])
        return [s.to_dict() for s in self.exchange.solutions_for_problem(pid)]

    # -- Revision -----------------------------------------------------

    async def _m_request_revision(self, params: dict) -> dict:
        """Request a revision on a solution.

        params: {solution_id, reviewer_id|token, reason}
        """
        sid = _uuid(params["solution_id"])
        rid = self._resolve_agent(params)
        reason = params.get("reason", "")
        if not reason:
            raise ValueError("'reason' is required")
        await self.exchange.request_revision(sid, rid, reason)
        return {"solution_id": str(sid), "revision_requested": True}

    async def _m_revise_solution(self, params: dict) -> dict:
        """Submit a revised solution body.

        params: {solution_id, agent_id|token, body}
        """
        sid = _uuid(params["solution_id"])
        aid = self._resolve_agent(params)
        body = params.get("body")
        if not body:
            raise ValueError("'body' is required")
        await self.exchange.revise_solution(sid, aid, revised_body=body)
        sol = self.exchange.get_solution(sid)
        return sol.to_dict()

    # -- Swap ---------------------------------------------------------

    async def _m_submit_swap(self, params: dict) -> dict:
        """Submit a problem to the swap pool.

        params: {problem_id, agent_id|token}
        """
        pid = _uuid(params["problem_id"])
        aid = self._resolve_agent(params)
        await self.exchange.submit_swap(aid, pid)
        return {"submitted": True, "problem_id": str(pid), "agent_id": str(aid)}

    async def _m_run_swaps(self, params: dict) -> list:
        """Run the swap matching algorithm.

        params: {}
        """
        matches = await self.exchange.run_swaps()
        return [
            {
                "match_id": str(m.id),
                "agent_a": str(m.agent_a),
                "problem_a": str(m.problem_a),
                "agent_b": str(m.agent_b),
                "problem_b": str(m.problem_b),
            }
            for m in matches
        ]

    async def _m_complete_swap(self, params: dict) -> dict:
        """Complete a swap match.

        params: {match_id}
        """
        mid = _uuid(params["match_id"])
        await self.exchange.complete_swap(mid)
        return {"match_id": str(mid), "completed": True}

    # -- Challenge ----------------------------------------------------

    async def _m_challenge_solution(self, params: dict) -> dict:
        """Challenge an accepted solution.

        params: {solution_id, challenger_id|token, reason?: str}
        """
        sid = _uuid(params["solution_id"])
        cid = self._resolve_agent(params)
        reason = params.get("reason", "")
        problem = await self.exchange.challenge_solution(
            sid, cid, reason=reason,
        )
        return problem.to_dict()

    # -- Archive ------------------------------------------------------

    async def _m_search_archive(self, params: dict) -> list:
        """Search the archive of solved problems.

        params: {tags?: [str], keywords?: [str], min_confidence?: float, limit?: int}
        """
        kwargs: dict[str, Any] = {}
        if "tags" in params:
            tag_set = set()
            for t in params["tags"]:
                try:
                    tag_set.add(ProblemTag[t])
                except KeyError:
                    raise ValueError(f"Unknown tag: {t}")
            kwargs["tags"] = tag_set
        if "keywords" in params:
            kwargs["keywords"] = params["keywords"]
        if "min_confidence" in params:
            kwargs["min_confidence"] = params["min_confidence"]
        if "limit" in params:
            kwargs["limit"] = params["limit"]
        entries = self.exchange.search_archive(**kwargs)
        return [_serialize(e) for e in entries]

    # -- Calibration --------------------------------------------------

    async def _m_evaluate_calibration(self, params: dict) -> dict:
        """Evaluate an answer to a calibration problem.

        params: {agent_id|token, problem_id, answer}
        """
        aid = self._resolve_agent(params)
        pid = _uuid(params["problem_id"])
        answer = params.get("answer", "")
        result = await self.exchange.evaluate_calibration(aid, pid, answer)
        return {
            "passed": result.passed,
            "score": result.score,
            "feedback": result.feedback,
        }

    async def _m_is_calibration_problem(self, params: dict) -> dict:
        """Check if a problem is a calibration problem.

        params: {problem_id}
        """
        pid = _uuid(params["problem_id"])
        return {"problem_id": str(pid), "is_calibration": self.exchange.is_calibration_problem(pid)}

    # -- Maintenance --------------------------------------------------

    async def _m_expire_stale_problems(self, params: dict) -> list:
        """Expire stale open problems. Returns expired problems.

        params: {}
        """
        expired = await self.exchange.expire_stale_problems()
        return [p.to_dict() for p in expired]

    async def _m_expire_stale_claims(self, params: dict) -> list:
        """Expire stale claimed problems. Returns list of (agent_id, problem_id) pairs.

        params: {}
        """
        expired = await self.exchange.expire_stale_claims()
        return [
            {"agent_id": str(aid), "problem_id": str(pid)}
            for aid, pid in expired
        ]

    async def _m_escalate_bounty(self, params: dict) -> dict:
        """Escalate the bounty on a problem.

        params: {problem_id}
        """
        pid = _uuid(params["problem_id"])
        problem = await self.exchange.escalate_bounty(pid)
        return problem.to_dict()

    async def _m_escalate_stale_bounties(self, params: dict) -> list:
        """Auto-escalate bounties on stale problems.

        params: {stale_seconds?: float}
        """
        stale = params.get("stale_seconds", 3600)
        escalated = await self.exchange.escalate_stale_bounties(stale_seconds=stale)
        return [p.to_dict() for p in escalated]

    # -- Snapshot / Restore -------------------------------------------

    async def _m_snapshot(self, params: dict) -> dict:
        """Capture the full exchange state as a serializable dict.

        params: {}
        """
        return self.exchange.snapshot()

    async def _m_restore(self, params: dict) -> dict:
        """Restore problems from a snapshot.

        params: {snapshot: dict}
        """
        snap = params.get("snapshot")
        if not snap:
            raise ValueError("'snapshot' dict is required")
        count = self.exchange.restore_problems(snap)
        return {"restored": count}

    # -- Event streaming -----------------------------------------------

    async def _broadcast_event(self, event: Event) -> None:
        """Push a JSON-RPC notification to all subscribed writers.

        This is registered as a global handler on the Exchange's EventBus.
        Notifications use the JSON-RPC notification format (no ``id``).
        """
        if not self._subscribers:
            return

        notification = json.dumps({
            "jsonrpc": JSONRPC_VERSION,
            "method": "event",
            "params": _serialize(event.to_dict()),
        }) + "\n"
        encoded = notification.encode()

        dead: list[int] = []
        for writer_id, (writer, kinds) in self._subscribers.items():
            # Filter by subscribed kinds (None = all)
            if kinds is not None and event.kind not in kinds:
                continue
            try:
                writer.write(encoded)
                await writer.drain()
            except Exception:
                dead.append(writer_id)

        for wid in dead:
            self._subscribers.pop(wid, None)

    def add_subscriber(
        self,
        writer: asyncio.StreamWriter,
        kinds: set[EventKind] | None = None,
    ) -> int:
        """Register a writer for event push notifications.

        Args:
            writer: The stream writer to push events to.
            kinds: Set of EventKind to filter on, or None for all events.

        Returns the subscriber ID (for removal later).
        """
        sub_id = id(writer)
        self._subscribers[sub_id] = (writer, kinds)
        return sub_id

    def remove_subscriber(self, sub_id: int) -> None:
        """Remove a subscriber by its ID."""
        self._subscribers.pop(sub_id, None)

    async def _m_subscribe(self, params: dict) -> dict:
        """Subscribe to event notifications (in-band via the current connection).

        This is a special method: it records the calling context so the
        station knows which writer to push notifications to.  For
        transport-agnostic use via ``handle()``, the subscription is
        registered but events are delivered only for TCP connections
        where the station tracks the writer.

        params: {kinds?: [str]}

        For direct (non-transport) use, call ``add_subscriber()`` instead.
        """
        kinds_raw = params.get("kinds")
        kinds: set[EventKind] | None = None
        if kinds_raw:
            kinds = set()
            for k in kinds_raw:
                try:
                    kinds.add(EventKind[k])
                except KeyError:
                    raise ValueError(f"Unknown event kind: {k}")

        # The _writer is injected by the TCP handler into params
        writer = params.get("_writer")
        if writer is not None:
            sub_id = self.add_subscriber(writer, kinds)
            return {"subscribed": True, "subscriber_id": sub_id}

        # No writer available (stdio or direct handle() call)
        return {
            "subscribed": False,
            "reason": "No persistent connection — use TCP transport for live events",
        }

    async def _m_unsubscribe(self, params: dict) -> dict:
        """Unsubscribe from event notifications.

        params: {subscriber_id: int}
        """
        sub_id = params.get("subscriber_id")
        if sub_id is None:
            raise ValueError("'subscriber_id' is required")
        self.remove_subscriber(int(sub_id))
        return {"unsubscribed": True}

    # -- Inbox ---------------------------------------------------------

    async def _m_inbox(self, params: dict) -> list:
        """Read notifications without consuming them.

        params: {agent_id|token, limit?: int}
        """
        aid = self._resolve_agent(params)
        limit = params.get("limit", 0)
        return self.exchange.inbox(aid, limit=limit)

    async def _m_inbox_count(self, params: dict) -> dict:
        """Get the count of unread notifications.

        params: {agent_id|token}
        """
        aid = self._resolve_agent(params)
        return {"agent_id": str(aid), "count": self.exchange.inbox_count(aid)}

    async def _m_consume_inbox(self, params: dict) -> list:
        """Read and remove notifications.

        params: {agent_id|token, count?: int}
        """
        aid = self._resolve_agent(params)
        count = params.get("count", 0)
        return self.exchange.consume_inbox(aid, count=count)

    async def _m_clear_inbox(self, params: dict) -> dict:
        """Clear all notifications for an agent.

        params: {agent_id|token}
        """
        aid = self._resolve_agent(params)
        cleared = self.exchange.clear_inbox(aid)
        return {"agent_id": str(aid), "cleared": cleared}

    # -- Presence / heartbeat -----------------------------------------

    async def _m_heartbeat(self, params: dict) -> dict:
        """Record a heartbeat for the calling agent.

        params: {agent_id|token}
        """
        aid = self._resolve_agent(params)
        ts = self.exchange.heartbeat(aid)
        return {"agent_id": str(aid), "timestamp": ts.isoformat()}

    async def _m_is_online(self, params: dict) -> dict:
        """Check if a specific agent is online.

        params: {agent_id}
        """
        aid = _uuid(params["agent_id"])
        return {"agent_id": str(aid), "online": self.exchange.is_online(aid)}

    async def _m_online_agents(self, params: dict) -> dict:
        """Return the list of agents currently online.

        params: {}
        """
        agents = self.exchange.online_agents()
        return {"online": [str(a) for a in agents]}

    async def _m_last_seen(self, params: dict) -> dict:
        """Return last heartbeat timestamp for an agent.

        params: {agent_id}
        """
        aid = _uuid(params["agent_id"])
        ts = self.exchange.last_seen(aid)
        return {
            "agent_id": str(aid),
            "last_seen": ts.isoformat() if ts else None,
        }

    # ------------------------------------------------------------------
    # Work discovery
    # ------------------------------------------------------------------

    async def _m_request_work(self, params: dict) -> dict:
        """Find open problems suited for the calling agent.

        params: {agent_id, ?tags, ?limit}
        """
        aid = self._resolve_agent(params)
        tags = set(params.get("tags", []))
        limit = int(params.get("limit", 5))
        problems = self.exchange.request_work(aid, tags=tags or None, limit=limit)
        return {
            "problems": [
                {"id": str(p.id), "title": p.title, "bounty": p.bounty,
                 "tags": [t.name for t in p.tags]}
                for p in problems
            ],
        }

    async def _m_update_watch_tags(self, params: dict) -> dict:
        """Set problem-tag preferences for an agent.

        params: {agent_id, tags: [str]}
        """
        from schwarma.problem import ProblemTag

        aid = self._resolve_agent(params)
        raw_tags = params.get("tags", [])
        tags = set()
        for t in raw_tags:
            try:
                tags.add(ProblemTag[t.upper()])
            except KeyError:
                pass
        self.exchange.update_watch_tags(aid, tags)
        return {"agent_id": str(aid), "watch_tags": [t.name for t in tags]}


# ── CLI entry-point ──────────────────────────────────────────────────────

def main() -> None:
    """Run the station from the command line.

    Usage::

        python -m schwarma.station              # stdio (default)
        python -m schwarma.station --tcp 9741   # TCP on port 9741
    """
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    parser = argparse.ArgumentParser(description="Schwarma Station — JSON-RPC server")
    parser.add_argument("--tcp", type=int, default=None, metavar="PORT",
                        help="Listen on TCP port instead of stdio")
    parser.add_argument("--host", default="127.0.0.1",
                        help="TCP bind address (default: 127.0.0.1)")
    args = parser.parse_args()

    station = SchwarmaStation()

    if args.tcp is not None:
        asyncio.run(station.serve_tcp(args.host, args.tcp))
    else:
        asyncio.run(station.serve_stdio())


if __name__ == "__main__":
    main()
