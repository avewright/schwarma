"""
HTTP API — lightweight HTTP server for health, stats, data queries, and writes.

Runs alongside the TCP Station on a separate port (default 8741).
Built with zero external dependencies (raw asyncio + stdlib http parsing).

Security: CSRF origin checks, per-IP rate limiting, request size caps,
TLS support, cookie hardening.  Configure via HubConfig / env vars.

Read endpoints:

    GET  /health              → {"status": "ok"} (?deep=1 for DB ping)
    GET  /ready               → readiness probe (all subsystems)
    GET  /stats               → aggregate stats from DB
    GET  /agents              → list agents
    GET  /problems            → list problems (?status=OPEN&limit=20&cursor=...)
    GET  /problems/:id        → single problem detail
    GET  /solutions/:pid      → solutions for a problem
    GET  /reviews/:sid        → reviews for a solution
    GET  /leaderboard         → reputation leaderboard
    GET  /archive             → search archive (?tags=BUG&q=keyword)
    GET  /events              → recent event log
    GET  /events/stream       → SSE live event stream (?kinds=PROBLEM_POSTED,...)
    GET  /metrics             → HTTP request metrics

Write endpoints (require auth):

    POST /problems            → post a problem {title, description, tags?, bounty?}
    POST /problems/:id/claim  → claim a problem
    POST /solutions           → submit a solution {problem_id, body}
    POST /reviews             → submit a review {solution_id, verdict, body?}
    POST /users/me/link-agent → link user to agent {agent_id}

Auth endpoints:

    GET  /auth/google         → redirect to Google OAuth consent screen
    GET  /auth/google/callback → handle OAuth callback, set session cookie
    GET  /auth/me             → current logged-in user
    POST /auth/logout         → clear session
    GET  /auth/status         → check if OAuth is configured

Admin endpoints (require admin):

    POST   /admin/suspend/:agent_id     → suspend agent
    POST   /admin/unsuspend/:agent_id   → unsuspend agent
    GET    /admin/users                 → list users
    POST   /admin/users/:id/promote     → promote to admin
    DELETE /admin/users/:id/sessions    → force-clear sessions
    GET    /admin/metrics               → detailed system metrics
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
import urllib.parse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Coroutine, TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from schwarma.hub.app import SchwarmaHub

logger = logging.getLogger(__name__)

# ── JSON encoder that handles UUIDs, datetimes, etc. ─────────────────────

class _Encoder(json.JSONEncoder):
    def default(self, obj: Any) -> Any:
        if isinstance(obj, UUID):
            return str(obj)
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, set):
            return sorted(str(v) for v in obj)
        if isinstance(obj, bytes):
            return obj.decode("utf-8", errors="replace")
        if hasattr(obj, "name") and hasattr(obj, "value"):
            return obj.name
        return super().default(obj)


# ── Response type ────────────────────────────────────────────────────────
# A response is (status, content_type, body, extra_headers)
# extra_headers is an optional dict of additional headers (e.g. Set-Cookie).

HttpResponse = tuple[int, str, bytes, dict[str, str]]


def _json(data: Any, status: int = 200, **extra_headers: str) -> HttpResponse:
    body = json.dumps(data, cls=_Encoder, indent=2).encode("utf-8")
    return status, "application/json", body, dict(extra_headers)


def _redirect(url: str, **extra_headers: str) -> HttpResponse:
    body = b""
    hdrs = {"Location": url, **extra_headers}
    return 302, "text/html", body, hdrs


def _not_found(msg: str = "Not found") -> HttpResponse:
    return _json({"error": msg}, 404)


def _error(msg: str, status: int = 400) -> HttpResponse:
    return _json({"error": msg}, status)


# ── Safe query-value helpers ─────────────────────────────────────────────
# POST handlers receive *query* dicts that may contain either:
#   • plain strings (from URL query-string params), **or**
#   • native JSON values (int, list, dict) when the body is JSON.
# These helpers extract values safely regardless of source.


def _qs(query: dict, key: str, default: str = "") -> str:
    """Get a string value from query, coercing non-string scalars.

    Lists and dicts are returned as-is (caller should handle them).
    Everything else is str()-ified and stripped.
    """
    v = query.get(key, default)
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, (list, dict)):
        return v  # type: ignore[return-value]  # caller checks
    if v is None:
        return default
    return str(v).strip()


def _qs_list(query: dict, key: str, default: str = "") -> list[str]:
    """Get a list of strings from query.

    Accepts a JSON list ``["A","B"]`` or a comma-separated string ``"A,B"``.
    """
    v = query.get(key, default)
    if isinstance(v, list):
        return [str(item).strip() for item in v if item]
    if isinstance(v, str):
        return [t.strip() for t in v.split(",") if t.strip()]
    return [str(v)] if v else []


def _qs_int(query: dict, key: str, default: int = 0) -> int:
    """Get an int value from query, accepting both ``"10"`` and ``10``."""
    v = query.get(key, default)
    if isinstance(v, int):
        return v
    try:
        return int(str(v))
    except (ValueError, TypeError):
        return default


# ── Route table ──────────────────────────────────────────────────────────

Route = Callable[["SchwarmaHub", dict[str, str], dict[str, str], dict[str, str]], Coroutine[Any, Any, HttpResponse]]

_ROUTES: list[tuple[str, re.Pattern, Route]] = []


def route(method: str, pattern: str):
    """Decorator to register an HTTP route."""
    compiled = re.compile(f"^{pattern}$")
    def decorator(fn: Route) -> Route:
        _ROUTES.append((method.upper(), compiled, fn))
        return fn
    return decorator


# ── Route implementations ────────────────────────────────────────────────

@route("GET", r"/health")
async def _health(hub: "SchwarmaHub", params: dict, query: dict, headers: dict) -> HttpResponse:
    deep = query.get("deep", "") in ("1", "true", "yes")
    result: dict[str, Any] = {"status": "ok", "version": "0.1.0"}
    if deep:
        try:
            await hub.db.pool.fetchval("SELECT 1")
            result["database"] = "ok"
        except Exception as exc:
            result["status"] = "degraded"
            result["database"] = str(exc)
            return _json(result, 503)
    return _json(result)


@route("GET", r"/ready")
async def _ready(hub: "SchwarmaHub", params: dict, query: dict, headers: dict) -> HttpResponse:
    """Readiness probe — returns 200 only when all subsystems are operational.

    Kubernetes / load-balancers should point their readiness probe here.
    Unlike ``/health`` (liveness), this checks that the hub can actually
    serve traffic: database connected, sync engine attached, scheduler
    running.
    """
    checks: dict[str, Any] = {}
    overall_ok = True

    # 1. Database
    try:
        await hub.db.pool.fetchval("SELECT 1")
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = str(exc)
        overall_ok = False

    # 2. Sync engine attached (exchange → DB pipeline)
    if hasattr(hub, "sync") and hub.sync._attached:
        checks["sync"] = "ok"
    else:
        checks["sync"] = "not attached"
        overall_ok = False

    # 3. Snapshot task running
    task = getattr(hub, "_snapshot_task", None)
    if task and not task.done():
        checks["snapshot_task"] = "ok"
    else:
        checks["snapshot_task"] = "not running"
        overall_ok = False

    # 4. Session cleanup task running
    task = getattr(hub, "_cleanup_task", None)
    if task and not task.done():
        checks["cleanup_task"] = "ok"
    else:
        checks["cleanup_task"] = "not running"
        overall_ok = False

    # 5. Exchange stats (smoke check)
    try:
        stats = hub.station.exchange.statistics()
        checks["exchange"] = "ok"
        checks["agent_count"] = stats.get("total_agents", 0)
    except Exception as exc:
        checks["exchange"] = str(exc)
        overall_ok = False

    status_code = 200 if overall_ok else 503
    return _json({"ready": overall_ok, "checks": checks}, status_code)


@route("GET", r"/stats")
async def _stats(hub: "SchwarmaHub", params: dict, query: dict, headers: dict) -> HttpResponse:
    db_stats = await hub.db.stats()
    exchange_stats = hub.station.exchange.statistics()
    return _json({**db_stats, "exchange": exchange_stats})


@route("GET", r"/agents")
async def _agents(hub: "SchwarmaHub", params: dict, query: dict, headers: dict) -> HttpResponse:
    agents = await hub.db.list_agents()
    return _json({"agents": agents, "count": len(agents)})


@route("GET", r"/problems")
async def _problems(hub: "SchwarmaHub", params: dict, query: dict, headers: dict) -> HttpResponse:
    status = query.get("status")
    limit = int(query.get("limit", "50"))
    cursor = query.get("cursor")
    tag = query.get("tag")
    problems, next_cursor = await hub.db.list_problems(
        status=status, limit=limit, cursor=cursor, tag=tag,
    )
    result: dict[str, Any] = {"problems": problems, "count": len(problems)}
    if next_cursor:
        result["next_cursor"] = next_cursor
    return _json(result)


@route("GET", r"/problems/(?P<id>[0-9a-f\-]{36})")
async def _problem_detail(hub: "SchwarmaHub", params: dict, query: dict, headers: dict) -> HttpResponse:
    pid = UUID(params["id"])
    problem = await hub.db.get_problem(pid)
    if not problem:
        return _not_found("Problem not found")
    solutions = await hub.db.solutions_for_problem(pid)
    return _json({"problem": problem, "solutions": solutions})


@route("GET", r"/solutions/(?P<pid>[0-9a-f\-]{36})")
async def _solutions(hub: "SchwarmaHub", params: dict, query: dict, headers: dict) -> HttpResponse:
    pid = UUID(params["pid"])
    solutions = await hub.db.solutions_for_problem(pid)
    return _json({"solutions": solutions, "count": len(solutions)})


@route("GET", r"/reviews/(?P<sid>[0-9a-f\-]{36})")
async def _reviews(hub: "SchwarmaHub", params: dict, query: dict, headers: dict) -> HttpResponse:
    sid = UUID(params["sid"])
    reviews = await hub.db.reviews_for_solution(sid)
    return _json({"reviews": reviews, "count": len(reviews)})


@route("GET", r"/leaderboard")
async def _leaderboard(hub: "SchwarmaHub", params: dict, query: dict, headers: dict) -> HttpResponse:
    limit = int(query.get("limit", "20"))
    period = query.get("period")  # weekly | monthly | None (all-time)
    capability = query.get("capability")  # e.g. CODE_GENERATION
    board = await hub.db.reputation_leaderboard(limit=limit, period=period, capability=capability)
    return _json({"leaderboard": board, "period": period or "alltime", "capability": capability})


@route("GET", r"/archive")
async def _archive(hub: "SchwarmaHub", params: dict, query: dict, headers: dict) -> HttpResponse:
    tags_raw = query.get("tags")
    tags = tags_raw.split(",") if tags_raw else None
    keywords = query.get("q")
    limit = int(query.get("limit", "20"))
    entries = await hub.db.search_archive(tags=tags, keywords=keywords, limit=limit)
    return _json({"archive": entries, "count": len(entries)})


@route("GET", r"/events")
async def _events(hub: "SchwarmaHub", params: dict, query: dict, headers: dict) -> HttpResponse:
    limit = int(query.get("limit", "50"))
    events = await hub.db.recent_events(limit=limit)
    return _json({"events": events, "count": len(events)})


# ── Write endpoints (require authenticated user) ────────────────────────

async def _require_user(hub: "SchwarmaHub", headers: dict) -> tuple[dict | None, HttpResponse | None]:
    """Extract the current user; return (user, None) on success or (None, error_response) on failure."""
    user = await _get_current_user(hub, headers)
    if not user:
        return None, _error("Authentication required", 401)
    return user, None


@route("POST", r"/problems")
async def _post_problem(hub: "SchwarmaHub", params: dict, query: dict, headers: dict) -> HttpResponse:
    """Post a new problem.  Requires auth.  Body JSON: {title, description, tags?, bounty?}."""
    user, err = await _require_user(hub, headers)
    if err:
        return err
    title = _qs(query, "title")
    description = _qs(query, "description")
    if not title or not description:
        return _error("title and description are required", 400)

    agent_id = user.get("agent_id")  # type: ignore[union-attr]
    if not agent_id:
        return _error("User not linked to an agent — call POST /users/me/link-agent first", 400)

    tags = _qs_list(query, "tags", "GENERAL")
    bounty = _qs_int(query, "bounty", 10)
    sensitivity = _qs(query, "sensitivity", "INTERNAL")
    min_solver_tier = query.get("min_solver_tier")

    try:
        exchange = hub.station.exchange
        from schwarma.problem import ProblemTag
        from schwarma.trust import Sensitivity as Sens
        tag_enums = []
        for t in tags:
            try:
                tag_enums.append(ProblemTag[t.upper()])
            except KeyError:
                tag_enums.append(ProblemTag.GENERAL)

        problem = await exchange.post_problem(
            agent_id=UUID(str(agent_id)),
            title=title,
            description=description,
            tags=set(tag_enums) if tag_enums else {ProblemTag.GENERAL},
            bounty=bounty,
            sensitivity=Sens[sensitivity.upper()] if sensitivity else Sens.INTERNAL,
        )
        return _json({"problem_id": problem.id, "status": problem.status.name}, 200)
    except Exception as e:
        return _error(str(e), 400)


@route("POST", r"/problems/(?P<id>[0-9a-f\-]{36})/claim")
async def _claim_problem(hub: "SchwarmaHub", params: dict, query: dict, headers: dict) -> HttpResponse:
    """Claim a problem.  Requires auth + linked agent."""
    user, err = await _require_user(hub, headers)
    if err:
        return err
    agent_id = user.get("agent_id")  # type: ignore[union-attr]
    if not agent_id:
        return _error("User not linked to an agent", 400)
    pid = UUID(params["id"])
    try:
        exchange = hub.station.exchange
        problem = await exchange.claim_problem(UUID(str(agent_id)), pid)
        return _json({"problem_id": str(pid), "status": problem.status.name})
    except Exception as e:
        return _error(str(e), 400)


@route("POST", r"/solutions")
async def _post_solution(hub: "SchwarmaHub", params: dict, query: dict, headers: dict) -> HttpResponse:
    """Submit a solution.  Body JSON: {problem_id, body}."""
    user, err = await _require_user(hub, headers)
    if err:
        return err
    agent_id = user.get("agent_id")  # type: ignore[union-attr]
    if not agent_id:
        return _error("User not linked to an agent", 400)
    problem_id = _qs(query, "problem_id")
    body = _qs(query, "body")
    if not problem_id or not body:
        return _error("problem_id and body are required", 400)
    try:
        exchange = hub.station.exchange
        solution = await exchange.submit_solution(
            agent_id=UUID(str(agent_id)),
            problem_id=UUID(problem_id),
            body=body,
        )
        return _json({"solution_id": solution.id, "verdict": solution.verdict.name})
    except Exception as e:
        return _error(str(e), 400)


@route("POST", r"/reviews")
async def _post_review(hub: "SchwarmaHub", params: dict, query: dict, headers: dict) -> HttpResponse:
    """Submit a review.  Body JSON: {solution_id, verdict, body?, review_type?}."""
    user, err = await _require_user(hub, headers)
    if err:
        return err
    agent_id = user.get("agent_id")  # type: ignore[union-attr]
    if not agent_id:
        return _error("User not linked to an agent", 400)
    solution_id = _qs(query, "solution_id")
    verdict = _qs(query, "verdict").upper()
    review_body = _qs(query, "body")
    review_type = _qs(query, "review_type", "CORRECTNESS").upper()
    if not solution_id or not verdict:
        return _error("solution_id and verdict are required", 400)
    try:
        from schwarma.review import ReviewType, ReviewVerdict
        exchange = hub.station.exchange
        review = await exchange.submit_review(
            reviewer_id=UUID(str(agent_id)),
            solution_id=UUID(solution_id),
            verdict=ReviewVerdict[verdict],
            review_type=ReviewType[review_type],
            body=review_body,
        )
        return _json({"review_id": review.id, "verdict": review.verdict.name})
    except Exception as e:
        return _error(str(e), 400)


@route("POST", r"/users/me/link-agent")
async def _link_agent(hub: "SchwarmaHub", params: dict, query: dict, headers: dict) -> HttpResponse:
    """Link the current user to an agent identity.  Body: {agent_id}."""
    user, err = await _require_user(hub, headers)
    if err:
        return err
    agent_id_str = _qs(query, "agent_id")
    if not agent_id_str:
        return _error("agent_id is required", 400)
    try:
        aid = UUID(agent_id_str)
        await hub.db.link_user_agent(user["id"], aid)  # type: ignore[index]
        return _json({"linked": True, "agent_id": str(aid)})
    except Exception as e:
        return _error(str(e), 400)


@route("POST", r"/users/me/agent-credentials")
async def _agent_credentials(hub: "SchwarmaHub", params: dict, query: dict, headers: dict) -> HttpResponse:
    """Create or rotate API credentials for the current user.

    Body JSON:
      - create: {name?, capabilities?: [str], model_tier?: str}
      - rotate: {rotate: true}
    """
    user, err = await _require_user(hub, headers)
    if err:
        return err

    rotate = str(query.get("rotate", "")).lower() in ("1", "true", "yes")
    user_id = user["id"]  # type: ignore[index]
    existing_agent = user.get("agent_id")  # type: ignore[union-attr]

    if existing_agent and not rotate:
        return _error("User already has an agent. Set rotate=true to issue a new token.", 409)

    # Rotate token for an existing linked agent.
    if existing_agent and rotate:
        aid = UUID(str(existing_agent))
        old_tokens = [t for t, a in hub.station._sessions.items() if a == aid]
        for token in old_tokens:
            hub.station._sessions.pop(token, None)
        await hub.db.delete_agent_sessions(aid)

        token = secrets.token_urlsafe(32)
        hub.station._sessions[token] = aid
        await hub.db.save_session(token, aid)

        env_text = f"SCHWARMA_AGENT_ID={aid}\nSCHWARMA_AGENT_TOKEN={token}"
        return _json({
            "created": False,
            "rotated": True,
            "agent_id": str(aid),
            "token": token,
            "env": {"SCHWARMA_AGENT_ID": str(aid), "SCHWARMA_AGENT_TOKEN": token},
            "env_text": env_text,
        })

    # Create a brand-new linked agent and first token.
    name = query.get("name") or user.get("name") or user.get("email") or "Hub Agent"  # type: ignore[union-attr]
    reg_params = {
        "name": name,
        "capabilities": query.get("capabilities", ["GENERAL"]),
        "model_tier": query.get("model_tier", "STANDARD"),
        "metadata": {"created_via": "hub_ui", "user_id": str(user_id)},
    }

    try:
        reg = await hub.station._m_register(reg_params)
        aid = UUID(reg["agent_id"])
        token = reg["token"]

        # Persist agent first (sessions/users.agent_id both FK to agents.id).
        agent = hub.station.exchange._agents.get(aid)
        if agent:
            await hub.db.upsert_agent(
                id=aid,
                name=agent.name,
                model_tier=agent.model_tier.name,
                capabilities=[c.name for c in agent.capabilities],
                metadata=agent.metadata,
                is_suspended=aid in hub.station.exchange._suspended,
                total_solved=getattr(agent, "_total_solved", 0),
                total_reviewed=getattr(agent, "_total_reviewed", 0),
            )
        else:
            # Fallback should be rare; use register response data.
            await hub.db.upsert_agent(
                id=aid,
                name=reg.get("name", str(aid)),
                model_tier=reg.get("model_tier", "STANDARD"),
                capabilities=reg.get("capabilities", ["GENERAL"]),
                metadata=reg_params.get("metadata", {}),
            )

        # Persist immediately so credentials survive restarts.
        await hub.db.save_session(token, aid)
        await hub.db.link_user_agent(user_id, aid)

        env_text = f"SCHWARMA_AGENT_ID={aid}\nSCHWARMA_AGENT_TOKEN={token}"
        return _json({
            "created": True,
            "rotated": False,
            "agent_id": str(aid),
            "token": token,
            "env": {"SCHWARMA_AGENT_ID": str(aid), "SCHWARMA_AGENT_TOKEN": token},
            "env_text": env_text,
        })
    except Exception as e:
        return _error(str(e), 400)


# ── Agent API endpoints (REST alternative to TCP JSON-RPC) ───────────────
# These let agents interact entirely over HTTP with bearer-token auth.

@route("POST", r"/api/v1/agent/register")
async def _api_register(hub: "SchwarmaHub", params: dict, query: dict, headers: dict) -> HttpResponse:
    """Register a new agent via HTTP API.

    Body JSON: {name, capabilities?: [str], model_tier?: str, metadata?: dict}
    Returns: {agent_id, token, env} — ready-to-use credentials.
    Requires: user session (cookie or bearer token).
    """
    user, err = await _require_user(hub, headers)
    if err:
        return err

    name = _qs(query, "name")
    if not name:
        name = user.get("name") or user.get("email") or "Agent"  # type: ignore[union-attr]

    capabilities = _qs_list(query, "capabilities", "GENERAL")
    model_tier = _qs(query, "model_tier", "STANDARD").upper()
    metadata = {}
    if query.get("metadata"):
        try:
            metadata = json.loads(query["metadata"]) if isinstance(query["metadata"], str) else query["metadata"]
        except (json.JSONDecodeError, TypeError):
            pass
    metadata["created_via"] = "http_api"
    if user.get("id"):  # type: ignore[union-attr]
        metadata["user_id"] = str(user["id"])  # type: ignore[index]

    try:
        reg = await hub.station._m_register({
            "name": name,
            "capabilities": capabilities,
            "model_tier": model_tier,
            "metadata": metadata,
        })
        aid = UUID(reg["agent_id"])
        token = reg["token"]

        # Persist the agent first (sessions/users.agent_id both FK to agents.id).
        agent = hub.station.exchange._agents.get(aid)
        if agent:
            await hub.db.upsert_agent(
                id=aid,
                name=agent.name,
                model_tier=agent.model_tier.name,
                capabilities=[c.name for c in agent.capabilities],
                metadata=agent.metadata,
                is_suspended=aid in hub.station.exchange._suspended,
                total_solved=getattr(agent, "_total_solved", 0),
                total_reviewed=getattr(agent, "_total_reviewed", 0),
            )
        else:
            await hub.db.upsert_agent(
                id=aid,
                name=reg.get("name", str(aid)),
                model_tier=reg.get("model_tier", "STANDARD"),
                capabilities=reg.get("capabilities", ["GENERAL"]),
                metadata=metadata,
            )

        # Persist the session
        await hub.db.save_session(token, aid)

        # Link to user if they don't have an agent yet
        user_id = user.get("id")  # type: ignore[union-attr]
        if user_id and not user.get("agent_id"):  # type: ignore[union-attr]
            try:
                await hub.db.link_user_agent(UUID(str(user_id)), aid)
            except Exception:
                pass  # non-fatal

        env_text = f"SCHWARMA_AGENT_ID={aid}\nSCHWARMA_AGENT_TOKEN={token}"
        return _json({
            "agent_id": str(aid),
            "token": token,
            "env": {
                "SCHWARMA_AGENT_ID": str(aid),
                "SCHWARMA_AGENT_TOKEN": token,
            },
            "env_text": env_text,
            "usage": {
                "http_api": f"Authorization: Bearer {token}",
                "tcp_station": f"schwarma-bot --host <hub> --port 9741 --token {token}",
                "mcp_config": {
                    "mcpServers": {
                        "schwarma": {
                            "command": "schwarma-mcp",
                            "args": ["--connect", "localhost:9741"],
                            "env": {
                                "SCHWARMA_AGENT_TOKEN": token,
                            },
                        },
                    },
                },
            },
        })
    except Exception as e:
        return _error(str(e), 400)


@route("GET", r"/api/v1/agent/me")
async def _api_agent_me(hub: "SchwarmaHub", params: dict, query: dict, headers: dict) -> HttpResponse:
    """Return the current agent's identity and stats."""
    user, err = await _require_user(hub, headers)
    if err:
        return err
    agent_id = user.get("agent_id")  # type: ignore[union-attr]
    if not agent_id:
        return _error("No agent linked to this account", 404)
    agent = hub.station.exchange._agents.get(UUID(str(agent_id)))
    if not agent:
        return _error("Agent not found in exchange", 404)
    rep = hub.station.exchange.reputation.get(UUID(str(agent_id)))
    skill_info = {}
    if hub.station.exchange._skill_tracker:
        tracker = hub.station.exchange._skill_tracker
        for cap in agent.capabilities:
            rating = tracker.get_rating(UUID(str(agent_id)), cap)
            skill_info[cap.name] = {
                "mu": round(rating.mu, 2),
                "sigma": round(rating.sigma, 2),
                "conservative": round(rating.conservative_rating, 2),
            }
    return _json({
        "agent_id": str(agent_id),
        "name": agent.name,
        "model_tier": agent.model_tier.name,
        "capabilities": [c.name for c in agent.capabilities],
        "reputation": rep,
        "skills": skill_info,
        "is_online": hub.station.exchange.is_agent_online(UUID(str(agent_id))),
    })


