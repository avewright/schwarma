"""
Agent — a participant in the Schwarma exchange.

Each agent has:
  • a unique identity
  • a set of declared capabilities (what kinds of problems it can help with)
  • a reputation score maintained by the ReputationLedger
  • an async ``solve`` callback the exchange invokes when work is assigned
"""

from __future__ import annotations

import logging
import inspect
from functools import wraps
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Protocol
from uuid import UUID, uuid4

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------

class AgentCapability(Enum):
    """Broad categories an agent can declare competence in."""

    CODE_GENERATION = auto()
    CODE_REVIEW = auto()
    DEBUGGING = auto()
    TESTING = auto()
    DOCUMENTATION = auto()
    ARCHITECTURE = auto()
    DATA_ANALYSIS = auto()
    MATH = auto()
    NATURAL_LANGUAGE = auto()
    RESEARCH = auto()
    SECURITY_AUDIT = auto()
    PROOFREADING = auto()
    GOOD_FAITH_CHECK = auto()
    GENERAL = auto()


class ModelTier(Enum):
    """Quality / cost bracket for the underlying model.

    Used for tier-matching in swaps and triage so that cheap models
    don't free-ride on expensive ones.

    Integer values allow ordinal comparison (higher = more capable).
    SPECIALIZED is a wildcard — it matches any tier.
    """

    LIGHTWEIGHT = 1    # 7B-class, cheap, fast
    STANDARD = 2       # GPT-3.5-class
    PREMIUM = 3        # GPT-4-class, expensive
    SPECIALIZED = 4    # Domain-expert, any size — matches any tier


# ---------------------------------------------------------------------------
# Solver protocol — what the exchange expects from every agent
# ---------------------------------------------------------------------------

class SolverProtocol(Protocol):
    """Callable that receives a problem description and returns a solution body."""

    async def __call__(self, problem_description: str, context: dict[str, Any]) -> str:
        ...


def adapt_solver(fn):
    """Normalize common solver callback shapes to Schwarma's async signature.

    Accepted forms:
    - ``(description, context) -> str``
    - ``(description) -> str``
    - async variants of both

    Returns an async callable with signature
    ``(description: str, context: dict[str, Any]) -> str``.
    """
    if not callable(fn):
        raise TypeError("solver must be callable")

    sig = inspect.signature(fn)
    positional = [
        p
        for p in sig.parameters.values()
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    has_varargs = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in sig.parameters.values())
    argc = len(positional)

    if not has_varargs and argc not in (1, 2):
        raise TypeError(
            "solver must accept 1 or 2 positional args: "
            "(description) or (description, context)"
        )

    @wraps(fn)
    async def wrapped(description: str, context: dict[str, Any]) -> str:
        if has_varargs or argc >= 2:
            result = fn(description, context)
        else:
            result = fn(description)
        if inspect.isawaitable(result):
            result = await result
        return str(result)

    return wrapped


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

@dataclass
class Agent:
    """A registered participant in a Schwarma exchange."""

    name: str
    solver: SolverProtocol
    capabilities: set[AgentCapability] = field(default_factory=lambda: {AgentCapability.GENERAL})
    model_tier: ModelTier = ModelTier.STANDARD
    id: UUID = field(default_factory=uuid4)
    metadata: dict[str, Any] = field(default_factory=dict)

    # Preference: only receive triage pushes for these tags (empty = all)
    watch_tags: set = field(default_factory=set)

    # Runtime bookkeeping (not part of identity)
    _active_problem_ids: set[UUID] = field(default_factory=set, repr=False)
    _total_solved: int = field(default=0, repr=False)
    _total_reviewed: int = field(default=0, repr=False)

    def __post_init__(self) -> None:
        """Normalize solver to Schwarma's async two-argument callback shape."""
        self.solver = adapt_solver(self.solver)

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    def has_capability(self, cap: AgentCapability) -> bool:
        return cap in self.capabilities or AgentCapability.GENERAL in self.capabilities

    def has_any_capability(self, caps: set[AgentCapability]) -> bool:
        return bool(self.capabilities & caps) or AgentCapability.GENERAL in self.capabilities

    async def solve(self, problem_description: str, context: dict[str, Any] | None = None) -> str:
        """Delegate to the agent's solver callback."""
        ctx = context or {}
        return await self.solver(problem_description, ctx)

    # ------------------------------------------------------------------
    # Work-tracking helpers
    # ------------------------------------------------------------------

    def claim(self, problem_id: UUID) -> None:
        self._active_problem_ids.add(problem_id)

    def release(self, problem_id: UUID) -> None:
        self._active_problem_ids.discard(problem_id)
        self._total_solved += 1

    @property
    def active_count(self) -> int:
        return len(self._active_problem_ids)

    # ------------------------------------------------------------------
    # Dunder
    # ------------------------------------------------------------------

    def __hash__(self) -> int:
        return hash(self.id)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Agent):
            return self.id == other.id
        return NotImplemented

    def __str__(self) -> str:
        caps = ", ".join(c.name for c in sorted(self.capabilities, key=lambda c: c.name))
        return f"Agent({self.name!r}, caps=[{caps}])"
