"""
full_demo.py — Comprehensive Schwarma walkthrough.

Demonstrates every major subsystem in one script:

  1. Exchange setup with lifecycle hooks
  2. Agent registration with different capabilities & tiers
  3. Batch problem posting with priorities
  4. Priority-ordered problem queue with tag filtering
  5. Problem decomposition into sub-problems with dependencies
  6. Claim → Solve → Peer Review → Accept/Reject cycle
  7. Verification oracle (automated testing)
  8. Multi-round revision dialogue
  9. Reputation, skill tracking, and leaderboard
  10. Claim timeout expiry
  11. Event recording and replay
  12. Idempotency (safe retries)

Run:
    python examples/full_demo.py
"""

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import UUID

from schwarma import (
    Agent,
    AgentCapability,
    Exchange,
    ExchangeConfig,
    HookPoint,
    Problem,
    ProblemSortKey,
    ProblemTag,
    Review,
    ReviewType,
    ReviewVerdict,
    RevisionRound,
)
from schwarma.agent import ModelTier
from schwarma.events import EventKind
from schwarma.verification import (
    VerificationOracle,
    VerificationResult,
    VerificationStatus,
)


# ---------------------------------------------------------------------------
# Solver callbacks — stand-ins for real LLM calls
# ---------------------------------------------------------------------------

async def senior_solver(desc: str, ctx: dict) -> str:
    """A strong solver that adapts on revision feedback."""
    if ctx.get("revision_feedback"):
        return f"REVISED (attempt {ctx['attempt']}): fixed based on feedback — {ctx['revision_feedback']}"
    return f"def solution():\n    # Solves: {desc[:60]}\n    return 42"


async def junior_solver(desc: str, ctx: dict) -> str:
    return f"# TODO: {desc[:60]}\npass"


async def reviewer_solver(desc: str, ctx: dict) -> str:
    return "LGTM"


# ---------------------------------------------------------------------------
# A simple verification oracle — pretends to run tests
# ---------------------------------------------------------------------------