@route("POST", r"/api/v1/agent/solve")
async def _api_agent_solve(hub: "SchwarmaHub", params: dict, query: dict, headers: dict) -> HttpResponse:
    """Claim and solve a problem in one request.

    Body JSON: {problem_id, solution_body}
    This is the easiest way for an agent to solve — single HTTP call.
    """
    user, err = await _require_user(hub, headers)
    if err:
        return err
    agent_id = user.get("agent_id")  # type: ignore[union-attr]
    if not agent_id:
        return _error("No agent linked — register first via POST /api/v1/agent/register", 400)
    problem_id = _qs(query, "problem_id")
    solution_body = _qs(query, "solution_body")
    if not problem_id or not solution_body:
        return _error("problem_id and solution_body are required", 400)
    try:
        exchange = hub.station.exchange
        aid = UUID(str(agent_id))
        pid = UUID(problem_id)

        # Claim (idempotent — will succeed if already claimed by this agent)
        try:
            await exchange.claim_problem(aid, pid)
        except Exception:
            pass  # may already be claimed

        # Submit solution
        solution = await exchange.submit_solution(aid, pid, solution_body)
        return _json({
            "solution_id": str(solution.id),
            "problem_id": problem_id,
            "verdict": solution.verdict.name,
        })
    except Exception as e:
        return _error(str(e), 400)


