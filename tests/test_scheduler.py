"""Tests for schwarma.scheduler — background maintenance loops."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from schwarma.agent import Agent
from schwarma.exchange import Exchange, ExchangeConfig
from schwarma.problem import Problem, ProblemStatus
from schwarma.glob import GlobStatus
from schwarma.scheduler import Scheduler, SchedulerConfig


# ── Helpers ──────────────────────────────────────────────────────────────

async def _dummy_solver(desc: str, ctx: dict) -> str:
    return "solved"


def _short_config(**overrides: float) -> SchedulerConfig:
    """Return a SchedulerConfig with very short intervals for testing."""
    defaults = dict(
        expire_problems_interval=0.05,
        expire_claims_interval=0.05,
        expire_globs_interval=0.05,
        escalate_bounties_interval=0.05,
        escalate_bounties_stale_seconds=0.0,  # treat everything as stale
        reputation_decay_interval=0.05,
        archive_expiry_interval=0.05,
        skill_decay_interval=0.05,
    )
    defaults.update(overrides)
    return SchedulerConfig(**defaults)


# ── Lifecycle ────────────────────────────────────────────────────────────


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start_stop(self):
        ex = Exchange()
        sched = Scheduler(ex, _short_config())
        assert not sched.running

        await sched.start()
        assert sched.running
        assert sched.active_tasks == 7  # all 7 jobs enabled

        await sched.stop()
        assert not sched.running
        assert sched.active_tasks == 0

    @pytest.mark.asyncio
    async def test_double_start_noop(self):
        ex = Exchange()
        sched = Scheduler(ex, _short_config())
        await sched.start()
        count = sched.active_tasks
        await sched.start()  # should be no-op
        assert sched.active_tasks == count
        await sched.stop()

    @pytest.mark.asyncio
    async def test_double_stop_noop(self):
        ex = Exchange()
        sched = Scheduler(ex, _short_config())
        await sched.start()
        await sched.stop()
        await sched.stop()  # should not raise

    @pytest.mark.asyncio
    async def test_context_manager(self):
        ex = Exchange()
        async with Scheduler(ex, _short_config()) as sched:
            assert sched.running
        assert not sched.running

    @pytest.mark.asyncio
    async def test_disabled_tasks(self):
        """Setting interval to 0 disables that task."""
        cfg = SchedulerConfig(
            expire_problems_interval=0,
            expire_claims_interval=0,
            expire_globs_interval=0,
            escalate_bounties_interval=0.05,
            reputation_decay_interval=0,
            archive_expiry_interval=0,
            skill_decay_interval=0,
        )
        sched = Scheduler(Exchange(), cfg)
        await sched.start()
        assert sched.active_tasks == 1  # only escalate_bounties
        await sched.stop()


# ── Job execution ────────────────────────────────────────────────────────


class TestJobs:
    @pytest.mark.asyncio
    async def test_expire_problems_fires(self):
        """Scheduler should expire problems whose deadline has passed."""
        cfg = ExchangeConfig(
            min_reputation_to_claim=0,
            enable_staking=False,
        )
        ex = Exchange(cfg)
        agent = Agent(name="Alice", solver=_dummy_solver)
        ex.register(agent)

        # Post problem with a deadline in the past
        problem = await ex.post_problem(Problem(
            title="Old one",
            description="Stale problem",
            author_id=agent.id,
        ))
        problem.deadline = datetime.now(timezone.utc) - timedelta(seconds=1)

        sched_cfg = _short_config(
            expire_claims_interval=0,
            expire_globs_interval=0,
            escalate_bounties_interval=0,
            reputation_decay_interval=0,
            archive_expiry_interval=0,
            skill_decay_interval=0,
        )
        async with Scheduler(ex, sched_cfg):
            await asyncio.sleep(0.15)  # let at least one cycle run

        assert problem.status == ProblemStatus.EXPIRED

    @pytest.mark.asyncio
    async def test_expire_claims_fires(self):
        """Scheduler should release stale claims."""
        cfg = ExchangeConfig(
            min_reputation_to_claim=0,
            enable_staking=False,
            claim_timeout_seconds=1,  # very short
        )
        ex = Exchange(cfg)
        author = Agent(name="Author", solver=_dummy_solver)
        ex.register(author)
        solver = Agent(name="Solver", solver=_dummy_solver)
        ex.register(solver)

        problem = await ex.post_problem(Problem(
            title="Claim me",
            description="Will time out",
            author_id=author.id,
        ))
        await ex.claim_problem(problem.id, solver.id)

        # Backdate the claim to force expiry
        ex._claim_times[(solver.id, problem.id)] = datetime.now(timezone.utc) - timedelta(seconds=5)

        sched_cfg = _short_config(
            expire_problems_interval=0,
            expire_globs_interval=0,
            escalate_bounties_interval=0,
            reputation_decay_interval=0,
            archive_expiry_interval=0,
            skill_decay_interval=0,
        )
        async with Scheduler(ex, sched_cfg):
            await asyncio.sleep(0.15)

        assert problem.status == ProblemStatus.OPEN

    @pytest.mark.asyncio
    async def test_expire_globs_fires(self):
        """Scheduler should disband inactive globs when timeout is enabled."""
        cfg = ExchangeConfig(
            min_reputation_to_claim=0,
            enable_staking=False,
            glob_timeout_seconds=1,
        )
        ex = Exchange(cfg)
        author = Agent(name="Author", solver=_dummy_solver)
        coordinator = Agent(name="Coordinator", solver=_dummy_solver)
        ex.register(author)
        ex.register(coordinator)

        problem = await ex.post_problem(Problem(
            title="Glob target",
            description="collaborative work",
            author_id=author.id,
        ))
        glob = await ex.form_glob(coordinator_id=coordinator.id, problem_id=problem.id, name="stale-glob")

        # Backdate activity to force timeout
        glob.created_at = datetime.now(timezone.utc) - timedelta(seconds=5)
        for m in glob.memberships:
            m.joined_at = datetime.now(timezone.utc) - timedelta(seconds=5)

        sched_cfg = _short_config(
            expire_problems_interval=0,
            expire_claims_interval=0,
            escalate_bounties_interval=0,
            reputation_decay_interval=0,
            archive_expiry_interval=0,
            skill_decay_interval=0,
        )
        async with Scheduler(ex, sched_cfg):
            await asyncio.sleep(0.15)

        assert glob.status == GlobStatus.DISBANDED

    @pytest.mark.asyncio
    async def test_escalate_bounties_fires(self):
        """Scheduler should escalate bounties on old open problems."""
        cfg = ExchangeConfig(
            min_reputation_to_claim=0,
            enable_staking=False,
            escalation_increment=5,
            max_bounty=200,
        )
        ex = Exchange(cfg)
        agent = Agent(name="Alice", solver=_dummy_solver)
        ex.register(agent)

        problem = await ex.post_problem(Problem(
            title="Stale",
            description="Needs bounty bump",
            author_id=agent.id,
        ))
        original_bounty = problem.bounty

        sched_cfg = _short_config(
            expire_problems_interval=0,
            expire_claims_interval=0,
            expire_globs_interval=0,
            escalate_bounties_stale_seconds=0.0,  # everything is stale
            reputation_decay_interval=0,
            archive_expiry_interval=0,
            skill_decay_interval=0,
        )
        async with Scheduler(ex, sched_cfg):
            await asyncio.sleep(0.15)

        assert problem.bounty > original_bounty

    @pytest.mark.asyncio
    async def test_default_config(self):
        """Default SchedulerConfig has reasonable production intervals."""
        cfg = SchedulerConfig()
        assert cfg.expire_problems_interval == 60.0
        assert cfg.expire_claims_interval == 30.0
        assert cfg.expire_globs_interval == 60.0
        assert cfg.escalate_bounties_interval == 300.0
        assert cfg.reputation_decay_interval == 3600.0
        assert cfg.archive_expiry_interval == 3600.0
        assert cfg.skill_decay_interval == 86400.0

    @pytest.mark.asyncio
    async def test_job_exception_does_not_crash(self):
        """If a maintenance job raises, the loop continues."""
        ex = Exchange()
        # Monkey-patch to raise an exception
        original = ex.expire_stale_problems

        call_count = 0

        async def _exploding():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("boom")
            return await original()

        ex.expire_stale_problems = _exploding

        sched_cfg = _short_config(
            expire_claims_interval=0,
            expire_globs_interval=0,
            escalate_bounties_interval=0,
            reputation_decay_interval=0,
            archive_expiry_interval=0,
            skill_decay_interval=0,
        )
        async with Scheduler(ex, sched_cfg):
            await asyncio.sleep(0.2)

        # Should have survived the first exception and run again
        assert call_count >= 2


# ── Station integration ──────────────────────────────────────────────────


class TestStationIntegration:
    @pytest.mark.asyncio
    async def test_station_has_scheduler(self):
        from schwarma.station import SchwarmaStation

        station = SchwarmaStation(require_auth=False)
        assert hasattr(station, "scheduler")
        assert isinstance(station.scheduler, Scheduler)
        assert not station.scheduler.running

    @pytest.mark.asyncio
    async def test_station_custom_scheduler_config(self):
        from schwarma.station import SchwarmaStation

        cfg = SchedulerConfig(expire_problems_interval=120.0)
        station = SchwarmaStation(
            require_auth=False,
            scheduler_config=cfg,
        )
        assert station.scheduler.config.expire_problems_interval == 120.0