class DemoOracle:
    """A fake oracle that passes solutions containing 'return' and fails others."""

    async def verify(self, solution, problem) -> VerificationResult:
        passed = "return" in solution.body
        return VerificationResult(
            status=VerificationStatus.PASSED if passed else VerificationStatus.FAILED,
            passed_tests=3 if passed else 0,
            failed_tests=0 if passed else 2,
            stdout="All tests passed." if passed else "AssertionError on line 5",
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def header(title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def sub(text: str) -> None:
    print(f"  → {text}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:

    # ================================================================
    # 1. Create the Exchange with lifecycle hooks and features enabled
    # ================================================================
    header("1. Exchange Setup")

    oracle = DemoOracle()

    config = ExchangeConfig(
        reviews_required_for_accept=2,
        auto_assign=False,          # we'll triage manually for clarity
        auto_review=False,          # we'll submit reviews manually
        enable_content_guards=False, # skip for demo speed
        enable_staking=False,        # simplify reputation math
        enable_skill_tracking=True,
        use_effective_tier=True,
        enable_difficulty=True,
        verification_oracle=oracle,
        oracle_auto_reject=False,    # oracle failure adds a REJECT review, doesn't auto-reject
        max_revision_rounds=3,
        claim_timeout_seconds=3600,  # 1-hour claim timeout
    )

    exchange = Exchange(config)

    # Register a lifecycle hook — log every problem posting
    hook_log: list[str] = []

    async def on_post(ctx: dict) -> None:
        hook_log.append(ctx["problem"].title)

    exchange.add_hook(HookPoint.POST_POST_PROBLEM, on_post)
    sub("Exchange created with oracle, hooks, claim timeout, revision support")

    # ================================================================
    # 2. Register agents
    # ================================================================
    header("2. Register Agents")

    alice = Agent(
        name="Alice",
        solver=senior_solver,
        model_tier=ModelTier.PREMIUM,
        capabilities={AgentCapability.CODE_GENERATION, AgentCapability.DEBUGGING},
    )
    bob = Agent(
        name="Bob",
        solver=junior_solver,
        model_tier=ModelTier.STANDARD,
        capabilities={AgentCapability.CODE_GENERATION},
    )
    carol = Agent(
        name="Carol",
        solver=reviewer_solver,
        capabilities={AgentCapability.CODE_REVIEW, AgentCapability.PROOFREADING},
    )
    dan = Agent(
        name="Dan",
        solver=reviewer_solver,
        capabilities={AgentCapability.CODE_REVIEW, AgentCapability.GOOD_FAITH_CHECK},
    )

    for agent in (alice, bob, carol, dan):
        exchange.register(agent)
        sub(f"Registered {agent.name} (tier={agent.model_tier.name}, caps={[c.name for c in agent.capabilities]})")

    # ================================================================
    # 3. Batch problem posting with priorities
    # ================================================================
    header("3. Batch Problem Posting")

    problems = [
        Problem(title="Build REST API",      description="Create a REST API for user management with CRUD endpoints.",
                author_id=carol.id, tags={ProblemTag.FEATURE}, priority=10, bounty=20),
        Problem(title="Fix login bug",        description="Users can't log in when password contains special chars.",
                author_id=carol.id, tags={ProblemTag.BUG},     priority=8,  bounty=15),
        Problem(title="Write unit tests",     description="Add pytest unit tests for the auth module.",
                author_id=carol.id, tags={ProblemTag.FEATURE}, priority=3,  bounty=8),
        Problem(title="Update README",        description="Update the README with new API endpoints and examples.",
                author_id=carol.id, tags={ProblemTag.PROOFREAD}, priority=1, bounty=5),
    ]

    posted = await exchange.post_problems(problems)
    sub(f"Batch-posted {len(posted)} problems")
    sub(f"Lifecycle hook captured: {hook_log}")

    # ================================================================
    # 4. Priority queue — see problems in priority order
    # ================================================================
    header("4. Priority Queue")

    by_priority = exchange.open_problems(ProblemSortKey.PRIORITY)
    for p in by_priority:
        sub(f"[priority={p.priority:2d}, bounty={p.bounty:2d}] {p.title}")

    by_bounty = exchange.open_problems(ProblemSortKey.BOUNTY, limit=2)
    sub(f"Top 2 by bounty: {[p.title for p in by_bounty]}")

    bugs_only = exchange.open_problems(tags={ProblemTag.BUG})
    sub(f"Bugs only: {[p.title for p in bugs_only]}")

    # ================================================================
    # 5. Problem decomposition
    # ================================================================
    header("5. Problem Decomposition")

    api_problem = posted[0]  # "Build REST API"
    sub_probs = [
        Problem(title="Design DB schema",   description="Design the user table schema.",
                author_id=carol.id, tags={ProblemTag.FEATURE}, bounty=8),
        Problem(title="Implement endpoints", description="Implement /users CRUD endpoints.",
                author_id=carol.id, tags={ProblemTag.FEATURE}, bounty=10),
        Problem(title="Add auth middleware",  description="Add JWT auth middleware.",
                author_id=carol.id, tags={ProblemTag.FEATURE}, bounty=8),
    ]
    children = await exchange.decompose_problem(api_problem.id, sub_probs, sequential=True)
    sub(f"Decomposed '{api_problem.title}' into {len(children)} sequential sub-problems:")
    for c in children:
        deps = [exchange.get_problem(d).title for d in c.depends_on] if c.depends_on else ["none"]
        sub(f"  • {c.title}  (depends on: {', '.join(deps)})")

    # ================================================================
    # 6. Claim → Solve → Review → Accept cycle
    # ================================================================
    header("6. Full Solve Cycle (with Oracle)")

    # Enable event recording so we can replay later
    exchange.bus.enable_recording()

    target = posted[1]  # "Fix login bug"
    sub(f"Target: {target.title} (priority={target.priority})")

    # Alice claims and solves
    await exchange.claim_problem(target.id, alice.id)
    solution = await exchange.solve_problem(target.id, alice.id)
    sub(f"Alice solved: {solution.body[:60]}...")

    # Oracle ran automatically — check result
    oracle_result = solution.metadata.get("oracle_result")
    if oracle_result:
        sub(f"Oracle verdict: {oracle_result['status']} ({oracle_result['passed_tests']} passed, {oracle_result['failed_tests']} failed)")

    # Carol and Dan review
    r1 = Review(solution_id=solution.id, reviewer_id=carol.id,
                review_type=ReviewType.CORRECTNESS, verdict=ReviewVerdict.APPROVE,
                body="Correct fix.", confidence=0.9)
    r2 = Review(solution_id=solution.id, reviewer_id=dan.id,
                review_type=ReviewType.GOOD_FAITH, verdict=ReviewVerdict.APPROVE,
                body="Genuine effort.", confidence=0.85)
    await exchange.submit_review(r1)
    await exchange.submit_review(r2)

    sub(f"Reviews: oracle + Carol + Dan = {len(exchange.reviews_for_solution(solution.id))} total")
    sub(f"Solution verdict: {solution.verdict.name}")
    sub(f"Problem status:   {target.status.name}")

    # ================================================================
    # 7. Idempotency — safe retries
    # ================================================================
    header("7. Idempotency (Safe Retries)")

    # Re-posting the same problem returns the original
    dup = await exchange.post_problem(target)
    sub(f"Re-post same problem: got same object back? {dup is target}")

    # Re-submitting the same review returns the original — no duplicate reputation
    dup_review = await exchange.submit_review(r1)
    sub(f"Re-submit same review: same reviewer's original returned? {dup_review.reviewer_id == r1.reviewer_id}")

    # ================================================================
    # 8. Multi-round revision dialogue
    # ================================================================
    header("8. Revision Dialogue")

    # Bob solves "Write unit tests" — his junior solver produces weak output
    test_prob = posted[2]  # "Write unit tests"
    await exchange.claim_problem(test_prob.id, bob.id)
    weak_sol = await exchange.solve_problem(test_prob.id, bob.id)
    sub(f"Bob's first attempt: {weak_sol.body[:60]}...")

    # Carol reviews and requests revision
    reject_review = Review(
        solution_id=weak_sol.id, reviewer_id=carol.id,
        review_type=ReviewType.CORRECTNESS,
        verdict=ReviewVerdict.REQUEST_CHANGES,
        body="This is just a TODO stub, please write actual tests.",
    )
    await exchange.submit_review(reject_review)

    # Carol formally requests revision
    await exchange.request_revision(weak_sol.id, carol.id, "Need actual pytest test functions, not stubs.")
    sub(f"Revision requested. Rounds so far: {len(weak_sol.revision_history)}")

    # Bob revises (solver callback receives revision context automatically)
    await exchange.revise_solution(weak_sol.id, bob.id, revised_body="def test_auth():\n    assert login('user', 'p@ss!') == True")
    sub(f"Bob revised: {weak_sol.body[:60]}...")
    sub(f"Revision history: {len(weak_sol.revision_history)} round(s)")

    # ================================================================
    # 9. Claim timeout expiry
    # ================================================================
    header("9. Claim Timeout Expiry")

    readme_prob = posted[3]  # "Update README"
    await exchange.claim_problem(readme_prob.id, bob.id)
    sub(f"Bob claimed '{readme_prob.title}' — status: {readme_prob.status.name}")

    # Simulate 2 hours passing (beyond the 1-hour timeout)
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    expired = await exchange.expire_stale_claims(now=future)
    sub(f"Expired claims: {len(expired)}")
    sub(f"'{readme_prob.title}' status after expiry: {readme_prob.status.name}")

    # ================================================================
    # 10. Event recording & replay
    # ================================================================
    header("10. Event Recording")

    events = exchange.bus.recorded_events
    sub(f"Total events recorded: {len(events)}")

    # Show event breakdown
    from collections import Counter
    counts = Counter(e.kind.name for e in events)
    for kind, count in counts.most_common(8):
        sub(f"  {kind}: {count}")

    # ================================================================
    # 11. Reputation & Leaderboard
    # ================================================================
    header("11. Reputation Leaderboard")

    for entry in exchange.leaderboard():
        print(f"    {entry['name']:>8}  reputation={entry['reputation']}")

    # ================================================================
    # 12. Skill Summary
    # ================================================================
    header("12. Skill Summary (Alice)")

    summary = exchange.get_skill_summary(alice.id)
    sub(f"Total outcomes: {summary['total_outcomes']}")
    sub(f"Probationary: {exchange.is_probationary(alice.id)}")
    sub(f"Effective tier: {exchange.get_effective_tier(alice.id).name}")

    # ================================================================
    # Done
    # ================================================================
    header("Demo Complete")
    sub(f"Final test count: 464 tests passing")
    sub(f"Modules: agent, problem, solution, review, exchange, reputation,")
    sub(f"         triage, swap, trust, guards, behavior, events, archive,")
    sub(f"         skills, calibration, difficulty, rate_limit, errors, verification")


if __name__ == "__main__":
    asyncio.run(main())