@route("GET", r"/api/v1/agent/work")
async def _api_agent_work(hub: "SchwarmaHub", params: dict, query: dict, headers: dict) -> HttpResponse:
    """Find open problems suitable for this agent.

    Returns problems triaged/filtered for the agent's capabilities and tier.
    """
    user, err = await _require_user(hub, headers)
    if err:
        return err
    agent_id = user.get("agent_id")  # type: ignore[union-attr]
    if not agent_id:
        return _error("No agent linked", 400)
    limit = int(query.get("limit", "10"))
    try:
        exchange = hub.station.exchange
        aid = UUID(str(agent_id))
        problems = exchange.open_problems(limit=limit * 3)
        # Filter to problems this agent could claim
        suitable = []
        for p in problems:
            if aid == p.author_id:
                continue
            if aid in p.claimed_by:
                continue
            if p.min_solver_tier:
                agent_obj = exchange._agents.get(aid)
                if agent_obj and agent_obj.model_tier.value < p.min_solver_tier.value:
                    continue
            suitable.append({
                "id": str(p.id),
                "title": p.title,
                "description": p.description[:500],
                "tags": [t.name for t in p.tags],
                "bounty": p.bounty,
                "priority": p.priority,
            })
            if len(suitable) >= limit:
                break
        return _json({"problems": suitable, "count": len(suitable)})
    except Exception as e:
        return _error(str(e), 400)


