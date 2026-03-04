"""Background scheduler for periodic Exchange maintenance tasks.

Runs configurable async loops for:
- Problem expiry (deadline-based)
- Claim timeout expiry
- Bounty escalation
- Reputation inactivity decay
- Archive TTL expiry
- Skill σ-decay
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from schwarma.exchange import Exchange

logger = logging.getLogger(__name__)


@dataclass
class SchedulerConfig:
    """Intervals (in seconds) for each maintenance loop.

    Set an interval to 0 to disable that task.
    """

    expire_problems_interval: float = 60.0
    expire_claims_interval: float = 30.0
    escalate_bounties_interval: float = 300.0
    escalate_bounties_stale_seconds: float = 3600.0
    reputation_decay_interval: float = 3600.0
    archive_expiry_interval: float = 3600.0
    skill_decay_interval: float = 86400.0  # daily


class Scheduler:
    """Async background scheduler that periodically runs Exchange maintenance.

    Usage::

        scheduler = Scheduler(exchange)
        await scheduler.start()
        # ... later ...
        await scheduler.stop()

    Or as an async context manager::

        async with Scheduler(exchange) as sched:
            ...
    """

    def __init__(
        self,
        exchange: Exchange,
        config: SchedulerConfig | None = None,
    ) -> None:
        self.exchange = exchange
        self.config = config or SchedulerConfig()
        self._tasks: list[asyncio.Task[None]] = []
        self._running = False

    # ── lifecycle ────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start all enabled maintenance loops."""
        if self._running:
            return
        self._running = True

        jobs: list[tuple[str, float]] = [
            ("expire_problems", self.config.expire_problems_interval),
            ("expire_claims", self.config.expire_claims_interval),
            ("escalate_bounties", self.config.escalate_bounties_interval),
            ("reputation_decay", self.config.reputation_decay_interval),
            ("archive_expiry", self.config.archive_expiry_interval),
            ("skill_decay", self.config.skill_decay_interval),
        ]

        for name, interval in jobs:
            if interval <= 0:
                continue
            coro = self._loop(name, interval)
            task = asyncio.create_task(coro, name=f"schwarma-{name}")
            self._tasks.append(task)
            logger.info("Scheduler: started %s (every %.1fs)", name, interval)

    async def stop(self) -> None:
        """Cancel all running loops and wait for them to finish."""
        if not self._running:
            return
        self._running = False
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("Scheduler: stopped all tasks")

    @property
    def running(self) -> bool:
        return self._running

    @property
    def active_tasks(self) -> int:
        """Number of background tasks still alive."""
        return sum(1 for t in self._tasks if not t.done())

    # ── context manager ─────────────────────────────────────────────

    async def __aenter__(self) -> Scheduler:
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.stop()

    # ── internal loop ───────────────────────────────────────────────

    async def _loop(self, name: str, interval: float) -> None:
        """Run *name* every *interval* seconds until cancelled."""
        while self._running:
            await asyncio.sleep(interval)
            if not self._running:
                break
            try:
                await self._run_job(name)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Scheduler job %s failed", name)

    async def _run_job(self, name: str) -> None:
        """Dispatch a single maintenance job by name."""
        ex = self.exchange

        if name == "expire_problems":
            expired = await ex.expire_stale_problems()
            if expired:
                logger.info("Scheduler: expired %d problems", len(expired))

        elif name == "expire_claims":
            released = await ex.expire_stale_claims()
            if released:
                logger.info("Scheduler: expired %d claims", len(released))

        elif name == "escalate_bounties":
            stale_s = self.config.escalate_bounties_stale_seconds
            escalated = await ex.escalate_stale_bounties(stale_seconds=stale_s)
            if escalated:
                logger.info("Scheduler: escalated %d bounties", len(escalated))

        elif name == "reputation_decay":
            entries = ex.ledger.apply_inactivity_decay()
            if entries:
                logger.info("Scheduler: decayed %d agents", len(entries))

        elif name == "archive_expiry":
            count = ex.archive.expire_stale()
            if count:
                logger.info("Scheduler: tombstoned %d archive entries", count)

        elif name == "skill_decay":
            count = ex.skill_tracker.apply_global_decay()
            if count:
                logger.info("Scheduler: decayed %d skill ratings", count)

        else:
            logger.warning("Scheduler: unknown job %s", name)
