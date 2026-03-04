"""Tests for schwarma.difficulty — DifficultyEstimator."""

import pytest
from uuid import uuid4

from schwarma.agent import ModelTier
from schwarma.difficulty import (
    DifficultyConfig,
    DifficultyEstimator,
    ProblemDifficultyRecord,
)


# ---------------------------------------------------------------------------
# ProblemDifficultyRecord
# ---------------------------------------------------------------------------

class TestProblemDifficultyRecord:
    def test_median_no_times(self):
        rec = ProblemDifficultyRecord(problem_id=uuid4())
        assert rec.median_solve_time is None

    def test_median_single_time(self):
        rec = ProblemDifficultyRecord(problem_id=uuid4(), solve_times=[120.0])
        assert rec.median_solve_time == 120.0

    def test_median_odd_count(self):
        rec = ProblemDifficultyRecord(
            problem_id=uuid4(), solve_times=[10, 30, 20]
        )
        assert rec.median_solve_time == 20.0

    def test_median_even_count(self):
        rec = ProblemDifficultyRecord(
            problem_id=uuid4(), solve_times=[10, 20, 30, 40]
        )
        assert rec.median_solve_time == 25.0

    def test_to_dict(self):
        rec = ProblemDifficultyRecord(
            problem_id=uuid4(),
            rejection_count=2,
            attempt_count=3,
            solver_tier=ModelTier.PREMIUM,
        )
        d = rec.to_dict()
        assert d["rejection_count"] == 2
        assert d["solver_tier"] == "PREMIUM"


# ---------------------------------------------------------------------------
# DifficultyEstimator basics
# ---------------------------------------------------------------------------

class TestDifficultyEstimatorBasic:
    def test_unknown_problem_returns_neutral(self):
        est = DifficultyEstimator()
        assert est.difficulty_score(uuid4()) == 1.0

    def test_record_attempt(self):
        est = DifficultyEstimator()
        pid = uuid4()
        est.record_attempt(pid)
        rec = est.get_record(pid)
        assert rec is not None
        assert rec.attempt_count == 1
        assert rec.first_claimed_at is not None

    def test_record_rejection(self):
        est = DifficultyEstimator()
        pid = uuid4()
        est.record_rejection(pid)
        rec = est.get_record(pid)
        assert rec.rejection_count == 1

    def test_record_acceptance(self):
        est = DifficultyEstimator()
        pid = uuid4()
        est.record_acceptance(pid, solver_tier=ModelTier.PREMIUM, solve_seconds=120.0)
        rec = est.get_record(pid)
        assert rec.accepted is True
        assert rec.solver_tier == ModelTier.PREMIUM
        assert 120.0 in rec.solve_times

    def test_record_solve_time(self):
        est = DifficultyEstimator()
        pid = uuid4()
        est.record_solve_time(pid, 60.0)
        est.record_solve_time(pid, 120.0)
        rec = est.get_record(pid)
        assert len(rec.solve_times) == 2

    def test_tracked_count(self):
        est = DifficultyEstimator()
        est.record_attempt(uuid4())
        est.record_attempt(uuid4())
        assert est.tracked_count == 2


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

class TestDifficultyScoring:
    def test_many_rejections_increase_difficulty(self):
        est = DifficultyEstimator()
        easy_pid = uuid4()
        hard_pid = uuid4()

        est.record_attempt(easy_pid)
        est.record_acceptance(easy_pid)

        est.record_attempt(hard_pid)
        for _ in range(5):
            est.record_rejection(hard_pid)

        easy_score = est.difficulty_score(easy_pid)
        hard_score = est.difficulty_score(hard_pid)
        assert hard_score > easy_score

    def test_premium_solver_increases_difficulty(self):
        est = DifficultyEstimator()
        cheap_pid = uuid4()
        prem_pid = uuid4()

        est.record_acceptance(cheap_pid, solver_tier=ModelTier.LIGHTWEIGHT)
        est.record_acceptance(prem_pid, solver_tier=ModelTier.PREMIUM)

        cheap_score = est.difficulty_score(cheap_pid)
        prem_score = est.difficulty_score(prem_pid)
        assert prem_score > cheap_score

    def test_score_clamped_to_range(self):
        cfg = DifficultyConfig(min_difficulty=0.5, max_difficulty=2.5)
        est = DifficultyEstimator(cfg)
        pid = uuid4()
        # Max out all signals
        for _ in range(20):
            est.record_rejection(pid)
            est.record_attempt(pid)
        est.record_acceptance(pid, solver_tier=ModelTier.PREMIUM, solve_seconds=10000.0)
        score = est.difficulty_score(pid)
        assert 0.5 <= score <= 2.5

    def test_long_solve_time_increases_difficulty(self):
        est = DifficultyEstimator()
        quick_pid = uuid4()
        slow_pid = uuid4()

        est.record_acceptance(quick_pid, solve_seconds=10.0)
        est.record_acceptance(slow_pid, solve_seconds=3000.0)

        quick_score = est.difficulty_score(quick_pid)
        slow_score = est.difficulty_score(slow_pid)
        assert slow_score > quick_score


# ---------------------------------------------------------------------------
# Hardest problems query
# ---------------------------------------------------------------------------

class TestHardestProblems:
    def test_hardest_returns_sorted(self):
        est = DifficultyEstimator()
        easy = uuid4()
        hard = uuid4()
        est.record_attempt(easy)
        est.record_attempt(hard)
        for _ in range(8):
            est.record_rejection(hard)

        top = est.hardest_problems(top_n=2)
        assert len(top) == 2
        assert top[0][0] == hard  # hardest first
        assert top[0][1] >= top[1][1]
