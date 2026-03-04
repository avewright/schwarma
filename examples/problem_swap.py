"""
problem_swap.py — Demonstrates the problem-swapping mechanism.

Two agents each have a problem they want fresh eyes on.  They submit to
the swap pool, get matched, and solve each other's problems.
"""

import asyncio

from schwarma import (
    Agent,
    AgentCapability,
    Exchange,
    Problem,
    ProblemTag,
    Review,
    ReviewType,
    ReviewVerdict,
)


async def debug_solver(desc: str, ctx: dict) -> str:
    return f"Found the bug in: {desc[:60]}... — it was an off-by-one error."


async def docs_solver(desc: str, ctx: dict) -> str:
    return f"Here's the documentation for: {desc[:60]}..."


async def reviewer(desc: str, ctx: dict) -> str:
    return "APPROVE"


async def main() -> None:
    exchange = Exchange()

    # Two specialists
    alice = Agent(
        name="Alice-Debugger",
        solver=debug_solver,
        capabilities={AgentCapability.DEBUGGING, AgentCapability.CODE_REVIEW},
    )
    bob = Agent(
        name="Bob-Writer",
        solver=docs_solver,
        capabilities={AgentCapability.DOCUMENTATION, AgentCapability.PROOFREADING},
    )
    # A neutral reviewer
    eve = Agent(name="Eve-Reviewer", solver=reviewer, capabilities={AgentCapability.CODE_REVIEW})

    for a in (alice, bob, eve):
        exchange.register(a)

    # Alice has a docs problem; Bob has a debugging problem
    alice_problem = Problem(
        title="Write API docs for auth module",
        description="Need comprehensive API documentation for the auth module.",
        author_id=alice.id,
        tags={ProblemTag.REVIEW_REQUEST, ProblemTag.PROOFREAD},
        bounty=10,
    )
    bob_problem = Problem(
        title="Fix race condition in cache layer",
        description="There's an intermittent race condition in the LRU cache invalidation.",
        author_id=bob.id,
        tags={ProblemTag.BUG},
        bounty=12,
    )

    await exchange.post_problem(alice_problem)
    await exchange.post_problem(bob_problem)

    # Submit to swap pool
    await exchange.submit_swap(alice.id, alice_problem.id)
    await exchange.submit_swap(bob.id, bob_problem.id)

    print(f"Swap pool waiting: {exchange.swap_pool.waiting_count}")

    # Match
    matches = await exchange.run_swaps()
    print(f"Matches found: {len(matches)}")

    for match in matches:
        agents = match.agents
        problems = match.problems
        print(f"  {agents[0].name} ↔ {agents[1].name}")
        print(f"    problems: {problems[0].title} ↔ {problems[1].title}")

        # Each agent solves the OTHER's problem
        # Alice (debugger) solves Bob's bug
        sol_a = await exchange.claim_and_solve(problems[1].id, agents[0].id)
        # Bob (writer) solves Alice's docs problem
        sol_b = await exchange.claim_and_solve(problems[0].id, agents[1].id)

        print(f"    Alice's solution: {sol_a.body[:60]}...")
        print(f"    Bob's solution:   {sol_b.body[:60]}...")

        # Eve reviews both
        for sol in (sol_a, sol_b):
            review = Review(
                solution_id=sol.id,
                reviewer_id=eve.id,
                review_type=ReviewType.CORRECTNESS,
                verdict=ReviewVerdict.APPROVE,
                body="Approved.",
            )
            await exchange.submit_review(review)

        # Mark swap complete
        await exchange.complete_swap(match.id)

    # Final state
    print("\n--- Leaderboard ---")
    for entry in exchange.leaderboard():
        print(f"  {entry['name']:>16}  rep={entry['reputation']}")


if __name__ == "__main__":
    asyncio.run(main())
