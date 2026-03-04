"""
Database — async PostgreSQL connection pool and query helpers.

Uses ``asyncpg`` for non-blocking access.  All write operations are
called from the :mod:`sync` module in response to Exchange events.

The ``Database`` class owns the connection pool and exposes typed
helper methods for each table.  It also handles auto-migration
(running ``schema.sql`` on first connect).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import asyncpg  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)


class Database:
    """Async PostgreSQL connection pool + query helpers."""

    def __init__(self, dsn: str, *, min_size: int = 2, max_size: int = 10) -> None:
        self._dsn = dsn
        self._min = min_size
        self._max = max_size
        self._pool: asyncpg.Pool | None = None

    # ── Lifecycle ────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Create the connection pool and run migrations."""
        self._pool = await asyncpg.create_pool(
            self._dsn, min_size=self._min, max_size=self._max,
        )
        await self._migrate()
        logger.info("Database connected: %s (pool %d–%d)", self._dsn, self._min, self._max)

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None
            logger.info("Database connection closed")

    async def health_check(self) -> bool:
        """Check if the database connection is alive."""
        try:
            if self._pool is None:
                return False
            await self._pool.fetchval("SELECT 1")
            return True
        except Exception:
            logger.warning("Database health check failed")
            return False

    async def reconnect(self) -> None:
        """Close and recreate the connection pool."""
        logger.warning("Attempting database reconnection...")
        try:
            if self._pool:
                await self._pool.close()
                self._pool = None
        except Exception:
            pass
        self._pool = await asyncpg.create_pool(
            self._dsn, min_size=self._min, max_size=self._max,
        )
        await self._migrate()
        logger.info("Database reconnected successfully")

    async def cleanup_expired_sessions(self) -> int:
        """Delete expired user sessions. Returns count of deleted rows."""
        result = await self.pool.execute(
            "DELETE FROM user_sessions WHERE expires_at < now()"
        )
        # asyncpg returns 'DELETE N'
        count = int(result.split()[-1]) if result else 0
        if count > 0:
            logger.info("Cleaned up %d expired user sessions", count)
        return count

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("Database not connected — call connect() first")
        return self._pool

    async def _migrate(self) -> None:
        """Run versioned migrations.

        Maintains a ``schema_migrations`` table to track which numbered
        migration files have already been applied.  Only new migrations
        are executed, and each runs inside its own transaction.
        """
        async with self.pool.acquire() as conn:
            # Ensure the tracking table exists (idempotent)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version  INTEGER PRIMARY KEY,
                    name     TEXT NOT NULL DEFAULT '',
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)

            # Determine already-applied versions
            rows = await conn.fetch("SELECT version FROM schema_migrations ORDER BY version")
            applied: set[int] = {r["version"] for r in rows}

            # Discover migration files on disk
            migrations_dir = Path(__file__).parent / "migrations"
            if not migrations_dir.is_dir():
                # Fallback: run legacy schema.sql for fresh installs without
                # the migrations directory (e.g. editable dev installs).
                schema_path = Path(__file__).parent / "schema.sql"
                if schema_path.exists() and not applied:
                    sql = schema_path.read_text(encoding="utf-8")
                    await conn.execute(sql)
                    await conn.execute(
                        "INSERT INTO schema_migrations (version, name) VALUES ($1, $2)",
                        0, "legacy_schema_sql",
                    )
                logger.info("Database schema applied (legacy mode)")
                return

            migration_files: list[tuple[int, str, Path]] = []
            for f in sorted(migrations_dir.iterdir()):
                if not f.suffix == ".sql":
                    continue
                # Expected format: 001_description.sql
                parts = f.stem.split("_", 1)
                try:
                    version = int(parts[0])
                except (ValueError, IndexError):
                    continue
                migration_files.append((version, f.stem, f))

            # Apply new migrations in order
            new_count = 0
            for version, name, path in sorted(migration_files):
                if version in applied:
                    continue
                sql = path.read_text(encoding="utf-8")
                async with conn.transaction():
                    await conn.execute(sql)
                    await conn.execute(
                        "INSERT INTO schema_migrations (version, name) VALUES ($1, $2)",
                        version, name,
                    )
                new_count += 1
                logger.info("Applied migration %03d: %s", version, name)

        if new_count:
            logger.info("Database migrations complete — %d new migration(s) applied", new_count)
        else:
            logger.info("Database schema up to date")

    # ── Transaction helper ───────────────────────────────────────────

    def transaction(self):
        """Return an async context manager that provides a transactional connection.

        Usage::

            async with db.transaction() as conn:
                await conn.execute(...)
                await conn.execute(...)
            # auto-commit on exit, auto-rollback on exception
        """
        return _Transaction(self.pool)

    @staticmethod
    def _target(conn: Any | None, pool: "asyncpg.Pool") -> Any:
        """Return *conn* when a transactional connection is provided,
        otherwise fall back to the connection *pool*."""
        return conn if conn is not None else pool

    # ── Agents ───────────────────────────────────────────────────────

    async def upsert_agent(
        self,
        *,
        id: UUID,
        name: str,
        model_tier: str,
        capabilities: list[str],
        metadata: dict | None = None,
        is_suspended: bool = False,
        total_solved: int = 0,
        total_reviewed: int = 0,
        conn: Any | None = None,
    ) -> None:
        target = self._target(conn, self.pool)
        await target.execute(
            """
            INSERT INTO agents (id, name, model_tier, capabilities, metadata,
                                is_suspended, total_solved, total_reviewed)
            VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8)
            ON CONFLICT (id) DO UPDATE SET
                name = EXCLUDED.name,
                model_tier = EXCLUDED.model_tier,
                capabilities = EXCLUDED.capabilities,
                metadata = EXCLUDED.metadata,
                is_suspended = EXCLUDED.is_suspended,
                total_solved = EXCLUDED.total_solved,
                total_reviewed = EXCLUDED.total_reviewed,
                updated_at = now()
            """,
            id, name, model_tier, capabilities,
            json.dumps(metadata or {}),
            is_suspended, total_solved, total_reviewed,
        )

    async def get_agent(self, agent_id: UUID) -> dict | None:
        row = await self.pool.fetchrow("SELECT * FROM agents WHERE id = $1", agent_id)
        return dict(row) if row else None

    async def list_agents(self) -> list[dict]:
        rows = await self.pool.fetch("SELECT * FROM agents ORDER BY created_at")
        return [dict(r) for r in rows]

    async def set_agent_suspended(self, agent_id: UUID, suspended: bool, *, conn: Any | None = None) -> None:
        target = self._target(conn, self.pool)
        await target.execute(
            "UPDATE agents SET is_suspended = $2, updated_at = now() WHERE id = $1",
            agent_id, suspended,
        )

    # ── Problems ─────────────────────────────────────────────────────

    async def upsert_problem(self, data: dict[str, Any], *, conn: Any | None = None) -> None:
        """Upsert a problem from its ``to_dict()`` representation."""
        target = self._target(conn, self.pool)
        await target.execute(
            """
            INSERT INTO problems (
                id, title, description, author_id, status, tags, priority,
                bounty, sensitivity, min_solver_tier, max_solvers, deadline,
                claimed_by, solution_ids, accepted_solution_id, context,
                failure_report, parent_id, sub_problem_ids, depends_on, created_at
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                $13, $14, $15, $16::jsonb, $17::jsonb, $18, $19, $20, $21
            )
            ON CONFLICT (id) DO UPDATE SET
                status = EXCLUDED.status,
                claimed_by = EXCLUDED.claimed_by,
                solution_ids = EXCLUDED.solution_ids,
                accepted_solution_id = EXCLUDED.accepted_solution_id,
                priority = EXCLUDED.priority,
                sub_problem_ids = EXCLUDED.sub_problem_ids,
                depends_on = EXCLUDED.depends_on,
                updated_at = now()
            """,
            UUID(data["id"]),
            data["title"],
            data["description"],
            UUID(data["author_id"]),
            data["status"],
            data.get("tags", ["GENERAL"]),
            data.get("priority", 0),
            data.get("bounty", 10),
            data.get("sensitivity", "INTERNAL"),
            data.get("min_solver_tier"),
            data.get("max_solvers", 1),
            _parse_ts(data.get("deadline")),
            [UUID(u) for u in data.get("claimed_by", [])],
            [UUID(u) for u in data.get("solution_ids", [])],
            UUID(data["accepted_solution_id"]) if data.get("accepted_solution_id") else None,
            json.dumps(data.get("context", {})),
            json.dumps(data["failure_report"]) if data.get("failure_report") else None,
            UUID(data["parent_id"]) if data.get("parent_id") else None,
            [UUID(u) for u in data.get("sub_problem_ids", [])],
            [UUID(u) for u in data.get("depends_on", [])],
            _parse_ts(data.get("created_at")) or datetime.now(timezone.utc),
        )

    async def get_problem(self, problem_id: UUID) -> dict | None:
        row = await self.pool.fetchrow("SELECT * FROM problems WHERE id = $1", problem_id)
        return dict(row) if row else None

    async def list_problems(
        self, *, status: str | None = None, limit: int = 100, offset: int = 0,
        cursor: str | None = None, tag: str | None = None,
    ) -> tuple[list[dict], str | None]:
        """List problems with optional cursor-based pagination.

        If *cursor* is provided, it should be an ISO timestamp from a previous
        ``next_cursor`` return value.  Returns ``(rows, next_cursor)`` where
        ``next_cursor`` is ``None`` when there are no more results.
        """
        params: list[Any] = []
        conditions: list[str] = []
        idx = 1

        if status:
            conditions.append(f"status = ${idx}")
            params.append(status)
            idx += 1

        if tag:
            conditions.append(f"${idx} = ANY(tags)")
            params.append(tag.upper())
            idx += 1

        if cursor:
            conditions.append(f"created_at < ${idx}")
            params.append(_parse_ts(cursor) or datetime.now(timezone.utc))
            idx += 1

        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""

        # Fetch one extra row to detect if there's a next page
        params.append(limit + 1)
        sql = f"SELECT * FROM problems{where} ORDER BY created_at DESC LIMIT ${idx}"
        rows = await self.pool.fetch(sql, *params)

        results = [dict(r) for r in rows[:limit]]
        next_cursor: str | None = None
        if len(rows) > limit:
            last = rows[limit - 1]
            next_cursor = last["created_at"].isoformat() if last.get("created_at") else None

        return results, next_cursor

    # ── Solutions ────────────────────────────────────────────────────

    async def upsert_solution(self, data: dict[str, Any], *, conn: Any | None = None) -> None:
        """Upsert a solution from its ``to_dict()`` representation."""
        target = self._target(conn, self.pool)
        await target.execute(
            """
            INSERT INTO solutions (
                id, problem_id, author_id, body, verdict,
                fix_package, outcome, revision_history,
                review_ids, metadata, created_at
            ) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb, $8::jsonb, $9, $10::jsonb, $11)
            ON CONFLICT (id) DO UPDATE SET
                verdict = EXCLUDED.verdict,
                body = EXCLUDED.body,
                outcome = EXCLUDED.outcome,
                revision_history = EXCLUDED.revision_history,
                review_ids = EXCLUDED.review_ids,
                updated_at = now()
            """,
            UUID(data["id"]),
            UUID(data["problem_id"]),
            UUID(data["author_id"]),
            data["body"],
            data["verdict"],
            json.dumps(data.get("fix_package")) if data.get("fix_package") else None,
            json.dumps(data.get("outcome")) if data.get("outcome") else None,
            json.dumps(data.get("revision_history", [])),
            [UUID(u) for u in data.get("review_ids", [])],
            json.dumps(data.get("metadata", {})),
            _parse_ts(data.get("created_at")) or datetime.now(timezone.utc),
        )

    async def get_solution(self, solution_id: UUID) -> dict | None:
        row = await self.pool.fetchrow("SELECT * FROM solutions WHERE id = $1", solution_id)
        return dict(row) if row else None

    async def solutions_for_problem(self, problem_id: UUID) -> list[dict]:
        rows = await self.pool.fetch(
            "SELECT * FROM solutions WHERE problem_id = $1 ORDER BY created_at",
            problem_id,
        )
        return [dict(r) for r in rows]

    # ── Reviews ──────────────────────────────────────────────────────

    async def upsert_review(self, data: dict[str, Any], *, conn: Any | None = None) -> None:
        """Upsert a review from its ``to_dict()`` representation."""
        target = self._target(conn, self.pool)
        await target.execute(
            """
            INSERT INTO reviews (
                id, solution_id, reviewer_id, review_type, verdict,
                body, confidence, metadata, created_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9)
            ON CONFLICT (id) DO UPDATE SET
                verdict = EXCLUDED.verdict,
                body = EXCLUDED.body,
                confidence = EXCLUDED.confidence
            """,
            UUID(data["id"]),
            UUID(data["solution_id"]),
            UUID(data["reviewer_id"]),
            data["review_type"],
            data["verdict"],
            data.get("body", ""),
            data.get("confidence", 1.0),
            json.dumps(data.get("metadata", {})),
            _parse_ts(data.get("created_at")) or datetime.now(timezone.utc),
        )

    async def reviews_for_solution(self, solution_id: UUID) -> list[dict]:
        rows = await self.pool.fetch(
            "SELECT * FROM reviews WHERE solution_id = $1 ORDER BY created_at",
            solution_id,
        )
        return [dict(r) for r in rows]

    # ── Reputation ───────────────────────────────────────────────────

    async def record_reputation_event(
        self,
        *,
        id: UUID,
        agent_id: UUID,
        event_type: str,
        delta: int,
        reason: str = "",
        related_id: UUID | None = None,
        conn: Any | None = None,
    ) -> None:
        async def _run(c):
            await c.execute(
                """
                INSERT INTO reputation_events (id, agent_id, event_type, delta, reason, related_id)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (id) DO NOTHING
                """,
                id, agent_id, event_type, delta, reason, related_id,
            )
            await c.execute(
                """
                INSERT INTO reputation_balances (agent_id, balance, updated_at)
                VALUES ($1, 50 + $2, now())
                ON CONFLICT (agent_id) DO UPDATE SET
                    balance = reputation_balances.balance + $2,
                    updated_at = now()
                """,
                agent_id, delta,
            )

        if conn is not None:
            # Caller already owns the transaction
            await _run(conn)
        else:
            # Standalone call — open our own transaction
            async with self.pool.acquire() as c:
                async with c.transaction():
                    await _run(c)

    async def get_reputation(self, agent_id: UUID) -> int:
        row = await self.pool.fetchrow(
            "SELECT balance FROM reputation_balances WHERE agent_id = $1",
            agent_id,
        )
        return row["balance"] if row else 50

    async def reputation_history(self, agent_id: UUID, limit: int = 50) -> list[dict]:
        rows = await self.pool.fetch(
            "SELECT * FROM reputation_events WHERE agent_id = $1 ORDER BY created_at DESC LIMIT $2",
            agent_id, limit,
        )
        return [dict(r) for r in rows]

    async def reputation_leaderboard(
        self,
        limit: int = 20,
        period: str | None = None,
        capability: str | None = None,
    ) -> list[dict]:
        """Return reputation leaderboard.

        ``period`` — 'weekly', 'monthly', or None for all-time.
        ``capability`` — filter to agents with this capability (e.g. 'CODE_GENERATION').
        """
        # Time-windowed leaderboard: sum reputation deltas in the period.
        if period in ("weekly", "monthly"):
            interval = "7 days" if period == "weekly" else "30 days"
            if capability:
                rows = await self.pool.fetch(
                    f"""
                    SELECT re.agent_id, a.name, a.model_tier,
                           COALESCE(SUM(re.delta), 0) AS balance,
                           a.total_solved, a.total_reviewed
                    FROM reputation_events re
                    JOIN agents a ON a.id = re.agent_id
                    WHERE re.created_at >= now() - interval '{interval}'
                      AND $2 = ANY(a.capabilities)
                    GROUP BY re.agent_id, a.name, a.model_tier, a.total_solved, a.total_reviewed
                    ORDER BY balance DESC
                    LIMIT $1
                    """,
                    limit, capability.upper(),
                )
            else:
                rows = await self.pool.fetch(
                    f"""
                    SELECT re.agent_id, a.name, a.model_tier,
                           COALESCE(SUM(re.delta), 0) AS balance,
                           a.total_solved, a.total_reviewed
                    FROM reputation_events re
                    JOIN agents a ON a.id = re.agent_id
                    WHERE re.created_at >= now() - interval '{interval}'
                    GROUP BY re.agent_id, a.name, a.model_tier, a.total_solved, a.total_reviewed
                    ORDER BY balance DESC
                    LIMIT $1
                    """,
                    limit,
                )
        else:
            # All-time: use materialized balances.
            if capability:
                rows = await self.pool.fetch(
                    """
                    SELECT rb.agent_id, a.name, a.model_tier, rb.balance,
                           a.total_solved, a.total_reviewed
                    FROM reputation_balances rb
                    JOIN agents a ON a.id = rb.agent_id
                    WHERE $2 = ANY(a.capabilities)
                    ORDER BY rb.balance DESC
                    LIMIT $1
                    """,
                    limit, capability.upper(),
                )
            else:
                rows = await self.pool.fetch(
                    """
                    SELECT rb.agent_id, a.name, a.model_tier, rb.balance,
                           a.total_solved, a.total_reviewed
                    FROM reputation_balances rb
                    JOIN agents a ON a.id = rb.agent_id
                    ORDER BY rb.balance DESC
                    LIMIT $1
                    """,
                    limit,
                )
        return [dict(r) for r in rows]

    # ── Archive ──────────────────────────────────────────────────────

    async def upsert_archive_entry(self, data: dict[str, Any], *, conn: Any | None = None) -> None:
        """Upsert an archive entry from its ``to_dict()`` representation."""
        target = self._target(conn, self.pool)
        await target.execute(
            """
            INSERT INTO archive_entries (
                id, problem_id, solution_id, problem_title, problem_description,
                tags, sensitivity, solution_body, solver_id, solver_tier,
                solver_reputation, reviews, status, metadata, ttl_seconds, created_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb, $13, $14::jsonb, $15, $16)
            ON CONFLICT (id) DO UPDATE SET
                status = EXCLUDED.status,
                problem_description = EXCLUDED.problem_description,
                solution_body = EXCLUDED.solution_body,
                reviews = EXCLUDED.reviews,
                metadata = EXCLUDED.metadata,
                tombstoned_at = CASE WHEN EXCLUDED.status = 'TOMBSTONED' THEN now() ELSE NULL END
            """,
            UUID(data["id"]),
            UUID(data["problem_id"]),
            UUID(data["solution_id"]),
            data.get("problem_title", ""),
            data.get("problem_description", ""),
            data.get("tags", []),
            data.get("sensitivity", "INTERNAL"),
            data.get("solution_body", ""),
            UUID(data["solver_id"]),
            data.get("solver_tier", "STANDARD"),
            data.get("solver_reputation", 0),
            json.dumps(data.get("reviews", [])),
            data.get("status", "ACTIVE"),
            json.dumps(data.get("metadata", {})),
            data.get("ttl_seconds"),
            _parse_ts(data.get("created_at")) or datetime.now(timezone.utc),
        )

    async def search_archive(
        self,
        *,
        tags: list[str] | None = None,
        keywords: str | None = None,
        status: str = "ACTIVE",
        limit: int = 20,
    ) -> list[dict]:
        conditions = ["status = $1"]
        params: list[Any] = [status]
        idx = 2

        if tags:
            conditions.append(f"tags && ${idx}")
            params.append(tags)
            idx += 1

        if keywords:
            conditions.append(
                f"(problem_title ILIKE ${idx} OR problem_description ILIKE ${idx})"
            )
            params.append(f"%{keywords}%")
            idx += 1

        where = " AND ".join(conditions)
        params.append(limit)
        sql = f"SELECT * FROM archive_entries WHERE {where} ORDER BY created_at DESC LIMIT ${idx}"
        rows = await self.pool.fetch(sql, *params)
        return [dict(r) for r in rows]

    # ── Event log ────────────────────────────────────────────────────

    async def log_event(
        self,
        *,
        kind: str,
        source_agent_id: UUID | None = None,
        target_agent_id: UUID | None = None,
        problem_id: UUID | None = None,
        solution_id: UUID | None = None,
        review_id: UUID | None = None,
        payload: dict | None = None,
    ) -> None:
        await self.pool.execute(
            """
            INSERT INTO event_log (kind, source_agent_id, target_agent_id,
                                    problem_id, solution_id, review_id, payload)
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
            """,
            kind, source_agent_id, target_agent_id,
            problem_id, solution_id, review_id,
            json.dumps(payload or {}),
        )

    async def recent_events(self, limit: int = 50) -> list[dict]:
        rows = await self.pool.fetch(
            "SELECT * FROM event_log ORDER BY created_at DESC LIMIT $1", limit,
        )
        return [dict(r) for r in rows]

    # ── Sessions ─────────────────────────────────────────────────────

    async def save_session(
        self, token: str, agent_id: UUID, *,
        conn: Any | None = None,
        expires_at: datetime | None = None,
    ) -> None:
        target = self._target(conn, self.pool)
        await target.execute(
            """
            INSERT INTO sessions (token, agent_id, expires_at)
            VALUES ($1, $2, $3)
            ON CONFLICT (token) DO UPDATE SET last_seen = now()
            """,
            token, agent_id, expires_at,
        )

    async def get_session_agent(self, token: str) -> UUID | None:
        row = await self.pool.fetchrow(
            """SELECT agent_id FROM sessions
               WHERE token = $1
                 AND (expires_at IS NULL OR expires_at > now())""",
            token,
        )
        return row["agent_id"] if row else None

    # Alias for HTTP API bearer-token auth
    get_agent_for_session = get_session_agent

    async def rotate_session(self, old_token: str, new_token: str, *, expires_at: datetime | None = None) -> UUID | None:
        """Rotate an API key: invalidate *old_token* and create *new_token*.

        Returns the agent_id on success, or None if the old token was invalid.
        """
        async with self.pool.acquire() as c:
            async with c.transaction():
                row = await c.fetchrow(
                    """SELECT agent_id FROM sessions
                       WHERE token = $1
                         AND (expires_at IS NULL OR expires_at > now())""",
                    old_token,
                )
                if not row:
                    return None
                agent_id = row["agent_id"]
                # Invalidate old
                await c.execute("DELETE FROM sessions WHERE token = $1", old_token)
                # Issue new
                await c.execute(
                    """INSERT INTO sessions (token, agent_id, expires_at, rotated_from)
                       VALUES ($1, $2, $3, $4)""",
                    new_token, agent_id, expires_at, old_token,
                )
                return agent_id

    async def load_all_sessions(self) -> dict[str, UUID]:
        rows = await self.pool.fetch(
            """SELECT token, agent_id FROM sessions
               WHERE expires_at IS NULL OR expires_at > now()"""
        )
        return {r["token"]: r["agent_id"] for r in rows}

    async def delete_agent_sessions(self, agent_id: UUID) -> None:
        """Delete all API sessions for a given agent."""
        await self.pool.execute("DELETE FROM sessions WHERE agent_id = $1", agent_id)

    async def get_user_by_agent(self, agent_id: UUID) -> dict | None:
        """Return the user row linked to the given agent_id."""
        row = await self.pool.fetchrow(
            "SELECT * FROM users WHERE agent_id = $1", agent_id,
        )
        return dict(row) if row else None

    # ── Stats ────────────────────────────────────────────────────────

    async def stats(self) -> dict[str, Any]:
        """Aggregate statistics from the database."""
        async with self.pool.acquire() as conn:
            agents = await conn.fetchval("SELECT count(*) FROM agents")
            problems = await conn.fetchval("SELECT count(*) FROM problems")
            open_problems = await conn.fetchval(
                "SELECT count(*) FROM problems WHERE status = 'OPEN'"
            )
            solutions = await conn.fetchval("SELECT count(*) FROM solutions")
            reviews = await conn.fetchval("SELECT count(*) FROM reviews")
            archive = await conn.fetchval("SELECT count(*) FROM archive_entries")
            events = await conn.fetchval("SELECT count(*) FROM event_log")
            users = await conn.fetchval("SELECT count(*) FROM users")
        return {
            "agents": agents,
            "problems": problems,
            "open_problems": open_problems,
            "solutions": solutions,
            "reviews": reviews,
            "archive_entries": archive,
            "events_logged": events,
            "users": users,
        }

    # ── Users (Google OAuth) ─────────────────────────────────────────

    async def upsert_user(
        self,
        *,
        email: str,
        name: str,
        picture_url: str,
        google_sub: str,
        auth_provider: str = "google",
        email_verified: bool = True,
    ) -> dict:
        """Create or update a user on Google login.  Returns the user row."""
        row = await self.pool.fetchrow(
            """
            INSERT INTO users (email, name, picture_url, google_sub, auth_provider, email_verified, verified_at, last_login)
            VALUES ($1, $2, $3, $4, $5, $6, CASE WHEN $6 THEN now() ELSE NULL END, now())
            ON CONFLICT (email) DO UPDATE SET
                name = EXCLUDED.name,
                picture_url = EXCLUDED.picture_url,
                google_sub = EXCLUDED.google_sub,
                auth_provider = EXCLUDED.auth_provider,
                email_verified = users.email_verified OR EXCLUDED.email_verified,
                verified_at = CASE
                    WHEN users.verified_at IS NOT NULL THEN users.verified_at
                    WHEN EXCLUDED.email_verified THEN now()
                    ELSE NULL
                END,
                last_login = now()
            RETURNING *
            """,
            email, name, picture_url, google_sub, auth_provider, email_verified,
        )
        return dict(row)

    async def get_user(self, user_id: UUID) -> dict | None:
        row = await self.pool.fetchrow("SELECT * FROM users WHERE id = $1", user_id)
        return dict(row) if row else None

    async def get_user_by_email(self, email: str) -> dict | None:
        row = await self.pool.fetchrow("SELECT * FROM users WHERE email = $1", email)
        return dict(row) if row else None

    async def link_user_agent(self, user_id: UUID, agent_id: UUID) -> None:
        """Link a user account to an agent identity."""
        await self.pool.execute(
            "UPDATE users SET agent_id = $2 WHERE id = $1",
            user_id, agent_id,
        )

    async def list_users(self, limit: int = 100) -> list[dict]:
        rows = await self.pool.fetch(
            "SELECT id, email, name, picture_url, agent_id, is_admin, created_at, last_login "
            "FROM users ORDER BY created_at LIMIT $1",
            limit,
        )
        return [dict(r) for r in rows]

    # ── User sessions (browser cookies) ──────────────────────────────

    async def create_user_session(self, token: str, user_id: UUID) -> None:
        await self.pool.execute(
            """
            INSERT INTO user_sessions (token, user_id)
            VALUES ($1, $2)
            ON CONFLICT (token) DO UPDATE SET user_id = EXCLUDED.user_id
            """,
            token, user_id,
        )

    async def get_user_session(self, token: str) -> dict | None:
        """Return the user row for a valid, non-expired session token."""
        row = await self.pool.fetchrow(
            """
            SELECT u.* FROM users u
            JOIN user_sessions s ON s.user_id = u.id
            WHERE s.token = $1 AND s.expires_at > now()
            """,
            token,
        )
        return dict(row) if row else None

    async def delete_user_session(self, token: str) -> None:
        await self.pool.execute("DELETE FROM user_sessions WHERE token = $1", token)

    async def delete_user_sessions(self, user_id: UUID) -> None:
        await self.pool.execute("DELETE FROM user_sessions WHERE user_id = $1", user_id)

    # ── Local email/password auth ────────────────────────────────────

    async def create_local_user(self, *, email: str, name: str) -> dict:
        """Create a local-auth user row. Raises if email already exists."""
        row = await self.pool.fetchrow(
            """
            INSERT INTO users (email, name, picture_url, google_sub, auth_provider, email_verified, last_login)
            VALUES ($1, $2, '', $3, 'local', FALSE, now())
            RETURNING *
            """,
            email,
            name,
            f"local:{uuid4()}",
        )
        return dict(row)

    async def set_local_credential(self, *, user_id: UUID, password_hash: str, password_salt: str) -> None:
        await self.pool.execute(
            """
            INSERT INTO local_credentials (user_id, password_hash, password_salt)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id) DO UPDATE SET
                password_hash = EXCLUDED.password_hash,
                password_salt = EXCLUDED.password_salt,
                updated_at = now()
            """,
            user_id, password_hash, password_salt,
        )

    async def get_local_credential_by_email(self, email: str) -> dict | None:
        row = await self.pool.fetchrow(
            """
            SELECT u.*, lc.password_hash, lc.password_salt
            FROM users u
            JOIN local_credentials lc ON lc.user_id = u.id
            WHERE lower(u.email) = lower($1)
            """,
            email,
        )
        return dict(row) if row else None

    async def touch_user_login(self, user_id: UUID) -> None:
        await self.pool.execute("UPDATE users SET last_login = now() WHERE id = $1", user_id)

    async def mark_email_verified(self, user_id: UUID) -> None:
        await self.pool.execute(
            "UPDATE users SET email_verified = TRUE, verified_at = now() WHERE id = $1",
            user_id,
        )

    async def create_email_verification_code(
        self, *, user_id: UUID, email: str, code_hash: str, expires_at: datetime
    ) -> None:
        await self.pool.execute(
            """
            INSERT INTO email_verification_codes (email, user_id, code_hash, expires_at, attempts)
            VALUES ($1, $2, $3, $4, 0)
            ON CONFLICT (email) DO UPDATE SET
                user_id = EXCLUDED.user_id,
                code_hash = EXCLUDED.code_hash,
                expires_at = EXCLUDED.expires_at,
                attempts = 0,
                created_at = now()
            """,
            email.lower(), user_id, code_hash, expires_at,
        )

    async def verify_email_code(self, *, email: str, code: str) -> dict | None:
        row = await self.pool.fetchrow(
            """
            SELECT evc.user_id, evc.code_hash, evc.expires_at, evc.attempts, u.email_verified
            FROM email_verification_codes evc
            JOIN users u ON u.id = evc.user_id
            WHERE evc.email = $1
            """,
            email.lower(),
        )
        if not row:
            return None
        if row["expires_at"] < datetime.now(timezone.utc):
            await self.pool.execute("DELETE FROM email_verification_codes WHERE email = $1", email.lower())
            return None

        digest = hashlib.sha256(code.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(digest, row["code_hash"]):
            await self.pool.execute(
                "UPDATE email_verification_codes SET attempts = attempts + 1 WHERE email = $1",
                email.lower(),
            )
            return None

        await self.pool.execute("DELETE FROM email_verification_codes WHERE email = $1", email.lower())
        user = await self.get_user(row["user_id"])
        return user

    async def user_count(self) -> int:
        """Return the total number of registered users."""
        return await self.pool.fetchval("SELECT count(*) FROM users") or 0

    async def promote_to_admin(self, user_id: UUID) -> None:
        """Promote a user to admin."""
        await self.pool.execute("UPDATE users SET is_admin = TRUE WHERE id = $1", user_id)


# ── Transaction context manager ──────────────────────────────────────────

class _Transaction:
    """Async context manager providing a transactional DB connection.

    On ``__aenter__`` acquires a connection and begins a transaction.
    On ``__aexit__`` commits on success or rolls back on exception.
    """

    def __init__(self, pool: "asyncpg.Pool") -> None:
        self._pool = pool
        self._conn: Any = None
        self._tr: Any = None

    async def __aenter__(self):
        self._conn = await self._pool.acquire()
        self._tr = self._conn.transaction()
        await self._tr.start()
        return self._conn

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        try:
            if exc_type is None:
                await self._tr.commit()
            else:
                await self._tr.rollback()
        finally:
            await self._pool.release(self._conn)
        return False


# ── Helpers ──────────────────────────────────────────────────────────────

def _parse_ts(value: str | None) -> datetime | None:
    """Parse an ISO timestamp string, returning None if absent."""
    if not value:
        return None
    return datetime.fromisoformat(value)
