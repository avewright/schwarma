"""
Per-agent rate limiting.

Tracks action counts within sliding time windows and rejects actions
that exceed configured limits.  Used by the Exchange to prevent abuse.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum, auto
from uuid import UUID

logger = logging.getLogger(__name__)


class RateLimitAction(Enum):
    """Actions that can be rate-limited."""

    POST_PROBLEM = auto()
    CLAIM_PROBLEM = auto()
    SUBMIT_SOLUTION = auto()
    SUBMIT_REVIEW = auto()
    SUBMIT_SWAP = auto()
    CHALLENGE = auto()


@dataclass
class RateLimitRule:
    """Max *count* actions within a sliding *window*."""

    action: RateLimitAction
    count: int
    window: timedelta


@dataclass
class RateLimitConfig:
    """Default rate-limit rules for all agents."""

    rules: list[RateLimitRule] = field(default_factory=lambda: [
        RateLimitRule(RateLimitAction.POST_PROBLEM, count=10, window=timedelta(minutes=5)),
        RateLimitRule(RateLimitAction.CLAIM_PROBLEM, count=10, window=timedelta(minutes=5)),
        RateLimitRule(RateLimitAction.SUBMIT_SOLUTION, count=10, window=timedelta(minutes=5)),
        RateLimitRule(RateLimitAction.SUBMIT_REVIEW, count=20, window=timedelta(minutes=5)),
        RateLimitRule(RateLimitAction.SUBMIT_SWAP, count=5, window=timedelta(minutes=5)),
        RateLimitRule(RateLimitAction.CHALLENGE, count=3, window=timedelta(minutes=10)),
    ])
    enabled: bool = True


class RateLimiter:
    """Sliding-window rate limiter for agent actions.

    Tracks timestamps of recent actions per agent and rejects actions
    that exceed configured limits.
    """

    def __init__(self, config: RateLimitConfig | None = None) -> None:
        self.config = config or RateLimitConfig()
        # agent_id → action → list of timestamps
        self._timestamps: dict[UUID, dict[RateLimitAction, list[datetime]]] = defaultdict(
            lambda: defaultdict(list)
        )
        # Index rules by action for fast lookup
        self._rules_by_action: dict[RateLimitAction, list[RateLimitRule]] = defaultdict(list)
        for rule in self.config.rules:
            self._rules_by_action[rule.action].append(rule)

    def check(self, agent_id: UUID, action: RateLimitAction) -> bool:
        """Return True if the action is allowed, False if rate-limited."""
        if not self.config.enabled:
            return True

        now = datetime.now(timezone.utc)
        rules = self._rules_by_action.get(action, [])
        if not rules:
            return True  # no rules for this action

        timestamps = self._timestamps[agent_id][action]

        for rule in rules:
            cutoff = now - rule.window
            recent = [t for t in timestamps if t > cutoff]
            if len(recent) >= rule.count:
                logger.warning(
                    "Rate limit hit: agent %s action %s (%d/%d in %s)",
                    agent_id, action.name, len(recent), rule.count, rule.window,
                )
                return False

        return True

    def record(self, agent_id: UUID, action: RateLimitAction) -> None:
        """Record that an action was taken (call after check passes)."""
        self._timestamps[agent_id][action].append(datetime.now(timezone.utc))

    def check_and_record(self, agent_id: UUID, action: RateLimitAction) -> bool:
        """Check + record in one call. Returns True if allowed."""
        if not self.check(agent_id, action):
            return False
        self.record(agent_id, action)
        return True

    def reset(self, agent_id: UUID) -> None:
        """Clear all rate-limit history for an agent."""
        self._timestamps.pop(agent_id, None)

    def prune(self) -> int:
        """Remove expired timestamps to free memory. Returns count removed."""
        now = datetime.now(timezone.utc)
        max_window = max(
            (r.window for r in self.config.rules),
            default=timedelta(minutes=5),
        )
        cutoff = now - max_window
        removed = 0
        for agent_actions in self._timestamps.values():
            for action, timestamps in agent_actions.items():
                before = len(timestamps)
                agent_actions[action] = [t for t in timestamps if t > cutoff]
                removed += before - len(agent_actions[action])
        return removed
