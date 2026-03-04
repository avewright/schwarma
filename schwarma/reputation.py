"""
ReputationLedger — tracks agent reputation through an append-only event log.

Agents earn (or lose) reputation from specific actions:
  • Solving a problem: +bounty
  • Having a solution accepted: +bonus
  • Submitting a review: +review reward
  • Having your solution rejected: -penalty
  • Problem expired while claimed: -penalty
  • Good-faith violation: -large penalty
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from typing import Any, Iterator
from uuid import UUID, uuid4

logger = logging.getLogger(__name__)


class ReputationEvent(Enum):
    """Reason for a reputation change."""

    SOLUTION_SUBMITTED = auto()    # +small
    SOLUTION_ACCEPTED = auto()      # +bounty
    SOLUTION_REJECTED = auto()      # -small
    REVIEW_SUBMITTED = auto()       # +small
    REVIEW_HELPFUL = auto()         # +medium  (community upvoted the review)
    PROBLEM_POSTED = auto()         # +tiny    (participation)
    PROBLEM_EXPIRED_CLAIMED = auto()  # -medium (let a claim lapse)
    GOOD_FAITH_VIOLATION = auto()   # -large
    SWAP_COMPLETED = auto()         # +small
    BONUS = auto()                  # arbitrary bonus
    PENALTY = auto()                # arbitrary penalty


# Default point values — consumers can override via LedgerConfig
DEFAULT_REWARDS: dict[ReputationEvent, int] = {
    ReputationEvent.SOLUTION_SUBMITTED: 2,
    ReputationEvent.SOLUTION_ACCEPTED: 0,    # uses problem bounty
    ReputationEvent.SOLUTION_REJECTED: -3,
    ReputationEvent.REVIEW_SUBMITTED: 3,
    ReputationEvent.REVIEW_HELPFUL: 5,
    ReputationEvent.PROBLEM_POSTED: 1,
    ReputationEvent.PROBLEM_EXPIRED_CLAIMED: -5,
    ReputationEvent.GOOD_FAITH_VIOLATION: -20,
    ReputationEvent.SWAP_COMPLETED: 2,
    ReputationEvent.BONUS: 0,
    ReputationEvent.PENALTY: 0,
}


@dataclass(frozen=True)
class LedgerEntry:
    """A single reputation mutation — append-only."""

    agent_id: UUID
    event: ReputationEvent
    delta: int
    reason: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    related_id: UUID | None = None  # problem / solution / review id
    id: UUID = field(default_factory=uuid4)


@dataclass
class LedgerConfig:
    """Tuneable reward / penalty values."""

    rewards: dict[ReputationEvent, int] = field(
        default_factory=lambda: dict(DEFAULT_REWARDS)
    )
    initial_reputation: int = 50
    floor: int = 0        # reputation can't go below this
    ceiling: int = 10_000  # hard cap

    # Diminishing returns for same-pair interactions
    diminishing_threshold: int = 3     # after this many interactions, rewards shrink
    diminishing_decay: float = 0.5     # multiplier applied per excess interaction

    # Inactivity decay — reputation slowly erodes when agents go silent
    inactivity_decay_rate: float = 0.0   # fraction of balance lost per decay period (0 = off)
    inactivity_period_days: int = 30     # days of silence before decay kicks in


class ReputationLedger:
    """
    Append-only log of reputation events.

    Provides fast O(1) balance queries backed by a running tally, with a
    full audit trail accessible via ``history()``.
    """

    def __init__(self, config: LedgerConfig | None = None) -> None:
        self.config = config or LedgerConfig()
        self._entries: list[LedgerEntry] = []
        self._balances: dict[UUID, int] = defaultdict(lambda: self.config.initial_reputation)
        # Pairwise interaction counter for diminishing returns
        self._pairwise_count: dict[frozenset[UUID], int] = defaultdict(int)
        # Last activity timestamp per agent (for inactivity decay)
        self._last_activity: dict[UUID, datetime] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record(
        self,
        agent_id: UUID,
        event: ReputationEvent,
        *,
        delta: int | None = None,
        reason: str = "",
        related_id: UUID | None = None,
    ) -> LedgerEntry:
        """Append a reputation event and update the running balance."""
        if delta is None:
            delta = self.config.rewards.get(event, 0)

        entry = LedgerEntry(
            agent_id=agent_id,
            event=event,
            delta=delta,
            reason=reason,
            related_id=related_id,
        )
        self._entries.append(entry)
        self._last_activity[agent_id] = entry.timestamp

        new_balance = self._balances[agent_id] + delta
        new_balance = max(self.config.floor, min(self.config.ceiling, new_balance))
        self._balances[agent_id] = new_balance

        logger.debug(
            "Reputation %+d for agent %s (%s) → %d",
            delta,
            agent_id,
            event.name,
            new_balance,
        )
        return entry

    def balance(self, agent_id: UUID) -> int:
        """Current reputation balance for *agent_id*."""
        return self._balances[agent_id]

    def history(self, agent_id: UUID) -> list[LedgerEntry]:
        """Full audit trail for *agent_id*, oldest first."""
        return [e for e in self._entries if e.agent_id == agent_id]

    def leaderboard(self, top_n: int = 10) -> list[tuple[UUID, int]]:
        """Return the top-N agents by reputation descending."""
        return sorted(self._balances.items(), key=lambda kv: kv[1], reverse=True)[:top_n]

    def all_balances(self) -> dict[UUID, int]:
        return dict(self._balances)

    def record_pairwise(self, agent_a: UUID, agent_b: UUID) -> None:
        """Increment pairwise interaction count between two agents."""
        pair = frozenset({agent_a, agent_b})
        self._pairwise_count[pair] += 1

    def diminishing_factor(self, agent_a: UUID, agent_b: UUID) -> float:
        """Return a [0, 1] multiplier for rewards between this agent pair.

        After ``diminishing_threshold`` interactions, rewards decay
        exponentially.  Returns 1.0 when interactions are below threshold.
        """
        pair = frozenset({agent_a, agent_b})
        count = self._pairwise_count[pair]
        if count <= self.config.diminishing_threshold:
            return 1.0
        excess = count - self.config.diminishing_threshold
        return max(0.0, self.config.diminishing_decay ** excess)

    def pairwise_interaction_count(self, agent_a: UUID, agent_b: UUID) -> int:
        """Number of recorded pairwise interactions between two agents."""
        pair = frozenset({agent_a, agent_b})
        return self._pairwise_count[pair]

    # ------------------------------------------------------------------
    # Inactivity decay
    # ------------------------------------------------------------------

    def last_activity(self, agent_id: UUID) -> datetime | None:
        """Return the timestamp of the agent's most recent ledger entry."""
        return self._last_activity.get(agent_id)

    def apply_inactivity_decay(
        self,
        *,
        now: datetime | None = None,
    ) -> list[LedgerEntry]:
        """Apply reputation decay to agents inactive for too long.

        For every agent whose last activity is older than
        ``inactivity_period_days``, deduct ``inactivity_decay_rate`` × balance
        (rounded down, minimum deduction of 1 when rate > 0 and balance > floor).

        Returns the list of decay entries created.
        """
        from datetime import timedelta

        rate = self.config.inactivity_decay_rate
        if rate <= 0:
            return []

        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(days=self.config.inactivity_period_days)
        entries: list[LedgerEntry] = []

        for agent_id in list(self._last_activity):
            last = self._last_activity[agent_id]
            if last >= cutoff:
                continue
            balance = self._balances[agent_id]
            if balance <= self.config.floor:
                continue
            loss = max(1, int(balance * rate))
            entry = self.record(
                agent_id,
                ReputationEvent.PENALTY,
                delta=-loss,
                reason="inactivity decay",
            )
            entries.append(entry)

        return entries

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self) -> Iterator[LedgerEntry]:
        return iter(self._entries)