# ── OpenAI-compatible proxy ──────────────────────────────────────────────

@route("POST", r"/v1/chat/completions")
async def _openai_chat_completions(hub: "SchwarmaHub", params: dict, query: dict, headers: dict) -> HttpResponse:
    """OpenAI-compatible chat completions endpoint.

    Accepts the standard OpenAI ``/v1/chat/completions`` request format,
    posts the last user message as a Schwarma problem, waits for a solution,
    and returns the result wrapped in the OpenAI response schema.

    This lets users point any OpenAI-compatible client (``openai`` SDK,
    ``litellm``, LangChain, etc.) at ``http://<hub>:8741/v1`` and seamlessly
    offload work to the Schwarma agent swarm.

    Body JSON (subset of OpenAI spec)::

        {
            "model": "schwarma",           # ignored — routed by agent tier
            "messages": [
                {"role": "user", "content": "Fix this bug: ..."}
            ],
            "max_tokens": 4096,            # optional
            "metadata": {                  # optional Schwarma extensions
                "tags": ["BUG"],
                "bounty": 20,
                "timeout": 120
            }
        }

    Returns an OpenAI-shaped response (non-streaming).
    """
    user, err = await _require_user(hub, headers)
    if err:
        return err
    agent_id = user.get("agent_id")  # type: ignore[union-attr]
    if not agent_id:
        return _error("No agent linked — register first via POST /api/v1/agent/register", 400)

    messages = query.get("messages", [])
    if not messages:
        return _error("messages array is required", 400)

    # Extract the last user message as the problem description
    user_messages = [m for m in messages if m.get("role") == "user"]
    if not user_messages:
        return _error("At least one user message is required", 400)
    description = user_messages[-1].get("content", "").strip()
    if not description:
        return _error("Last user message content must not be empty", 400)

    # Build a title from the first line (max 120 chars)
    first_line = description.split("\n")[0][:120]
    title = first_line if len(first_line) > 5 else "Chat completions request"

    # Optional Schwarma metadata
    metadata = query.get("metadata", {})
    tags_raw = metadata.get("tags", [])
    bounty = int(metadata.get("bounty", 10))
    timeout_secs = float(metadata.get("timeout", 120))

    try:
        exchange = hub.station.exchange
        aid = UUID(str(agent_id))

        # Post problem
        from schwarma.problem import ProblemTag
        tags = []
        for t in tags_raw:
            try:
                tags.append(ProblemTag[t.upper()])
            except KeyError:
                pass

        problem = await exchange.post_problem(
            aid, title=title, description=description,
            tags=tags or None, bounty=bounty,
        )

        # Wait for a solution (poll)
        solution_body = None
        deadline = asyncio.get_event_loop().time() + timeout_secs
        poll_interval = 0.5
        while asyncio.get_event_loop().time() < deadline:
            solutions = exchange.solutions_for(problem.id)
            accepted = [s for s in solutions if s.verdict.name == "ACCEPTED"]
            if accepted:
                solution_body = accepted[0].body
                break
            # Any solution at all?
            if solutions:
                solution_body = solutions[0].body
                break
            await asyncio.sleep(poll_interval)
            poll_interval = min(poll_interval * 1.5, 5.0)

        if not solution_body:
            # Return a timeout response (still valid OpenAI shape)
            solution_body = (
                "[Schwarma] No solution received within "
                f"{timeout_secs:.0f}s for problem {problem.id}. "
                "The problem remains open — solutions may arrive later."
            )

        # Format as OpenAI response
        completion_id = f"schwarma-{problem.id}"
        return _json({
            "id": completion_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "schwarma-swarm",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": solution_body,
                },
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": len(description.split()),
                "completion_tokens": len(solution_body.split()),
                "total_tokens": len(description.split()) + len(solution_body.split()),
            },
            "schwarma": {
                "problem_id": str(problem.id),
                "solution_count": len(exchange.solutions_for(problem.id)),
            },
        })
    except Exception as e:
        logger.exception("OpenAI proxy error")
        return _error(str(e), 500)


@route("GET", r"/v1/models")
async def _openai_models(hub: "SchwarmaHub", params: dict, query: dict, headers: dict) -> HttpResponse:
    """OpenAI-compatible models list endpoint.

    Returns the Schwarma model so OpenAI SDK ``client.models.list()`` works.
    """
    return _json({
        "object": "list",
        "data": [{
            "id": "schwarma-swarm",
            "object": "model",
            "created": 1700000000,
            "owned_by": "schwarma",
            "permission": [],
        }],
    })


# ── SSE live events ──────────────────────────────────────────────────────

@route("GET", r"/events/stream")
async def _events_stream(hub: "SchwarmaHub", params: dict, query: dict, headers: dict) -> HttpResponse:
    """Server-Sent Events endpoint.

    This returns a special status (209) that the HTTP server factory
    recognises as "take over the connection for SSE streaming".
    The actual streaming is handled in the server factory loop.
    """
    # Store filter preferences in the response body for the factory to read
    kinds = query.get("kinds", "")  # comma-separated EventKind names
    return 209, "text/event-stream", kinds.encode(), {}


# ── Admin / moderation endpoints ─────────────────────────────────────────

async def _require_admin(hub: "SchwarmaHub", headers: dict) -> tuple[dict | None, HttpResponse | None]:
    """Like _require_user but also checks is_admin."""
    user = await _get_current_user(hub, headers)
    if not user:
        return None, _error("Authentication required", 401)
    if not user.get("is_admin", False):
        return None, _error("Admin access required", 403)
    return user, None


@route("POST", r"/admin/suspend/(?P<agent_id>[0-9a-f\-]{36})")
async def _admin_suspend(hub: "SchwarmaHub", params: dict, query: dict, headers: dict) -> HttpResponse:
    """Suspend an agent.  Admin only."""
    _, err = await _require_admin(hub, headers)
    if err:
        return err
    agent_id = UUID(params["agent_id"])
    try:
        exchange = hub.station.exchange
        exchange.suspend_agent(agent_id)
        await hub.db.set_agent_suspended(agent_id, True)
        return _json({"suspended": True, "agent_id": str(agent_id)})
    except Exception as e:
        return _error(str(e), 400)


@route("POST", r"/admin/unsuspend/(?P<agent_id>[0-9a-f\-]{36})")
async def _admin_unsuspend(hub: "SchwarmaHub", params: dict, query: dict, headers: dict) -> HttpResponse:
    """Unsuspend an agent.  Admin only."""
    _, err = await _require_admin(hub, headers)
    if err:
        return err
    agent_id = UUID(params["agent_id"])
    try:
        exchange = hub.station.exchange
        exchange.unsuspend_agent(agent_id)
        await hub.db.set_agent_suspended(agent_id, False)
        return _json({"suspended": False, "agent_id": str(agent_id)})
    except Exception as e:
        return _error(str(e), 400)


@route("GET", r"/admin/users")
async def _admin_list_users(hub: "SchwarmaHub", params: dict, query: dict, headers: dict) -> HttpResponse:
    """List all registered users.  Admin only."""
    _, err = await _require_admin(hub, headers)
    if err:
        return err
    limit = int(query.get("limit", "100"))
    users = await hub.db.list_users(limit=limit)
    return _json({"users": users, "count": len(users)})


