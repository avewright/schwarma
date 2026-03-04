"""
Archive — persistent knowledge store for solved problems.

When a solution is accepted, the exchange writes an ArchiveEntry containing
the problem, solution, review verdicts, and metadata.  This serves as:

  • A knowledge base to avoid duplicate work
  • Training signal for future agents
  • An audit trail of solved problems
  • A research backlog for open/unsolved problems

The archive supports:
  • Writing entries on solution acceptance
  • Searching by tags, keywords, sensitivity
  • Tombstoning (purge content, keep metadata skeleton)
  • TTL-based auto-expiry
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum, auto
from typing import Any
from uuid import UUID, uuid4

from schwarma.agent import ModelTier
from schwarma.problem import ProblemTag
from schwarma.review import ReviewVerdict
from schwarma.trust import Sensitivity

logger = logging.getLogger(__name__)


class ArchiveStatus(Enum):
    ACTIVE = auto()       # full content available
    TOMBSTONED = auto()   # content purged, metadata skeleton kept


@dataclass
class ReviewSnapshot:
    """Lightweight summary of a review for archive storage."""

    reviewer_id: UUID
    verdict: ReviewVerdict
    review_type: str
    confidence: float
    body: str = ""


@dataclass
class ArchiveEntry:
    """A solved problem + accepted solution persisted for reference.

    After tombstoning, ``problem_description``, ``solution_body``, and
    review bodies are set to ``""`` but the metadata skeleton remains
    so the archive can still answer "a problem of this type was solved."
    """

    # Identity
    id: UUID = field(default_factory=uuid4)
    problem_id: UUID = field(default_factory=uuid4)
    solution_id: UUID = field(default_factory=uuid4)

    # Problem snapshot
    problem_title: str = ""
    problem_description: str = ""
    tags: set[ProblemTag] = field(default_factory=set)
    sensitivity: Sensitivity = Sensitivity.INTERNAL

    # Solution snapshot
    solution_body: str = ""
    solver_id: UUID = field(default_factory=uuid4)
    solver_tier: ModelTier = ModelTier.STANDARD
    solver_reputation: int = 0

    # Review summaries
    reviews: list[ReviewSnapshot] = field(default_factory=list)

    # Metadata
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    ttl: timedelta | None = None  # None = never expires
    status: ArchiveStatus = ArchiveStatus.ACTIVE

    # Arbitrary extra data
    metadata: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def is_active(self) -> bool:
        return self.status == ArchiveStatus.ACTIVE

    @property
    def is_expired(self) -> bool:
        if self.ttl is None:
            return False
        return datetime.now(timezone.utc) > self.created_at + self.ttl

    def tombstone(self) -> None:
        """Purge content but keep the metadata skeleton."""
        self.problem_description = ""
        self.solution_body = ""
        for r in self.reviews:
            r.body = ""
        self.status = ArchiveStatus.TOMBSTONED
        logger.info("Tombstoned archive entry %s (%s)", self.id, self.problem_title)

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict for storage / transport."""
        return {
            "id": str(self.id),
            "problem_id": str(self.problem_id),
            "solution_id": str(self.solution_id),
            "problem_title": self.problem_title,
            "problem_description": self.problem_description,
            "tags": [t.name for t in self.tags],
            "sensitivity": self.sensitivity.name,
            "solution_body": self.solution_body,
            "solver_id": str(self.solver_id),
            "solver_tier": self.solver_tier.name,
            "solver_reputation": self.solver_reputation,
            "reviews": [
                {
                    "reviewer_id": str(r.reviewer_id),
                    "verdict": r.verdict.name,
                    "review_type": r.review_type,
                    "confidence": r.confidence,
                    "body": r.body,
                }
                for r in self.reviews
            ],
            "created_at": self.created_at.isoformat(),
            "ttl_seconds": self.ttl.total_seconds() if self.ttl else None,
            "status": self.status.name,
            "metadata": self.metadata,
        }


