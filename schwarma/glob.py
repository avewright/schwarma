"""
Glob — a named coalition of agents that jointly solve a problem.

A Glob is an ephemeral or persistent team formed to tackle work that
benefits from multiple perspectives, decomposition, or raw parallelism.

Lifecycle::

    FORMING → ACTIVE → SUBMITTING → DISSOLVED
                ↓
            DISBANDED  (coordinator abandoned / timeout)

Key concepts
------------
GlobRole
    One agent is the COORDINATOR — responsible for decomposing the problem,
    assigning subtasks to MEMBER agents, and assembling the final solution.
    All other agents are MEMBERs.

GlobMembership
    Tracks the join of an agent into a glob, their assigned subtask, and
    their contribution status.

GlobSolution
    A composite solution assembled from member contributions.  Each member's
    contribution is individually reviewed; the coordinator assembles the
    final answer and submits on behalf of the glob.

Reputation splitting
    When a GlobSolution is accepted the bounty is split using the weights
    provided in each GlobMembership.  The coordinator receives an extra
    bonus for orchestration.  If a member's contribution is rejected by the
    coordinator, they receive no share.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from typing import Any
from uuid import UUID, uuid4

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class GlobStatus(Enum):
    """Lifecycle state of a glob."""

    FORMING = auto()       # coordinator created, accepting members
    ACTIVE = auto()        # all seats filled, work underway
    SUBMITTING = auto()    # solution being assembled
    DISSOLVED = auto()     # work complete, glob disbanded gracefully
    DISBANDED = auto()     # glob broke apart without completing


class GlobRole(Enum):
    """Role of an agent within a glob."""

    COORDINATOR = auto()   # orchestrates, decomposes, assembles
    MEMBER = auto()        # executes an assigned subtask


class ContributionStatus(Enum):
    """Status of a member's subtask contribution."""

    PENDING = auto()       # not yet submitted
    SUBMITTED = auto()     # submitted to coordinator, awaiting review
    ACCEPTED = auto()      # coordinator accepted this piece
    REJECTED = auto()      # coordinator rejected; member may resubmit
    REVISED = auto()       # member submitted a revision


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class GlobMembership:
    """Records one agent's participation in a glob.

    Attributes
    ----------
    agent_id : UUID
        The participating agent.
    role : GlobRole
        Whether this agent is the COORDINATOR or a MEMBER.
    subtask : str
        Plain-text description of the subtask assigned to this member.
        Coordinators' subtask is "orchestration" by convention.
    weight : float
        Fraction of the bounty this member receives on acceptance.
        All member weights + coordinator_bonus must sum to <= 1.0.
        The Exchange normalises these automatically before payout.
    contribution_status : ContributionStatus
        Current state of this member's contribution.
    contribution_text : str | None
        The actual content submitted for this subtask.
    joined_at : datetime
        When this agent joined the glob.
    submitted_at : datetime | None
        When the contribution was last submitted.
    id : UUID
        Unique identity for this membership record.
    """

    agent_id: UUID
    glob_id: UUID
    role: GlobRole = GlobRole.MEMBER
    subtask: str = ""
    weight: float = 1.0          # normalised by split_reputation() before payout
    contribution_status: ContributionStatus = ContributionStatus.PENDING
    contribution_text: str | None = None
    joined_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    submitted_at: datetime | None = None
    id: UUID = field(default_factory=uuid4)

    def submit(self, text: str) -> None:
        """Record a contribution from this member."""
        self.contribution_text = text
        self.contribution_status = ContributionStatus.SUBMITTED
        self.submitted_at = datetime.now(timezone.utc)

    def accept(self) -> None:
        self.contribution_status = ContributionStatus.ACCEPTED

    def reject(self) -> None:
        self.contribution_status = ContributionStatus.REJECTED

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "agent_id": str(self.agent_id),
            "glob_id": str(self.glob_id),
            "role": self.role.name,
            "subtask": self.subtask,
            "weight": self.weight,
            "contribution_status": self.contribution_status.name,
            "contribution_text": self.contribution_text,
            "joined_at": self.joined_at.isoformat(),
            "submitted_at": self.submitted_at.isoformat() if self.submitted_at else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GlobMembership":
        from uuid import UUID as _UUID
        from datetime import datetime as _dt
        m = cls(
            agent_id=_UUID(data["agent_id"]),
            glob_id=_UUID(data["glob_id"]),
            role=GlobRole[data["role"]],
            subtask=data.get("subtask", ""),
            weight=data.get("weight", 1.0),
            contribution_text=data.get("contribution_text"),
        )
        m.id = _UUID(data["id"])
        m.contribution_status = ContributionStatus[data["contribution_status"]]
        m.joined_at = _dt.fromisoformat(data["joined_at"])
        if data.get("submitted_at"):
            m.submitted_at = _dt.fromisoformat(data["submitted_at"])
        return m


