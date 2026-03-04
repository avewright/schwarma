"""Tests for the BehaviorAnalyzer — anomaly detection for agents."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from schwarma.behavior import AnomalyFlag, BehaviorAnalyzer, BehaviorConfig


def _now() -> datetime:
    return datetime.now(timezone.utc)


class TestRubberStampDetection:
    def test_no_flag_with_few_reviews(self):
        analyzer = BehaviorAnalyzer()
        agent = uuid4()
        # Only 3 reviews — below minimum
        for _ in range(3):
            analyzer.record_review(agent, uuid4(), "APPROVE")
        flags = analyzer.check_rubber_stamp(agent)
        assert flags == []

    def test_flag_high_approval_rate(self):
        config = BehaviorConfig(max_approval_rate=0.9, min_reviews_for_rate_check=5)
        analyzer = BehaviorAnalyzer(config)
        agent = uuid4()
        # 10 approvals, 0 rejections → 100% approval rate
        for _ in range(10):
            analyzer.record_review(agent, uuid4(), "APPROVE")
        flags = analyzer.check_rubber_stamp(agent)
        assert len(flags) == 1
        assert flags[0].kind == "rubber_stamp"
        assert flags[0].severity > 0

    def test_no_flag_balanced_reviews(self):
        config = BehaviorConfig(max_approval_rate=0.9, min_reviews_for_rate_check=5)
        analyzer = BehaviorAnalyzer(config)
        agent = uuid4()
        for _ in range(5):
            analyzer.record_review(agent, uuid4(), "APPROVE")
        for _ in range(5):
            analyzer.record_review(agent, uuid4(), "REJECT")
        flags = analyzer.check_rubber_stamp(agent)
        assert flags == []

    def test_approval_rate_property(self):
        analyzer = BehaviorAnalyzer()
        agent = uuid4()
        assert analyzer.approval_rate(agent) is None
        analyzer.record_review(agent, uuid4(), "APPROVE")
        analyzer.record_review(agent, uuid4(), "REJECT")
        assert analyzer.approval_rate(agent) == 0.5


class TestCollusionDetection:
    def test_no_flag_normal_interactions(self):
        analyzer = BehaviorAnalyzer(BehaviorConfig(max_pairwise_interactions=5))
        a, b = uuid4(), uuid4()
        for _ in range(3):
            analyzer.record_review(a, b, "APPROVE")
        flags = analyzer.check_collusion(a)
        assert flags == []

    def test_flag_excessive_pairwise_interactions(self):
        analyzer = BehaviorAnalyzer(BehaviorConfig(max_pairwise_interactions=3))
        a, b = uuid4(), uuid4()
        for _ in range(6):
            analyzer.record_review(a, b, "APPROVE")
        flags = analyzer.check_collusion(a)
        assert len(flags) == 1
        assert flags[0].kind == "collusion"
        assert flags[0].related_agent_id == b

    def test_pairwise_count(self):
        analyzer = BehaviorAnalyzer()
        a, b, c = uuid4(), uuid4(), uuid4()
        analyzer.record_review(a, b, "APPROVE")
        analyzer.record_review(a, b, "REJECT")
        analyzer.record_review(a, c, "APPROVE")
        assert analyzer.pairwise_count(a, b) == 2
        assert analyzer.pairwise_count(a, c) == 1
        assert analyzer.pairwise_count(b, c) == 0


class TestSolveSpeedDetection:
    def test_no_flag_with_few_solves(self):
        analyzer = BehaviorAnalyzer()
        agent = uuid4()
        t = _now()
        analyzer.record_solve(agent, uuid4(), t, t + timedelta(seconds=1))
        flags = analyzer.check_solve_speed(agent)
        assert flags == []  # need >= 3 solves

    def test_flag_suspiciously_fast(self):
        config = BehaviorConfig(min_solve_seconds=10.0)
        analyzer = BehaviorAnalyzer(config)
        agent = uuid4()
        t = _now()
        for i in range(5):
            claimed = t + timedelta(minutes=i)
            solved = claimed + timedelta(seconds=2)  # 2s each — way too fast
            analyzer.record_solve(agent, uuid4(), claimed, solved)
        flags = analyzer.check_solve_speed(agent)
        assert len(flags) == 1
        assert flags[0].kind == "speed"

    def test_no_flag_normal_speed(self):
        config = BehaviorConfig(min_solve_seconds=5.0)
        analyzer = BehaviorAnalyzer(config)
        agent = uuid4()
        t = _now()
        for i in range(5):
            claimed = t + timedelta(minutes=i)
            solved = claimed + timedelta(seconds=30)  # 30s each — fine
            analyzer.record_solve(agent, uuid4(), claimed, solved)
        flags = analyzer.check_solve_speed(agent)
        assert flags == []


class TestActivityBalance:
    def test_no_flag_with_little_activity(self):
        analyzer = BehaviorAnalyzer()
        agent = uuid4()
        analyzer.record_review(agent, uuid4(), "APPROVE")
        flags = analyzer.check_activity_balance(agent)
        assert flags == []

    def test_flag_only_reviews_no_solves(self):
        config = BehaviorConfig(min_activity_ratio=0.15)
        analyzer = BehaviorAnalyzer(config)
        agent = uuid4()
        for _ in range(10):
            analyzer.record_review(agent, uuid4(), "APPROVE")
        # 0 solves, 10 reviews → ratio=0
        flags = analyzer.check_activity_balance(agent)
        assert len(flags) == 1
        assert flags[0].kind == "activity_imbalance"
        assert "reviewing" in flags[0].detail

    def test_flag_only_solves_no_reviews(self):
        config = BehaviorConfig(min_activity_ratio=0.15)
        analyzer = BehaviorAnalyzer(config)
        agent = uuid4()
        t = _now()
        for i in range(10):
            claimed = t + timedelta(minutes=i)
            solved = claimed + timedelta(minutes=1)
            analyzer.record_solve(agent, uuid4(), claimed, solved)
        flags = analyzer.check_activity_balance(agent)
        assert len(flags) == 1
        assert "solving" in flags[0].detail

    def test_balanced_activity_no_flag(self):
        config = BehaviorConfig(min_activity_ratio=0.15)
        analyzer = BehaviorAnalyzer(config)
        agent = uuid4()
        t = _now()
        for _ in range(5):
            analyzer.record_review(agent, uuid4(), "APPROVE")
        for i in range(5):
            claimed = t + timedelta(minutes=i)
            solved = claimed + timedelta(minutes=1)
            analyzer.record_solve(agent, uuid4(), claimed, solved)
        flags = analyzer.check_activity_balance(agent)
        assert flags == []


class TestAnalyzeAll:
    def test_analyze_returns_flags(self):
        config = BehaviorConfig(max_approval_rate=0.9, min_reviews_for_rate_check=5)
        analyzer = BehaviorAnalyzer(config)
        rubber_stamper = uuid4()
        normal_agent = uuid4()
        for _ in range(10):
            analyzer.record_review(rubber_stamper, uuid4(), "APPROVE")
        for _ in range(5):
            analyzer.record_review(normal_agent, uuid4(), "APPROVE")
        for _ in range(5):
            analyzer.record_review(normal_agent, uuid4(), "REJECT")

        all_flags = analyzer.analyze_all([rubber_stamper, normal_agent])
        stamper_flags = [f for f in all_flags if f.agent_id == rubber_stamper and f.kind == "rubber_stamp"]
        assert len(stamper_flags) >= 1

    def test_flags_for_agent(self):
        analyzer = BehaviorAnalyzer(BehaviorConfig(max_approval_rate=0.9, min_reviews_for_rate_check=5))
        agent = uuid4()
        for _ in range(10):
            analyzer.record_review(agent, uuid4(), "APPROVE")
        analyzer.analyze(agent)
        assert len(analyzer.flags_for(agent)) >= 1

    def test_flags_stored_cumulatively(self):
        analyzer = BehaviorAnalyzer(BehaviorConfig(max_approval_rate=0.9, min_reviews_for_rate_check=5))
        agent = uuid4()
        for _ in range(10):
            analyzer.record_review(agent, uuid4(), "APPROVE")
        analyzer.analyze(agent)
        first_count = len(analyzer.flags)
        analyzer.analyze(agent)
        assert len(analyzer.flags) >= first_count  # flags accumulate