@route("POST", r"/admin/users/(?P<user_id>[0-9a-f\-]{36})/promote")
async def _admin_promote(hub: "SchwarmaHub", params: dict, query: dict, headers: dict) -> HttpResponse:
    """Promote a user to admin.  Admin only."""
    _, err = await _require_admin(hub, headers)
    if err:
        return err
    user_id = UUID(params["user_id"])
    try:
        await hub.db.pool.execute(
            "UPDATE users SET is_admin = TRUE WHERE id = $1", user_id,
        )
        return _json({"promoted": True, "user_id": str(user_id)})
    except Exception as e:
        return _error(str(e), 400)


@route("DELETE", r"/admin/users/(?P<user_id>[0-9a-f\-]{36})/sessions")
async def _admin_clear_sessions(hub: "SchwarmaHub", params: dict, query: dict, headers: dict) -> HttpResponse:
    """Force-clear all sessions for a user.  Admin only."""
    _, err = await _require_admin(hub, headers)
    if err:
        return err
    user_id = UUID(params["user_id"])
    await hub.db.delete_user_sessions(user_id)
    return _json({"cleared": True, "user_id": str(user_id)})


@route("GET", r"/admin/metrics")
async def _admin_metrics(hub: "SchwarmaHub", params: dict, query: dict, headers: dict) -> HttpResponse:
    """Detailed metrics including exchange stats.  Admin only."""
    _, err = await _require_admin(hub, headers)
    if err:
        return err
    m = getattr(hub, "_http_metrics", None)
    db_healthy = await hub.db.health_check()
    exchange_stats = hub.station.exchange.statistics()
    db_stats = await hub.db.stats()
    data: dict[str, Any] = {
        "http": m.snapshot() if m else {},
        "database": {"healthy": db_healthy, **db_stats},
        "exchange": exchange_stats,
    }
    return _json(data)


# ── Auth routes ──────────────────────────────────────────────────────────

@route("GET", r"/auth/google")
async def _auth_google(hub: "SchwarmaHub", params: dict, query: dict, headers: dict) -> HttpResponse:
    """Redirect to Google OAuth consent screen with CSRF state token."""
    from schwarma.hub.auth import google_login_url, is_google_configured, generate_session_token, set_cookie_header
    if not is_google_configured(hub.config):
        return _error("Google OAuth not configured — set SCHWARMA_GOOGLE_CLIENT_ID and SCHWARMA_GOOGLE_CLIENT_SECRET", 503)
    state = generate_session_token()
    url = google_login_url(hub.config, state=state)
    # Set the state in a short-lived cookie so we can verify it on callback
    state_cookie = set_cookie_header("schwarma_oauth_state", state, max_age=600, same_site="Lax")
    return _redirect(url, **{"Set-Cookie": state_cookie})


@route("GET", r"/auth/google/callback")
async def _auth_google_callback(hub: "SchwarmaHub", params: dict, query: dict, headers: dict) -> HttpResponse:
    """Handle Google OAuth callback — verify state, exchange code, create user, set cookie."""
    from schwarma.hub.auth import (
        exchange_code_for_user, generate_session_token,
        is_google_configured, set_cookie_header, SESSION_COOKIE_NAME,
        parse_cookies, clear_cookie_header,
    )
    if not is_google_configured(hub.config):
        return _error("Google OAuth not configured", 503)

    # Verify CSRF state parameter
    cookie_header = headers.get("cookie", "")
    cookies = parse_cookies(cookie_header) if cookie_header else {}
    expected_state = cookies.get("schwarma_oauth_state", "")
    returned_state = query.get("state", "")
    if expected_state and returned_state != expected_state:
        return _error("OAuth state mismatch — possible CSRF attack", 403)

    code = query.get("code")
    if not code:
        error_desc = query.get("error", "missing authorization code")
        return _error(f"OAuth failed: {error_desc}", 400)

    try:
        userinfo = await exchange_code_for_user(hub.config, code)
    except Exception as e:
        logger.exception("Google OAuth token exchange failed")
        return _error(f"OAuth token exchange failed: {e}", 502)

    # Upsert user in database
    user = await hub.db.upsert_user(
        email=userinfo["email"],
        name=userinfo["name"],
        picture_url=userinfo["picture"],
        google_sub=f"google:{userinfo['sub']}",
        auth_provider="google",
        email_verified=True,
    )

    # Auto-promote first user to admin
    if not user.get("is_admin"):
        if await hub.db.user_count() == 1:
            await hub.db.promote_to_admin(user["id"])
            user["is_admin"] = True
            logger.info("Auto-promoted first user %s to admin", user["email"])

    # Create session
    token = generate_session_token()
    await hub.db.create_user_session(token, user["id"])

    logger.info("User logged in: %s (%s)", user["email"], user["id"])

    # Redirect to SPA root with session cookie (secure if TLS enabled)
    secure = hub.config.tls_enabled
    cookie = set_cookie_header(SESSION_COOKIE_NAME, token, secure=secure)
    # New users (no agent yet) go to onboarding hash route in SPA.
    redirect_to = "/#getting-started" if not user.get("agent_id") else "/#dashboard"
    return _redirect(redirect_to, **{"Set-Cookie": cookie})


@route("GET", r"/auth/github")
async def _auth_github(hub: "SchwarmaHub", params: dict, query: dict, headers: dict) -> HttpResponse:
    from schwarma.hub.auth import github_login_url, is_github_configured, generate_session_token, set_cookie_header
    if not is_github_configured(hub.config):
        return _error("GitHub OAuth not configured — set SCHWARMA_GITHUB_CLIENT_ID and SCHWARMA_GITHUB_CLIENT_SECRET", 503)
    state = generate_session_token()
    url = github_login_url(hub.config, state=state)
    state_cookie = set_cookie_header("schwarma_oauth_state", state, max_age=600, same_site="Lax")
    return _redirect(url, **{"Set-Cookie": state_cookie})


@route("GET", r"/auth/github/callback")
async def _auth_github_callback(hub: "SchwarmaHub", params: dict, query: dict, headers: dict) -> HttpResponse:
    from schwarma.hub.auth import (
        exchange_github_code_for_user, generate_session_token, is_github_configured,
        parse_cookies, set_cookie_header, clear_cookie_header, SESSION_COOKIE_NAME,
    )
    if not is_github_configured(hub.config):
        return _error("GitHub OAuth not configured", 503)

    cookie_header = headers.get("cookie", "")
    cookies = parse_cookies(cookie_header) if cookie_header else {}
    expected_state = cookies.get("schwarma_oauth_state", "")
    returned_state = query.get("state", "")
    if expected_state and returned_state != expected_state:
        return _error("OAuth state mismatch — possible CSRF attack", 403)

    code = query.get("code")
    if not code:
        return _error(f"OAuth failed: {query.get('error', 'missing authorization code')}", 400)
    try:
        userinfo = await exchange_github_code_for_user(hub.config, code)
    except Exception as e:
        logger.exception("GitHub OAuth token exchange failed")
        return _error(f"OAuth token exchange failed: {e}", 502)
    if not userinfo.get("email"):
        return _error("GitHub account has no visible email", 400)

    user = await hub.db.upsert_user(
        email=userinfo["email"],
        name=userinfo["name"],
        picture_url=userinfo["picture"],
        google_sub=f"github:{userinfo['sub']}",
        auth_provider="github",
        email_verified=bool(userinfo.get("email_verified", False)),
    )

    # Auto-promote first user to admin
    if not user.get("is_admin"):
        if await hub.db.user_count() == 1:
            await hub.db.promote_to_admin(user["id"])
            user["is_admin"] = True
            logger.info("Auto-promoted first user %s to admin", user["email"])

    token = generate_session_token()
    await hub.db.create_user_session(token, user["id"])
    secure = hub.config.tls_enabled
    cookie = set_cookie_header(SESSION_COOKIE_NAME, token, secure=secure)
    redirect_to = "/#getting-started" if not user.get("agent_id") else "/#dashboard"
    return _redirect(redirect_to, **{"Set-Cookie": cookie})


@route("GET", r"/auth/me")
async def _auth_me(hub: "SchwarmaHub", params: dict, query: dict, headers: dict) -> HttpResponse:
    """Return the current logged-in user from the session cookie."""
    user = await _get_current_user(hub, headers)
    if not user:
        return _json({"authenticated": False}, 401)
    return _json({
        "authenticated": True,
        "user": {
            "id": user["id"],
            "email": user["email"],
            "name": user["name"],
            "picture_url": user["picture_url"],
            "email_verified": user.get("email_verified", False),
            "agent_id": user.get("agent_id"),
            "is_admin": user.get("is_admin", False),
        },
    })


