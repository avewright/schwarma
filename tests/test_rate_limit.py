"""Tests for schwarma.rate_limit — per-agent rate limiting."""

from datetime import timedelta
from uuid import uuid4

import pytest

from schwarma.rate_limit import (
    RateLimitAction,
    RateLimitConfig,
    RateLimiter,
    RateLimitRule,
)


class TestRateLimiter:
    def test_allows_within_limit(self):
        config = RateLimitConfig(rules=[
            RateLimitRule(RateLimitAction.POST_PROBLEM, count=3, window=timedelta(minutes=5)),
        ])
        limiter = RateLimiter(config)
        agent = uuid4()
        for _ in range(3):
            assert limiter.check_and_record(agent, RateLimitAction.POST_PROBLEM)

    def test_blocks_over_limit(self):
        config = RateLimitConfig(rules=[
            RateLimitRule(RateLimitAction.POST_PROBLEM, count=2, window=timedelta(minutes=5)),
        ])
        limiter = RateLimiter(config)
        agent = uuid4()
        assert limiter.check_and_record(agent, RateLimitAction.POST_PROBLEM)
        assert limiter.check_and_record(agent, RateLimitAction.POST_PROBLEM)
        assert not limiter.check_and_record(agent, RateLimitAction.POST_PROBLEM)

    def test_different_agents_independent(self):
        config = RateLimitConfig(rules=[
            RateLimitRule(RateLimitAction.CLAIM_PROBLEM, count=1, window=timedelta(minutes=5)),
        ])
        limiter = RateLimiter(config)
        a, b = uuid4(), uuid4()
        assert limiter.check_and_record(a, RateLimitAction.CLAIM_PROBLEM)
        assert limiter.check_and_record(b, RateLimitAction.CLAIM_PROBLEM)
        assert not limiter.check_and_record(a, RateLimitAction.CLAIM_PROBLEM)

    def test_different_actions_independent(self):
        config = RateLimitConfig(rules=[
            RateLimitRule(RateLimitAction.POST_PROBLEM, count=1, window=timedelta(minutes=5)),
            RateLimitRule(RateLimitAction.CLAIM_PROBLEM, count=1, window=timedelta(minutes=5)),
        ])
        limiter = RateLimiter(config)
        agent = uuid4()
        assert limiter.check_and_record(agent, RateLimitAction.POST_PROBLEM)
        assert limiter.check_and_record(agent, RateLimitAction.CLAIM_PROBLEM)

    def test_disabled_limiter_always_allows(self):
        config = RateLimitConfig(enabled=False, rules=[
            RateLimitRule(RateLimitAction.POST_PROBLEM, count=1, window=timedelta(minutes=5)),
        ])
        limiter = RateLimiter(config)
        agent = uuid4()
        for _ in range(10):
            assert limiter.check_and_record(agent, RateLimitAction.POST_PROBLEM)

    def test_reset_clears_history(self):
        config = RateLimitConfig(rules=[
            RateLimitRule(RateLimitAction.POST_PROBLEM, count=1, window=timedelta(minutes=5)),
        ])
        limiter = RateLimiter(config)
        agent = uuid4()
        assert limiter.check_and_record(agent, RateLimitAction.POST_PROBLEM)
        assert not limiter.check(agent, RateLimitAction.POST_PROBLEM)
        limiter.reset(agent)
        assert limiter.check(agent, RateLimitAction.POST_PROBLEM)

    def test_prune_removes_old_entries(self):
        config = RateLimitConfig(rules=[
            RateLimitRule(RateLimitAction.POST_PROBLEM, count=5, window=timedelta(minutes=1)),
        ])
        limiter = RateLimiter(config)
        agent = uuid4()
        limiter.record(agent, RateLimitAction.POST_PROBLEM)
        # Prune won't remove recent entries
        removed = limiter.prune()
        assert removed == 0

    def test_no_rules_allows_all(self):
        config = RateLimitConfig(rules=[])
        limiter = RateLimiter(config)
        agent = uuid4()
        assert limiter.check(agent, RateLimitAction.POST_PROBLEM)

    def test_default_config_has_rules(self):
        config = RateLimitConfig()
        assert len(config.rules) >= 5
