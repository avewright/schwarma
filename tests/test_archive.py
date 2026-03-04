"""Tests for the Archive — store, search, tombstone, expiry."""

from datetime import timedelta
from uuid import uuid4

import pytest

from schwarma.agent import ModelTier
from schwarma.archive import (
    Archive,
    ArchiveConfig,
    ArchiveEntry,
    ArchiveStatus,
    ReviewSnapshot,
)
from schwarma.problem import ProblemTag
from schwarma.review import ReviewVerdict
from schwarma.trust import Sensitivity


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _make_entry(**kw) -> ArchiveEntry:
    defaults = dict(
        problem_id=uuid4(),
        solution_id=uuid4(),
        problem_title="FizzBuzz",
        problem_description="Write FizzBuzz in Python",
        tags={ProblemTag.FEATURE},
        sensitivity=Sensitivity.INTERNAL,
        solution_body="def fizzbuzz(): ...",
        solver_id=uuid4(),
        solver_tier=ModelTier.STANDARD,
        solver_reputation=75,
        reviews=[
            ReviewSnapshot(
                reviewer_id=uuid4(),
                verdict=ReviewVerdict.APPROVE,
                review_type="CORRECTNESS",
                confidence=0.9,
                body="Looks good",
            )
        ],
    )
    defaults.update(kw)
    return ArchiveEntry(**defaults)


# ------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------

class TestStore:
    def test_store_and_count(self):
        archive = Archive()
        entry = _make_entry()
        archive.store(entry)
        assert archive.count == 1
        assert archive.active_count == 1

    def test_get_by_id(self):
        archive = Archive()
        entry = _make_entry()
        archive.store(entry)
        assert archive.get(entry.id) is entry

    def test_get_by_problem(self):
        archive = Archive()
        entry = _make_entry()
        archive.store(entry)
        found = archive.get_by_problem(entry.problem_id)
        assert found is entry

    def test_get_nonexistent_returns_none(self):
        archive = Archive()
        assert archive.get(uuid4()) is None
        assert archive.get_by_problem(uuid4()) is None

    def test_default_ttl_applied(self):
        config = ArchiveConfig(default_ttl=timedelta(days=30))
        archive = Archive(config)
        entry = _make_entry()
        assert entry.ttl is None
        archive.store(entry)
        assert entry.ttl == timedelta(days=30)


class TestSearch:
    def test_search_by_tags(self):
        archive = Archive()
        archive.store(_make_entry(tags={ProblemTag.FEATURE}))
        archive.store(_make_entry(tags={ProblemTag.BUG}))

        results = archive.search(tags={ProblemTag.FEATURE})
        assert len(results) == 1

    def test_search_by_keywords(self):
        archive = Archive()
        archive.store(_make_entry(problem_title="FizzBuzz", problem_description="Write FizzBuzz"))
        archive.store(_make_entry(problem_title="Sorting", problem_description="Implement quicksort"))

        results = archive.search(keywords=["fizzbuzz"])
        assert len(results) == 1
        assert results[0].problem_title == "FizzBuzz"

    def test_search_by_sensitivity(self):
        archive = Archive()
        archive.store(_make_entry(sensitivity=Sensitivity.PUBLIC))
        archive.store(_make_entry(sensitivity=Sensitivity.CONFIDENTIAL))

        results = archive.search(sensitivity=Sensitivity.PUBLIC)
        assert len(results) == 1

    def test_search_by_min_solver_tier(self):
        archive = Archive()
        archive.store(_make_entry(solver_tier=ModelTier.LIGHTWEIGHT))
        archive.store(_make_entry(solver_tier=ModelTier.PREMIUM))

        results = archive.search(min_solver_tier=ModelTier.PREMIUM)
        assert len(results) == 1

    def test_search_specialized_passes_tier_filter(self):
        archive = Archive()
        archive.store(_make_entry(solver_tier=ModelTier.SPECIALIZED))

        results = archive.search(min_solver_tier=ModelTier.PREMIUM)
        assert len(results) == 1

    def test_search_limit(self):
        archive = Archive()
        for _ in range(10):
            archive.store(_make_entry())

        results = archive.search(limit=3)
        assert len(results) == 3

    def test_search_excludes_tombstoned_by_default(self):
        archive = Archive()
        entry = _make_entry()
        archive.store(entry)
        entry.tombstone()

        results = archive.search()
        assert len(results) == 0

    def test_search_includes_tombstoned_when_requested(self):
        archive = Archive()
        entry = _make_entry()
        archive.store(entry)
        entry.tombstone()

        results = archive.search(include_tombstoned=True)
        assert len(results) == 1

    def test_search_no_filters_returns_all_active(self):
        archive = Archive()
        archive.store(_make_entry())
        archive.store(_make_entry())

        results = archive.search()
        assert len(results) == 2


class TestTombstone:
    def test_tombstone_purges_content(self):
        archive = Archive()
        entry = _make_entry()
        archive.store(entry)

        archive.tombstone(entry.id)

        assert entry.status == ArchiveStatus.TOMBSTONED
        assert entry.problem_description == ""
        assert entry.solution_body == ""
        assert entry.reviews[0].body == ""
        # But metadata skeleton is preserved
        assert entry.problem_title == "FizzBuzz"
        assert entry.solver_tier == ModelTier.STANDARD

    def test_tombstone_nonexistent_raises(self):
        archive = Archive()
        with pytest.raises(ValueError):
            archive.tombstone(uuid4())

    def test_active_count_after_tombstone(self):
        archive = Archive()
        entry = _make_entry()
        archive.store(entry)
        assert archive.active_count == 1

        archive.tombstone(entry.id)
        assert archive.active_count == 0
        assert archive.count == 1  # still stored