@route("POST", r"/auth/signup")
async def _auth_signup(hub: "SchwarmaHub", params: dict, query: dict, headers: dict) -> HttpResponse:
    """Create a local email/password account and send verification code."""
    from schwarma.hub.auth import send_verification_email
    email = _qs(query, "email").lower()
    password = _qs(query, "password")
    name = _qs(query, "name") or email.split("@")[0]
    if "@" not in email:
        return _error("Valid email is required", 400)
    if len(password) < 8:
        return _error("Password must be at least 8 characters", 400)
    if len(name) > 120:
        return _error("Name is too long", 400)
    try:
        user = await hub.db.create_local_user(email=email, name=name)

        # Auto-promote first user to admin
        if await hub.db.user_count() == 1:
            await hub.db.promote_to_admin(user["id"])
            logger.info("Auto-promoted first user %s to admin", email)

        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
        await hub.db.set_local_credential(
            user_id=user["id"],
            password_hash=base64.b64encode(digest).decode("ascii"),
            password_salt=base64.b64encode(salt).decode("ascii"),
        )
        code = f"{secrets.randbelow(1_000_000):06d}"
        code_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()
        await hub.db.create_email_verification_code(
            user_id=user["id"],
            email=email,
            code_hash=code_hash,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
        )
        sent = await send_verification_email(hub.config, email, code)
        payload: dict[str, Any] = {"signed_up": True, "verification_required": True, "email_sent": sent}
        if not sent:
            # Log code for dev — NEVER send it to the client.
            logger.warning("SMTP not configured — verification code for %s: %s", email, code)
        return _json(payload)
    except Exception as e:
        return _error(str(e), 400)


@route("POST", r"/auth/login")
async def _auth_login(hub: "SchwarmaHub", params: dict, query: dict, headers: dict) -> HttpResponse:
    """Login with local email/password."""
    from schwarma.hub.auth import generate_session_token, set_cookie_header, SESSION_COOKIE_NAME
    email = _qs(query, "email").lower()
    password = _qs(query, "password")
    if not email or not password:
        return _error("email and password are required", 400)
    row = await hub.db.get_local_credential_by_email(email)
    if not row:
        return _error("Invalid email or password", 401)
    try:
        salt = base64.b64decode(str(row["password_salt"]))
        expected = base64.b64decode(str(row["password_hash"]))
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
        if not hmac.compare_digest(actual, expected):
            return _error("Invalid email or password", 401)
        if not row.get("email_verified", False):
            return _error("Email not verified. Use /auth/verify-email first.", 403)
        await hub.db.touch_user_login(row["id"])
        token = generate_session_token()
        await hub.db.create_user_session(token, row["id"])
        secure = hub.config.tls_enabled
        cookie = set_cookie_header(SESSION_COOKIE_NAME, token, secure=secure)
        return _json({"authenticated": True}, 200, **{"Set-Cookie": cookie})
    except Exception:
        return _error("Invalid email or password", 401)


@route("POST", r"/auth/verify-email")
async def _auth_verify_email(hub: "SchwarmaHub", params: dict, query: dict, headers: dict) -> HttpResponse:
    from schwarma.hub.auth import generate_session_token, set_cookie_header, SESSION_COOKIE_NAME
    email = _qs(query, "email").lower()
    code = _qs(query, "code")
    if not email or not code:
        return _error("email and code are required", 400)
    user = await hub.db.verify_email_code(email=email, code=code)
    if not user:
        return _error("Invalid or expired verification code", 400)
    await hub.db.mark_email_verified(user["id"])
    token = generate_session_token()
    await hub.db.create_user_session(token, user["id"])
    secure = hub.config.tls_enabled
    cookie = set_cookie_header(SESSION_COOKIE_NAME, token, secure=secure)
    return _json({"verified": True, "authenticated": True}, 200, **{"Set-Cookie": cookie})


@route("POST", r"/auth/logout")
async def _auth_logout(hub: "SchwarmaHub", params: dict, query: dict, headers: dict) -> HttpResponse:
    """Delete the session and clear the cookie."""
    from schwarma.hub.auth import parse_cookies, clear_cookie_header, SESSION_COOKIE_NAME
    cookie_header = headers.get("cookie", "")
    if cookie_header:
        cookies = parse_cookies(cookie_header)
        token = cookies.get(SESSION_COOKIE_NAME)
        if token:
            await hub.db.delete_user_session(token)
    clear = clear_cookie_header(SESSION_COOKIE_NAME)
    return _json({"logged_out": True}, **{"Set-Cookie": clear})


@route("GET", r"/auth/logout")
async def _auth_logout_get(hub: "SchwarmaHub", params: dict, query: dict, headers: dict) -> HttpResponse:
    """GET logout — clears session cookie, redirects to /."""
    from schwarma.hub.auth import parse_cookies, clear_cookie_header, SESSION_COOKIE_NAME
    cookie_header = headers.get("cookie", "")
    if cookie_header:
        cookies = parse_cookies(cookie_header)
        token = cookies.get(SESSION_COOKIE_NAME)
        if token:
            await hub.db.delete_user_session(token)
    clear = clear_cookie_header(SESSION_COOKIE_NAME)
    return _redirect("/", **{"Set-Cookie": clear})


@route("GET", r"/auth/status")
async def _auth_status(hub: "SchwarmaHub", params: dict, query: dict, headers: dict) -> HttpResponse:
    """Check if Google OAuth is configured (public, no auth needed)."""
    from schwarma.hub.auth import is_google_configured, is_github_configured
    return _json({
        "google_configured": is_google_configured(hub.config),
        "github_configured": is_github_configured(hub.config),
        "local_auth_enabled": True,
        "login_url": "/auth/google" if is_google_configured(hub.config) else None,
    })


# ── Metrics / observability route ────────────────────────────────────────

@route("GET", r"/metrics")
async def _metrics_route(hub: "SchwarmaHub", params: dict, query: dict, headers: dict) -> HttpResponse:
    """Return HTTP request metrics and rate limiter state.

    Supports two output formats:

    * **JSON** (default or ``Accept: application/json``): nested dict
      with ``http`` and ``rate_limiter`` keys.
    * **Prometheus text exposition** (``Accept`` contains
      ``text/plain``, ``text/plain; version=0.0.4``, or
      ``application/openmetrics-text``): standard ``# HELP`` /
      ``# TYPE`` / metric lines that Prometheus can scrape natively.
    """
    accept = headers.get("accept", "")
    want_prom = any(t in accept for t in (
        "text/plain", "application/openmetrics-text",
    ))

    m = getattr(hub, "_http_metrics", None)
    rl = getattr(hub, "_http_rate_limiter", None)

    if want_prom:
        lines: list[str] = []
        # HTTP metrics
        if m:
            snap = m.snapshot()
            lines.append("# HELP schwarma_http_requests_total Total HTTP requests handled.")
            lines.append("# TYPE schwarma_http_requests_total counter")
            lines.append(f"schwarma_http_requests_total {snap['total_requests']}")
            lines.append("# HELP schwarma_http_latency_avg_ms Average request latency in ms.")
            lines.append("# TYPE schwarma_http_latency_avg_ms gauge")
            lines.append(f"schwarma_http_latency_avg_ms {snap['avg_latency_ms']}")
            lines.append("# HELP schwarma_http_responses_total HTTP responses by status code.")
            lines.append("# TYPE schwarma_http_responses_total counter")
            for code, count in sorted(snap.get("status_counts", {}).items()):
                lines.append(f'schwarma_http_responses_total{{status="{code}"}} {count}')
        # Rate limiter
        if rl:
            lines.append("# HELP schwarma_rate_limiter_tracked_ips Number of tracked IP addresses.")
            lines.append("# TYPE schwarma_rate_limiter_tracked_ips gauge")
            lines.append(f"schwarma_rate_limiter_tracked_ips {len(rl._hits)}")
        # Exchange stats (best-effort — may not be available)
        try:
            stats = hub.station.exchange.statistics()
            for key in ("total_agents", "active_agents", "suspended_agents",
                        "total_problems", "total_solutions", "total_reviews",
                        "archive_total", "archive_active"):
                val = stats.get(key, 0)
                lines.append(f"# HELP schwarma_{key} Exchange metric: {key.replace('_', ' ')}.")
                lines.append(f"# TYPE schwarma_{key} gauge")
                lines.append(f"schwarma_{key} {val}")
            lines.append("# HELP schwarma_acceptance_rate Solution acceptance rate (0-1).")
            lines.append("# TYPE schwarma_acceptance_rate gauge")
            lines.append(f"schwarma_acceptance_rate {stats.get('acceptance_rate', 0)}")
        except Exception:
            pass
        body = ("\n".join(lines) + "\n").encode()
        return 200, "text/plain; version=0.0.4; charset=utf-8", body, {}

    # Default: JSON
    data: dict[str, Any] = {}
    if m:
        data["http"] = m.snapshot()
    if rl:
        data["rate_limiter"] = {"tracked_ips": len(rl._hits)}
    return _json(data)


_STATIC_DIR = __import__("pathlib").Path(__file__).resolve().parent / "static"


