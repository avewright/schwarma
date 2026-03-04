"""Tests for schwarma.calibration — CalibrationBank, scoring, history."""

import pytest
from uuid import uuid4

from schwarma.agent import AgentCapability
from schwarma.calibration import (
    CalibrationBank,
    CalibrationConfig,
    CalibrationDifficulty,
    CalibrationProblem,
    CalibrationResult,
    CalibrationVerdict,
    default_scorer,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cal_problem(
    title: str = "Cal Problem",
    caps: set[AgentCapability] | None = None,
    difficulty: CalibrationDifficulty = CalibrationDifficulty.MEDIUM,
    known_solution: str = "42",
) -> CalibrationProblem:
    return CalibrationProblem(
        title=title,
        description="Calibration test",
        known_solution=known_solution,
        capabilities=caps or {AgentCapability.DEBUGGING},
        difficulty=difficulty,
    )


# ---------------------------------------------------------------------------
# Default scorer
# ---------------------------------------------------------------------------

class TestDefaultScorer:
    def test_exact_match(self):
        verdict, score = default_scorer("42", "42")
        assert verdict == CalibrationVerdict.PASS
        assert score == 1.0

    def test_case_insensitive_match(self):
        verdict, score = default_scorer("Hello World", "hello world")
        assert verdict == CalibrationVerdict.PASS
        assert score == 1.0

    def test_partial_substring(self):
        verdict, score = default_scorer("The answer is 42", "42")
        assert verdict == CalibrationVerdict.PARTIAL
        assert score == 0.5

    def test_no_match(self):
        verdict, score = default_scorer("wrong answer", "42")
        assert verdict == CalibrationVerdict.FAIL
        assert score == 0.0


# ---------------------------------------------------------------------------
# CalibrationBank basics
# ---------------------------------------------------------------------------

class TestCalibrationBankBasic:
    def test_add_problem(self):
        bank = CalibrationBank()
        p = _cal_problem()
        bank.add_problem(p)
        assert bank.problem_count == 1

    def test_remove_problem(self):
        bank = CalibrationBank()
        p = _cal_problem()
        bank.add_problem(p)
        bank.remove_problem(p.id)
        assert bank.problem_count == 0

    def test_remove_nonexistent_is_safe(self):
        bank = CalibrationBank()
        bank.remove_problem(uuid4())  # no error

    def test_problems_for_capability(self):
        bank = CalibrationBank()
        p1 = _cal_problem(caps={AgentCapability.DEBUGGING})
        p2 = _cal_problem(caps={AgentCapability.CODE_REVIEW})
        bank.add_problem(p1)
        bank.add_problem(p2)
        debug_probs = bank.problems_for(AgentCapability.DEBUGGING)
        assert len(debug_probs) == 1
        assert debug_probs[0].id == p1.id


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------

class TestCalibrationDraw:
    def test_draw_returns_unseen_problem(self):
        bank = CalibrationBank()
        p = _cal_problem()
        bank.add_problem(p)
        agent_id = uuid4()
        drawn = bank.draw(agent_id, {AgentCapability.DEBUGGING})
        assert drawn is not None
        assert drawn.id == p.id

    def test_draw_excludes_already_seen(self):
        bank = CalibrationBank()
        p = _cal_problem()
        bank.add_problem(p)
        agent_id = uuid4()
        bank.draw(agent_id, {AgentCapability.DEBUGGING})
        # Second draw — already seen the only problem
        drawn2 = bank.draw(agent_id, {AgentCapability.DEBUGGING})
        assert drawn2 is None

    def test_draw_respects_max_per_agent(self):
        bank = CalibrationBank(CalibrationConfig(max_per_agent=1))
        bank.add_problem(_cal_problem(title="A"))
        bank.add_problem(_cal_problem(title="B"))
        agent_id = uuid4()
        d1 = bank.draw(agent_id, {AgentCapability.DEBUGGING})
        assert d1 is not None
        d2 = bank.draw(agent_id, {AgentCapability.DEBUGGING})
        assert d2 is None

    def test_draw_filters_by_difficulty(self):
        bank = CalibrationBank()
        easy = _cal_problem(title="Easy", difficulty=CalibrationDifficulty.EASY)
        hard = _cal_problem(title="Hard", difficulty=CalibrationDifficulty.HARD)
        bank.add_problem(easy)
        bank.add_problem(hard)
        agent_id = uuid4()
        drawn = bank.draw(
            agent_id,
            {AgentCapability.DEBUGGING},
            difficulty=CalibrationDifficulty.EASY,
        )
        assert drawn is not None
        assert drawn.difficulty == CalibrationDifficulty.EASY

    def test_draw_returns_none_for_unmatched_cap(self):
        bank = CalibrationBank()
        bank.add_problem(_cal_problem(caps={AgentCapability.DEBUGGING}))
        agent_id = uuid4()
        drawn = bank.draw(agent_id, {AgentCapability.MATH})
        assert drawn is None


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

class TestCalibrationEvaluation:
    def test_evaluate_correct_answer(self):
        bank = CalibrationBank()
        p = _cal_problem(known_solution="42")
        bank.add_problem(p)
        agent_id = uuid4()
        result = bank.evaluate(agent_id, p.id, "42")
        assert result.verdict == CalibrationVerdict.PASS
        assert result.score == 1.0
        assert bank.is_pass(result)

    def test_evaluate_wrong_answer(self):
        bank = CalibrationBank()
        p = _cal_problem(known_solution="42")
        bank.add_problem(p)
        agent_id = uuid4()
        result = bank.evaluate(agent_id, p.id, "wrong")
        assert result.verdict == CalibrationVerdict.FAIL
        assert not bank.is_pass(result)

    def test_evaluate_unknown_problem_raises(self):
        bank = CalibrationBank()
        with pytest.raises(ValueError, match="Unknown calibration"):
            bank.evaluate(uuid4(), uuid4(), "answer")

    def test_results_stored_in_history(self):
        bank = CalibrationBank()
        p = _cal_problem()
        bank.add_problem(p)
        agent_id = uuid4()
        bank.evaluate(agent_id, p.id, "42")
        bank.evaluate(agent_id, p.id, "wrong")
        assert bank.total_results == 2

    def test_results_for_agent(self):
        bank = CalibrationBank()
        p = _cal_problem()
        bank.add_problem(p)
        a1, a2 = uuid4(), uuid4()
        bank.evaluate(a1, p.id, "42")
        bank.evaluate(a2, p.id, "wrong")
        assert len(bank.results_for_agent(a1)) == 1
        assert len(bank.results_for_agent(a2)) == 1


# ---------------------------------------------------------------------------
# Pass rate
# ---------------------------------------------------------------------------

class TestPassRate:
    def test_pass_rate_all_pass(self):
        bank = CalibrationBank()
        p1 = _cal_problem(title="A", known_solution="yes")
        p2 = _cal_problem(title="B", known_solution="yes")
        bank.add_problem(p1)
        bank.add_problem(p2)
        agent_id = uuid4()
        bank.evaluate(agent_id, p1.id, "yes")
        bank.evaluate(agent_id, p2.id, "yes")
        assert bank.pass_rate(agent_id) == 1.0

    def test_pass_rate_half(self):
        bank = CalibrationBank()
        p1 = _cal_problem(title="A", known_solution="yes")
        p2 = _cal_problem(title="B", known_solution="yes")
        bank.add_problem(p1)
        bank.add_problem(p2)
        agent_id = uuid4()
        bank.evaluate(agent_id, p1.id, "yes")
        bank.evaluate(agent_id, p2.id, "no")
        assert bank.pass_rate(agent_id) == pytest.approx(0.5)

    def test_pass_rate_no_results(self):
        bank = CalibrationBank()
        assert bank.pass_rate(uuid4()) == 0.0

    def test_agent_seen_count(self):
        bank = CalibrationBank()
        p = _cal_problem()
        bank.add_problem(p)
        agent_id = uuid4()
        assert bank.agent_seen_count(agent_id) == 0
        bank.draw(agent_id, {AgentCapability.DEBUGGING})
        assert bank.agent_seen_count(agent_id) == 1


# ---------------------------------------------------------------------------
# Injection probability
# ---------------------------------------------------------------------------

class TestInjection:
    def test_should_inject_respects_probability(self):
        bank = CalibrationBank(CalibrationConfig(injection_probability=1.0))
        assert bank.should_inject() is True

    def test_should_inject_never(self):
        bank = CalibrationBank(CalibrationConfig(injection_probability=0.0))
        assert bank.should_inject() is False
