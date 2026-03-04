"""
Trust — sensitivity levels for problems, clearance tiers for agents.

Every problem declares how sensitive its content is.  Every agent has a
clearance level.  The exchange enforces that an agent can only *see* and
*claim* problems at or below its clearance.

Trust tiers can be earned automatically through reputation thresholds or
granted manually by the exchange operator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from uuid import UUID


class Sensitivity(IntEnum):
    """Problem sensitivity — higher = more restricted."""

    PUBLIC = 0        # Anyone can see
    INTERNAL = 1      # Registered agents only (default)
    CONFIDENTIAL = 2  # Trusted agents only
    RESTRICTED = 3    # Highest clearance required


class TrustTier(IntEnum):
    """Agent clearance level — higher = more access."""

    UNTRUSTED = 0     # Just registered, probationary
    BASIC = 1         # Has some track record (default start)
    TRUSTED = 2       # Consistent good behaviour
    PRIVILEGED = 3    # Full access to all sensitivity levels


@dataclass
class TrustPolicy:
    """Configurable thresholds for automatic tier promotion.

    When an agent's reputation crosses a threshold, their trust tier is
    automatically upgraded (but never downgraded automatically — that
    requires an explicit suspension action).
    """

    # reputation → tier promotion thresholds
    thresholds: dict[TrustTier, int] = field(default_factory=lambda: {
        TrustTier.UNTRUSTED: 0,
        TrustTier.BASIC: 50,       # starting reputation = instant BASIC
        TrustTier.TRUSTED: 120,
        TrustTier.PRIVILEGED: 250,
    })

    # Default tier for newly-registered agents
    default_tier: TrustTier = TrustTier.BASIC

    # Default sensitivity for problems that don't specify one
    default_sensitivity: Sensitivity = Sensitivity.INTERNAL


class TrustGate:
    """Enforces that agents can only access problems at their clearance level.

    Also provides automatic tier promotion based on reputation.
    """

    def __init__(self, policy: TrustPolicy | None = None) -> None:
        self.policy = policy or TrustPolicy()
        self._tiers: dict[UUID, TrustTier] = {}

    def assign_tier(self, agent_id: UUID, tier: TrustTier) -> None:
        """Manually set an agent's trust tier."""
        self._tiers[agent_id] = tier

    def get_tier(self, agent_id: UUID) -> TrustTier:
        """Current tier (defaults to policy default if not set)."""
        return self._tiers.get(agent_id, self.policy.default_tier)

    def maybe_promote(self, agent_id: UUID, reputation: int) -> TrustTier:
        """Check if *agent_id* qualifies for a higher tier based on reputation.

        Promotes automatically if threshold is met.  Returns the (possibly
        new) tier.
        """
        current = self.get_tier(agent_id)
        # Walk tiers from highest to lowest; promote to the highest qualified
        for tier in sorted(TrustTier, reverse=True):
            threshold = self.policy.thresholds.get(tier, 999_999)
            if reputation >= threshold and tier > current:
                self._tiers[agent_id] = tier
                return tier
        return current

    def can_access(self, agent_id: UUID, sensitivity: Sensitivity) -> bool:
        """Can *agent_id* see / claim a problem with *sensitivity*?"""
        tier = self.get_tier(agent_id)
        return int(tier) >= int(sensitivity)

    def filter_visible(
        self,
        agent_id: UUID,
        problems: list,  # list[Problem] — avoiding circular import
    ) -> list:
        """Return only problems the agent is cleared to see."""
        tier = self.get_tier(agent_id)
        return [
            p for p in problems
            if int(tier) >= int(getattr(p, "sensitivity", Sensitivity.INTERNAL))
        ]
