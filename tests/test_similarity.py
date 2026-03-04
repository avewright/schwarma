"""Tests for problem-similarity detection on post."""

from __future__ import annotations

import pytest

from schwarma.agent import Agent
from schwarma.events import EventKind
from schwarma.exchange import Exchange, ExchangeConfig
from schwarma.problem import Problem
from schwarma.review import Review, ReviewType, ReviewVerdict
from schwarma.errors import DuplicateError


# ── Helpers ──────────────────────────────────────────────────────────────

async def _dummy(desc: str, ctx: dict) -> str:
    return "solved"


def _cfg(**overrides) -> ExchangeConfig:
    defaults = dict(
        min_reputation_to_claim=0,
        enable_staking=False,
        enable_content_guards=False,
        enable_effort_guards=False,
        enable_archive=True,
        enable_similarity_check=True,
        similarity_threshold=0.3,
        reviews_required_for_accept=1,
    )
    defaults.update(overrides)
    return ExchangeConfig(**defaults)


async def _archive_one(ex: Exchange, title: str, desc: str) -> None:
    """Post a problem, claim it, solve it, approve it → archive."""
    agents = list(ex._agents.values())
    author = agents[0]
    solver = agents[1]
    reviewer = agents[2]

    p = await ex.post_problem(Problem(
        title=title,
        description=desc,
        author_id=author.id,
        bounty=10,
    ))
    await ex.claim_problem(p.id, solver.id)
    sol = await ex.solve_problem(p.id, solver.id, solution_body="answer")
    await ex.submit_review(Review(
        solution_id=sol.id,
        reviewer_id=reviewer.id,
        review_type=ReviewType.CORRECTNESS,
        verdict=ReviewVerdict.APPROVE,
        body="Looks good",
    ))
    # problem should now be closed and archived


# ── Tests ────────────────────────────────────────────────────────────────


class TestSimilarityOnPost:

    @pytest.mark.asyncio
    async def test_duplicate_event_emitted(self):
        """Posting a problem similar to an archived one emits DUPLICATE_DETECTED."""
        cfg = _cfg()
        ex = Exchange(cfg)
        author = Agent(name="Author", solver=_dummy)
        solver = Agent(name="Solver", solver=_dummy)
        reviewer = Agent(name="Reviewer", solver=_dummy)
        for a in (author, solver, reviewer):
            ex.register(a)

        await _archive_one(ex, "Fix the login bug", "The login page crashes on submit")

        events = []
        ex.bus.subscribe(EventKind.DUPLICATE_DETECTED, events.append)

        # Post a similar problem
        await ex.post_problem(Problem(
            title="Fix the login page crash",
            description="Login crashes on submit",
            author_id=author.id,
        ))

        assert len(events) == 1
        ev = events[0]
        assert ev.payload["similar_count"] >= 1
        assert float(ev.payload["matches"][0]["score"]) > 0.0

    @pytest.mark.asyncio
    async def test_no_event_for_unrelated_problem(self):
        """Posting an unrelated problem emits no DUPLICATE_DETECTED."""
        cfg = _cfg(similarity_threshold=0.5)
        ex = Exchange(cfg)
        author = Agent(name="Author", solver=_dummy)
        solver = Agent(name="Solver", solver=_dummy)
        reviewer = Agent(name="Reviewer", solver=_dummy)
        for a in (author, solver, reviewer):
            ex.register(a)

        await _archive_one(ex, "Fix the login bug", "The login page crashes on submit")

        events = []
        ex.bus.subscribe(EventKind.DUPLICATE_DETECTED, events.append)

        # Post a completely unrelated problem
        await ex.post_problem(Problem(
            title="Optimize database queries",
            description="SQL aggregation is slow on large tables",
            author_id=author.id,
        ))

        assert len(events) == 0

    @pytest.mark.asyncio
    async def test_similarity_disabled_no_event(self):
        """When similarity checking is off, no events are emitted."""
        cfg = _cfg(enable_similarity_check=False)
        ex = Exchange(cfg)
        author = Agent(name="Author", solver=_dummy)
        solver = Agent(name="Solver", solver=_dummy)
        reviewer = Agent(name="Reviewer", solver=_dummy)
        for a in (author, solver, reviewer):
            ex.register(a)

        await _archive_one(ex, "Fix the login bug", "The login page crashes on submit")

        events = []
        ex.bus.subscribe(EventKind.DUPLICATE_DETECTED, events.append)

        # Same title, but check is disabled
        await ex.post_problem(Problem(
            title="Fix the login bug",
            description="The login page crashes on submit",
            author_id=author.id,
        ))

        assert len(events) == 0

    @pytest.mark.asyncio
    async def test_block_exact_duplicates(self):
        """With block_exact_duplicates=True, exact matches raise DuplicateError."""
        cfg = _cfg(
            block_exact_duplicates=True,
            similarity_threshold=0.3,
        )
        ex = Exchange(cfg)
        author = Agent(name="Author", solver=_dummy)
        solver = Agent(name="Solver", solver=_dummy)
        reviewer = Agent(name="Reviewer", solver=_dummy)
        for a in (author, solver, reviewer):
            ex.register(a)

        await _archive_one(ex, "Fix the login bug", "The login page crashes")

        with pytest.raises(DuplicateError):
            await ex.post_problem(Problem(
                title="Fix the login bug",
                description="The login page crashes",
                author_id=author.id,
            ))

    @pytest.mark.asyncio
    async def test_find_similar_problems_method(self):
        """find_similar_problems() returns scored archive entries."""
        cfg = _cfg()
        ex = Exchange(cfg)
        author = Agent(name="Author", solver=_dummy)
        solver = Agent(name="Solver", solver=_dummy)
        reviewer = Agent(name="Reviewer", solver=_dummy)
        for a in (author, solver, reviewer):
            ex.register(a)

        await _archive_one(ex, "Fix the login bug", "The login page crashes on submit")

        results = ex.find_similar_problems(
            "login page crash bug",
            threshold=0.2,
        )
        assert len(results) >= 1
        entry, score = results[0]
        assert score > 0.0
        assert entry.problem_title == "Fix the login bug"

    @pytest.mark.asyncio
    async def test_empty_archive_no_hits(self):
        """With an empty archive, no similar problems are found."""
        cfg = _cfg()
        ex = Exchange(cfg)
        author = Agent(name="Author", solver=_dummy)
        ex.register(author)

        events = []
        ex.bus.subscribe(EventKind.DUPLICATE_DETECTED, events.append)

        await ex.post_problem(Problem(
            title="First problem ever",
            description="Nothing in archive",
            author_id=author.id,
        ))

        assert len(events) == 0

    @pytest.mark.asyncio
    async def test_similarity_threshold_respected(self):
        """High threshold filters out low-similarity matches."""
        cfg = _cfg(similarity_threshold=0.99)
        ex = Exchange(cfg)
        author = Agent(name="Author", solver=_dummy)
        solver = Agent(name="Solver", solver=_dummy)
        reviewer = Agent(name="Reviewer", solver=_dummy)
        for a in (author, solver, reviewer):
            ex.register(a)

        await _archive_one(ex, "Fix the login bug", "The login page crashes on submit")

        events = []
        ex.bus.subscribe(EventKind.DUPLICATE_DETECTED, events.append)

        # Similar but not identical — high threshold should filter it out
        await ex.post_problem(Problem(
            title="Fix login page crash",
            description="The login crashes on submit sometimes",
            author_id=author.id,
        ))

        assert len(events) == 0
