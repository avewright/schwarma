"""
Sync — bidirectional sync between the in-memory Exchange and PostgreSQL.

On startup:  load agents, problems, solutions, reviews, reputation, archive
             from the database into the Exchange (rehydrate).

At runtime:  subscribe to the Exchange EventBus and write every mutation
             to PostgreSQL in real time (write-through).

This means the in-memory Exchange is always the "hot" data path (fast),
and PostgreSQL is the durable store that survives restarts.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from schwarma.agent import Agent, AgentCapability, ModelTier
from schwarma.archive import ArchiveEntry, ArchiveStatus, ReviewSnapshot
from schwarma.events import Event, EventKind
from schwarma.exchange import Exchange
from schwarma.hub.database import Database
from schwarma.problem import Problem, ProblemStatus, ProblemTag
from schwarma.reputation import LedgerEntry, ReputationEvent
from schwarma.review import Review, ReviewType, ReviewVerdict
from schwarma.solution import Solution, SolutionVerdict
from schwarma.station import SchwarmaStation, _external_solver
from schwarma.trust import Sensitivity

logger = logging.getLogger(__name__)


class ExchangeSync:
    """Keeps Exchange ↔ PostgreSQL in sync.

    Usage::

        sync = ExchangeSync(station, db)
        await sync.rehydrate()   # load from DB on startup
        sync.attach()            # wire EventBus → DB writes
    """

    def __init__(self, station: SchwarmaStation, db: Database) -> None:
        self.station = station
        self.exchange: Exchange = station.exchange
        self.db = db
        self._attached = False

    # ── Startup: DB → Exchange ───────────────────────────────────────

    async def rehydrate(self) -> dict[str, int]:
        """Load all data from PostgreSQL into the Exchange.

        Returns a summary dict of how many objects were loaded.
        """
        counts: dict[str, int] = {}

        # 1. Agents
        agent_rows = await self.db.list_agents()
        for row in agent_rows:
            agent = Agent(
                name=row["name"],
                solver=_external_solver,
                capabilities={AgentCapability[c] for c in row["capabilities"]},
                model_tier=ModelTier[row["model_tier"]],
                id=row["id"],
                metadata=row.get("metadata") or {},
            )
            agent._total_solved = row.get("total_solved", 0)
            agent._total_reviewed = row.get("total_reviewed", 0)
            # Register into exchange internals (bypass normal registration flow)
            self.exchange._agents[agent.id] = agent
            if row.get("is_suspended"):
                self.exchange._suspended.add(agent.id)
        counts["agents"] = len(agent_rows)

        # 2. Reputation balances
        for agent_id in self.exchange._agents:
            balance = await self.db.get_reputation(agent_id)
            # Set the ledger balance directly
            self.exchange.ledger._balances[agent_id] = balance
        counts["reputation_loaded"] = len(self.exchange._agents)

        # 3. Problems
        problem_rows, _ = await self.db.list_problems(limit=100_000)
        for row in problem_rows:
            p = _problem_from_row(row)
            self.exchange._problems[p.id] = p
        counts["problems"] = len(problem_rows)

        # 4. Solutions
        for pid in list(self.exchange._problems):
            sol_rows = await self.db.solutions_for_problem(pid)
            for row in sol_rows:
                s = _solution_from_row(row)
                self.exchange._solutions[s.id] = s
        counts["solutions"] = len(self.exchange._solutions)

        # 5. Reviews
        for sid in list(self.exchange._solutions):
            rev_rows = await self.db.reviews_for_solution(sid)
            for row in rev_rows:
                r = _review_from_row(row)
                self.exchange._reviews[r.id] = r
        counts["reviews"] = len(self.exchange._reviews)

        # 6. Archive
        archive_rows = await self.db.search_archive(limit=100_000)
        for row in archive_rows:
            entry = _archive_entry_from_row(row)
            self.exchange.archive._entries[entry.id] = entry
        counts["archive"] = len(archive_rows)

        # 7. Sessions
        sessions = await self.db.load_all_sessions()
        for token, agent_id in sessions.items():
            self.station._sessions[token] = agent_id
        counts["sessions"] = len(sessions)

        logger.info("Rehydrated from database: %s", counts)
        return counts

    # ── Runtime: Exchange → DB ───────────────────────────────────────

    def attach(self) -> None:
        """Subscribe to the Exchange EventBus for write-through persistence."""
        self.exchange.bus.subscribe_all(self._on_event)
        self._attached = True
        logger.info("EventBus → Database sync attached")

    async def _on_event(self, event: Event) -> None:
        """Handle an exchange event by persisting the relevant state.

        The event log write and the domain-specific handler run inside a
        single database transaction so either both succeed or neither does.
        """
        try:
            # Events that touch multiple tables use an explicit transaction
            # to guarantee atomicity (e.g. solution verdict + problem update
            # + archive write).  Single-table events get the same treatment
            # for consistency and WAL ordering.
            handler = _EVENT_HANDLERS.get(event.kind)
            async with self.db.transaction() as conn:
                # Always log the event first (WAL-style)
                await conn.execute(
                    """
                    INSERT INTO event_log (kind, source_agent_id, target_agent_id,
                                           problem_id, solution_id, review_id, payload)
                    VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
                    """,
                    event.kind.name,
                    event.source_agent_id,
                    event.target_agent_id,
                    event.problem_id,
                    event.solution_id,
                    event.review_id,
                    json.dumps(event.payload or {}),
                )

                # Route to specific handler (receives the transactional conn)
                if handler:
                    await handler(self, event, conn)

        except Exception:
            logger.exception("Failed to sync event %s to database", event.kind.name)

    # ── Event-specific sync handlers ─────────────────────────────────
    # All handlers receive the transactional ``conn`` from ``_on_event``
    # so that the event-log write and every domain write are atomic.

    async def _sync_agent_registered(self, event: Event, conn: Any) -> None:
        agent_id = event.source_agent_id
        if agent_id and agent_id in self.exchange._agents:
            agent = self.exchange._agents[agent_id]
            await self.db.upsert_agent(
                id=agent.id,
                name=agent.name,
                model_tier=agent.model_tier.name,
                capabilities=[c.name for c in agent.capabilities],
                metadata=agent.metadata,
                conn=conn,
            )
            # Persist the session token if available
            for token, aid in self.station._sessions.items():
                if aid == agent_id:
                    await self.db.save_session(token, agent_id, conn=conn)
                    break

    async def _sync_agent_suspended(self, event: Event, conn: Any) -> None:
        agent_id = event.target_agent_id or event.source_agent_id
        if agent_id:
            is_suspended = agent_id in self.exchange._suspended
            await self.db.set_agent_suspended(agent_id, is_suspended, conn=conn)

    async def _sync_problem_posted(self, event: Event, conn: Any) -> None:
        pid = event.problem_id
        if pid and pid in self.exchange._problems:
            p = self.exchange._problems[pid]
            await self.db.upsert_problem(p.to_dict(), conn=conn)

    async def _sync_problem_update(self, event: Event, conn: Any) -> None:
        """Generic problem state change (claimed, solved, closed, etc.)."""
        pid = event.problem_id
        if pid and pid in self.exchange._problems:
            p = self.exchange._problems[pid]
            await self.db.upsert_problem(p.to_dict(), conn=conn)

    async def _sync_solution_submitted(self, event: Event, conn: Any) -> None:
        sid = event.solution_id
        if sid and sid in self.exchange._solutions:
            s = self.exchange._solutions[sid]
            await self.db.upsert_solution(s.to_dict(), conn=conn)
            # Also update the parent problem
            await self._sync_problem_update(event, conn)

    async def _sync_solution_verdict(self, event: Event, conn: Any) -> None:
        sid = event.solution_id
        if sid and sid in self.exchange._solutions:
            s = self.exchange._solutions[sid]
            await self.db.upsert_solution(s.to_dict(), conn=conn)
        # Problem state may have changed too
        await self._sync_problem_update(event, conn)

    async def _sync_review_submitted(self, event: Event, conn: Any) -> None:
        rid = event.review_id
        if rid and rid in self.exchange._reviews:
            r = self.exchange._reviews[rid]
            await self.db.upsert_review(r.to_dict(), conn=conn)
            # Update solution's review_ids
            sid = r.solution_id
            if sid in self.exchange._solutions:
                s = self.exchange._solutions[sid]
                await self.db.upsert_solution(s.to_dict(), conn=conn)

    async def _sync_reputation_changed(self, event: Event, conn: Any) -> None:
        agent_id = event.target_agent_id or event.source_agent_id
        if not agent_id:
            return
        payload = event.payload
        from uuid import uuid4
        await self.db.record_reputation_event(
            id=uuid4(),
            agent_id=agent_id,
            event_type=payload.get("event", "UNKNOWN"),
            delta=payload.get("delta", 0),
            reason=payload.get("reason", ""),
            related_id=event.problem_id or event.solution_id,
            conn=conn,
        )

    async def _sync_archive(self, event: Event, conn: Any) -> None:
        """When a solution is accepted, the exchange archives it."""
        pid = event.problem_id
        if pid:
            entry = self.exchange.archive.get_by_problem(pid)
            if entry:
                await self.db.upsert_archive_entry(entry.to_dict(), conn=conn)

    async def _sync_solution_accepted(self, event: Event, conn: Any) -> None:
        """Combined handler for SOLUTION_ACCEPTED — update verdict AND archive."""
        await self._sync_solution_verdict(event, conn)
        await self._sync_archive(event, conn)

    # ── Full snapshot (safety net) ───────────────────────────────────

    async def full_snapshot(self) -> None:
        """Write the entire Exchange state to the database.

        Called periodically as a safety net in addition to event-level writes.
        """
        logger.info("Writing full snapshot to database...")

        # Agents
        for agent in self.exchange._agents.values():
            await self.db.upsert_agent(
                id=agent.id,
                name=agent.name,
                model_tier=agent.model_tier.name,
                capabilities=[c.name for c in agent.capabilities],
                metadata=agent.metadata,
                is_suspended=agent.id in self.exchange._suspended,
                total_solved=agent._total_solved,
                total_reviewed=agent._total_reviewed,
            )

        # Problems
        for p in self.exchange._problems.values():
            await self.db.upsert_problem(p.to_dict())

        # Solutions
        for s in self.exchange._solutions.values():
            await self.db.upsert_solution(s.to_dict())

        # Reviews
        for r in self.exchange._reviews.values():
            await self.db.upsert_review(r.to_dict())

        # Archive
        for entry in self.exchange.archive._entries.values():
            await self.db.upsert_archive_entry(entry.to_dict())

        # Sessions
        for token, agent_id in self.station._sessions.items():
            await self.db.save_session(token, agent_id)

        logger.info("Full snapshot complete")


# ── Event routing table ──────────────────────────────────────────────────

_EVENT_HANDLERS: dict[EventKind, Any] = {
    EventKind.AGENT_REGISTERED:         ExchangeSync._sync_agent_registered,
    EventKind.AGENT_SUSPENDED:          ExchangeSync._sync_agent_suspended,
    EventKind.PROBLEM_POSTED:           ExchangeSync._sync_problem_posted,
    EventKind.PROBLEM_CLAIMED:          ExchangeSync._sync_problem_update,
    EventKind.PROBLEM_SOLVED:           ExchangeSync._sync_problem_update,
    EventKind.PROBLEM_CLOSED:           ExchangeSync._sync_solution_verdict,
    EventKind.PROBLEM_EXPIRED:          ExchangeSync._sync_problem_update,
    EventKind.PROBLEM_ESCALATED:        ExchangeSync._sync_problem_update,
    EventKind.SOLUTION_SUBMITTED:       ExchangeSync._sync_solution_submitted,
    EventKind.SOLUTION_ACCEPTED:        ExchangeSync._sync_solution_accepted,
    EventKind.SOLUTION_REJECTED:        ExchangeSync._sync_solution_verdict,
    EventKind.SOLUTION_REVISION_REQUESTED: ExchangeSync._sync_solution_verdict,
    EventKind.REVIEW_SUBMITTED:         ExchangeSync._sync_review_submitted,
    EventKind.REVIEW_APPROVED:          ExchangeSync._sync_review_submitted,
    EventKind.REVIEW_REJECTED:          ExchangeSync._sync_review_submitted,
    EventKind.REPUTATION_CHANGED:       ExchangeSync._sync_reputation_changed,
}


# ── Row → domain object helpers ──────────────────────────────────────────

def _problem_from_row(row: dict) -> Problem:
    """Convert a database row to a Problem dataclass."""
    p = Problem(
        title=row["title"],
        description=row["description"],
        author_id=row["author_id"],
        tags={ProblemTag[t] for t in (row.get("tags") or ["GENERAL"])},
        bounty=row.get("bounty", 10),
        sensitivity=Sensitivity[row.get("sensitivity", "INTERNAL")],
        min_solver_tier=ModelTier[row["min_solver_tier"]] if row.get("min_solver_tier") else None,
        max_solvers=row.get("max_solvers", 1),
        deadline=row.get("deadline"),
        context=row.get("context") or {},
    )
    p.id = row["id"]
    p.status = ProblemStatus[row["status"]]
    p.priority = row.get("priority", 0)
    p.created_at = row.get("created_at", datetime.now(timezone.utc))
    p.claimed_by = list(row.get("claimed_by") or [])
    p.solution_ids = list(row.get("solution_ids") or [])
    p.accepted_solution_id = row.get("accepted_solution_id")
    p.parent_id = row.get("parent_id")
    p.sub_problem_ids = list(row.get("sub_problem_ids") or [])
    p.depends_on = list(row.get("depends_on") or [])
    return p


def _solution_from_row(row: dict) -> Solution:
    """Convert a database row to a Solution dataclass."""
    from schwarma.solution import FixPackage, OutcomeRecord, RevisionRound

    s = Solution(
        problem_id=row["problem_id"],
        author_id=row["author_id"],
        body=row["body"],
    )
    s.id = row["id"]
    s.verdict = SolutionVerdict[row["verdict"]]
    s.created_at = row.get("created_at", datetime.now(timezone.utc))
    s.review_ids = list(row.get("review_ids") or [])
    s.metadata = row.get("metadata") or {}

    if row.get("fix_package"):
        s.fix_package = FixPackage.from_dict(row["fix_package"])
    if row.get("outcome"):
        s.outcome = OutcomeRecord.from_dict(row["outcome"])
    if row.get("revision_history"):
        s.revision_history = [
            RevisionRound(
                round_number=rr["round_number"],
                reviewer_feedback=rr["reviewer_feedback"],
                reviewer_id=UUID(rr["reviewer_id"]) if isinstance(rr["reviewer_id"], str) else rr["reviewer_id"],
                revised_body=rr.get("revised_body", ""),
            )
            for rr in row["revision_history"]
        ]
    return s


def _review_from_row(row: dict) -> Review:
    """Convert a database row to a Review dataclass."""
    r = Review(
        solution_id=row["solution_id"],
        reviewer_id=row["reviewer_id"],
        review_type=ReviewType[row["review_type"]],
        verdict=ReviewVerdict[row["verdict"]],
        body=row.get("body", ""),
        confidence=row.get("confidence", 1.0),
        metadata=row.get("metadata") or {},
    )
    r.id = row["id"]
    r.created_at = row.get("created_at", datetime.now(timezone.utc))
    return r


def _archive_entry_from_row(row: dict) -> ArchiveEntry:
    """Convert a database row to an ArchiveEntry dataclass."""
    entry = ArchiveEntry(
        id=row["id"],
        problem_id=row["problem_id"],
        solution_id=row["solution_id"],
        problem_title=row.get("problem_title", ""),
        problem_description=row.get("problem_description", ""),
        tags={ProblemTag[t] for t in (row.get("tags") or [])},
        sensitivity=Sensitivity[row.get("sensitivity", "INTERNAL")],
        solution_body=row.get("solution_body", ""),
        solver_id=row["solver_id"],
        solver_tier=ModelTier[row.get("solver_tier", "STANDARD")],
        solver_reputation=row.get("solver_reputation", 0),
        status=ArchiveStatus[row.get("status", "ACTIVE")],
        metadata=row.get("metadata") or {},
    )
    entry.created_at = row.get("created_at", datetime.now(timezone.utc))
    # Reviews are stored as JSONB array
    reviews_data = row.get("reviews") or []
    entry.reviews = [
        ReviewSnapshot(
            reviewer_id=UUID(rd["reviewer_id"]) if isinstance(rd["reviewer_id"], str) else rd["reviewer_id"],
            verdict=ReviewVerdict[rd["verdict"]],
            review_type=rd.get("review_type", "CORRECTNESS"),
            confidence=rd.get("confidence", 1.0),
            body=rd.get("body", ""),
        )
        for rd in reviews_data
    ]
    return entry