@dataclass
class ArchiveConfig:
    """Configuration for the archive store."""

    default_ttl: timedelta | None = None  # None = entries never expire
    max_entries: int = 10_000             # hard cap on archive size


class Archive:
    """In-memory archive of solved problems.

    Provides search by tags, keywords, and sensitivity level.
    Supports tombstoning and TTL-based expiry.
    """

    def __init__(self, config: ArchiveConfig | None = None) -> None:
        self.config = config or ArchiveConfig()
        self._entries: dict[UUID, ArchiveEntry] = {}

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def store(self, entry: ArchiveEntry) -> ArchiveEntry:
        """Add an entry to the archive."""
        if entry.ttl is None and self.config.default_ttl is not None:
            entry.ttl = self.config.default_ttl
        self._entries[entry.id] = entry
        logger.info(
            "Archived: %s (problem=%s, tags=%s)",
            entry.id,
            entry.problem_title,
            {t.name for t in entry.tags},
        )
        return entry

    # ------------------------------------------------------------------
    # Read / search
    # ------------------------------------------------------------------

    def get(self, entry_id: UUID) -> ArchiveEntry | None:
        return self._entries.get(entry_id)

    def get_by_problem(self, problem_id: UUID) -> ArchiveEntry | None:
        """Find the archive entry for a specific problem."""
        for entry in self._entries.values():
            if entry.problem_id == problem_id:
                return entry
        return None

    def search(
        self,
        *,
        tags: set[ProblemTag] | None = None,
        keywords: list[str] | None = None,
        sensitivity: Sensitivity | None = None,
        min_solver_tier: ModelTier | None = None,
        include_tombstoned: bool = False,
        limit: int = 20,
    ) -> list[ArchiveEntry]:
        """Search the archive with optional filters.

        All filters are ANDed together.
        """
        results: list[ArchiveEntry] = []

        for entry in self._entries.values():
            # Skip tombstoned unless requested
            if not include_tombstoned and entry.status == ArchiveStatus.TOMBSTONED:
                continue

            # Skip expired
            if entry.is_expired:
                continue

            # Tag filter: entry must have at least one matching tag
            if tags and not (entry.tags & tags):
                continue

            # Sensitivity filter
            if sensitivity is not None and entry.sensitivity != sensitivity:
                continue

            # Tier filter
            if min_solver_tier is not None:
                if (
                    entry.solver_tier != ModelTier.SPECIALIZED
                    and entry.solver_tier.value < min_solver_tier.value
                ):
                    continue

            # Keyword filter: at least one keyword in title or description
            if keywords:
                text = f"{entry.problem_title} {entry.problem_description}".lower()
                if not any(kw.lower() in text for kw in keywords):
                    continue

            results.append(entry)
            if len(results) >= limit:
                break

        return results

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def tombstone(self, entry_id: UUID) -> None:
        """Tombstone a specific entry."""
        entry = self._entries.get(entry_id)
        if entry is None:
            raise ValueError(f"No archive entry with id {entry_id}")
        entry.tombstone()

    def expire_stale(self) -> int:
        """Tombstone all entries past their TTL. Returns count expired."""
        count = 0
        for entry in self._entries.values():
            if entry.is_active and entry.is_expired:
                entry.tombstone()
                count += 1
        if count:
            logger.info("Expired %d stale archive entries", count)
        return count

    @property
    def count(self) -> int:
        return len(self._entries)

    @property
    def active_count(self) -> int:
        return sum(1 for e in self._entries.values() if e.is_active)

    # ------------------------------------------------------------------
    # Similarity search
    # ------------------------------------------------------------------

    def search_by_signature(self, signature: str) -> list[ArchiveEntry]:
        """Find entries whose problem had a matching FailureReport signature.

        Relies on ``ArchiveEntry.metadata["failure_signature"]`` being set
        at archive time.  Returns all active matches (exact signature match).
        """
        results: list[ArchiveEntry] = []
        for entry in self._entries.values():
            if entry.status != ArchiveStatus.ACTIVE:
                continue
            if entry.metadata.get("failure_signature") == signature:
                results.append(entry)
        return results

    def search_similar(
        self,
        text: str,
        *,
        threshold: float = 0.3,
        limit: int = 10,
    ) -> list[tuple[ArchiveEntry, float]]:
        """Return entries with text similarity above *threshold*.

        Uses normalised word-overlap (Jaccard similarity) between the
        query text and each entry's title + description.  No external
        dependencies required.

        Returns ``(entry, score)`` pairs sorted descending by score.
        """
        query_words = self._tokenise(text)
        if not query_words:
            return []

        scored: list[tuple[ArchiveEntry, float]] = []
        for entry in self._entries.values():
            if entry.status != ArchiveStatus.ACTIVE:
                continue
            entry_words = self._tokenise(
                f"{entry.problem_title} {entry.problem_description}"
            )
            if not entry_words:
                continue
            intersection = query_words & entry_words
            union = query_words | entry_words
            score = len(intersection) / len(union)
            if score >= threshold:
                scored.append((entry, score))

        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:limit]

    @staticmethod
    def _tokenise(text: str) -> set[str]:
        """Simple whitespace/punctuation tokeniser."""
        return set(re.findall(r"[a-z0-9]+", text.lower()))

    # ------------------------------------------------------------------
    # Open challenges & glob history
    # ------------------------------------------------------------------

    def open_challenges(self, origin: str | None = None) -> list[ArchiveEntry]:
        """Return archive entries sourced from open challenge feeds.

        Parameters
        ----------
        origin : str | None
            Filter to a specific ``ProblemOrigin`` name (e.g. "KAGGLE",
            "ARXIV").  If None, all open-challenge entries are returned.
        """
        results: list[ArchiveEntry] = []
        for entry in self._entries.values():
            entry_origin = entry.metadata.get("origin")
            if entry_origin in ("OPEN_CHALLENGE", "KAGGLE", "ARXIV", "LEETCODE", "PROJECT_EULER", "CUSTOM"):
                if origin is None or entry_origin == origin:
                    results.append(entry)
        return results

    def glob_results(self, glob_id: str) -> list[ArchiveEntry]:
        """Return all archive entries that were produced by a specific glob."""
        return [
            e for e in self._entries.values()
            if e.metadata.get("glob_id") == glob_id
        ]

    def store_external_score(self, solution_id: str, score_data: dict) -> None:
        """Attach an ExternalScore record to the matching archive entry.

        Looks up the entry by ``solution_id`` in metadata and stores the
        score dict under ``metadata["external_scores"]``.
        """
        for entry in self._entries.values():
            if str(entry.solution_id) == solution_id:
                scores = entry.metadata.setdefault("external_scores", [])
                scores.append(score_data)
                return
        logger.warning("store_external_score: no archive entry found for solution %s", solution_id)

    def challenge_leaderboard(self, external_id: str, top_n: int = 10) -> list[dict]:
        """Return top-N solutions for an open challenge ranked by external score.

        Only entries with ``metadata["external_id"] == external_id`` and a
        stored external score are included.
        """
        results: list[tuple[float, dict]] = []
        for entry in self._entries.values():
            if entry.metadata.get("external_id") != external_id:
                continue
            scores = entry.metadata.get("external_scores", [])
            best_score = max((s.get("score", -1.0) for s in scores), default=-1.0)
            if best_score >= 0:
                results.append((best_score, {
                    "solver_id": str(entry.solver_id),
                    "solution_id": str(entry.solution_id),
                    "score": best_score,
                    "glob_id": entry.metadata.get("glob_id"),
                }))
        results.sort(reverse=True)
        return [r[1] for r in results[:top_n]]

