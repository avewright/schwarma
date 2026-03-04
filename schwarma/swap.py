"""
SwapPool — problem-swapping mechanism for fresh perspectives.

The idea: two agents each have a problem they're stuck on.  They swap
problems so each gets a "fresh pair of eyes".  The pool matches agents
whose problems look like a good fit for the other's capabilities.

Flow:
  1. Agent A posts their problem to the swap pool.
  2. Agent B does the same.
  3. The pool matches them (capability affinity, reputation guard-rails).
  4. Each agent receives the other's problem as a new assignment.
  5. Both solve → both earn reputation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from typing import Callable
from uuid import UUID, uuid4

from schwarma.agent import Agent, ModelTier
from schwarma.problem import Problem

logger = logging.getLogger(__name__)

# Type alias for optional effective-tier function
EffectiveTierFn = Callable[[Agent], ModelTier] | None


class SwapStatus(Enum):
    WAITING = auto()      # in the pool, unmatched
    MATCHED = auto()      # paired with another entry
    COMPLETED = auto()    # both sides solved
    CANCELLED = auto()
    DECLINED = auto()


@dataclass
class SwapEntry:
    """One side of a potential swap."""

    agent: Agent
    problem: Problem
    id: UUID = field(default_factory=uuid4)
    status: SwapStatus = SwapStatus.WAITING
    partner_entry_id: UUID | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __hash__(self) -> int:
        return hash(self.id)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, SwapEntry):
            return self.id == other.id
        return NotImplemented


@dataclass
class SwapMatch:
    """A confirmed pairing of two swap entries."""

    entry_a: SwapEntry
    entry_b: SwapEntry
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completed: bool = False

    @property
    def agents(self) -> tuple[Agent, Agent]:
        return (self.entry_a.agent, self.entry_b.agent)

    @property
    def problems(self) -> tuple[Problem, Problem]:
        return (self.entry_a.problem, self.entry_b.problem)


class SwapPool:
    """
    Maintains a pool of agents willing to swap problems.

    When a compatible pair is found, it creates a :class:`SwapMatch` and
    notifies both sides.
    """

    def __init__(
        self,
        *,
        max_tier_gap: int = 1,
        effective_tier_fn: EffectiveTierFn = None,
    ) -> None:
        self._waiting: dict[UUID, SwapEntry] = {}  # entry_id → entry
        self._matches: list[SwapMatch] = []
        self.max_tier_gap = max_tier_gap
        self._effective_tier_fn = effective_tier_fn

    # ------------------------------------------------------------------
    # Pool management
    # ------------------------------------------------------------------

    def submit(self, agent: Agent, problem: Problem) -> SwapEntry:
        """Submit a problem to the swap pool.  Returns immediately."""
        entry = SwapEntry(agent=agent, problem=problem)
        self._waiting[entry.id] = entry
        logger.info("Swap pool: %s submitted problem %s", agent.name, problem.title)
        return entry

    def cancel(self, entry_id: UUID) -> None:
        entry = self._waiting.pop(entry_id, None)
        if entry:
            entry.status = SwapStatus.CANCELLED

    def try_match(self) -> SwapMatch | None:
        """Try to pair two waiting entries.

        Matching heuristic:
          • Agent A can help with B's problem tags (and vice-versa).
          • Agents are not the same.
        """
        waiting = list(self._waiting.values())
        for i, a in enumerate(waiting):
            for b in waiting[i + 1 :]:
                if a.agent.id == b.agent.id:
                    continue
                if self._is_compatible(a, b):
                    return self._create_match(a, b)
        return None

    def match_all(self) -> list[SwapMatch]:
        """Greedily match as many pairs as possible."""
        matches: list[SwapMatch] = []
        while True:
            m = self.try_match()
            if m is None:
                break
            matches.append(m)
        return matches

    @property
    def waiting_count(self) -> int:
        return len(self._waiting)

    @property
    def matches(self) -> list[SwapMatch]:
        return list(self._matches)

    # ------------------------------------------------------------------
    # Completion
    # ------------------------------------------------------------------

    def complete(self, match_id: UUID) -> None:
        for m in self._matches:
            if m.id == match_id:
                m.completed = True
                m.entry_a.status = SwapStatus.COMPLETED
                m.entry_b.status = SwapStatus.COMPLETED
                return
        raise ValueError(f"No match with id {match_id}")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _is_compatible(self, a: SwapEntry, b: SwapEntry) -> bool:
        """Check bidirectional capability affinity and tier compatibility."""
        from schwarma.triage import DEFAULT_TAG_CAP_MAP

        # --- Tier compatibility (use effective tier if available) ---
        tier_a = (
            self._effective_tier_fn(a.agent)
            if self._effective_tier_fn is not None
            else a.agent.model_tier
        )
        tier_b = (
            self._effective_tier_fn(b.agent)
            if self._effective_tier_fn is not None
            else b.agent.model_tier
        )
        # SPECIALIZED matches anything
        if tier_a != ModelTier.SPECIALIZED and tier_b != ModelTier.SPECIALIZED:
            if abs(tier_a.value - tier_b.value) > self.max_tier_gap:
                return False

        # --- Capability affinity ---
        # What capabilities does B's problem need?
        b_needs = set()
        for tag in b.problem.tags:
            b_needs.update(DEFAULT_TAG_CAP_MAP.get(tag, set()))

        # What capabilities does A's problem need?
        a_needs = set()
        for tag in a.problem.tags:
            a_needs.update(DEFAULT_TAG_CAP_MAP.get(tag, set()))

        a_can_help_b = a.agent.has_any_capability(b_needs) if b_needs else True
        b_can_help_a = b.agent.has_any_capability(a_needs) if a_needs else True

        return a_can_help_b and b_can_help_a

    def _create_match(self, a: SwapEntry, b: SwapEntry) -> SwapMatch:
        a.status = SwapStatus.MATCHED
        b.status = SwapStatus.MATCHED
        a.partner_entry_id = b.id
        b.partner_entry_id = a.id
        self._waiting.pop(a.id, None)
        self._waiting.pop(b.id, None)

        match = SwapMatch(entry_a=a, entry_b=b)
        self._matches.append(match)
        logger.info(
            "Swap matched: %s ↔ %s (problems: %s ↔ %s)",
            a.agent.name,
            b.agent.name,
            a.problem.title,
            b.problem.title,
        )
        return match
