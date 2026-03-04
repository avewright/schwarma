"""Tests for the review tiebreaker protocol."""

from __future__ import annotations

import pytest

from schwarma.agent import Agent
from schwarma.events import EventKind
from schwarma.exchange import Exchange, ExchangeConfig
from schwarma.problem import Problem, ProblemStatus
from schwarma.review import Review, ReviewType, ReviewVerdict
from schwarma.solution import SolutionVerdict


# ── Helpers ──────────────────────────────────────────────────────────────

async def _dummy(desc: str, ctx: dict) -> str:
    return "solved"


def _cfg(**overrides) -> ExchangeConfig:
    """Sensible defaults with guards off so we can focus on tiebreakers."""
    defaults = dict(
        min_reputation_to_claim=0,
        enable_staking=False,
        enable_content_guards=False,
        enable_effort_guards=False,
    )
    defaults.update(overrides)
    return ExchangeConfig(**defaults)


async def _setup(cfg: ExchangeConfig, n_reviewers: int = 4):
    """Register author + solver + N reviewers, post + claim + solve a problem."""
    ex = Exchange(cfg)
    author = Agent(name="Author", solver=_dummy)
    solver = Agent(name="Solver", solver=_dummy)
    reviewers = [Agent(name=f"R{i}", solver=_dummy) for i in range(n_reviewers)]
    for a in [author, solver] + reviewers:
        ex.register(a)

    problem = await ex.post_problem(Problem(
        title="Tiebreak test",
        description="Testing tiebreaker",
        author_id=author.id,
        bounty=20,
    ))
    await ex.claim_problem(problem.id, solver.id)
    solution = await ex.solve_problem(
        problem.id, solver.id, solution_body="my answer",
    )
    return ex, problem, solution, solver, reviewers


async def _submit_verdicts(ex, solution_id, reviewers, verdicts):
    """Submit a list of (reviewer, verdict) reviews."""
    for reviewer, verdict in zip(reviewers, verdicts):
        await ex.submit_review(Review(
            solution_id=solution_id,
            reviewer_id=reviewer.id,
            review_type=ReviewType.CORRECTNESS,
            verdict=verdict,
            body=f"vote:{verdict.name}",
        ))


# ── Tests ────────────────────────────────────────────────────────────────