@dataclass
class Glob:
    """A coalition of agents working together on a single problem.

    Attributes
    ----------
    problem_id : UUID
        The problem this glob is solving.
    coordinator_id : UUID
        The agent responsible for decomposition and assembly.
    name : str
        Human-readable name for this glob (e.g. "bug-squad-42").
    max_members : int
        Hard cap on total members including coordinator.
    coordinator_bonus : float
        Extra fraction of the bounty the coordinator earns on top of their
        normal weight, as compensation for orchestration overhead.
    status : GlobStatus
    memberships : list[GlobMembership]
    created_at : datetime
    dissolved_at : datetime | None
    id : UUID
    """

    problem_id: UUID
    coordinator_id: UUID
    name: str = ""
    max_members: int = 5
    coordinator_bonus: float = 0.10    # 10% orchestration bonus
    status: GlobStatus = GlobStatus.FORMING
    memberships: list[GlobMembership] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    dissolved_at: datetime | None = None
    id: UUID = field(default_factory=uuid4)

    # ------------------------------------------------------------------
    # Membership management
    # ------------------------------------------------------------------

    @property
    def member_ids(self) -> list[UUID]:
        return [m.agent_id for m in self.memberships]

    @property
    def coordinator_membership(self) -> GlobMembership | None:
        for m in self.memberships:
            if m.role == GlobRole.COORDINATOR:
                return m
        return None

    @property
    def is_full(self) -> bool:
        return len(self.memberships) >= self.max_members

    def add_member(self, agent_id: UUID, subtask: str = "", weight: float = 1.0) -> GlobMembership:
        """Add a member to the glob.  Raises ValueError if already full or agent already in."""
        if agent_id in self.member_ids:
            raise ValueError(f"Agent {agent_id} is already in glob {self.id}")
        if self.is_full:
            raise ValueError(f"Glob {self.id} is full ({self.max_members} members)")
        if self.status not in (GlobStatus.FORMING, GlobStatus.ACTIVE):
            raise ValueError(f"Cannot add members to glob in status {self.status.name}")
        role = GlobRole.COORDINATOR if agent_id == self.coordinator_id else GlobRole.MEMBER
        membership = GlobMembership(
            agent_id=agent_id,
            glob_id=self.id,
            role=role,
            subtask=subtask if subtask else ("orchestration" if role == GlobRole.COORDINATOR else ""),
            weight=weight,
        )
        self.memberships.append(membership)
        if len(self.memberships) >= 2:
            self.status = GlobStatus.ACTIVE
        return membership

    def activate(self) -> None:
        if self.status != GlobStatus.FORMING:
            raise ValueError("Glob must be FORMING to activate")
        self.status = GlobStatus.ACTIVE

    def dissolve(self) -> None:
        self.status = GlobStatus.DISSOLVED
        self.dissolved_at = datetime.now(timezone.utc)

    def disband(self) -> None:
        self.status = GlobStatus.DISBANDED
        self.dissolved_at = datetime.now(timezone.utc)

    def get_membership(self, agent_id: UUID) -> GlobMembership | None:
        for m in self.memberships:
            if m.agent_id == agent_id:
                return m
        return None

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "problem_id": str(self.problem_id),
            "coordinator_id": str(self.coordinator_id),
            "name": self.name,
            "max_members": self.max_members,
            "coordinator_bonus": self.coordinator_bonus,
            "status": self.status.name,
            "memberships": [m.to_dict() for m in self.memberships],
            "created_at": self.created_at.isoformat(),
            "dissolved_at": self.dissolved_at.isoformat() if self.dissolved_at else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Glob":
        from uuid import UUID as _UUID
        from datetime import datetime as _dt
        g = cls(
            problem_id=_UUID(data["problem_id"]),
            coordinator_id=_UUID(data["coordinator_id"]),
            name=data.get("name", ""),
            max_members=data.get("max_members", 5),
            coordinator_bonus=data.get("coordinator_bonus", 0.10),
        )
        g.id = _UUID(data["id"])
        g.status = GlobStatus[data["status"]]
        g.memberships = [GlobMembership.from_dict(m) for m in data.get("memberships", [])]
        g.created_at = _dt.fromisoformat(data["created_at"])
        if data.get("dissolved_at"):
            g.dissolved_at = _dt.fromisoformat(data["dissolved_at"])
        return g

    def __hash__(self) -> int:
        return hash(self.id)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Glob):
            return self.id == other.id
        return NotImplemented

    def __str__(self) -> str:
        return f"Glob({self.name!r}, status={self.status.name}, members={len(self.memberships)})"


