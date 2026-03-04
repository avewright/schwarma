"""
Exchange — the central marketplace / orchestrator.

The Exchange is the single entry-point for all agent interactions:

  • Register agents
  • Post problems
  • Claim & solve problems
  • Request & submit reviews
  • Manage swaps
  • Query the reputation leaderboard

It coordinates the other components (TriageRouter, ReputationLedger,
SwapPool, EventBus) into a coherent workflow.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from dataclasses import dataclass, field
from typing import Any, Sequence
from uuid import UUID, uuid5, NAMESPACE_DNS

# Well-known sentinel UUID for oracle-generated reviews so they never
# collide with a real agent's reviewer_id.
_ORACLE_REVIEWER_ID = uuid5(NAMESPACE_DNS, "schwarma.oracle")

from schwarma.agent import Agent, AgentCapability, ModelTier
from schwarma.glob import (
    ContributionStatus,
    Glob,
    GlobMembership,
    GlobSolution,
    GlobStatus,
    ReputationShare,
    split_reputation,
)
from schwarma.archive import Archive, ArchiveConfig, ArchiveEntry, ReviewSnapshot
from schwarma.behavior import BehaviorAnalyzer, BehaviorConfig
from schwarma.errors import (
    CapacityError,
    DuplicateError,
    GuardBlockError,
    NotFoundError,
    PermissionError_,
    RateLimitError,
    StateError,
    SuspendedError,
    ValidationError,
)
from schwarma.calibration import CalibrationBank, CalibrationConfig, CalibrationResult
from schwarma.difficulty import DifficultyEstimator, DifficultyConfig
from schwarma.events import Event, EventBus, EventKind
from schwarma.guards import GuardAction, GuardResult, QualityConfig, redact_secrets, run_guards
from schwarma.problem import Problem, ProblemStatus, ProblemTag
from schwarma.reputation import LedgerConfig, ReputationEvent, ReputationLedger
from schwarma.rate_limit import RateLimitAction, RateLimitConfig, RateLimiter
from schwarma.review import Review, ReviewType, ReviewVerdict
from schwarma.skills import SkillConfig, SkillTracker
from schwarma.solution import Solution, SolutionVerdict
from schwarma.swap import SwapMatch, SwapPool
from schwarma.triage import TriageConfig, TriageRouter
from schwarma.trust import Sensitivity, TrustGate, TrustPolicy

from datetime import datetime, timedelta, timezone
from enum import Enum, auto as _auto

logger = logging.getLogger(__name__)


class DeploymentMode(Enum):
    """Controls privacy posture and access policy for the exchange instance.

    PRIVATE — single-team, full privacy enforced. Agents must be explicitly
              registered. Sensitivity defaults to INTERNAL. Good for
              internal CI/CD quality gates, internal code review, etc.

    TEAM    — semi-open instance shared by a known group. Agents can
              self-register but are reviewed. Sensitivity defaults to
              INTERNAL but PUBLIC problems are visible to all registered
              agents. Good for cross-team or partner deployments.

    PUBLIC  — community exchange. Anyone can register. Problems default to
              PUBLIC sensitivity. This is the community platform mode —
              think Stack Exchange for agents. Open challenges, Kaggle
              imports, and globs are all fully enabled here.
    """

    PRIVATE = _auto()
    TEAM = _auto()
    PUBLIC = _auto()


class ProblemSortKey(Enum):
    """Sort keys for priority-ordered problem retrieval."""

    PRIORITY = _auto()     # highest priority first
    BOUNTY = _auto()       # highest bounty first
    OLDEST = _auto()       # oldest first (FIFO)
    NEWEST = _auto()       # newest first (LIFO)


class HookPoint(Enum):
    """Extension points in the Exchange lifecycle.

    PRE hooks run before the core logic and receive a mutable context dict.
    POST hooks run after the core logic completes successfully.
    """

    PRE_POST_PROBLEM = _auto()
    POST_POST_PROBLEM = _auto()
    PRE_CLAIM_PROBLEM = _auto()
    POST_CLAIM_PROBLEM = _auto()
    PRE_SOLVE_PROBLEM = _auto()
    POST_SOLVE_PROBLEM = _auto()
    PRE_SUBMIT_REVIEW = _auto()
    POST_SUBMIT_REVIEW = _auto()


@dataclass
class ExchangeConfig:
    """Top-level knobs."""

    # Deployment posture — controls defaults for privacy, registration,
    # open-challenge ingestion, and glob formation.
    deployment_mode: DeploymentMode = DeploymentMode.PRIVATE

    reviews_required_for_accept: int = 2
    auto_assign: bool = True          # auto-triage when a problem is posted
    auto_review: bool = True          # auto-request reviews when a solution arrives
    max_active_per_agent: int = 5     # concurrency cap
    min_unique_reviewers: int = 1     # distinct reviewer agents required
    triage_config: TriageConfig = field(default_factory=TriageConfig)
    ledger_config: LedgerConfig = field(default_factory=LedgerConfig)

    # --- Privacy & safety ---
    trust_policy: TrustPolicy = field(default_factory=TrustPolicy)
    enable_content_guards: bool = True     # scan problems/solutions for secrets
    enable_effort_guards: bool = True      # reject trivially short solutions
    strict_privacy_mode: bool = False      # escalate FLAG findings to BLOCK
    redact_flagged_content: bool = True    # redact flagged content before persistence
    quality_config: QualityConfig = field(default_factory=QualityConfig)
    behavior_config: BehaviorConfig = field(default_factory=BehaviorConfig)

    # --- Reputation gating & staking ---
    min_reputation_to_claim: int = 10      # below this, can't claim any problem
    stake_fraction: float = 0.1            # fraction of bounty staked from solver's rep
    enable_staking: bool = True            # whether staking is active
    challenge_stake: int = 15              # reputation cost to challenge an accepted solution

    # --- Archive ---
    archive_config: ArchiveConfig = field(default_factory=ArchiveConfig)
    enable_archive: bool = True            # auto-archive on solution acceptance

    # --- Skill system ---
    skill_config: SkillConfig = field(default_factory=SkillConfig)
    enable_skill_tracking: bool = True     # track per-capability skill ratings
    use_effective_tier: bool = True        # use proven tier instead of declared

    # --- Calibration ---
    calibration_config: CalibrationConfig = field(default_factory=CalibrationConfig)
    enable_calibration: bool = False       # off by default (needs problems in bank)

    # --- Difficulty ---
    difficulty_config: DifficultyConfig = field(default_factory=DifficultyConfig)
    enable_difficulty: bool = True         # track empirical difficulty

    # --- Rate limits ---
    rate_limit_config: RateLimitConfig = field(default_factory=RateLimitConfig)
    enable_rate_limits: bool = False       # off by default (no breaking change)

    # --- Registration ---
    require_approval: bool = False         # new agents go into pending queue
    registration_hook: Any = None          # callable(Agent) -> bool, sync gate

    # --- Bounty escalation ---
    escalation_increment: int = 5          # bounty increase per escalation
    max_bounty: int = 200                  # hard cap on escalated bounty

    # --- Verification oracle ---
    verification_oracle: Any = None        # VerificationOracle instance (optional)
    oracle_auto_reject: bool = False       # when True, oracle FAIL auto-rejects

    # --- Revision dialogue ---
    max_revision_rounds: int = 3           # max back-and-forth per solution

    # --- Claim timeout ---
    claim_timeout_seconds: int = 0         # 0 = no timeout; seconds before stale claims expire

    # --- Review tiebreaker ---
    tiebreaker_extra_reviews: int = 1      # extra reviews requested on a tie
    tiebreaker_fallback: str = "reject"    # accept | reject | revision — used if still tied

    # --- Similarity / deduplication ---
    enable_similarity_check: bool = True   # scan archive on post for near-duplicates
    similarity_threshold: float = 0.5      # Jaccard score to flag as duplicate
    similarity_limit: int = 5              # max similar entries to return
    block_exact_duplicates: bool = False   # when True, raise DuplicateError on score >= 1.0


def _locked(fn):
    """Decorator that acquires ``self._lock`` for the duration of the call.

    Prevents concurrent mutation of Exchange state when multiple TCP
    clients are interleaving operations.
    """
    @functools.wraps(fn)
    async def wrapper(self, *args, **kwargs):
        async with self._lock:
            return await fn(self, *args, **kwargs)
    return wrapper


class Exchange:
    """The Schwarma marketplace."""

    def __init__(self, config: ExchangeConfig | None = None) -> None:
        self.config = config or ExchangeConfig()

        # Concurrency gate — all state-mutating async methods acquire this
        # so two concurrent callers can't interleave check-then-act logic
        # (e.g. two agents claiming the same problem simultaneously).
        self._lock = asyncio.Lock()

        # ---- stores ----
        self._agents: dict[UUID, Agent] = {}
        self._problems: dict[UUID, Problem] = {}
        self._solutions: dict[UUID, Solution] = {}
        self._reviews: dict[UUID, Review] = {}

        # ---- subsystems ----
        self.ledger = ReputationLedger(self.config.ledger_config)
        self.bus = EventBus()
        self.swap_pool = SwapPool(effective_tier_fn=self._effective_tier)
        self.skill_tracker = SkillTracker(self.config.skill_config)
        self.calibration_bank = CalibrationBank(self.config.calibration_config)
        self.difficulty = DifficultyEstimator(self.config.difficulty_config)
        self.router = TriageRouter(
            config=self.config.triage_config,
            reputation_fn=self.ledger.balance,
            skill_rating_fn=self._skill_rating_for_triage,
            effective_tier_fn=self._effective_tier,
        )
        self.trust_gate = TrustGate(self.config.trust_policy)
        self.behavior = BehaviorAnalyzer(self.config.behavior_config)
        self.archive = Archive(self.config.archive_config)
        self.rate_limiter = RateLimiter(self.config.rate_limit_config)

        # Glob coalition stores
        self._globs: dict[UUID, Glob] = {}
        self._glob_solutions: dict[UUID, GlobSolution] = {}

        # Tracks reputation staked per (agent_id, problem_id)
        self._stakes: dict[tuple[UUID, UUID], int] = {}

        # Tracks when an agent claimed a problem (for solve-speed analysis)
        self._claim_times: dict[tuple[UUID, UUID], datetime] = {}

        # Maps injected problem_id → calibration_problem_id
        self._calibration_map: dict[UUID, UUID] = {}

        # Tracks challenge stakes: solution_id → (challenger_id, stake)
        self._challenge_stakes: dict[UUID, tuple[UUID, int]] = {}

        # Suspended agents
        self._suspended: set[UUID] = set()
        self._suspension_reasons: dict[UUID, str] = {}

        # Pending registration approval queue
        self._pending_agents: dict[UUID, Agent] = {}

        # Lifecycle hooks: HookPoint → ordered list of async callables
        self._hooks: dict[HookPoint, list] = {hp: [] for hp in HookPoint}

        # Per-agent notification inbox
        self._inboxes: dict[UUID, list[dict[str, Any]]] = {}

        # Agent presence / heartbeat tracking
        self._heartbeats: dict[UUID, datetime] = {}
        self._heartbeat_timeout: float = 120.0  # seconds before "offline"

        # Wire inbox delivery on relevant events
        self.bus.subscribe_all(self._deliver_to_inbox)

    # ==================================================================
    # Lifecycle hooks
    # ==================================================================

    def add_hook(
        self,
        point: HookPoint,
        callback,
    ) -> None:
        """Register an async hook for a lifecycle point.

        *callback* must be an async callable accepting a single ``dict``
        context argument.  Hooks are called in registration order.
        """
        self._hooks[point].append(callback)

    def remove_hook(self, point: HookPoint, callback) -> None:
        """Remove a previously registered hook."""
        self._hooks[point].remove(callback)

    async def _run_hooks(self, point: HookPoint, ctx: dict) -> None:
        """Execute all hooks registered for *point* with *ctx*."""
        for hook in self._hooks[point]:
            await hook(ctx)

    # ==================================================================
    # Agent registration
    # ==================================================================

    def register(self, agent: Agent) -> None:
        """Register an agent so it can participate.

        If ``require_approval`` is set, the agent is placed in a pending
        queue and cannot act until :meth:`approve_agent` is called.

        If ``registration_hook`` is set, it is called with the agent and
        must return ``True`` for registration to proceed (acts as a
        proof-of-work or eligibility gate).
        """
        if agent.id in self._agents:
            raise DuplicateError(f"Agent {agent.id} already registered")
        if agent.id in self._pending_agents:
            raise DuplicateError(f"Agent {agent.id} already pending approval")

        # Registration hook (proof-of-work / eligibility gate)
        hook = self.config.registration_hook
        if hook is not None:
            if not hook(agent):
                raise PermissionError_(
                    f"Agent {agent.name} rejected by registration hook"
                )

        if self.config.require_approval:
            self._pending_agents[agent.id] = agent
            logger.info("Agent %s queued for approval", agent.name)
            return

        self._agents[agent.id] = agent
        logger.info("Registered agent %s", agent)

        # Emit registration event (fire-and-forget safe: bus is sync-safe)
        import asyncio
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.bus.publish(Event(
                kind=EventKind.AGENT_REGISTERED,
                source_agent_id=agent.id,
            )))
        except RuntimeError:
            pass  # no event loop yet; skip event

    def approve_agent(self, agent_id: UUID) -> Agent:
        """Approve a pending agent, moving it to the active roster."""
        agent = self._pending_agents.pop(agent_id, None)
        if agent is None:
            raise NotFoundError("pending agent", agent_id)
        self._agents[agent.id] = agent
        logger.info("Approved agent %s", agent.name)
        return agent

    def reject_pending_agent(self, agent_id: UUID) -> None:
        """Reject a pending agent registration."""
        if agent_id not in self._pending_agents:
            raise NotFoundError("pending agent", agent_id)
        del self._pending_agents[agent_id]
        logger.info("Rejected pending agent %s", agent_id)

    @property
    def pending_agents(self) -> list[Agent]:
        """Agents awaiting approval."""
        return list(self._pending_agents.values())

    def get_agent(self, agent_id: UUID) -> Agent:
        return self._agents[agent_id]

    @property
    def agents(self) -> list[Agent]:
        return list(self._agents.values())

    # ==================================================================
    # Agent suspension
    # ==================================================================

    @_locked
    async def suspend_agent(
        self, agent_id: UUID, *, reason: str = ""
    ) -> None:
        """Suspend an agent — they cannot claim, solve, or review."""
        if agent_id not in self._agents:
            raise NotFoundError("agent", agent_id)
        self._suspended.add(agent_id)
        self._suspension_reasons[agent_id] = reason

        await self.bus.publish(Event(
            kind=EventKind.AGENT_SUSPENDED,
            source_agent_id=agent_id,
            payload={"reason": reason},
        ))
        logger.info("Agent %s suspended: %s", agent_id, reason)

    @_locked
    async def unsuspend_agent(self, agent_id: UUID) -> None:
        """Lift a suspension."""
        self._suspended.discard(agent_id)
        self._suspension_reasons.pop(agent_id, None)
        logger.info("Agent %s unsuspended", agent_id)

    def is_suspended(self, agent_id: UUID) -> bool:
        """Return *True* if the agent is currently suspended."""
        return agent_id in self._suspended

    def update_watch_tags(self, agent_id: UUID, tags: set) -> None:
        """Update the problem-tag preferences for *agent_id*.

        When set, the agent only receives triage pushes for problems
        matching at least one of these tags.  Pass an empty set to clear.
        """
        agent = self._agents.get(agent_id)
        if agent is None:
            raise NotFoundError("agent", str(agent_id))
        agent.watch_tags = set(tags)

    def _require_not_suspended(self, agent_id: UUID) -> None:
        """Raise :class:`SuspendedError` if agent is suspended."""
        if agent_id in self._suspended:
            reason = self._suspension_reasons.get(agent_id, "")
            raise SuspendedError(
                f"Agent {agent_id} is suspended"
                + (f": {reason}" if reason else "")
            )

    def _enforce_rate_limit(self, agent_id: UUID, action: RateLimitAction) -> None:
        """Raise :class:`RateLimitError` if the agent has exceeded a rate limit."""
        if not self.config.enable_rate_limits:
            return
        if not self.rate_limiter.check_and_record(agent_id, action):
            raise RateLimitError(
                f"Rate limit exceeded for {action.name}. Try again later."
            )

    # ==================================================================
    # Problem lifecycle
    # ==================================================================

    @_locked
    async def post_problem(self, problem: Problem) -> Problem:
        """Post a new problem to the exchange.

        Content guards are run on the description.  If the content is
        blocked, a ``ValueError`` is raised.  If flagged, the problem is
        still posted but an event is emitted for operator review.

        **Idempotent:** Re-posting a problem with the same ID returns the
        existing record without side effects.
        """
        return await self._post_problem_impl(problem)

    async def _post_problem_impl(self, problem: Problem) -> Problem:
        """Internal (unlocked) implementation of :meth:`post_problem`.

        Used by compound methods that already hold the lock.
        """
        # --- Idempotency: same ID already posted → return existing ---
        if problem.id in self._problems:
            return self._problems[problem.id]

        await self._run_hooks(HookPoint.PRE_POST_PROBLEM, {
            "problem": problem,
        })

        self._require_not_suspended(problem.author_id)
        self._enforce_rate_limit(problem.author_id, RateLimitAction.POST_PROBLEM)

        # --- Content guard: scan problem description ---
        if self.config.enable_content_guards:
            guard_result = run_guards(
                problem.description,
                check_secrets=True,
                check_effort=False,
                block_flagged=self.config.strict_privacy_mode,
            )
            if guard_result.action == GuardAction.BLOCK:
                raise GuardBlockError(
                    f"Problem blocked by content guard: {guard_result}"
                )
            if guard_result.action == GuardAction.FLAG:
                if self.config.strict_privacy_mode:
                    raise GuardBlockError(
                        f"Problem blocked by strict privacy policy: {guard_result}"
                    )
                if self.config.redact_flagged_content:
                    problem.description = redact_secrets(problem.description)
                logger.warning("Problem %s flagged: %s", problem.id, guard_result)

        # --- Similarity check: scan archive for near-duplicates ---
        similar_hits: list[tuple[Any, float]] = []
        if self.config.enable_archive and self.config.enable_similarity_check:
            query_text = f"{problem.title} {problem.description}"
            similar_hits = self.archive.search_similar(
                query_text,
                threshold=self.config.similarity_threshold,
                limit=self.config.similarity_limit,
            )
            if similar_hits:
                # Check for exact duplicate blocking
                if self.config.block_exact_duplicates:
                    for entry, score in similar_hits:
                        if score >= 1.0:
                            raise DuplicateError(
                                f"Problem is an exact duplicate of archived entry "
                                f"{entry.id} (score={score:.2f})"
                            )

                logger.info(
                    "Problem %s has %d similar archived entries (best=%.2f)",
                    problem.id, len(similar_hits), similar_hits[0][1],
                )

        self._problems[problem.id] = problem

        # Reputation: tiny reward for participation
        self.ledger.record(
            problem.author_id,
            ReputationEvent.PROBLEM_POSTED,
            related_id=problem.id,
        )

        await self.bus.publish(Event(
            kind=EventKind.PROBLEM_POSTED,
            source_agent_id=problem.author_id,
            problem_id=problem.id,
        ))

        # Emit duplicate event after posting (non-blocking — problem is accepted)
        if similar_hits:
            await self.bus.publish(Event(
                kind=EventKind.DUPLICATE_DETECTED,
                source_agent_id=problem.author_id,
                problem_id=problem.id,
                payload={
                    "similar_count": len(similar_hits),
                    "matches": [
                        {"entry_id": str(entry.id), "score": round(score, 3)}
                        for entry, score in similar_hits
                    ],
                },
            ))

        # Auto-triage
        if self.config.auto_assign:
            await self._auto_assign(problem)

        await self._run_hooks(HookPoint.POST_POST_PROBLEM, {
            "problem": problem,
        })

        logger.info("Problem posted: %s", problem)
        return problem

    @_locked
    async def decompose_problem(
        self,
        parent_id: UUID,
        sub_problems: list[Problem],
        *,
        sequential: bool = False,
    ) -> list[Problem]:
        """Break a parent problem into sub-problems.

        Each sub-problem's ``parent_id`` is set to *parent_id* and the
        parent's ``sub_problem_ids`` list is extended.  If *sequential* is
        ``True``, each sub-problem depends on the previous one (chain).

        Returns the list of posted sub-problems.
        """
        parent = self._problems.get(parent_id)
        if parent is None:
            raise NotFoundError("problem", str(parent_id))

        posted: list[Problem] = []
        prev_id: UUID | None = None

        for sp in sub_problems:
            sp.parent_id = parent_id
            if sequential and prev_id is not None:
                if prev_id not in sp.depends_on:
                    sp.depends_on.append(prev_id)
            result = await self._post_problem_impl(sp)
            parent.sub_problem_ids.append(result.id)
            posted.append(result)
            prev_id = result.id

        return posted

    def dependencies_met(self, problem_id: UUID) -> bool:
        """Return True if all dependencies of *problem_id* are CLOSED."""
        problem = self._problems.get(problem_id)
        if problem is None:
            raise NotFoundError("problem", str(problem_id))
        for dep_id in problem.depends_on:
            dep = self._problems.get(dep_id)
            if dep is None or dep.status != ProblemStatus.CLOSED:
                return False
        return True

    def sub_problems(self, parent_id: UUID) -> list[Problem]:
        """Return all sub-problems of *parent_id*."""
        parent = self._problems.get(parent_id)
        if parent is None:
            raise NotFoundError("problem", str(parent_id))
        return [
            self._problems[sid]
            for sid in parent.sub_problem_ids
            if sid in self._problems
        ]

    @_locked
    async def post_problems(
        self,
        problems: Sequence[Problem],
    ) -> list[Problem]:
        """Batch-post multiple problems in a single call.

        Each problem is posted individually (guards, reputation, events all
        fire per problem).  If a problem is blocked by content guards, it is
        skipped — the remaining problems are still posted.

        Returns the list of successfully posted problems (preserving order).
        """
        posted: list[Problem] = []
        for problem in problems:
            try:
                result = await self._post_problem_impl(problem)
                posted.append(result)
            except (GuardBlockError, SuspendedError, RateLimitError):
                logger.warning("Batch post skipped problem %s", problem.id)
                continue
        return posted

    @_locked
    async def claim_problem(self, problem_id: UUID, agent_id: UUID) -> Problem:
        """An agent claims a problem to solve it.

        Enforces:
          • Concurrency limit
          • Trust-tier clearance for problem sensitivity
          • Minimum reputation to claim
          • Reputation staking (deducts stake, refunded on acceptance)

        **Idempotent:** If the agent already claimed this problem, return it
        without side effects.
        """
        problem = self._problems[problem_id]
        agent = self._agents[agent_id]

        # --- Idempotency: agent already claimed this problem ---
        if agent_id in problem.claimed_by:
            return problem

        self._require_not_suspended(agent_id)
        self._enforce_rate_limit(agent_id, RateLimitAction.CLAIM_PROBLEM)

        await self._run_hooks(HookPoint.PRE_CLAIM_PROBLEM, {
            "problem": problem,
            "agent": agent,
        })

        if agent.active_count >= self.config.max_active_per_agent:
            raise CapacityError(f"Agent {agent.name} at concurrency limit")

        # --- Trust gate: can this agent see this problem? ---
        if not self.trust_gate.can_access(agent_id, problem.sensitivity):
            raise PermissionError_(
                f"Agent {agent.name} (tier={self.trust_gate.get_tier(agent_id).name}) "
                f"cannot access {problem.sensitivity.name} problems"
            )

        # --- Reputation gating ---
        balance = self.ledger.balance(agent_id)
        if balance < self.config.min_reputation_to_claim:
            raise PermissionError_(
                f"Agent {agent.name} reputation {balance} below minimum "
                f"{self.config.min_reputation_to_claim} to claim"
            )

        # --- Model-tier gating (uses effective tier when skill tracking is on) ---
        if problem.min_solver_tier is not None:
            effective = self._effective_tier(agent)
            if (
                effective != ModelTier.SPECIALIZED
                and effective.value < problem.min_solver_tier.value
            ):
                raise PermissionError_(
                    f"Agent {agent.name} (effective tier={effective.name}) below "
                    f"minimum solver tier {problem.min_solver_tier.name} "
                    f"required by '{problem.title}'"
                )

        # --- Reputation staking ---
        if self.config.enable_staking:
            stake = max(1, int(problem.bounty * self.config.stake_fraction))
            if balance < stake:
                raise PermissionError_(
                    f"Agent {agent.name} cannot afford stake of {stake} "
                    f"(balance={balance})"
                )
            self.ledger.record(
                agent_id,
                ReputationEvent.PENALTY,
                delta=-stake,
                reason=f"Stake for claiming '{problem.title}'",
                related_id=problem_id,
            )
            self._stakes[(agent_id, problem_id)] = stake

        # --- Dependency gate: all depends_on problems must be CLOSED ---
        for dep_id in problem.depends_on:
            dep = self._problems.get(dep_id)
            if dep is None or dep.status != ProblemStatus.CLOSED:
                dep_title = dep.title if dep else str(dep_id)
                raise StateError(
                    f"Cannot claim '{problem.title}': dependency "
                    f"'{dep_title}' is not yet resolved"
                )

        problem.claim(agent_id)
        agent.claim(problem_id)
        self._claim_times[(agent_id, problem_id)] = datetime.now(timezone.utc)

        # Track attempt in difficulty estimator
        if self.config.enable_difficulty:
            self.difficulty.record_attempt(problem_id)

        await self.bus.publish(Event(
            kind=EventKind.PROBLEM_CLAIMED,
            source_agent_id=agent_id,
            problem_id=problem_id,
        ))

        # --- Calibration injection: transparently inject a test problem ---
        if self.config.enable_calibration:
            await self._maybe_inject_calibration(agent)

        await self._run_hooks(HookPoint.POST_CLAIM_PROBLEM, {
            "problem": problem,
            "agent": agent,
        })

        logger.info("Agent %s claimed problem %s", agent.name, problem.title)
        return problem

    @_locked
    async def solve_problem(
        self,
        problem_id: UUID,
        agent_id: UUID,
        *,
        solution_body: str | None = None,
    ) -> Solution:
        """Have an agent solve a problem.

        If *solution_body* is ``None``, the agent's solver callback is invoked.

        Content guards (secrets + effort) are run on the solution body.
        """
        problem = self._problems[problem_id]
        agent = self._agents[agent_id]

        self._require_not_suspended(agent_id)
        self._enforce_rate_limit(agent_id, RateLimitAction.SUBMIT_SOLUTION)

        await self._run_hooks(HookPoint.PRE_SOLVE_PROBLEM, {
            "problem": problem,
            "agent": agent,
            "solution_body": solution_body,
        })

        if solution_body is None:
            solution_body = await agent.solve(problem.description, problem.context)

        # --- Content guards on solution ---
        if self.config.enable_content_guards:
            guard_result = run_guards(
                solution_body,
                check_secrets=True,
                check_effort=self.config.enable_effort_guards,
                block_flagged=self.config.strict_privacy_mode,
                quality_config=self.config.quality_config,
            )
            if guard_result.action == GuardAction.BLOCK:
                raise GuardBlockError(
                    f"Solution blocked by content guard: {guard_result}"
                )
            if guard_result.action == GuardAction.FLAG:
                if self.config.redact_flagged_content:
                    solution_body = redact_secrets(solution_body)
                logger.warning(
                    "Solution by %s flagged: %s", agent.name, guard_result
                )

        solution = Solution(
            problem_id=problem_id,
            author_id=agent_id,
            body=solution_body,
        )
        self._solutions[solution.id] = solution
        problem.add_solution(solution.id)
        agent.release(problem_id)

        # Track solve in behavior analyzer
        claimed_at = self._claim_times.pop((agent_id, problem_id), solution.created_at)
        self.behavior.record_solve(
            solver_id=agent_id,
            problem_author_id=problem.author_id,
            claimed_at=claimed_at,
            solved_at=solution.created_at,
        )

        # Reputation: submitted a solution
        self.ledger.record(
            agent_id,
            ReputationEvent.SOLUTION_SUBMITTED,
            related_id=solution.id,
        )

        await self.bus.publish(Event(
            kind=EventKind.SOLUTION_SUBMITTED,
            source_agent_id=agent_id,
            problem_id=problem_id,
            solution_id=solution.id,
        ))

        # --- Verification oracle (if configured) ---
        if self.config.verification_oracle is not None:
            await self._run_verification_oracle(solution, problem)

        # Auto-request reviews
        if self.config.auto_review:
            await self._auto_request_reviews(solution)

        await self._run_hooks(HookPoint.POST_SOLVE_PROBLEM, {
            "problem": problem,
            "solution": solution,
        })

        logger.info("Solution submitted by %s for %s", agent.name, problem.title)
        return solution

    # NOTE: Not locked — delegates to locked methods internally
    async def claim_and_solve(self, problem_id: UUID, agent_id: UUID) -> Solution:
        """Convenience: claim + solve in one step."""
        await self.claim_problem(problem_id, agent_id)
        return await self.solve_problem(problem_id, agent_id)

    def get_problem(self, problem_id: UUID) -> Problem:
        return self._problems[problem_id]

    def open_problems(
        self,
        sort_by: ProblemSortKey = ProblemSortKey.PRIORITY,
        *,
        tags: set[ProblemTag] | None = None,
        limit: int = 0,
    ) -> list[Problem]:
        """Return open problems, optionally filtered and sorted.

        Args:
            sort_by: Ordering — PRIORITY (default), BOUNTY, OLDEST, NEWEST.
            tags: If given, only return problems whose tags overlap.
            limit: Max results (0 = unlimited).
        """
        result = [p for p in self._problems.values() if p.is_open]
        if tags:
            result = [p for p in result if p.tags & tags]
        result = self._sort_problems(result, sort_by)
        if limit > 0:
            result = result[:limit]
        return result

    def open_problems_for(
        self,
        agent_id: UUID,
        sort_by: ProblemSortKey = ProblemSortKey.PRIORITY,
        *,
        tags: set[ProblemTag] | None = None,
        limit: int = 0,
    ) -> list[Problem]:
        """Open problems visible to *agent_id* given their trust tier."""
        visible = self.trust_gate.filter_visible(
            agent_id,
            [p for p in self._problems.values() if p.is_open],
        )
        if tags:
            visible = [p for p in visible if p.tags & tags]
        visible = self._sort_problems(visible, sort_by)
        if limit > 0:
            visible = visible[:limit]
        return visible

    @staticmethod
    def _sort_problems(
        problems: list[Problem],
        sort_by: ProblemSortKey,
    ) -> list[Problem]:
        """Sort a list of problems by the given key."""
        if sort_by == ProblemSortKey.PRIORITY:
            return sorted(problems, key=lambda p: (-p.priority, -p.bounty))
        elif sort_by == ProblemSortKey.BOUNTY:
            return sorted(problems, key=lambda p: (-p.bounty, -p.priority))
        elif sort_by == ProblemSortKey.OLDEST:
            return sorted(problems, key=lambda p: p.created_at)
        elif sort_by == ProblemSortKey.NEWEST:
            return sorted(problems, key=lambda p: p.created_at, reverse=True)
        return problems

    # ==================================================================
    # Review lifecycle
    # ==================================================================

    async def request_review(
        self,
        solution_id: UUID,
        reviewer_id: UUID,
        review_type: ReviewType = ReviewType.CORRECTNESS,
    ) -> None:
        """Explicitly ask an agent to review a solution."""
        await self.bus.publish(Event(
            kind=EventKind.REVIEW_REQUESTED,
            target_agent_id=reviewer_id,
            solution_id=solution_id,
            payload={"review_type": review_type.name},
        ))

    @_locked
    async def submit_review(self, review: Review) -> Review:
        """Submit a completed review.

        **Idempotent:** If this reviewer already reviewed this solution,
        the existing review is returned without side effects.
        """
        # --- Idempotency: same reviewer + same solution → return existing ---
        solution = self._solutions[review.solution_id]
        for existing_rid in solution.review_ids:
            existing = self._reviews.get(existing_rid)
            if existing is not None and existing.reviewer_id == review.reviewer_id:
                return existing

        self._require_not_suspended(review.reviewer_id)
        self._enforce_rate_limit(review.reviewer_id, RateLimitAction.SUBMIT_REVIEW)

        await self._run_hooks(HookPoint.PRE_SUBMIT_REVIEW, {
            "review": review,
            "solution": solution,
        })

        self._reviews[review.id] = review
        solution.review_ids.append(review.id)

        # Increment reviewer's counter
        reviewer = self._agents.get(review.reviewer_id)
        if reviewer is not None:
            reviewer._total_reviewed += 1

        # --- Track in behavior analyzer ---
        self.behavior.record_review(
            reviewer_id=review.reviewer_id,
            solution_author_id=solution.author_id,
            verdict=review.verdict.name,
        )

        # Track pairwise interaction + compute diminishing factor
        self.ledger.record_pairwise(review.reviewer_id, solution.author_id)
        dim_factor = self.ledger.diminishing_factor(
            review.reviewer_id, solution.author_id
        )

        # Reputation for reviewing (with diminishing returns)
        base_reward = self.ledger.config.rewards.get(ReputationEvent.REVIEW_SUBMITTED, 3)
        adjusted = max(1, int(base_reward * dim_factor))
        self.ledger.record(
            review.reviewer_id,
            ReputationEvent.REVIEW_SUBMITTED,
            delta=adjusted,
            related_id=review.id,
        )

        # Auto-promote reviewer trust tier
        self.trust_gate.maybe_promote(
            review.reviewer_id, self.ledger.balance(review.reviewer_id)
        )

        await self.bus.publish(Event(
            kind=EventKind.REVIEW_SUBMITTED,
            source_agent_id=review.reviewer_id,
            solution_id=solution.id,
            review_id=review.id,
        ))

        # Check if enough reviews to auto-accept/reject
        await self._evaluate_solution(solution)

        await self._run_hooks(HookPoint.POST_SUBMIT_REVIEW, {
            "review": review,
            "solution": solution,
        })

        logger.info("Review submitted: %s", review)
        return review

    @_locked
    async def request_revision(
        self,
        solution_id: UUID,
        reviewer_id: UUID,
        feedback: str,
    ) -> None:
        """Request that the solver revise their solution.

        Stores structured feedback in the solution's revision history and
        sets the verdict to NEEDS_REVISION.  The problem is kept CLAIMED
        so the solver can resubmit.

        Raises :class:`StateError` if the maximum number of revision rounds
        has been reached.
        """
        from schwarma.solution import RevisionRound

        solution = self._solutions.get(solution_id)
        if solution is None:
            raise NotFoundError("solution", str(solution_id))

        round_num = len(solution.revision_history) + 1
        if round_num > self.config.max_revision_rounds:
            raise StateError(
                f"Solution {solution_id} has reached the maximum of "
                f"{self.config.max_revision_rounds} revision rounds"
            )

        rr = RevisionRound(
            round_number=round_num,
            reviewer_feedback=feedback,
            reviewer_id=reviewer_id,
        )
        solution.revision_history.append(rr)
        solution.request_revision()

        # Keep problem CLAIMED so solver can resubmit
        problem = self._problems[solution.problem_id]
        problem.request_revision()

        await self.bus.publish(Event(
            kind=EventKind.SOLUTION_REVISION_REQUESTED,
            source_agent_id=reviewer_id,
            target_agent_id=solution.author_id,
            solution_id=solution_id,
            problem_id=solution.problem_id,
            payload={"feedback": feedback, "round": round_num},
        ))

        logger.info(
            "Revision requested for solution %s (round %d)", solution_id, round_num
        )

    @_locked
    async def revise_solution(
        self,
        solution_id: UUID,
        agent_id: UUID,
        *,
        revised_body: str | None = None,
    ) -> None:
        """Submit a revised solution body for the latest revision round.

        If *revised_body* is ``None``, the solver callback is invoked with
        revision context (feedback + attempt number).
        """
        from schwarma.solution import RevisionRound

        solution = self._solutions.get(solution_id)
        if solution is None:
            raise NotFoundError("solution", str(solution_id))
        if solution.author_id != agent_id:
            raise PermissionError_(
                f"Agent {agent_id} is not the author of solution {solution_id}"
            )
        if not solution.revision_history:
            raise StateError("No revision has been requested yet")

        current_round = solution.revision_history[-1]
        if current_round.revised_body:
            raise StateError(
                f"Round {current_round.round_number} already has a revised body"
            )

        if revised_body is None:
            # Invoke solver callback with revision context
            agent = self._agents[agent_id]
            problem = self._problems[solution.problem_id]
            ctx = dict(problem.context)
            ctx["revision_feedback"] = current_round.reviewer_feedback
            ctx["attempt"] = current_round.round_number + 1
            ctx["previous_body"] = solution.body
            revised_body = await agent.solve(problem.description, ctx)

        current_round.revised_body = revised_body
        solution.body = revised_body
        solution.verdict = SolutionVerdict.PENDING  # re-enter review

        logger.info(
            "Solution %s revised (round %d)", solution_id, current_round.round_number
        )

    async def auto_review_solution(
        self,
        solution_id: UUID,
        reviewer_id: UUID,
        review_type: ReviewType = ReviewType.CORRECTNESS,
    ) -> Review:
        """Have a reviewer agent generate a review via its solver callback.

        The reviewer's solver is invoked with a prompt describing the review
        task.  The response is parsed into a Review object.
        """
        solution = self._solutions[solution_id]
        problem = self._problems[solution.problem_id]
        reviewer = self._agents[reviewer_id]

        prompt = (
            f"Review the following solution for the problem below.\n\n"
            f"## Problem\n{problem.title}\n{problem.description}\n\n"
            f"## Solution\n{solution.body}\n\n"
            f"Review type: {review_type.name}\n"
            f"Respond with APPROVE, REJECT, or REQUEST_CHANGES followed by your reasoning."
        )
        raw = await reviewer.solve(prompt, {"review_mode": True})
        verdict = self._parse_review_verdict(raw)

        review = Review(
            solution_id=solution_id,
            reviewer_id=reviewer_id,
            review_type=review_type,
            verdict=verdict,
            body=raw,
        )
        return await self.submit_review(review)

    # ==================================================================
    # Swap interface
    # ==================================================================

    @_locked
    async def submit_swap(self, agent_id: UUID, problem_id: UUID) -> None:
        """Submit a problem to the swap pool."""
        agent = self._agents[agent_id]
        problem = self._problems[problem_id]
        self.swap_pool.submit(agent, problem)

        await self.bus.publish(Event(
            kind=EventKind.SWAP_PROPOSED,
            source_agent_id=agent_id,
            problem_id=problem_id,
        ))

    @_locked
    async def run_swaps(self) -> list[SwapMatch]:
        """Match waiting swap entries and return all new matches."""
        matches = self.swap_pool.match_all()
        for match in matches:
            await self.bus.publish(Event(
                kind=EventKind.SWAP_ACCEPTED,
                payload={
                    "agent_a": str(match.entry_a.agent.id),
                    "agent_b": str(match.entry_b.agent.id),
                },
            ))
        return matches

    @_locked
    async def complete_swap(self, match_id: UUID) -> None:
        self.swap_pool.complete(match_id)
        # Both agents get swap reputation
        match = next(m for m in self.swap_pool.matches if m.id == match_id)
        for agent in match.agents:
            self.ledger.record(
                agent.id,
                ReputationEvent.SWAP_COMPLETED,
                related_id=match_id,
            )
        await self.bus.publish(Event(
            kind=EventKind.SWAP_COMPLETED,
            payload={"match_id": str(match_id)},
        ))

    # ==================================================================
    # Challenge mechanism
    # ==================================================================

    @_locked
    async def challenge_solution(
        self,
        solution_id: UUID,
        challenger_id: UUID,
        reason: str = "",
    ) -> Problem:
        """Challenge an accepted solution, triggering re-review.

        The challenger stakes ``challenge_stake`` reputation.  If the
        challenge succeeds (solution is ultimately rejected on re-review),
        the stake is refunded with a bonus.  If the challenge fails, the
        stake is forfeited.

        Returns the re-opened problem.
        """
        solution = self._solutions[solution_id]
        problem = self._problems[solution.problem_id]
        challenger = self._agents[challenger_id]

        if solution.verdict != SolutionVerdict.ACCEPTED:
            raise StateError(
                f"Can only challenge ACCEPTED solutions "
                f"(current verdict: {solution.verdict.name})"
            )

        if challenger_id == solution.author_id:
            raise ValidationError("Cannot challenge your own solution")

        # Stake reputation
        balance = self.ledger.balance(challenger_id)
        stake = self.config.challenge_stake
        if balance < stake:
            raise PermissionError_(
                f"Agent {challenger.name} cannot afford challenge stake of "
                f"{stake} (balance={balance})"
            )

        self.ledger.record(
            challenger_id,
            ReputationEvent.PENALTY,
            delta=-stake,
            reason=f"Challenge stake for solution on '{problem.title}'",
            related_id=solution_id,
        )
        # Track challenge stake for resolution
        self._challenge_stakes[solution_id] = (challenger_id, stake)

        # Re-open the solution and problem for review
        solution.verdict = SolutionVerdict.PENDING
        solution.review_ids.clear()
        problem.status = ProblemStatus.SOLVED  # back to SOLVED awaiting reviews

        # Clear old reviews for this solution to force fresh review cycle
        old_review_ids = [
            rid for rid, r in self._reviews.items()
            if r.solution_id == solution_id
        ]
        for rid in old_review_ids:
            del self._reviews[rid]

        await self.bus.publish(Event(
            kind=EventKind.SOLUTION_CHALLENGED,
            source_agent_id=challenger_id,
            problem_id=problem.id,
            solution_id=solution_id,
            payload={"reason": reason},
        ))

        # Auto-request fresh reviews
        if self.config.auto_review:
            await self._auto_request_reviews(solution)

        logger.info(
            "Solution %s challenged by %s — re-review triggered",
            solution_id, challenger.name,
        )
        return problem

    # ==================================================================
    # Queries
    # ==================================================================

    def leaderboard(self, top_n: int = 10) -> list[dict[str, Any]]:
        """Return the reputation leaderboard with agent details."""
        raw = self.ledger.leaderboard(top_n)
        result = []
        for agent_id, score in raw:
            agent = self._agents.get(agent_id)
            result.append({
                "agent_id": agent_id,
                "name": agent.name if agent else "unknown",
                "reputation": score,
            })
        return result

    def get_solution(self, solution_id: UUID) -> Solution:
        return self._solutions[solution_id]

    def get_review(self, review_id: UUID) -> Review:
        return self._reviews[review_id]

    def solutions_for_problem(self, problem_id: UUID) -> list[Solution]:
        return [s for s in self._solutions.values() if s.problem_id == problem_id]

    def reviews_for_solution(self, solution_id: UUID) -> list[Review]:
        return [r for r in self._reviews.values() if r.solution_id == solution_id]

    def solutions_needing_review(
        self,
        agent_id: UUID | None = None,
        *,
        limit: int = 0,
    ) -> list[Solution]:
        """Return PENDING solutions that still need reviews.

        Filters out solutions the caller authored or already reviewed.
        Oldest first so nothing gets starved.

        Args:
            agent_id: If given, exclude solutions this agent authored or
                already reviewed.
            limit: Max results (0 = unlimited).
        """
        needed = self.config.reviews_required_for_accept
        pending: list[Solution] = []
        for sol in self._solutions.values():
            if sol.verdict != SolutionVerdict.PENDING:
                continue
            if len(sol.review_ids) >= needed:
                continue
            if agent_id is not None:
                # Can't review your own work
                if sol.author_id == agent_id:
                    continue
                # Already reviewed this one
                already = any(
                    self._reviews[rid].reviewer_id == agent_id
                    for rid in sol.review_ids
                    if rid in self._reviews
                )
                if already:
                    continue
            pending.append(sol)
        pending.sort(key=lambda s: s.created_at)
        if limit > 0:
            pending = pending[:limit]
        return pending

    def request_work(
        self,
        agent_id: UUID,
        *,
        tags: set | None = None,
        limit: int = 5,
    ) -> list[Problem]:
        """Pull-based work discovery: find open problems suited for *agent_id*.

        Uses the triage router to score and rank OPEN problems against the
        requesting agent's capabilities, respecting tag filters, capacity,
        and suspension status.

        Args:
            agent_id: The agent requesting work.
            tags: Optional tag filter (intersect with problem tags).
            limit: Max problems to return.

        Returns a ranked list of open problems the agent can claim.
        """
        agent = self._agents.get(agent_id)
        if agent is None:
            raise NotFoundError("agent", str(agent_id))
        if self.is_suspended(agent_id):
            return []
        if agent.active_count >= self.config.max_active_per_agent:
            return []

        # Gather open, unblocked problems
        open_problems: list[Problem] = []
        for p in self._problems.values():
            if p.status != ProblemStatus.OPEN:
                continue
            if p.author_id == agent_id:
                continue
            if agent_id in p.claimed_by:
                continue
            # Respect tag filter
            if tags and not (tags & set(p.tags)):
                continue
            # Check dependencies
            if p.depends_on and not self.dependencies_met(p.id):
                continue
            open_problems.append(p)

        if not open_problems:
            return []

        # Score via triage router, returning top matches
        ranked = self.router.rank(
            # Create a dummy problem for rank signature — we score each separately
            open_problems[0],
            [agent],
            top_n=1,
        )
        # Actually, rank expects (problem, candidates) not the other way.
        # We need to rank problems for one agent. Do it manually:
        scored: list[tuple[Problem, float]] = []
        for p in open_problems:
            # Use composite score from agent's perspective
            score = self.router._composite_score(p, agent) if hasattr(self.router, '_composite_score') else 0.0
            scored.append((p, score))

        scored.sort(key=lambda pair: pair[1], reverse=True)
        return [p for p, _ in scored[:limit]]

    def statistics(self) -> dict[str, Any]:
        """Compute exchange-wide KPIs.

        Returns a dict with counts, rates, and distribution data suitable
        for dashboards, benchmarking, and research analysis.
        """
        total_problems = len(self._problems)
        total_solutions = len(self._solutions)
        total_reviews = len(self._reviews)
        total_agents = len(self._agents)

        # Problem status distribution
        status_counts: dict[str, int] = {}
        for p in self._problems.values():
            name = p.status.name
            status_counts[name] = status_counts.get(name, 0) + 1

        # Solution verdict distribution
        verdict_counts: dict[str, int] = {}
        for s in self._solutions.values():
            name = s.verdict.name
            verdict_counts[name] = verdict_counts.get(name, 0) + 1

        # Acceptance rate
        accepted = verdict_counts.get("ACCEPTED", 0)
        acceptance_rate = accepted / total_solutions if total_solutions > 0 else 0.0

        # Review verdict distribution
        review_verdicts: dict[str, int] = {}
        for r in self._reviews.values():
            name = r.verdict.name
            review_verdicts[name] = review_verdicts.get(name, 0) + 1

        # Average reviews per solution
        avg_reviews = total_reviews / total_solutions if total_solutions > 0 else 0.0

        # Agent activity
        active_agents = sum(1 for a in self._agents.values() if a.active_count > 0)
        suspended_agents = len(self._suspended)

        # Reputation stats
        balances = [self.ledger.balance(aid) for aid in self._agents]
        avg_reputation = sum(balances) / len(balances) if balances else 0.0
        max_reputation = max(balances) if balances else 0
        min_reputation = min(balances) if balances else 0

        # Archive stats
        archive_total = self.archive.count
        archive_active = self.archive.active_count

        return {
            "total_agents": total_agents,
            "active_agents": active_agents,
            "suspended_agents": suspended_agents,
            "total_problems": total_problems,
            "problem_status": status_counts,
            "total_solutions": total_solutions,
            "solution_verdicts": verdict_counts,
            "acceptance_rate": round(acceptance_rate, 4),
            "total_reviews": total_reviews,
            "review_verdicts": review_verdicts,
            "avg_reviews_per_solution": round(avg_reviews, 2),
            "reputation_avg": round(avg_reputation, 2),
            "reputation_max": max_reputation,
            "reputation_min": min_reputation,
            "archive_total": archive_total,
            "archive_active": archive_active,
        }

    # ==================================================================
    # Agent inbox (notification queue)
    # ==================================================================

    async def _deliver_to_inbox(self, event: Event) -> None:
        """EventBus handler: routes events to relevant agent inboxes.

        An event is delivered to:
          • ``target_agent_id`` — always (if set)
          • ``source_agent_id`` — only for certain event kinds where the
            source needs a confirmation notification
        """
        import time

        entry = {
            "kind": event.kind.name,
            "timestamp": time.time(),
            "event": event.to_dict(),
        }

        # Always deliver to target agent
        if event.target_agent_id:
            self._inboxes.setdefault(event.target_agent_id, []).append(entry)

        # Deliver confirmations to source for specific kinds
        _confirm_kinds = {
            EventKind.PROBLEM_POSTED,
            EventKind.SOLUTION_SUBMITTED,
            EventKind.REVIEW_SUBMITTED,
            EventKind.SOLUTION_ACCEPTED,
            EventKind.SOLUTION_REJECTED,
        }
        if event.kind in _confirm_kinds and event.source_agent_id:
            # Don't double-deliver if source == target
            if event.source_agent_id != event.target_agent_id:
                self._inboxes.setdefault(event.source_agent_id, []).append(entry)

    def inbox(self, agent_id: UUID, *, limit: int = 0) -> list[dict[str, Any]]:
        """Read notifications for *agent_id* without consuming them.

        Args:
            agent_id: The agent to check.
            limit: Max notifications to return (0 = all).
        """
        msgs = self._inboxes.get(agent_id, [])
        if limit > 0:
            return msgs[:limit]
        return list(msgs)

    def inbox_count(self, agent_id: UUID) -> int:
        """Return the number of unread notifications for *agent_id*."""
        return len(self._inboxes.get(agent_id, []))

    def consume_inbox(
        self, agent_id: UUID, *, count: int = 0,
    ) -> list[dict[str, Any]]:
        """Read and remove notifications from *agent_id*'s inbox.

        Args:
            count: Number of messages to consume (0 = all).

        Returns the consumed messages.
        """
        msgs = self._inboxes.get(agent_id, [])
        if not msgs:
            return []
        if count <= 0 or count >= len(msgs):
            consumed = list(msgs)
            msgs.clear()
            return consumed
        consumed = msgs[:count]
        del msgs[:count]
        return consumed

    def clear_inbox(self, agent_id: UUID) -> int:
        """Remove all notifications for *agent_id*. Returns count cleared."""
        msgs = self._inboxes.pop(agent_id, [])
        return len(msgs)

    # ------------------------------------------------------------------
    # Agent presence / heartbeat
    # ------------------------------------------------------------------

    def heartbeat(self, agent_id: UUID) -> datetime:
        """Record a heartbeat for *agent_id*. Returns the timestamp."""
        now = datetime.now(timezone.utc)
        self._heartbeats[agent_id] = now
        return now

    def set_heartbeat_timeout(self, seconds: float) -> None:
        """Configure how long before an agent is considered offline."""
        self._heartbeat_timeout = seconds

    def last_seen(self, agent_id: UUID) -> datetime | None:
        """Return the last heartbeat timestamp, or None if never seen."""
        return self._heartbeats.get(agent_id)

    def is_online(self, agent_id: UUID) -> bool:
        """Check if an agent has sent a heartbeat within the timeout."""
        ts = self._heartbeats.get(agent_id)
        if ts is None:
            return False
        elapsed = (datetime.now(timezone.utc) - ts).total_seconds()
        return elapsed <= self._heartbeat_timeout

    def online_agents(self) -> list[UUID]:
        """Return a list of agent IDs currently considered online."""
        return [aid for aid in self._agents if self.is_online(aid)]

    def offline_agents(self) -> list[UUID]:
        """Return registered agents that are NOT currently online."""
        return [aid for aid in self._agents if not self.is_online(aid)]

    # ==================================================================
    # Internal helpers
    # ==================================================================

    async def _auto_assign(self, problem: Problem) -> None:
        """Use the triage router to find candidates and notify them.

        Filters candidates by:
        1. Agent preferences (watch_tags must overlap problem tags, if set)
        2. Online status (online agents are preferred)
        3. Capacity (agents at max_active_per_agent are skipped)
        """
        # Pre-filter candidates
        eligible: list[Agent] = []
        for agent in self.agents:
            if agent.id == problem.author_id:
                continue
            # Respect watch_tags preference
            if agent.watch_tags and not (agent.watch_tags & set(problem.tags)):
                continue
            # Skip agents at capacity
            if agent.active_count >= self.config.max_active_per_agent:
                continue
            # Skip suspended agents
            if self.is_suspended(agent.id):
                continue
            eligible.append(agent)

        if not eligible:
            logger.debug("No eligible agents for triage on %s", problem.title)
            return

        # Prefer online agents: sort online first, then let triage rank
        online_set = set(self.online_agents())
        eligible.sort(key=lambda a: (a.id not in online_set, 0))

        candidates = self.router.rank(problem, eligible)
        for agent in candidates:
            await self.bus.publish(Event(
                kind=EventKind.TRIAGE_ASSIGNED,
                target_agent_id=agent.id,
                problem_id=problem.id,
            ))
            logger.debug("Triage → suggested %s for %s", agent.name, problem.title)

    async def _run_verification_oracle(
        self, solution: Solution, problem: Problem,
    ) -> None:
        """Invoke the configured verification oracle on a solution.

        * On PASS → synthesize an APPROVE review (confidence=1.0).
        * On FAIL → if ``oracle_auto_reject`` is enabled, set verdict to REJECTED;
          otherwise attach the result and let peer review proceed.
        * On ERROR/SKIPPED → log and continue.
        """
        from schwarma.verification import VerificationStatus

        oracle = self.config.verification_oracle
        try:
            result = await oracle.verify(solution, problem)
        except Exception:
            logger.exception(
                "Verification oracle crashed for solution %s", solution.id,
            )
            return

        # Store result on solution metadata for downstream visibility
        solution.metadata["oracle_result"] = {
            "status": result.status.name,
            "passed_tests": result.passed_tests,
            "failed_tests": result.failed_tests,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "execution_time_s": result.execution_time_s,
            "details": result.details,
        }

        if result.status == VerificationStatus.PASSED:
            # Synthesize an auto-review from "oracle"
            oracle_review = Review(
                solution_id=solution.id,
                reviewer_id=_ORACLE_REVIEWER_ID,
                review_type=ReviewType.CORRECTNESS,
                verdict=ReviewVerdict.APPROVE,
                body="Verification oracle: PASSED",
                confidence=1.0,
                metadata={"oracle": True},
            )
            self._reviews[oracle_review.id] = oracle_review
            solution.review_ids.append(oracle_review.id)

        elif result.status == VerificationStatus.FAILED:
            if self.config.oracle_auto_reject:
                solution.reject()
                problem.reject_and_reopen()
                logger.info(
                    "Oracle auto-rejected solution %s (failed_tests=%d)",
                    solution.id, result.failed_tests,
                )
            else:
                # Attach failure info but let peer review proceed
                oracle_review = Review(
                    solution_id=solution.id,
                    reviewer_id=_ORACLE_REVIEWER_ID,
                    review_type=ReviewType.CORRECTNESS,
                    verdict=ReviewVerdict.REJECT,
                    body=f"Verification oracle: FAILED ({result.failed_tests} tests)",
                    confidence=1.0,
                    metadata={"oracle": True},
                )
                self._reviews[oracle_review.id] = oracle_review
                solution.review_ids.append(oracle_review.id)

    async def _auto_request_reviews(self, solution: Solution) -> None:
        """Pick reviewer agents and request reviews."""
        problem = self._problems[solution.problem_id]
        # Pick agents who didn't write the solution and didn't author the problem
        reviewer_candidates = [
            a for a in self.agents
            if a.id != solution.author_id
            and a.id != problem.author_id
            and a.has_any_capability({AgentCapability.CODE_REVIEW, AgentCapability.GENERAL})
        ]

        # Rank by reputation, pick top N
        reviewer_candidates.sort(
            key=lambda a: self.ledger.balance(a.id), reverse=True
        )
        for reviewer in reviewer_candidates[: self.config.reviews_required_for_accept]:
            await self.request_review(solution.id, reviewer.id)

    async def _evaluate_solution(self, solution: Solution) -> None:
        """If enough reviews are in, auto-accept or auto-reject.

        Reviews are weighted by their ``confidence`` field (0.0–1.0).
        A review with confidence 0.5 counts as half a vote.
        A minimum number of *unique* reviewers is also enforced.
        """
        reviews = self.reviews_for_solution(solution.id)
        if len(reviews) < self.config.reviews_required_for_accept:
            return  # not enough data yet

        # Diversity gate: require distinct reviewers
        unique_reviewers = {r.reviewer_id for r in reviews}
        if len(unique_reviewers) < self.config.min_unique_reviewers:
            return  # not enough distinct reviewers

        # Confidence-weighted tallies
        approvals = sum(r.confidence for r in reviews if r.is_positive)
        rejections = sum(r.confidence for r in reviews if r.verdict == ReviewVerdict.REJECT)
        revision_requests = sum(
            r.confidence for r in reviews if r.verdict == ReviewVerdict.REQUEST_CHANGES
        )

        problem = self._problems[solution.problem_id]

        if approvals >= self.config.reviews_required_for_accept:
            solution.accept()
            problem.accept(solution.id)

            # Refund stake
            stake_key = (solution.author_id, problem.id)
            stake = self._stakes.pop(stake_key, 0)
            if stake:
                self.ledger.record(
                    solution.author_id,
                    ReputationEvent.BONUS,
                    delta=stake,
                    reason=f"Stake refund for '{problem.title}'",
                    related_id=solution.id,
                )

            # Bounty to solver
            self.ledger.record(
                solution.author_id,
                ReputationEvent.SOLUTION_ACCEPTED,
                delta=problem.bounty,
                related_id=solution.id,
                reason=f"Bounty for solving '{problem.title}'",
            )

            # --- Skill tracking: record win ---
            if self.config.enable_skill_tracking:
                caps = self._problem_capabilities(problem)
                diff = self._problem_difficulty(problem)
                self.skill_tracker.record_outcome(
                    solution.author_id, caps, won=True, difficulty=diff,
                )

            # --- Difficulty: record acceptance ---
            if self.config.enable_difficulty:
                solver = self._agents.get(solution.author_id)
                solve_secs = None
                if solution.created_at and problem.created_at:
                    solve_secs = (solution.created_at - problem.created_at).total_seconds()
                self.difficulty.record_acceptance(
                    problem.id,
                    solver_tier=self._effective_tier(solver) if solver else None,
                    solve_seconds=solve_secs,
                )

            # Auto-promote solver trust tier
            self.trust_gate.maybe_promote(
                solution.author_id, self.ledger.balance(solution.author_id)
            )

            await self.bus.publish(Event(
                kind=EventKind.SOLUTION_ACCEPTED,
                source_agent_id=solution.author_id,
                problem_id=problem.id,
                solution_id=solution.id,
            ))

            # Archive the solved problem
            if self.config.enable_archive:
                self._archive_solution(problem, solution)

            logger.info("Solution %s ACCEPTED for %s", solution.id, problem.title)

            # If this was a challenged solution, challenger loses stake
            challenge = self._challenge_stakes.pop(solution.id, None)
            if challenge:
                logger.info("Challenge on %s failed — stake forfeited", solution.id)

        elif rejections >= self.config.reviews_required_for_accept:
            solution.reject()

            # Stake is forfeited (already deducted, no refund)
            self._stakes.pop((solution.author_id, problem.id), None)

            self.ledger.record(
                solution.author_id,
                ReputationEvent.SOLUTION_REJECTED,
                related_id=solution.id,
            )

            # --- Skill tracking: record loss ---
            if self.config.enable_skill_tracking:
                caps = self._problem_capabilities(problem)
                diff = self._problem_difficulty(problem)
                self.skill_tracker.record_outcome(
                    solution.author_id, caps, won=False, difficulty=diff,
                )

            # --- Difficulty: record rejection ---
            if self.config.enable_difficulty:
                self.difficulty.record_rejection(problem.id)

            problem.reject_and_reopen()
            await self.bus.publish(Event(
                kind=EventKind.SOLUTION_REJECTED,
                source_agent_id=solution.author_id,
                problem_id=problem.id,
                solution_id=solution.id,
            ))
            logger.info("Solution %s REJECTED — problem re-opened", solution.id)

            # If this was a challenged solution, challenger wins stake back + bonus
            challenge = self._challenge_stakes.pop(solution.id, None)
            if challenge:
                challenger_id, stake = challenge
                self.ledger.record(
                    challenger_id,
                    ReputationEvent.BONUS,
                    delta=stake * 2,
                    reason=f"Challenge succeeded on '{problem.title}'",
                    related_id=solution.id,
                )
                logger.info("Challenge on %s succeeded — bonus paid", solution.id)

        elif revision_requests >= self.config.reviews_required_for_accept:
            # Reviewers want changes, not outright rejection
            solution.request_revision()
            problem.request_revision()

            # Stake is preserved — solver still has a chance
            await self.bus.publish(Event(
                kind=EventKind.SOLUTION_REVISION_REQUESTED,
                source_agent_id=solution.author_id,
                problem_id=problem.id,
                solution_id=solution.id,
            ))
            logger.info(
                "Solution %s NEEDS_REVISION — solver can resubmit", solution.id
            )

        else:
            # None of approve, reject, or revision reached quorum.
            # Check for tie state: enough total reviews but split verdict.
            total_reviews = len(reviews)
            required = self.config.reviews_required_for_accept
            tiebreaker_extra = self.config.tiebreaker_extra_reviews

            if total_reviews >= required + tiebreaker_extra:
                # Tiebreaker round has been exhausted — apply fallback
                fallback = self.config.tiebreaker_fallback.lower()
                logger.info(
                    "Solution %s tied after %d reviews — fallback=%s",
                    solution.id, total_reviews, fallback,
                )

                await self.bus.publish(Event(
                    kind=EventKind.REVIEW_SUBMITTED,
                    problem_id=problem.id,
                    solution_id=solution.id,
                    payload={"tiebreaker": True, "fallback": fallback},
                ))

                if fallback == "accept":
                    solution.accept()
                    problem.accept(solution.id)
                    # Refund stake
                    stake_key = (solution.author_id, problem.id)
                    stake = self._stakes.pop(stake_key, 0)
                    if stake:
                        self.ledger.record(
                            solution.author_id,
                            ReputationEvent.BONUS,
                            delta=stake,
                            reason=f"Tiebreaker stake refund for '{problem.title}'",
                            related_id=solution.id,
                        )
                    # Half bounty for tied accept
                    self.ledger.record(
                        solution.author_id,
                        ReputationEvent.SOLUTION_ACCEPTED,
                        delta=problem.bounty // 2,
                        related_id=solution.id,
                        reason=f"Tiebreaker accept for '{problem.title}'",
                    )
                    await self.bus.publish(Event(
                        kind=EventKind.SOLUTION_ACCEPTED,
                        source_agent_id=solution.author_id,
                        problem_id=problem.id,
                        solution_id=solution.id,
                        payload={"tiebreaker": True},
                    ))
                    if self.config.enable_archive:
                        self._archive_solution(problem, solution)

                elif fallback == "revision":
                    solution.request_revision()
                    problem.request_revision()
                    await self.bus.publish(Event(
                        kind=EventKind.SOLUTION_REVISION_REQUESTED,
                        source_agent_id=solution.author_id,
                        problem_id=problem.id,
                        solution_id=solution.id,
                        payload={"tiebreaker": True},
                    ))

                else:  # "reject" (default)
                    solution.reject()
                    self._stakes.pop((solution.author_id, problem.id), None)
                    self.ledger.record(
                        solution.author_id,
                        ReputationEvent.SOLUTION_REJECTED,
                        related_id=solution.id,
                    )
                    problem.reject_and_reopen()
                    await self.bus.publish(Event(
                        kind=EventKind.SOLUTION_REJECTED,
                        source_agent_id=solution.author_id,
                        problem_id=problem.id,
                        solution_id=solution.id,
                        payload={"tiebreaker": True},
                    ))

    def _archive_solution(self, problem: Problem, solution: Solution) -> ArchiveEntry:
        """Persist a solved problem to the archive."""
        reviews = self.reviews_for_solution(solution.id)
        review_snapshots = [
            ReviewSnapshot(
                reviewer_id=r.reviewer_id,
                verdict=r.verdict,
                review_type=r.review_type.name,
                confidence=r.confidence,
                body=r.body,
            )
            for r in reviews
        ]

        solver = self._agents.get(solution.author_id)
        entry = ArchiveEntry(
            problem_id=problem.id,
            solution_id=solution.id,
            problem_title=problem.title,
            problem_description=problem.description,
            tags=set(problem.tags),
            sensitivity=problem.sensitivity,
            solution_body=solution.body,
            solver_id=solution.author_id,
            solver_tier=solver.model_tier if solver else ModelTier.STANDARD,
            solver_reputation=self.ledger.balance(solution.author_id),
            reviews=review_snapshots,
        )

        # Attach failure signature for similarity search
        if problem.failure_report is not None:
            entry.metadata["failure_signature"] = problem.failure_report.signature

        return self.archive.store(entry)

    def search_archive(self, **kwargs: Any) -> list[ArchiveEntry]:
        """Search the archive for past solutions. Delegates to Archive.search()."""
        return self.archive.search(**kwargs)

    def find_similar_problems(
        self,
        text: str,
        *,
        threshold: float | None = None,
        limit: int | None = None,
    ) -> list[tuple[ArchiveEntry, float]]:
        """Search the archive for problems similar to *text*.

        Returns ``(ArchiveEntry, score)`` pairs. Falls back to the
        Exchange-level similarity config when *threshold* / *limit* are
        not supplied.
        """
        return self.archive.search_similar(
            text,
            threshold=threshold if threshold is not None else self.config.similarity_threshold,
            limit=limit if limit is not None else self.config.similarity_limit,
        )

    @staticmethod
    def _parse_review_verdict(raw: str) -> ReviewVerdict:
        """Best-effort extraction of a verdict from free-text."""
        upper = raw.strip().upper()
        if upper.startswith("APPROVE"):
            return ReviewVerdict.APPROVE
        if upper.startswith("REJECT"):
            return ReviewVerdict.REJECT
        if upper.startswith("REQUEST_CHANGES") or upper.startswith("REQUEST CHANGES"):
            return ReviewVerdict.REQUEST_CHANGES
        # fallback: scan for keywords
        if "APPROVE" in upper:
            return ReviewVerdict.APPROVE
        if "REJECT" in upper:
            return ReviewVerdict.REJECT
        return ReviewVerdict.ABSTAIN

    # ==================================================================
    # Glob — multi-agent collaborative solving
    # ==================================================================

    @_locked
    async def form_glob(
        self,
        coordinator_id: UUID,
        problem_id: UUID,
        *,
        name: str = "",
        max_members: int = 5,
        coordinator_subtask: str = "orchestration",
        coordinator_bonus: float = 0.10,
    ) -> Glob:
        """Create a new glob for collaborative solving.

        The coordinator immediately joins as the first member.  The problem
        is not claimed yet — :meth:`join_glob` collects additional members;
        once the coordinator calls :meth:`assemble_glob_solution` the
        underlying ``claim_problem`` + ``solve_problem`` are invoked on
        behalf of the glob.
        """
        if coordinator_id not in self._agents:
            raise NotFoundError(f"Agent {coordinator_id} not registered")
        if problem_id not in self._problems:
            raise NotFoundError(f"Problem {problem_id} not found")
        self._require_not_suspended(coordinator_id)

        glob = Glob(
            problem_id=problem_id,
            coordinator_id=coordinator_id,
            name=name or f"glob-{str(problem_id)[:8]}",
            max_members=max_members,
            coordinator_bonus=coordinator_bonus,
        )
        glob.add_member(
            coordinator_id,
            subtask=coordinator_subtask,
            weight=1.0,
        )
        self._globs[glob.id] = glob

        await self.bus.publish(Event(
            kind=EventKind.PROBLEM_CLAIMED,
            source_agent_id=coordinator_id,
            problem_id=problem_id,
            payload={"glob_id": str(glob.id), "event": "glob_formed"},
        ))
        logger.info("Glob %s formed by %s for problem %s", glob.id, coordinator_id, problem_id)
        return glob

    @_locked
    async def join_glob(
        self,
        glob_id: UUID,
        agent_id: UUID,
        *,
        subtask: str = "",
        weight: float = 1.0,
    ) -> GlobMembership:
        """Join an existing glob as a contributing member."""
        if glob_id not in self._globs:
            raise NotFoundError(f"Glob {glob_id} not found")
        if agent_id not in self._agents:
            raise NotFoundError(f"Agent {agent_id} not registered")
        self._require_not_suspended(agent_id)

        glob = self._globs[glob_id]
        if glob.status not in (GlobStatus.FORMING, GlobStatus.ACTIVE):
            raise StateError(f"Glob {glob_id} is not accepting members (status={glob.status.name})")

        membership = glob.add_member(agent_id, subtask=subtask, weight=weight)
        logger.info("Agent %s joined glob %s", agent_id, glob_id)
        return membership

    @_locked
    async def submit_to_glob(
        self,
        glob_id: UUID,
        agent_id: UUID,
        contribution_text: str,
    ) -> GlobMembership:
        """Submit a member's contribution to the glob coordinator."""
        if glob_id not in self._globs:
            raise NotFoundError(f"Glob {glob_id} not found")
        glob = self._globs[glob_id]
        membership = glob.get_membership(agent_id)
        if membership is None:
            raise PermissionError_(f"Agent {agent_id} is not in glob {glob_id}")
        if glob.status != GlobStatus.ACTIVE:
            raise StateError(f"Glob {glob_id} is not ACTIVE")

        membership.submit(contribution_text)
        logger.info("Agent %s submitted contribution to glob %s", agent_id, glob_id)
        return membership

    @_locked
    async def accept_glob_contribution(
        self,
        glob_id: UUID,
        coordinator_id: UUID,
        member_agent_id: UUID,
    ) -> GlobMembership:
        """Coordinator marks a member's contribution as accepted."""
        if glob_id not in self._globs:
            raise NotFoundError(f"Glob {glob_id} not found")
        glob = self._globs[glob_id]
        if glob.coordinator_id != coordinator_id:
            raise PermissionError_("Only the coordinator can accept contributions")
        membership = glob.get_membership(member_agent_id)
        if membership is None:
            raise NotFoundError(f"Agent {member_agent_id} not in glob {glob_id}")
        membership.accept()
        return membership

    @_locked
    async def assemble_glob_solution(
        self,
        glob_id: UUID,
        coordinator_id: UUID,
        assembly_notes: str,
    ) -> Solution:
        """Assemble accepted contributions into a final solution.

        The coordinator must be the one to call this.  The Exchange:
        1. Claims the problem on behalf of the coordinator.
        2. Builds a combined solution body from accepted contributions.
        3. Submits via solve_problem (guards, reputation, auto-review).
        4. Records a GlobSolution for provenance tracking.
        5. Distributes reputation shares via split_reputation.
        """
        if glob_id not in self._globs:
            raise NotFoundError(f"Glob {glob_id} not found")
        glob = self._globs[glob_id]
        if glob.coordinator_id != coordinator_id:
            raise PermissionError_("Only the coordinator can assemble the solution")
        if glob.status != GlobStatus.ACTIVE:
            raise StateError(f"Glob {glob_id} must be ACTIVE to assemble solution")

        # Build combined body
        accepted_memberships = [
            m for m in glob.memberships
            if m.contribution_status == ContributionStatus.ACCEPTED
        ]
        parts: list[str] = []
        member_contribs: dict[str, str] = {}
        for m in accepted_memberships:
            part = f"### [{m.role.name}] Agent {m.agent_id}\n{m.contribution_text or ''}"
            parts.append(part)
            member_contribs[str(m.agent_id)] = m.contribution_text or ""

        combined = (
            f"## Glob Solution — {glob.name}\n\n"
            f"{assembly_notes}\n\n"
            + "\n\n".join(parts)
        )

        glob.status = GlobStatus.SUBMITTING

        # Claim + solve on coordinator's behalf (locks re-entrant — use internal helpers)
        problem = self._problems[glob.problem_id]
        if problem.is_open:
            problem.claim(coordinator_id)
            problem.glob_id = glob.id

        solution = Solution(
            problem_id=glob.problem_id,
            author_id=coordinator_id,
            body=combined,
        )
        self._solutions[solution.id] = solution
        problem.add_solution(solution.id)
        agent = self._agents[coordinator_id]
        agent.release(glob.problem_id)

        # Track the GlobSolution
        gs = GlobSolution(
            glob_id=glob.id,
            problem_id=glob.problem_id,
            solution_id=solution.id,
            assembled_by=coordinator_id,
            assembly_notes=assembly_notes,
            member_contributions=member_contribs,
        )
        self._glob_solutions[solution.id] = gs

        # Distribute reputation shares immediately (full bounty before review;
        # a future enhancement could defer until acceptance).
        bounty = problem.bounty
        shares = split_reputation(glob, bounty)
        for share in shares:
            if share.delta > 0 and share.agent_id in self._agents:
                self.ledger.record(
                    share.agent_id,
                    ReputationEvent.BONUS,
                    delta=share.delta,
                    reason=share.reason,
                )

        glob.dissolve()

        await self.bus.publish(Event(
            kind=EventKind.SOLUTION_SUBMITTED,
            source_agent_id=coordinator_id,
            problem_id=glob.problem_id,
            solution_id=solution.id,
            payload={"glob_id": str(glob.id), "shares": [str(s.agent_id) for s in shares]},
        ))
        logger.info("Glob %s assembled solution %s", glob_id, solution.id)
        return solution

    def get_glob(self, glob_id: UUID) -> Glob:
        """Return a glob by ID."""
        if glob_id not in self._globs:
            raise NotFoundError(f"Glob {glob_id} not found")
        return self._globs[glob_id]

    def list_globs(
        self,
        problem_id: UUID | None = None,
        status: GlobStatus | None = None,
    ) -> list[Glob]:
        """List globs, optionally filtered by problem or status."""
        globs = list(self._globs.values())
        if problem_id is not None:
            globs = [g for g in globs if g.problem_id == problem_id]
        if status is not None:
            globs = [g for g in globs if g.status == status]
        return globs

    # ==================================================================
    # Skill / tier helpers
    # ==================================================================

    def _effective_tier(self, agent: Agent) -> ModelTier:
        """Return the proven effective tier if skill tracking is enabled,
        otherwise the declared model_tier."""
        if self.config.enable_skill_tracking and self.config.use_effective_tier:
            return self.skill_tracker.effective_tier(agent.id, agent.model_tier)
        return agent.model_tier

    def _skill_rating_for_triage(self, agent_id: UUID, problem: Problem) -> float:
        """Compute a skill-based score for triage ranking.

        Returns the best conservative rating across the problem's
        required capabilities, or the aggregate rating if no specific
        capabilities are required.
        """
        if not self.config.enable_skill_tracking:
            return 0.0
        caps = self._problem_capabilities(problem)
        if not caps:
            return self.skill_tracker.aggregate_rating(agent_id)
        ratings = [
            self.skill_tracker.conservative_rating_for(agent_id, cap)
            for cap in caps
        ]
        return max(ratings) if ratings else 0.0

    def _problem_capabilities(self, problem: Problem) -> set[AgentCapability]:
        """Derive the relevant capabilities for a problem (for skill updates)."""
        return self.router._required_capabilities(problem)

    def _problem_difficulty(self, problem_id_or_problem: Problem | UUID) -> float:
        """Get the empirical difficulty score for a problem."""
        if not self.config.enable_difficulty:
            return 1.0
        pid = (
            problem_id_or_problem.id
            if isinstance(problem_id_or_problem, Problem)
            else problem_id_or_problem
        )
        return self.difficulty.difficulty_score(pid)

    def get_effective_tier(self, agent_id: UUID) -> ModelTier:
        """Public accessor: effective tier for a registered agent."""
        agent = self._agents[agent_id]
        return self._effective_tier(agent)

    def get_skill_summary(self, agent_id: UUID) -> dict:
        """Public accessor: full skill profile for a registered agent."""
        return self.skill_tracker.summary(agent_id)

    def is_probationary(self, agent_id: UUID) -> bool:
        """Is the agent still in probationary period?"""
        return self.skill_tracker.is_probationary(agent_id)

    # ==================================================================
    # Calibration injection
    # ==================================================================

    async def _maybe_inject_calibration(self, agent: Agent) -> Problem | None:
        """Probabilistically inject a calibration problem for *agent*.

        The injected problem looks like a normal problem — the agent does
        not know it's being tested.  Returns the injected problem or None.
        """
        if not self.calibration_bank.should_inject():
            return None

        caps = agent.capabilities
        cal_problem = self.calibration_bank.draw(agent.id, caps)
        if cal_problem is None:
            return None

        # Wrap the calibration problem as a regular problem
        problem = Problem(
            title=cal_problem.title,
            description=cal_problem.description,
            author_id=agent.id,  # self-authored so it doesn't pollute others
        )
        # Track the mapping from real problem → calibration problem
        self._calibration_map[problem.id] = cal_problem.id

        self._problems[problem.id] = problem

        await self.bus.publish(Event(
            kind=EventKind.CALIBRATION_INJECTED,
            target_agent_id=agent.id,
            problem_id=problem.id,
            payload={"calibration_problem_id": str(cal_problem.id)},
        ))

        logger.info("Calibration problem injected for agent %s", agent.name)
        return problem

    async def evaluate_calibration(
        self,
        problem_id: UUID,
        agent_id: UUID,
        answer: str,
    ) -> CalibrationResult | None:
        """Evaluate an agent's answer to a calibration problem.

        Returns the CalibrationResult, or None if this is not a calibration
        problem.  Results are fed back into the SkillTracker.
        """
        cal_problem_id = self._calibration_map.get(problem_id)
        if cal_problem_id is None:
            return None

        result = self.calibration_bank.evaluate(agent_id, cal_problem_id, answer)

        # Feed result into skill tracker
        if self.config.enable_skill_tracking:
            cal_problem = self.calibration_bank._by_id.get(cal_problem_id)
            if cal_problem is not None:
                caps = cal_problem.capabilities
                diff = float(cal_problem.difficulty.value)  # 1.0, 2.0, or 3.0
                won = self.calibration_bank.is_pass(result)
                self.skill_tracker.record_outcome(
                    agent_id, caps, won=won, difficulty=diff,
                )

        await self.bus.publish(Event(
            kind=EventKind.CALIBRATION_EVALUATED,
            source_agent_id=agent_id,
            problem_id=problem_id,
            payload={
                "calibration_problem_id": str(cal_problem_id),
                "verdict": result.verdict.name,
                "score": result.score,
            },
        ))

        return result

    def is_calibration_problem(self, problem_id: UUID) -> bool:
        """Check if a problem is a calibration injection."""
        return problem_id in self._calibration_map

    # ==================================================================
    # Problem expiry
    # ==================================================================

    @_locked
    async def expire_stale_problems(self) -> list[Problem]:
        """Expire problems whose deadline has passed.

        Returns the list of newly-expired problems.  Agents who held a claim
        on an expired problem receive a small reputation penalty and have their
        stakes forfeited.
        """
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        expired: list[Problem] = []

        for problem in list(self._problems.values()):
            if problem.status in (ProblemStatus.CLOSED, ProblemStatus.EXPIRED):
                continue
            if problem.deadline is None or now <= problem.deadline:
                continue

            # Expire it
            problem.expire()
            expired.append(problem)

            # Penalise agents who held claims (they didn't deliver)
            for agent_id in problem.claimed_by:
                stake_key = (agent_id, problem.id)
                self._stakes.pop(stake_key, None)  # forfeit stake

                self.ledger.record(
                    agent_id,
                    ReputationEvent.PENALTY,
                    delta=-2,
                    reason=f"Problem '{problem.title}' expired while claimed",
                    related_id=problem.id,
                )

                # Release the agent's active problem slot
                agent = self._agents.get(agent_id)
                if agent is not None:
                    agent.release(problem.id)

            await self.bus.publish(Event(
                kind=EventKind.PROBLEM_EXPIRED,
                problem_id=problem.id,
            ))

            logger.info("Problem %s expired (deadline %s)", problem.title, problem.deadline)

        return expired

    @_locked
    async def expire_stale_claims(
        self,
        now: datetime | None = None,
    ) -> list[tuple[UUID, UUID]]:
        """Release claims that have exceeded ``claim_timeout_seconds``.

        Returns a list of ``(agent_id, problem_id)`` pairs whose claims
        were expired.  Each expired claim:

        * releases the agent's active-problem slot,
        * forfeits any reputation stake,
        * applies a small reputation penalty (``-2``),
        * resets the problem to OPEN so another agent can claim it,
        * emits a ``CLAIM_EXPIRED`` event.

        If ``claim_timeout_seconds`` is 0 (the default), this method
        is a no-op and returns an empty list.
        """
        timeout = self.config.claim_timeout_seconds
        if timeout <= 0:
            return []

        if now is None:
            now = datetime.now(timezone.utc)

        cutoff = now - timedelta(seconds=timeout)
        released: list[tuple[UUID, UUID]] = []

        for (agent_id, problem_id), claimed_at in list(self._claim_times.items()):
            if claimed_at > cutoff:
                continue

            problem = self._problems.get(problem_id)
            if problem is None or problem.status != ProblemStatus.CLAIMED:
                continue

            # Remove this agent's claim
            if agent_id in problem.claimed_by:
                problem.claimed_by.remove(agent_id)

            # Release active-problem slot
            agent = self._agents.get(agent_id)
            if agent is not None:
                agent.release(problem_id)

            # Forfeit stake
            self._stakes.pop((agent_id, problem_id), None)

            # Reputation penalty
            self.ledger.record(
                agent_id,
                ReputationEvent.PENALTY,
                delta=-2,
                reason=f"Claim on '{problem.title}' expired (timeout {timeout}s)",
                related_id=problem_id,
            )

            # Remove claim-time tracking entry
            del self._claim_times[(agent_id, problem_id)]

            # Reopen the problem if no more claimants
            if not problem.claimed_by:
                problem.status = ProblemStatus.OPEN

            released.append((agent_id, problem_id))

            await self.bus.publish(Event(
                kind=EventKind.CLAIM_EXPIRED,
                source_agent_id=agent_id,
                problem_id=problem_id,
            ))

            logger.info(
                "Claim expired: agent %s on problem %s (claimed %s, cutoff %s)",
                agent_id, problem.title, claimed_at, cutoff,
            )

        return released

    # ==================================================================
    # Bounty escalation
    # ==================================================================

    @_locked
    async def escalate_bounty(self, problem_id: UUID) -> Problem:
        """Manually increase a problem's bounty to attract solvers.

        Increments the bounty by ``escalation_increment``, capped at
        ``max_bounty``.  Emits a PROBLEM_ESCALATED event.
        """
        problem = self._problems[problem_id]
        if problem.status not in (ProblemStatus.OPEN, ProblemStatus.CLAIMED):
            raise StateError(
                f"Can only escalate OPEN or CLAIMED problems "
                f"(current: {problem.status.name})"
            )

        old_bounty = problem.bounty
        problem.bounty = min(
            problem.bounty + self.config.escalation_increment,
            self.config.max_bounty,
        )

        if problem.bounty == old_bounty:
            logger.info("Problem %s already at max bounty %d", problem.title, problem.bounty)
            return problem

        await self.bus.publish(Event(
            kind=EventKind.PROBLEM_ESCALATED,
            problem_id=problem.id,
            payload={"old_bounty": old_bounty, "new_bounty": problem.bounty},
        ))

        logger.info(
            "Bounty escalated for %s: %d → %d",
            problem.title, old_bounty, problem.bounty,
        )
        return problem

    # NOTE: Not locked — delegates to locked methods internally
    async def escalate_stale_bounties(
        self,
        *,
        stale_seconds: float = 3600,
    ) -> list[Problem]:
        """Auto-escalate bounties on problems that have been open too long.

        Only OPEN problems (not CLAIMED) that have been open longer than
        *stale_seconds* are escalated.  Returns list of escalated problems.
        """
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        escalated: list[Problem] = []

        for problem in list(self._problems.values()):
            if problem.status != ProblemStatus.OPEN:
                continue
            age = (now - problem.created_at).total_seconds()
            if age < stale_seconds:
                continue
            if problem.bounty >= self.config.max_bounty:
                continue

            await self.escalate_bounty(problem.id)
            escalated.append(problem)

        return escalated

    # ==================================================================
    # Snapshot / restore
    # ==================================================================

    def snapshot(self) -> dict[str, Any]:
        """Capture the full exchange state as a serializable dict.

        This enables checkpointing, reproducible benchmarks, and
        offline analysis.  Note: agent solver callbacks are not
        captured (they're runtime-only).
        """
        return {
            "problems": {
                str(pid): p.to_dict() for pid, p in self._problems.items()
            },
            "solutions": {
                str(sid): s.to_dict() for sid, s in self._solutions.items()
            },
            "reviews": {
                str(rid): r.to_dict() for rid, r in self._reviews.items()
            },
            "agents": {
                str(aid): {
                    "id": str(a.id),
                    "name": a.name,
                    "capabilities": [c.name for c in a.capabilities],
                    "model_tier": a.model_tier.name,
                    "active_problem_ids": [str(pid) for pid in a._active_problem_ids],
                    "total_solved": a._total_solved,
                    "total_reviewed": a._total_reviewed,
                }
                for aid, a in self._agents.items()
            },
            "reputation_balances": {
                str(aid): self.ledger.balance(aid) for aid in self._agents
            },
            "suspended": [str(aid) for aid in self._suspended],
            "statistics": self.statistics(),
            "archive_entries": [
                e.to_dict() for e in self.archive._entries.values()
            ],
        }

    def restore_problems(self, snapshot: dict[str, Any]) -> int:
        """Restore problems from a snapshot dict.

        Returns the number of problems restored.  Only restores problem
        data; agents, solutions, and reviews must be restored separately
        or rebuilt from the snapshot metadata.
        """
        from uuid import UUID as _UUID

        problems_data = snapshot.get("problems", {})
        count = 0
        for pid_str, pdata in problems_data.items():
            pid = _UUID(pid_str)
            if pid not in self._problems:
                p = Problem(
                    title=pdata["title"],
                    description=pdata["description"],
                    author_id=_UUID(pdata["author_id"]),
                    bounty=pdata.get("bounty", 10),
                )
                # Override the auto-generated id
                object.__setattr__(p, "id", pid) if hasattr(p, "__dataclass_fields__") else None
                p.id = pid
                p.status = ProblemStatus[pdata["status"]]
                p.priority = pdata.get("priority", 0)
                self._problems[pid] = p
                count += 1
        return count