@route("GET", r"/")
async def _index(hub: "SchwarmaHub", params: dict, query: dict, headers: dict) -> HttpResponse:
    """Serve the SPA index.html."""
    index_path = _STATIC_DIR / "index.html"
    try:
        body = index_path.read_bytes()
        return 200, "text/html; charset=utf-8", body, {}
    except FileNotFoundError:
        return _json({"error": "Dashboard not installed — missing static/index.html"}, 404)


@route("GET", r"/dashboard(?:/.*)?")
async def _dashboard(hub: "SchwarmaHub", params: dict, query: dict, headers: dict) -> HttpResponse:
    """Protected dashboard — redirect to / (login page) if no valid session."""
    user = await _get_current_user(hub, headers)
    if not user:
        return _redirect("/")
    index_path = _STATIC_DIR / "index.html"
    try:
        body = index_path.read_bytes()
        return 200, "text/html; charset=utf-8", body, {}
    except FileNotFoundError:
        return _json({"error": "Dashboard not installed — missing static/index.html"}, 404)


@route("GET", r"/file\.svg")
async def _file_svg(hub: "SchwarmaHub", params: dict, query: dict, headers: dict) -> HttpResponse:
    """Serve uploaded brand mark SVG."""
    svg_path = _STATIC_DIR / "file.svg"
    try:
        body = svg_path.read_bytes()
        return 200, "image/svg+xml", body, {}
    except FileNotFoundError:
        return _not_found("Logo not found")

async def _get_current_user(hub: "SchwarmaHub", headers: dict[str, str]) -> dict | None:
    """Extract user from session cookie or Authorization: Bearer token.

    Supports two auth modes:
      1. Browser session cookie (``schwarma_session``)
      2. API key via ``Authorization: Bearer <agent_token>`` — resolves the
         agent's linked user record so the same permission model applies.
    """
    from schwarma.hub.auth import parse_cookies, SESSION_COOKIE_NAME

    # 1. Try session cookie first (browser flow)
    cookie_header = headers.get("cookie", "")
    if cookie_header:
        cookies = parse_cookies(cookie_header)
        token = cookies.get(SESSION_COOKIE_NAME)
        if token:
            user = await hub.db.get_user_session(token)
            if user:
                return user

    # 2. Try Authorization: Bearer <token> (API / agent flow)
    auth_header = headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        bearer_token = auth_header[7:].strip()
        if bearer_token:
            # First check if it's a user session token
            user = await hub.db.get_user_session(bearer_token)
            if user:
                return user
            # Then check if it's an agent API token
            agent_id = await hub.db.get_agent_for_session(bearer_token)
            if agent_id:
                # Find the user linked to this agent
                user = await hub.db.get_user_by_agent(agent_id)
                if user:
                    return user
                # No linked user — return a synthetic user dict for API-only agents
                return {
                    "id": str(agent_id),
                    "email": "",
                    "name": f"Agent {str(agent_id)[:8]}",
                    "picture_url": "",
                    "agent_id": agent_id,
                    "is_admin": False,
                    "email_verified": False,
                }

    return None


# ── HTTP server factory ──────────────────────────────────────────────────

# ── Per-IP rate limiter ──────────────────────────────────────────────────

class _IPRateLimiter:
    """Sliding-window per-IP request rate limiter."""

    def __init__(self, max_requests: int = 100, window: int = 60) -> None:
        self.max_requests = max_requests
        self.window = window
        self._hits: dict[str, list[float]] = defaultdict(list)

    def allow(self, ip: str) -> bool:
        if self.max_requests <= 0:
            return True
        now = time.monotonic()
        window_start = now - self.window
        bucket = self._hits[ip]
        # Prune old entries
        self._hits[ip] = bucket = [t for t in bucket if t > window_start]
        if len(bucket) >= self.max_requests:
            return False
        bucket.append(now)
        return True

    def prune(self) -> None:
        """Remove stale IP entries to prevent memory growth."""
        now = time.monotonic()
        cutoff = now - self.window * 2
        stale = [ip for ip, ts in self._hits.items() if not ts or ts[-1] < cutoff]
        for ip in stale:
            del self._hits[ip]


# ── Request metrics collector ────────────────────────────────────────────

class _Metrics:
    """Simple in-process HTTP request metrics."""

    def __init__(self) -> None:
        self.total_requests: int = 0
        self.status_counts: dict[int, int] = defaultdict(int)
        self.latency_sum: float = 0.0
        self.latency_count: int = 0

    def record(self, status: int, latency: float) -> None:
        self.total_requests += 1
        self.status_counts[status] += 1
        self.latency_sum += latency
        self.latency_count += 1

    def snapshot(self) -> dict[str, Any]:
        avg = (self.latency_sum / self.latency_count) if self.latency_count else 0
        return {
            "total_requests": self.total_requests,
            "status_counts": dict(self.status_counts),
            "avg_latency_ms": round(avg * 1000, 2),
        }


# ── HTTP server factory ──────────────────────────────────────────────────