class TestTiebreaker:

    # ─── reject fallback (default) ─────────────────────────────────

    @pytest.mark.asyncio
    async def test_reject_fallback_on_tie(self):
        """When tied after extra reviews, default reject fallback kicks in.

        Setup: required=3, extra=1 → need 4 reviews with no quorum of 3.
        2 approve + 2 reject → fallback=reject → REJECTED.
        """
        cfg = _cfg(
            reviews_required_for_accept=3,
            tiebreaker_extra_reviews=1,
            tiebreaker_fallback="reject",
        )
        ex, problem, solution, solver, reviewers = await _setup(cfg)

        await _submit_verdicts(ex, solution.id, reviewers, [
            ReviewVerdict.APPROVE,
            ReviewVerdict.REJECT,
            ReviewVerdict.APPROVE,
            ReviewVerdict.REJECT,
        ])

        assert solution.verdict == SolutionVerdict.REJECTED
        assert problem.status == ProblemStatus.OPEN  # re-opened

    # ─── accept fallback ───────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_accept_fallback_half_bounty(self):
        """Accept tiebreaker awards half bounty."""
        cfg = _cfg(
            reviews_required_for_accept=3,
            tiebreaker_extra_reviews=1,
            tiebreaker_fallback="accept",
        )
        ex, problem, solution, solver, reviewers = await _setup(cfg)

        rep_before = ex.ledger.balance(solver.id)

        await _submit_verdicts(ex, solution.id, reviewers, [
            ReviewVerdict.APPROVE,
            ReviewVerdict.REJECT,
            ReviewVerdict.REJECT,
            ReviewVerdict.APPROVE,
        ])

        assert solution.verdict == SolutionVerdict.ACCEPTED
        assert problem.status == ProblemStatus.CLOSED

        rep_after = ex.ledger.balance(solver.id)
        # Half bounty = 10 (from bounty 20)
        assert rep_after - rep_before == problem.bounty // 2

    # ─── revision fallback ─────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_revision_fallback(self):
        """Revision tiebreaker requests changes instead of accepting/rejecting."""
        cfg = _cfg(
            reviews_required_for_accept=3,
            tiebreaker_extra_reviews=1,
            tiebreaker_fallback="revision",
        )
        ex, problem, solution, solver, reviewers = await _setup(cfg)

        await _submit_verdicts(ex, solution.id, reviewers, [
            ReviewVerdict.APPROVE,
            ReviewVerdict.REJECT,
            ReviewVerdict.REJECT,
            ReviewVerdict.APPROVE,
        ])

        assert solution.verdict == SolutionVerdict.NEEDS_REVISION
        assert problem.status == ProblemStatus.CLAIMED

    # ─── event metadata ───────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_tiebreaker_events_carry_metadata(self):
        """Events emitted during tiebreaker carry tiebreaker=True in data."""
        cfg = _cfg(
            reviews_required_for_accept=3,
            tiebreaker_extra_reviews=1,
            tiebreaker_fallback="accept",
        )
        ex, problem, solution, solver, reviewers = await _setup(cfg)

        captured = []
        ex.bus.subscribe(EventKind.SOLUTION_ACCEPTED, captured.append)

        await _submit_verdicts(ex, solution.id, reviewers, [
            ReviewVerdict.APPROVE,
            ReviewVerdict.REJECT,
            ReviewVerdict.REJECT,
            ReviewVerdict.APPROVE,
        ])

        tb_events = [e for e in captured if e.payload and e.payload.get("tiebreaker")]
        assert len(tb_events) == 1
        assert tb_events[0].payload["tiebreaker"] is True

    # ─── normal quorum bypasses tiebreaker ─────────────────────────

    @pytest.mark.asyncio
    async def test_normal_accept_quorum_unaffected(self):
        """A clear approve quorum doesn't trigger tiebreaker."""
        cfg = _cfg(
            reviews_required_for_accept=2,
            tiebreaker_extra_reviews=1,
            tiebreaker_fallback="reject",
        )
        ex, problem, solution, solver, reviewers = await _setup(cfg, n_reviewers=2)

        await _submit_verdicts(ex, solution.id, reviewers, [
            ReviewVerdict.APPROVE,
            ReviewVerdict.APPROVE,
        ])

        assert solution.verdict == SolutionVerdict.ACCEPTED
        assert problem.status == ProblemStatus.CLOSED

    @pytest.mark.asyncio
    async def test_normal_reject_quorum_unaffected(self):
        """A clear reject quorum doesn't trigger tiebreaker."""
        cfg = _cfg(
            reviews_required_for_accept=2,
            tiebreaker_extra_reviews=1,
            tiebreaker_fallback="accept",  # would accept if tiebreaker fired
        )
        ex, problem, solution, solver, reviewers = await _setup(cfg, n_reviewers=2)

        await _submit_verdicts(ex, solution.id, reviewers, [
            ReviewVerdict.REJECT,
            ReviewVerdict.REJECT,
        ])

        assert solution.verdict == SolutionVerdict.REJECTED
        assert problem.status == ProblemStatus.OPEN  # re-opened

    # ─── extra_reviews=0 still triggers fallback immediately ───────

    @pytest.mark.asyncio
    async def test_zero_extra_reviews_fallback_immediate(self):
        """With tiebreaker_extra_reviews=0, fallback fires as soon as tie detected.

        required=2, extra=0 → 2 reviews needed. 1 approve + 1 reject = tie
        → total(2) >= required(2)+extra(0)=2 → fallback fires.
        """
        cfg = _cfg(
            reviews_required_for_accept=2,
            tiebreaker_extra_reviews=0,
            tiebreaker_fallback="reject",
        )
        ex, problem, solution, solver, reviewers = await _setup(cfg, n_reviewers=2)

        await _submit_verdicts(ex, solution.id, reviewers, [
            ReviewVerdict.APPROVE,
            ReviewVerdict.REJECT,
        ])

        assert solution.verdict == SolutionVerdict.REJECTED

    # ─── three-way split ──────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_three_way_split_triggers_fallback(self):
        """A three-way split (approve/reject/revision) triggers tiebreaker.

        required=3, extra=0 → 3 reviews needed. 1-1-1 split → fallback.
        """
        cfg = _cfg(
            reviews_required_for_accept=3,
            tiebreaker_extra_reviews=0,
            tiebreaker_fallback="revision",
        )
        ex, problem, solution, solver, reviewers = await _setup(cfg, n_reviewers=3)

        await _submit_verdicts(ex, solution.id, reviewers[:3], [
            ReviewVerdict.APPROVE,
            ReviewVerdict.REJECT,
            ReviewVerdict.REQUEST_CHANGES,
        ])

        assert solution.verdict == SolutionVerdict.NEEDS_REVISION
        assert problem.status == ProblemStatus.CLAIMED
