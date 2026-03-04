"""
Event bus for decoupled communication between exchange components.

Components publish events (problem posted, solution submitted, review
completed, etc.) and other components subscribe to react accordingly.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from typing import Any, Callable, Coroutine
from uuid import UUID

logger = logging.getLogger(__name__)


class EventKind(Enum):
    """All event types that flow through the exchange."""

    # Problem lifecycle
    PROBLEM_POSTED = auto()
    PROBLEM_CLAIMED = auto()
    PROBLEM_SOLVED = auto()
    PROBLEM_CLOSED = auto()
    PROBLEM_EXPIRED = auto()
    PROBLEM_ESCALATED = auto()

    # Solution lifecycle
    SOLUTION_SUBMITTED = auto()
    SOLUTION_ACCEPTED = auto()
    SOLUTION_REJECTED = auto()
    SOLUTION_REVISION_REQUESTED = auto()
    SOLUTION_CHALLENGED = auto()

    # Review lifecycle
    REVIEW_REQUESTED = auto()
    REVIEW_SUBMITTED = auto()
    REVIEW_APPROVED = auto()
    REVIEW_REJECTED = auto()

    # Reputation
    REPUTATION_CHANGED = auto()

    # Swap lifecycle
    SWAP_PROPOSED = auto()
    SWAP_ACCEPTED = auto()
    SWAP_COMPLETED = auto()
    SWAP_DECLINED = auto()

    # Triage
    TRIAGE_ASSIGNED = auto()
    TRIAGE_REROUTED = auto()

    # Agent lifecycle
    AGENT_REGISTERED = auto()
    AGENT_SUSPENDED = auto()
    AGENT_CAPABILITY_UPDATED = auto()

    # Skill / calibration
    SKILL_UPDATED = auto()
    CALIBRATION_INJECTED = auto()
    CALIBRATION_EVALUATED = auto()
    PROBATION_ENDED = auto()

    # Claim lifecycle
    CLAIM_EXPIRED = auto()

    # Similarity / deduplication
    DUPLICATE_DETECTED = auto()


@dataclass(frozen=True)
class Event:
    """An immutable event emitted within the exchange."""

    kind: EventKind
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    source_agent_id: UUID | None = None
    target_agent_id: UUID | None = None
    problem_id: UUID | None = None
    solution_id: UUID | None = None
    review_id: UUID | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"Event({self.kind.name}, problem={self.problem_id}, source={self.source_agent_id})"

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict for storage / transport."""
        return {
            "kind": self.kind.name,
            "timestamp": self.timestamp.isoformat(),
            "source_agent_id": str(self.source_agent_id) if self.source_agent_id else None,
            "target_agent_id": str(self.target_agent_id) if self.target_agent_id else None,
            "problem_id": str(self.problem_id) if self.problem_id else None,
            "solution_id": str(self.solution_id) if self.solution_id else None,
            "review_id": str(self.review_id) if self.review_id else None,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Event":
        """Reconstruct an Event from a dict produced by ``to_dict``."""
        return cls(
            kind=EventKind[data["kind"]],
            timestamp=datetime.fromisoformat(data["timestamp"]),
            source_agent_id=UUID(data["source_agent_id"]) if data.get("source_agent_id") else None,
            target_agent_id=UUID(data["target_agent_id"]) if data.get("target_agent_id") else None,
            problem_id=UUID(data["problem_id"]) if data.get("problem_id") else None,
            solution_id=UUID(data["solution_id"]) if data.get("solution_id") else None,
            review_id=UUID(data["review_id"]) if data.get("review_id") else None,
            payload=data.get("payload", {}),
        )


# Type alias for event handlers
EventHandler = Callable[[Event], Coroutine[Any, Any, None]]

# Type alias for event filter predicates
EventFilter = Callable[[Event], bool]


class EventBus:
    """Publish/subscribe event bus.

    Handlers are async callables.  Publishing is fire-and-forget by default
    but ``publish_and_wait`` lets callers block until all handlers finish.
    """

    def __init__(self) -> None:
        self._handlers: dict[EventKind, list[EventHandler]] = {}
        self._global_handlers: list[EventHandler] = []
        self._filtered_handlers: list[tuple[EventFilter, EventHandler]] = []
        self._recording: bool = False
        self._recorded: list[Event] = []

    # ------------------------------------------------------------------
    # Subscription
    # ------------------------------------------------------------------

    def subscribe(self, kind: EventKind, handler: EventHandler) -> None:
        """Subscribe *handler* to a specific event kind."""
        self._handlers.setdefault(kind, []).append(handler)

    def subscribe_all(self, handler: EventHandler) -> None:
        """Subscribe *handler* to **every** event kind."""
        self._global_handlers.append(handler)

    def subscribe_filtered(
        self,
        predicate: EventFilter,
        handler: EventHandler,
    ) -> None:
        """Subscribe *handler* only to events matching *predicate*.

        The predicate is called for every event; the handler is invoked
        only when the predicate returns ``True``.  This enables
        agent-specific or tag-specific subscriptions.
        """
        self._filtered_handlers.append((predicate, handler))

    def unsubscribe(self, kind: EventKind, handler: EventHandler) -> None:
        handlers = self._handlers.get(kind, [])
        if handler in handlers:
            handlers.remove(handler)

    def unsubscribe_filtered(self, handler: EventHandler) -> None:
        """Remove all filtered subscriptions for *handler*."""
        self._filtered_handlers = [
            (pred, h) for pred, h in self._filtered_handlers if h is not handler
        ]

    # ------------------------------------------------------------------
    # Recording / Replay
    # ------------------------------------------------------------------

    def enable_recording(self, *, enabled: bool = True) -> None:
        """Turn event recording on or off.

        When recording is enabled, every published event is appended to
        an internal list accessible via :attr:`recorded_events`.
        """
        self._recording = enabled

    @property
    def recorded_events(self) -> list[Event]:
        """Return the list of recorded events (read-only copy)."""
        return list(self._recorded)

    def clear_recording(self) -> None:
        """Discard all recorded events."""
        self._recorded.clear()

    async def replay(
        self,
        events: list[Event] | None = None,
        *,
        filter_kinds: set[EventKind] | None = None,
    ) -> int:
        """Re-publish previously recorded events through the bus.

        Parameters
        ----------
        events:
            Events to replay.  Defaults to the internal recorded list.
        filter_kinds:
            If provided, only events whose kind is in this set are replayed.

        Returns the number of events replayed.
        """
        source = events if events is not None else list(self._recorded)
        count = 0
        for event in source:
            if filter_kinds and event.kind not in filter_kinds:
                continue
            await self.publish(event)
            count += 1
        return count

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    async def publish(self, event: Event) -> None:
        """Publish *event* to all matching handlers (fire-and-forget)."""
        if self._recording:
            self._recorded.append(event)
        handlers = list(self._global_handlers) + list(
            self._handlers.get(event.kind, [])
        )
        # Add filtered handlers whose predicates match
        for predicate, handler in self._filtered_handlers:
            try:
                if predicate(event):
                    handlers.append(handler)
            except Exception:
                logger.exception("Filter predicate failed for %s", event)

        for handler in handlers:
            try:
                await handler(event)
            except Exception:
                logger.exception("Handler %s failed for %s", handler, event)

    async def publish_and_wait(self, event: Event) -> list[Any]:
        """Publish and gather results from all handlers."""
        if self._recording:
            self._recorded.append(event)
        handlers = list(self._global_handlers) + list(
            self._handlers.get(event.kind, [])
        )
        for predicate, handler in self._filtered_handlers:
            try:
                if predicate(event):
                    handlers.append(handler)
            except Exception:
                logger.exception("Filter predicate failed for %s", event)

        results = await asyncio.gather(
            *(h(event) for h in handlers), return_exceptions=True
        )
        return list(results)