@dataclass
class GlobSolution:
    """The composite solution assembled by a glob coordinator.

    The coordinator collects all accepted member contributions,
    assembles them into a unified answer, and submits this as a
    single solution on behalf of the glob.

    Attributes
    ----------
    glob_id : UUID
    problem_id : UUID
    solution_id : UUID
        The Schwarma Solution object created from this GlobSolution.
    assembled_by : UUID
        The coordinator agent_id.
    assembly_notes : str
        How the coordinator combined the contributions.
    member_contributions : dict[UUID, str]
        agent_id → contribution_text snapshot at submission time.
    id : UUID
    created_at : datetime
    """

    glob_id: UUID
    problem_id: UUID
    solution_id: UUID
    assembled_by: UUID
    assembly_notes: str = ""
    member_contributions: dict[str, str] = field(default_factory=dict)  # str(UUID) → text
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "glob_id": str(self.glob_id),
            "problem_id": str(self.problem_id),
            "solution_id": str(self.solution_id),
            "assembled_by": str(self.assembled_by),
            "assembly_notes": self.assembly_notes,
            "member_contributions": self.member_contributions,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GlobSolution":
        from uuid import UUID as _UUID
        from datetime import datetime as _dt
        gs = cls(
            glob_id=_UUID(data["glob_id"]),
            problem_id=_UUID(data["problem_id"]),
            solution_id=_UUID(data["solution_id"]),
            assembled_by=_UUID(data["assembled_by"]),
            assembly_notes=data.get("assembly_notes", ""),
            member_contributions=data.get("member_contributions", {}),
        )
        gs.id = _UUID(data["id"])
        gs.created_at = _dt.fromisoformat(data["created_at"])
        return gs


# ---------------------------------------------------------------------------
# Reputation splitting
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ReputationShare:
    """How much reputation a single agent earns from an accepted GlobSolution."""

    agent_id: UUID
    delta: int
    reason: str


def split_reputation(glob: Glob, total_bounty: int) -> list[ReputationShare]:
    """Compute per-agent reputation payouts for an accepted GlobSolution.

    Algorithm
    ---------
    1. Identify accepted member contributions (only they earn a share).
    2. Normalise their weights to sum to 1.0.
    3. Multiply each normalised weight by ``total_bounty``.
    4. Add ``coordinator_bonus`` (as a fraction of total_bounty) to the
       coordinator's payout on top of their normalised share.
    5. Round to integers; any rounding remainder goes to the coordinator.

    Only members whose contribution_status is ACCEPTED earn a share.
    The coordinator always earns at least the coordinator_bonus if the
    solution was accepted, regardless of their own contribution status.

    Returns
    -------
    list[ReputationShare]
        One entry per agent that earns something.  May be empty if no
        members have accepted contributions and total_bounty is 0.
    """
    accepted = [
        m for m in glob.memberships
        if m.contribution_status == ContributionStatus.ACCEPTED
        or m.role == GlobRole.COORDINATOR  # coordinator always earns base bonus
    ]
    if not accepted:
        logger.warning("split_reputation: no accepted contributions in glob %s", glob.id)
        return []

    # Deduplicate coordinator (may appear twice if they also have ACCEPTED)
    seen: set[UUID] = set()
    unique_accepted: list[GlobMembership] = []
    for m in accepted:
        if m.agent_id not in seen:
            unique_accepted.append(m)
            seen.add(m.agent_id)

    # Compute raw weights (members only, not coordinator bonus)
    member_weight_total = sum(
        m.weight for m in unique_accepted if m.role == GlobRole.MEMBER
    )
    coordinator_bonus_amount = int(total_bounty * glob.coordinator_bonus)
    member_pool = total_bounty - coordinator_bonus_amount

    shares: list[ReputationShare] = []
    allocated = 0

    coordinator_membership = glob.coordinator_membership

    for m in unique_accepted:
        if m.role == GlobRole.COORDINATOR:
            continue  # handled separately below
        if member_weight_total <= 0:
            share = 0
        else:
            normalised = m.weight / member_weight_total
            share = int(member_pool * normalised)
        allocated += share
        shares.append(ReputationShare(
            agent_id=m.agent_id,
            delta=share,
            reason=f"glob {glob.id} member contribution accepted",
        ))

    # Coordinator gets the bonus + any rounding remainder
    remainder = total_bounty - allocated - coordinator_bonus_amount
    coord_total = coordinator_bonus_amount + remainder
    if coordinator_membership:
        shares.append(ReputationShare(
            agent_id=coordinator_membership.agent_id,
            delta=coord_total,
            reason=f"glob {glob.id} coordinator orchestration bonus",
        ))

    return shares