class TestExpiry:
    def test_expire_stale(self):
        archive = Archive()
        entry = _make_entry(ttl=timedelta(seconds=-1))  # already expired
        archive.store(entry)
        assert entry.is_expired

        count = archive.expire_stale()
        assert count == 1
        assert entry.status == ArchiveStatus.TOMBSTONED

    def test_no_expiry_when_ttl_none(self):
        archive = Archive()
        entry = _make_entry()  # ttl=None
        archive.store(entry)

        count = archive.expire_stale()
        assert count == 0
        assert entry.is_active

    def test_not_expired_within_ttl(self):
        archive = Archive()
        entry = _make_entry(ttl=timedelta(hours=24))
        archive.store(entry)

        assert not entry.is_expired
        count = archive.expire_stale()
        assert count == 0


class TestSerialization:
    def test_to_dict(self):
        entry = _make_entry()
        d = entry.to_dict()
        assert d["problem_title"] == "FizzBuzz"
        assert d["solver_tier"] == "STANDARD"
        assert d["status"] == "ACTIVE"
        assert isinstance(d["tags"], list)
        assert isinstance(d["reviews"], list)
        assert d["reviews"][0]["verdict"] == "APPROVE"


class TestArchiveIntegration:
    """Test archive integration through the Exchange."""

    async def test_solution_archived_on_accept(self):
        from schwarma.exchange import Exchange, ExchangeConfig
        from schwarma.agent import Agent, AgentCapability
        from schwarma.problem import Problem
        from schwarma.review import Review, ReviewType

        async def solver(desc, ctx):
            return "This is a thorough solution with enough words to pass quality checks"

        config = ExchangeConfig(
            auto_assign=False,
            auto_review=False,
            enable_staking=False,
            enable_content_guards=False,
            enable_effort_guards=False,
        )
        ex = Exchange(config)

        alice = Agent(name="Alice", solver=solver,
                      capabilities={AgentCapability.CODE_GENERATION})
        bob = Agent(name="Bob", solver=solver,
                    capabilities={AgentCapability.CODE_REVIEW})
        carol = Agent(name="Carol", solver=solver,
                      capabilities={AgentCapability.CODE_REVIEW})

        ex.register(alice)
        ex.register(bob)
        ex.register(carol)

        p = Problem(
            title="FizzBuzz",
            description="Write FizzBuzz in Python",
            author_id=bob.id,
            tags={ProblemTag.FEATURE},
            bounty=15,
        )
        await ex.post_problem(p)
        sol = await ex.claim_and_solve(p.id, alice.id)

        # Two approvals
        r1 = Review(solution_id=sol.id, reviewer_id=bob.id,
                     review_type=ReviewType.CORRECTNESS,
                     verdict=ReviewVerdict.APPROVE)
        r2 = Review(solution_id=sol.id, reviewer_id=carol.id,
                     review_type=ReviewType.CORRECTNESS,
                     verdict=ReviewVerdict.APPROVE)
        await ex.submit_review(r1)
        await ex.submit_review(r2)

        # Check archive
        assert ex.archive.count == 1
        entry = ex.archive.get_by_problem(p.id)
        assert entry is not None
        assert entry.problem_title == "FizzBuzz"
        assert entry.solver_id == alice.id
        assert len(entry.reviews) == 2


# ===================================================================
# Similarity search
# ===================================================================

class TestSimilaritySearch:
    """Tests for search_similar and search_by_signature."""

    def _make_entry(self, title: str, desc: str, **kw) -> ArchiveEntry:
        return ArchiveEntry(
            problem_title=title,
            problem_description=desc,
            **kw,
        )

    def test_search_similar_basic(self):
        archive = Archive()
        e1 = self._make_entry("Fix null pointer in handler", "NullPointerException in request handler")
        e2 = self._make_entry("Add dark mode toggle", "CSS theme toggle for dark mode")
        archive.store(e1)
        archive.store(e2)

        results = archive.search_similar("null pointer exception handler")
        assert len(results) >= 1
        assert results[0][0].id == e1.id
        assert results[0][1] > 0.0

    def test_search_similar_threshold(self):
        archive = Archive()
        e1 = self._make_entry("Alpha beta gamma", "completely unrelated words")
        archive.store(e1)

        results = archive.search_similar("fix a bug in the code", threshold=0.5)
        assert len(results) == 0  # no overlap exceeds threshold

    def test_search_similar_empty_query(self):
        archive = Archive()
        archive.store(self._make_entry("x", "y"))
        assert archive.search_similar("") == []

    def test_search_by_signature_match(self):
        archive = Archive()
        e1 = self._make_entry("Error fix", "fixing runtime error",
                               metadata={"failure_signature": "RUNTIME_ERROR|indexerror|main.py"})
        e2 = self._make_entry("Other fix", "different problem",
                               metadata={"failure_signature": "SYNTAX_ERROR|missing|foo.py"})
        archive.store(e1)
        archive.store(e2)

        results = archive.search_by_signature("RUNTIME_ERROR|indexerror|main.py")
        assert len(results) == 1
        assert results[0].id == e1.id

    def test_search_by_signature_no_match(self):
        archive = Archive()
        archive.store(self._make_entry("x", "y"))
        results = archive.search_by_signature("NONEXISTENT|sig")
        assert len(results) == 0

    def test_tombstoned_excluded_from_similar(self):
        archive = Archive()
        e1 = self._make_entry("Fix bug in parser", "parser throws error")
        archive.store(e1)
        e1.tombstone()

        results = archive.search_similar("fix bug in parser")
        assert len(results) == 0
