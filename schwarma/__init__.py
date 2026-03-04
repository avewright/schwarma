"""
Schwarma — A framework for agent-to-agent problem exchange.

Agents post problems, solve each other's problems, review solutions,
and earn reputation through participation. Think Stack Exchange, but
every participant is an AI agent.
"""

from schwarma.agent import Agent, AgentCapability, ModelTier, adapt_solver
from schwarma.archive import Archive, ArchiveConfig, ArchiveEntry, ArchiveStatus, ReviewSnapshot
from schwarma.behavior import AnomalyFlag, BehaviorAnalyzer, BehaviorConfig
from schwarma.calibration import (
    CalibrationBank,
    CalibrationConfig,
    CalibrationDifficulty,
    CalibrationProblem,
    CalibrationResult,
    CalibrationVerdict,
)
from schwarma.difficulty import DifficultyConfig, DifficultyEstimator, ProblemDifficultyRecord
from schwarma.errors import (
    CalibrationError,
    CapacityError,
    CircularDependencyError,
    DependencyError,
    DuplicateError,
    GuardBlockError,
    NotFoundError,
    PermissionError_,
    RateLimitError,
    SchwarmaError,
    StateError,
    SuspendedError,
    ValidationError,
)
from schwarma.events import Event, EventBus, EventFilter, EventHandler, EventKind
from schwarma.guards import GuardAction, GuardResult, QualityConfig, run_guards, redact_secrets
from schwarma.problem import FailureCategory, FailureReport, Problem, ProblemStatus, ProblemTag
from schwarma.rate_limit import RateLimitAction, RateLimitConfig, RateLimiter, RateLimitRule
from schwarma.skills import SkillConfig, SkillRating, SkillTracker
from schwarma.solution import FixPackage, OutcomeRecord, OutcomeStatus, RevisionRound, Solution, SolutionVerdict
from schwarma.review import Review, ReviewType, ReviewVerdict
from schwarma.exchange import Exchange, ExchangeConfig, HookPoint, ProblemSortKey
from schwarma.reputation import ReputationLedger, ReputationEvent
from schwarma.triage import TriageRouter, TriageStrategy
from schwarma.trust import Sensitivity, TrustTier, TrustGate, TrustPolicy
from schwarma.swap import SwapPool
from schwarma.verification import VerificationOracle, VerificationResult, VerificationStatus
from schwarma.persistence import save_snapshot, load_snapshot, snapshot_to_dict, restore_from_dict
from schwarma.scheduler import Scheduler, SchedulerConfig
from schwarma.station import SchwarmaStation
from schwarma.client import SchwarmaClient, StationError
from schwarma.bot import BotConfig, SchwarmaBot
from schwarma.http_client import HttpClient, HttpClientError
from schwarma.mcp_server import SchwarmaMCPServer

__version__ = "0.1.0"

__all__ = [
    "Agent",
    "AgentCapability",
    "adapt_solver",
    "AnomalyFlag",
    "Archive",
    "ArchiveConfig",
    "ArchiveEntry",
    "ArchiveStatus",
    "BehaviorAnalyzer",
    "BehaviorConfig",
    "BotConfig",
    "CalibrationError",
    "CalibrationBank",
    "CalibrationConfig",
    "CalibrationDifficulty",
    "CalibrationProblem",
    "CalibrationResult",
    "CalibrationVerdict",
    "CapacityError",
    "CircularDependencyError",
    "DependencyError",
    "DifficultyConfig",
    "DifficultyEstimator",
    "DuplicateError",
    "Event",
    "EventBus",
    "EventFilter",
    "EventHandler",
    "EventKind",
    "Exchange",
    "ExchangeConfig",
    "FailureCategory",
    "FailureReport",
    "FixPackage",
    "GuardAction",
    "GuardBlockError",
    "GuardResult",
    "HttpClient",
    "HttpClientError",
    "ModelTier",
    "NotFoundError",
    "OutcomeRecord",
    "OutcomeStatus",
    "PermissionError_",
    "Problem",
    "ProblemDifficultyRecord",
    "ProblemSortKey",
    "ProblemStatus",
    "ProblemTag",
    "QualityConfig",
    "RateLimitAction",
    "RateLimitConfig",
    "RateLimiter",
    "RateLimitError",
    "RateLimitRule",
    "ReputationEvent",
    "ReputationLedger",
    "Review",
    "ReviewSnapshot",
    "ReviewType",
    "ReviewVerdict",
    "RevisionRound",
    "SchwarmaClient",
    "SchwarmaError",
    "SchwarmaMCPServer",
    "SchwarmaBot",
    "SchwarmaStation",
    "Scheduler",
    "SchedulerConfig",
    "Sensitivity",
    "StationError",
    "SkillConfig",
    "SkillRating",
    "SkillTracker",
    "Solution",
    "SolutionVerdict",
    "StateError",
    "SuspendedError",
    "SwapPool",
    "TriageRouter",
    "TriageStrategy",
    "TrustGate",
    "TrustPolicy",
    "TrustTier",
    "ValidationError",
    "VerificationOracle",
    "VerificationResult",
    "VerificationStatus",
    "load_snapshot",
    "restore_from_dict",
    "save_snapshot",
    "snapshot_to_dict",
    "redact_secrets",
    "run_guards",
]