def create_http_server(hub: "SchwarmaHub"):
    """Return an asyncio client handler for the HTTP API.

    This is a bare-bones HTTP/1.1 server — no framework needed.
    Includes: CORS origin enforcement, per-IP rate limiting, request
    size limits, keep-alive, request metrics, and CSRF checks.
    """
    config = hub.config
    rate_limiter = _IPRateLimiter(config.http_rate_limit, config.http_rate_window)
    # Stricter limiter for auth endpoints (login/signup/verify) to prevent brute-force.
    auth_rate_limiter = _IPRateLimiter(max_requests=10, window=60)
    _prune_counter = 0  # prune rate limiter tables every N requests
    metrics = _Metrics()
    max_line = 8192  # max request-line / header-line length
    max_headers = 100
    max_body = config.max_request_size

    # Pre-compute allowed origins set
    allowed_origins_raw = config.allowed_origins.strip()
    if allowed_origins_raw.lower() == "auto":
        # Safe default: only allow requests from localhost on the configured port.
        allowed_origins_set = {
            f"http://localhost:{config.http_port}",
            f"http://127.0.0.1:{config.http_port}",
        }
        allow_all_origins = False
        logger.info("CORS: auto-detected allowed origins %s", allowed_origins_set)
    elif allowed_origins_raw == "*":
        allow_all_origins = True
        allowed_origins_set: set[str] = set()
        logger.warning("CORS: allow-all (*) — CSRF protection disabled.  Do not use in production.")
    else:
        allow_all_origins = False
        allowed_origins_set = {o.strip() for o in allowed_origins_raw.split(",") if o.strip()}

    def _cors_headers(origin: str | None) -> str:
        effective_origin = "*"
        if not allow_all_origins:
            if origin and origin in allowed_origins_set:
                effective_origin = origin
            elif allowed_origins_set:
                effective_origin = next(iter(allowed_origins_set))
        elif origin:
            effective_origin = origin
        return (
            f"Access-Control-Allow-Origin: {effective_origin}\r\n"
            "Access-Control-Allow-Methods: GET, POST, DELETE, OPTIONS\r\n"
            "Access-Control-Allow-Headers: Content-Type, Authorization\r\n"
            "Access-Control-Allow-Credentials: true\r\n"
            "Vary: Origin\r\n"
        )

    def _check_csrf(method: str, origin: str | None, referer: str | None) -> bool:
        """Return True if the request passes CSRF origin checking.

        Safe methods (GET, HEAD, OPTIONS) are always allowed.
        State-changing methods require a matching Origin or Referer header
        when specific allowed_origins are configured.
        """
        if method in ("GET", "HEAD", "OPTIONS"):
            return True
        if allow_all_origins:
            return True  # dev mode — no CSRF enforcement
        check_value = origin or referer or ""
        if not check_value:
            return False
        # Check if origin matches any allowed origin
        for ao in allowed_origins_set:
            if check_value == ao or check_value.startswith(ao + "/"):
                return True
        return False

    async def _read_line_limited(reader: asyncio.StreamReader) -> bytes:
        data = await reader.readline()
        if len(data) > max_line:
            raise ValueError("Request line too long")
        return data

    async def handle_client(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        addr_info = writer.get_extra_info("peername")
        client_ip = addr_info[0] if addr_info else "unknown"
        keep_alive = True

        try:
            while keep_alive:
                # ── Rate limit check ──
                if not rate_limiter.allow(client_ip):
                    _write_error(writer, 429, "Too Many Requests", _cors_headers(None))
                    await writer.drain()
                    break

                start_time = time.monotonic()

                # ── Read request line ──
                try:
                    request_line_raw = await asyncio.wait_for(
                        _read_line_limited(reader), timeout=30.0,
                    )
                except (asyncio.TimeoutError, ValueError):
                    break
                request_line = request_line_raw.decode("utf-8", errors="replace").strip()
                if not request_line:
                    break

                parts = request_line.split(" ", 2)
                if len(parts) < 2:
                    break

                method = parts[0].upper()
                raw_path = parts[1]

                # ── Read headers (with limits) ──
                req_headers: dict[str, str] = {}
                header_count = 0
                total_header_bytes = 0
                while True:
                    try:
                        header_line = await asyncio.wait_for(
                            _read_line_limited(reader), timeout=10.0,
                        )
                    except (asyncio.TimeoutError, ValueError):
                        keep_alive = False
                        break
                    if header_line in (b"\r\n", b"\n", b""):
                        break
                    header_count += 1
                    total_header_bytes += len(header_line)
                    if header_count > max_headers or total_header_bytes > max_line * 50:
                        _write_error(writer, 431, "Request Header Fields Too Large", _cors_headers(None))
                        await writer.drain()
                        keep_alive = False
                        break
                    decoded = header_line.decode("utf-8", errors="replace").strip()
                    if ":" in decoded:
                        hk, hv = decoded.split(":", 1)
                        req_headers[hk.strip().lower()] = hv.strip()

                if not keep_alive:
                    break

                # ── Read body if Content-Length present ──
                request_body = b""
                content_length = int(req_headers.get("content-length", "0"))
                if content_length > max_body:
                    _write_error(writer, 413, "Payload Too Large", _cors_headers(None))
                    await writer.drain()
                    break
                if content_length > 0:
                    request_body = await asyncio.wait_for(
                        reader.readexactly(content_length), timeout=30.0,
                    )

                # ── Parse path ──
                path, query = _parse_path(raw_path)

                # ── Inject body into query for POST JSON payloads ──
                if method == "POST" and request_body:
                    ct = req_headers.get("content-type", "")
                    if "application/json" in ct:
                        try:
                            body_data = json.loads(request_body)
                            if isinstance(body_data, dict):
                                # Preserve original types (lists, dicts, ints)
                                # so route handlers can work with structured data.
                                query.update(body_data)
                                query["_body"] = request_body.decode("utf-8", errors="replace")
                        except json.JSONDecodeError:
                            pass

                origin = req_headers.get("origin")
                referer = req_headers.get("referer")
                cors = _cors_headers(origin)

                # ── CORS preflight ──
                if method == "OPTIONS":
                    response = f"HTTP/1.1 204 No Content\r\n{cors}Content-Length: 0\r\nConnection: keep-alive\r\n\r\n"
                    writer.write(response.encode())
                    await writer.drain()
                    continue

                # ── CSRF check ──
                if not _check_csrf(method, origin, referer):
                    _write_error(writer, 403, "Forbidden: origin not allowed", cors)
                    await writer.drain()
                    latency = time.monotonic() - start_time
                    metrics.record(403, latency)
                    continue

                # ── Auth-specific rate limit (brute-force protection) ──
                if path.startswith("/auth/") and method == "POST":
                    if not auth_rate_limiter.allow(client_ip):
                        _write_error(writer, 429, "Too many auth attempts — try again later", cors)
                        await writer.drain()
                        latency = time.monotonic() - start_time
                        metrics.record(429, latency)
                        continue

                # ── Periodic rate limiter prune ──
                nonlocal _prune_counter
                _prune_counter += 1
                if _prune_counter >= 500:
                    _prune_counter = 0
                    rate_limiter.prune()
                    auth_rate_limiter.prune()

                # ── Dispatch to route handler ──
                status_code, content_type, body, extra_headers = await _dispatch(
                    hub, method, path, query, req_headers,
                )

                # ── SSE streaming (status 209 is our internal signal) ──
                if status_code == 209:
                    await _handle_sse(hub, writer, body, cors)
                    keep_alive = False
                    continue

                # ── Build response ──
                latency = time.monotonic() - start_time
                metrics.record(status_code, latency)

                status_text = _STATUS_TEXT.get(status_code, "Unknown")
                extra_hdr_str = ""
                for hk, hv in extra_headers.items():
                    extra_hdr_str += f"{hk}: {hv}\r\n"

                # Determine connection behavior
                conn_header = req_headers.get("connection", "").lower()
                if conn_header == "close":
                    keep_alive = False
                connection_value = "keep-alive" if keep_alive else "close"

                response_header = (
                    f"HTTP/1.1 {status_code} {status_text}\r\n"
                    f"Content-Type: {content_type}\r\n"
                    f"Content-Length: {len(body)}\r\n"
                    f"{cors}"
                    f"{extra_hdr_str}"
                    f"X-Request-Time-Ms: {round(latency * 1000, 1)}\r\n"
                    f"Connection: {connection_value}\r\n"
                    f"\r\n"
                )
                writer.write(response_header.encode() + body)
                await writer.drain()

        except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
            pass
        except Exception:
            logger.exception("HTTP handler error for %s", client_ip)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    # Attach metrics to hub for /metrics endpoint access
    hub._http_metrics = metrics  # type: ignore[attr-defined]
    hub._http_rate_limiter = rate_limiter  # type: ignore[attr-defined]

    return handle_client


def _write_error(writer: asyncio.StreamWriter, status: int, msg: str, cors: str) -> None:
    """Write a minimal HTTP error response."""
    body = json.dumps({"error": msg}).encode()
    text = _STATUS_TEXT.get(status, "Error")
    header = (
        f"HTTP/1.1 {status} {text}\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"{cors}"
        f"Connection: close\r\n"
        f"\r\n"
    )
    writer.write(header.encode() + body)


async def _handle_sse(
    hub: "SchwarmaHub",
    writer: asyncio.StreamWriter,
    filter_body: bytes,
    cors: str,
) -> None:
    """Stream Exchange events to the client via Server-Sent Events.

    The connection stays open until the client disconnects.
    """
    from schwarma.events import Event, EventKind

    # Parse optional event kind filter
    kinds_str = filter_body.decode("utf-8", errors="replace").strip()
    allowed_kinds: set[str] | None = None
    if kinds_str:
        allowed_kinds = {k.strip().upper() for k in kinds_str.split(",") if k.strip()}

    # Send SSE headers
    header = (
        f"HTTP/1.1 200 OK\r\n"
        f"Content-Type: text/event-stream\r\n"
        f"Cache-Control: no-cache\r\n"
        f"{cors}"
        f"Connection: keep-alive\r\n"
        f"\r\n"
    )
    writer.write(header.encode())
    await writer.drain()

    # Event queue for this subscriber
    queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=256)

    async def _enqueue(event: Event) -> None:
        if allowed_kinds and event.kind.name not in allowed_kinds:
            return
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            pass  # drop if client can't keep up

    # Subscribe to the exchange event bus
    hub.station.exchange.bus.subscribe_all(_enqueue)

    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=30.0)
                data = json.dumps({
                    "kind": event.kind.name,
                    "source_agent_id": str(event.source_agent_id) if event.source_agent_id else None,
                    "problem_id": str(event.problem_id) if event.problem_id else None,
                    "solution_id": str(event.solution_id) if event.solution_id else None,
                    "payload": event.payload,
                    "timestamp": event.timestamp.isoformat() if hasattr(event, "timestamp") else None,
                }, cls=_Encoder)
                sse_msg = f"event: {event.kind.name}\ndata: {data}\n\n"
                writer.write(sse_msg.encode())
                await writer.drain()
            except asyncio.TimeoutError:
                # Send keepalive comment
                writer.write(b": keepalive\n\n")
                await writer.drain()
    except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
        pass
    finally:
        # Unsubscribe
        try:
            hub.station.exchange.bus._global_handlers.discard(_enqueue)
        except Exception:
            pass


async def _dispatch(
    hub: "SchwarmaHub", method: str, path: str, query: dict[str, str],
    headers: dict[str, str] | None = None,
) -> HttpResponse:
    """Match a request to a route and execute the handler."""
    headers = headers or {}
    for route_method, pattern, handler in _ROUTES:
        if method != route_method:
            continue
        m = pattern.match(path)
        if m:
            try:
                return await handler(hub, m.groupdict(), query, headers)
            except Exception as e:
                logger.exception("Route handler error: %s %s", method, path)
                return _error(str(e), 500)

    return _not_found(f"No route for {method} {path}")


def _parse_path(raw: str) -> tuple[str, dict[str, str]]:
    """Split '/foo?bar=baz&x=1' into ('/foo', {'bar': 'baz', 'x': '1'})."""
    if "?" in raw:
        path, qs = raw.split("?", 1)
        query: dict[str, str] = {}
        # Keep literal '+' characters intact (OAuth codes may contain '+').
        for pair in qs.split("&"):
            if "=" in pair:
                key, value = pair.split("=", 1)
            else:
                key, value = pair, ""
            query[urllib.parse.unquote(key)] = urllib.parse.unquote(value)
        return path, query
    return raw, {}


_STATUS_TEXT = {
    200: "OK",
    204: "No Content",
    301: "Moved Permanently",
    302: "Found",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    413: "Payload Too Large",
    429: "Too Many Requests",
    431: "Request Header Fields Too Large",
    500: "Internal Server Error",
    502: "Bad Gateway",
    503: "Service Unavailable",
}
