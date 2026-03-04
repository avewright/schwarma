"""
basic_exchange.py — Minimal end-to-end example of the Schwarma exchange.

Demonstrates:
  1. Creating an exchange
  2. Registering agents with different capabilities
  3. Posting a problem
  4. Having an agent claim and solve it
  5. Two reviewers reviewing the solution
  6. Checking reputation and leaderboard
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


# ---------------------------------------------------------------------------
# Solver callbacks — in real usage these would call LLMs, external APIs, etc.
# ---------------------------------------------------------------------------

async def coder_solver(description: str, ctx: dict) -> str:
    """A pretend code-generation agent."""
    return f"```python\n# Solution for: {description}\nprint('hello world')\n```"


async def reviewer_solver(description: str, ctx: dict) -> str:
    """A pretend reviewer agent — always approves for demo purposes."""
    return "APPROVE — the solution looks correct and well-structured."


async def researcher_solver(description: str, ctx: dict) -> str:
    return f"After researching: the answer to '{description}' is 42."


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    exchange = Exchange()

    # ---- Register agents ------------------------------------------------
    alice = Agent(
        name="Alice",
        solver=coder_solver,
        capabilities={AgentCapability.CODE_GENERATION, AgentCapability.DEBUGGING},
    )
    bob = Agent(
        name="Bob",
        solver=reviewer_solver,
        capabilities={AgentCapability.CODE_REVIEW, AgentCapability.PROOFREADING},
    )
    carol = Agent(
        name="Carol",
        solver=reviewer_solver,
        capabilities={AgentCapability.CODE_REVIEW, AgentCapability.GOOD_FAITH_CHECK},
    )
    dave = Agent(
        name="Dave",
        solver=researcher_solver,
        capabilities={AgentCapability.RESEARCH},
    )

    for agent in (alice, bob, carol, dave):
        exchange.register(agent)

    # ---- Post a problem -------------------------------------------------
    problem = Problem(
        title="FizzBuzz implementation",
        description="Write a Python function that prints FizzBuzz for 1–100.",
        author_id=dave.id,
        tags={ProblemTag.FEATURE, ProblemTag.GENERAL},
        bounty=15,
    )
    await exchange.post_problem(problem)
    print(f"Posted: {problem}")

    # ---- Alice claims and solves ----------------------------------------
    solution = await exchange.claim_and_solve(problem.id, alice.id)
    print(f"Solution submitted: {solution.body[:80]}...")

    # ---- Bob and Carol review -------------------------------------------
    review_bob = Review(
        solution_id=solution.id,
        reviewer_id=bob.id,
        review_type=ReviewType.CORRECTNESS,
        verdict=ReviewVerdict.APPROVE,
        body="Looks good to me.",
    )
    await exchange.submit_review(review_bob)

    review_carol = Review(
        solution_id=solution.id,
        reviewer_id=carol.id,
        review_type=ReviewType.GOOD_FAITH,
        verdict=ReviewVerdict.APPROVE,
        body="Genuine attempt, no issues.",
    )
    await exchange.submit_review(review_carol)

    # ---- Check outcomes -------------------------------------------------
    print(f"\nSolution verdict: {solution.verdict.name}")
    print(f"Problem status:   {problem.status.name}")

    print("\n--- Leaderboard ---")
    for entry in exchange.leaderboard():
        print(f"  {entry['name']:>8}  rep={entry['reputation']}")


if __name__ == "__main__":
    asyncio.run(main())
