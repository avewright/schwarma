"""Persistence — save and load Exchange snapshots to/from disk.

Provides JSON-based file persistence for the Exchange state.
Uses the existing ``Exchange.snapshot()`` for serialisation and
provides a full ``restore()`` that rebuilds agents, problems,
solutions, reviews, reputation, and archive.

Usage::

    from schwarma.persistence import save_snapshot, load_snapshot

    # Save
    save_snapshot(exchange, "state.json")

    # Load into a fresh exchange
    exchange2 = load_snapshot("state.json")
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from schwarma.agent import Agent, AgentCapability, ModelTier
from schwarma.archive import ArchiveEntry
from schwarma.exchange import Exchange, ExchangeConfig
from schwarma.problem import Problem, ProblemStatus, ProblemTag
from schwarma.reputation import ReputationEvent
from schwarma.review import Review, ReviewType, ReviewVerdict
from schwarma.solution import Solution, SolutionVerdict

logger = logging.getLogger(__name__)

# ── Custom JSON encoder ─────────────────────────────────────────────────


class _SchwarmaEncoder(json.JSONEncoder):
    """Handles UUID, datetime, Enum, and set serialisation."""

    def default(self, obj: Any) -> Any:
        if isinstance(obj, UUID):
            return str(obj)
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, set):
            return sorted(str(v) for v in obj)
        if hasattr(obj, "name") and hasattr(obj, "value"):
            # Enum
            return obj.name
        return super().default(obj)


# ── Public API ───────────────────────────────────────────────────────────


def save_snapshot(
    exchange: Exchange,
    path: str | Path,
    *,
    indent: int = 2,
) -> Path:
    """Serialise the exchange state to a JSON file.

    Returns the resolved path that was written.
    """
    path = Path(path)
    data = exchange.snapshot()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, cls=_SchwarmaEncoder, indent=indent),
        encoding="utf-8",
    )
    logger.info("Snapshot saved to %s", path)
    return path


def load_snapshot(
    path: str | Path,
    config: ExchangeConfig | None = None,
) -> Exchange:
    """Create a new Exchange and restore state from a JSON snapshot.

    Agent solver callbacks are **not** restored (they're runtime-only).
    After loading, callers should re-register solvers for active agents.

    Returns a fully populated Exchange.
    """
    path = Path(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    exchange = Exchange(config or ExchangeConfig())
    _restore_full(exchange, raw)
    logger.info("Snapshot loaded from %s", path)
    return exchange


def snapshot_to_dict(exchange: Exchange) -> dict[str, Any]:
    """Return the snapshot dict (convenience alias for ``exchange.snapshot()``)."""
    return exchange.snapshot()


def restore_from_dict(
    data: dict[str, Any],
    config: ExchangeConfig | None = None,
) -> Exchange:
    """Create a new Exchange and restore from an in-memory dict."""
    exchange = Exchange(config or ExchangeConfig())
    _restore_full(exchange, data)
    return exchange


# ── Internal restore logic ───────────────────────────────────────────────


async def _noop_solver(desc: str, ctx: dict) -> str:
    raise RuntimeError("Solver not restored — re-register a solver callback")


def _restore_full(exchange: Exchange, data: dict[str, Any]) -> None:
    """Populate *exchange* from a snapshot dict."""
    _restore_agents(exchange, data)
    _restore_problems(exchange, data)
    _restore_solutions(exchange, data)
    _restore_reviews(exchange, data)
    _restore_reputation(exchange, data)
    _restore_suspended(exchange, data)
    _restore_archive(exchange, data)


def _restore_agents(exchange: Exchange, data: dict[str, Any]) -> None:
    for _aid_str, info in data.get("agents", {}).items():
        aid = UUID(info["id"])
        if aid in exchange._agents:
            continue

        caps = set()
        for c in info.get("capabilities", ["GENERAL"]):
            try:
                caps.add(AgentCapability[c])
            except KeyError:
                caps.add(AgentCapability.GENERAL)

        tier = ModelTier.STANDARD
        tier_str = info.get("model_tier")
        if tier_str:
            try:
                tier = ModelTier[tier_str]
            except KeyError:
                pass

        agent = Agent(
            name=info["name"],
            capabilities=caps,
            model_tier=tier,
            solver=_noop_solver,
        )
        agent.id = aid
        agent._total_solved = info.get("total_solved", 0)
        agent._total_reviewed = info.get("total_reviewed", 0)

        # Restore active problem set
        for pid_str in info.get("active_problem_ids", []):
            agent._active_problem_ids.add(UUID(pid_str))

        exchange._agents[aid] = agent


def _restore_problems(exchange: Exchange, data: dict[str, Any]) -> None:
    for pid_str, pdata in data.get("problems", {}).items():
        pid = UUID(pid_str)
        if pid in exchange._problems:
            continue

        try:
            problem = Problem.from_dict(pdata)
        except Exception:
            # Fallback: minimal construction
            problem = Problem(
                title=pdata.get("title", ""),
                description=pdata.get("description", ""),
                author_id=UUID(pdata["author_id"]),
            )
            problem.id = pid
            problem.status = ProblemStatus[pdata.get("status", "OPEN")]

        exchange._problems[problem.id] = problem


def _restore_solutions(exchange: Exchange, data: dict[str, Any]) -> None:
    for sid_str, sdata in data.get("solutions", {}).items():
        sid = UUID(sid_str)
        if sid in exchange._solutions:
            continue

        try:
            solution = Solution.from_dict(sdata)
        except Exception:
            # Fallback: minimal construction
            solution = Solution(
                problem_id=UUID(sdata["problem_id"]),
                author_id=UUID(sdata["author_id"]),
                body=sdata.get("body", ""),
            )
            solution.id = sid

        exchange._solutions[solution.id] = solution


def _restore_reviews(exchange: Exchange, data: dict[str, Any]) -> None:
    for rid_str, rdata in data.get("reviews", {}).items():
        rid = UUID(rid_str)
        if rid in exchange._reviews:
            continue

        if hasattr(Review, "from_dict"):
            try:
                review = Review.from_dict(rdata)
                exchange._reviews[review.id] = review
                continue
            except Exception:
                pass

        review = Review(
            solution_id=UUID(rdata["solution_id"]),
            reviewer_id=UUID(rdata["reviewer_id"]),
            verdict=ReviewVerdict[rdata["verdict"]],
            body=rdata.get("body", ""),
            review_type=ReviewType[rdata.get("review_type", "CORRECTNESS")],
        )
        review.id = rid

        confidence = rdata.get("confidence")
        if confidence is not None:
            review.confidence = confidence

        created = rdata.get("created_at")
        if created:
            review.created_at = datetime.fromisoformat(created)

        exchange._reviews[rid] = review


def _restore_reputation(exchange: Exchange, data: dict[str, Any]) -> None:
    """Restore reputation balances by crediting the delta."""
    for aid_str, balance in data.get("reputation_balances", {}).items():
        aid = UUID(aid_str)
        current = exchange.ledger.balance(aid)
        delta = balance - current
        if delta != 0:
            exchange.ledger.record(
                aid,
                ReputationEvent.BONUS if delta > 0 else ReputationEvent.PENALTY,
                delta=delta,
                reason="snapshot_restore",
            )


def _restore_suspended(exchange: Exchange, data: dict[str, Any]) -> None:
    for aid_str in data.get("suspended", []):
        exchange._suspended.add(UUID(aid_str))


def _restore_archive(exchange: Exchange, data: dict[str, Any]) -> None:
    """Restore archive entries from snapshot."""
    for entry_data in data.get("archive_entries", []):
        entry_id = UUID(entry_data["id"])
        if entry_id in exchange.archive._entries:
            continue

        entry = ArchiveEntry(
            problem_title=entry_data.get("problem_title", ""),
            problem_description=entry_data.get("problem_description", ""),
            solution_body=entry_data.get("solution_body", ""),
            solver_id=UUID(entry_data["solver_id"]),
        )
        entry.id = entry_id
        entry.problem_id = UUID(entry_data.get("problem_id", str(entry_id)))
        entry.solution_id = UUID(entry_data.get("solution_id", str(entry_id)))

        # Restore tags
        tags = set()
        for t in entry_data.get("tags", []):
            try:
                tags.add(ProblemTag[t])
            except KeyError:
                pass
        entry.tags = tags

        exchange.archive._entries[entry.id] = entry
