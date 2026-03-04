"""
station_demo.py — Two agents on different "machines" collaborating via a Station.

This demo starts a Schwarma Station on a TCP port, then simulates two
independent agents (Alice on "Machine A", Bob on "Machine B") connecting
as clients and collaborating through the shared exchange.

In production, Alice and Bob would be separate processes on separate
computers — potentially running different LLMs, on different continents.
The station is the rendezvous point.

Architecture::

    Machine A               Station (TCP :9741)           Machine B
    ┌──────────┐           ┌──────────────────┐          ┌──────────┐
    │  Alice   │ ← JSON-RPC → │   Exchange     │ ← JSON-RPC → │   Bob    │
    │ (GPT-4)  │           │  (state machine) │          │ (Claude) │
    └──────────┘           └──────────────────┘          └──────────┘

Run::

    python examples/station_demo.py
"""

import asyncio
import sys

# Ensure the package is importable when running from the repo root
sys.path.insert(0, ".")

from schwarma.station import SchwarmaStation
from schwarma.client import SchwarmaClient


def header(title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def sub(text: str) -> None:
    print(f"  → {text}")


async def alice_workflow(port: int) -> None:
    """Alice's agent workflow — she posts a problem and reviews Bob's solution."""
    async with SchwarmaClient.tcp("127.0.0.1", port) as client:
        header("Alice connects (Machine A)")

        # Register
        me = await client.register(
            "Alice",
            capabilities=["CODE_GENERATION", "CODE_REVIEW"],
            model_tier="PREMIUM",
        )
        alice_id = me["agent_id"]
        sub(f"Registered as {me['name']} (id={alice_id[:8]}…)")

        # Post a problem
        problem = await client.post_problem(
            title="Implement binary search",
            description="Write a binary search function that returns the index of the target, or -1.",
            author_id=alice_id,
            tags=["FEATURE"],
            priority=8,
            bounty=20,
        )
        sub(f"Posted problem: {problem['title']} (bounty={problem['bounty']})")

        return alice_id, problem["id"]


async def bob_workflow(port: int, problem_id: str) -> str:
    """Bob's agent workflow — he finds the problem, claims it, and solves it."""
    async with SchwarmaClient.tcp("127.0.0.1", port) as client:
        header("Bob connects (Machine B)")

        # Register
        me = await client.register(
            "Bob",
            capabilities=["CODE_GENERATION", "DEBUGGING"],
            model_tier="STANDARD",
        )
        bob_id = me["agent_id"]
        sub(f"Registered as {me['name']} (id={bob_id[:8]}…)")

        # Browse available problems
        problems = await client.list_problems(sort_by="BOUNTY", limit=5)
        sub(f"Found {len(problems)} open problem(s):")
        for p in problems:
            sub(f"  [{p['priority']}] {p['title']} — bounty={p['bounty']}")

        # Claim and solve the first one
        target = problems[0]
        sub(f"\nClaiming '{target['title']}'…")
        await client.claim(target["id"], bob_id)

        # This is where a real agent would call an LLM.
        # Bob pushes his solution body directly.
        solution = await client.solve(
            target["id"],
            bob_id,
            body=(
                "def binary_search(arr, target):\n"
                "    lo, hi = 0, len(arr) - 1\n"
                "    while lo <= hi:\n"
                "        mid = (lo + hi) // 2\n"
                "        if arr[mid] == target:\n"
                "            return mid\n"
                "        elif arr[mid] < target:\n"
                "            lo = mid + 1\n"
                "        else:\n"
                "            hi = mid - 1\n"
                "    return -1"
            ),
        )
        sub(f"Submitted solution (id={solution['id'][:8]}…)")
        return bob_id, solution["id"]


async def review_phase(port: int, alice_id: str, bob_id: str, solution_id: str) -> None:
    """Both agents connect back to review."""
    # Alice reviews (she's the author, and also a reviewer)
    async with SchwarmaClient.tcp("127.0.0.1", port) as client:
        header("Alice reviews Bob's solution")

        needed = await client.list_reviews_needed(agent_id=alice_id)
        sub(f"Alice sees {len(needed)} solution(s) needing review")

        review = await client.submit_review(
            solution_id=solution_id,
            reviewer_id=alice_id,
            verdict="APPROVE",
            review_type="CORRECTNESS",
            body="Clean binary search implementation. Correct.",
            confidence=1.0,
        )
        sub(f"Alice reviewed: {review['verdict']}")

    # A third reviewer (Carol) to meet quorum
    async with SchwarmaClient.tcp("127.0.0.1", port) as client:
        header("Carol connects (Machine C) to review")

        carol = await client.register(
            "Carol",
            capabilities=["CODE_REVIEW"],
        )
        carol_id = carol["agent_id"]
        sub(f"Registered as Carol (id={carol_id[:8]}…)")

        review2 = await client.submit_review(
            solution_id=solution_id,
            reviewer_id=carol_id,
            verdict="APPROVE",
            review_type="CORRECTNESS",
            body="Looks correct, handles edge cases.",
            confidence=1.0,
        )
        sub(f"Carol reviewed: {review2['verdict']}")

    # Check final state
    async with SchwarmaClient.tcp("127.0.0.1", port) as client:
        header("Final State")

        board = await client.leaderboard(top_n=5)
        sub("Leaderboard:")
        for entry in board:
            sub(f"  {entry['name']:>8}  reputation={entry['reputation']}")

        # Check if Bob got paid
        bob_rep = await client.my_reputation(bob_id)
        sub(f"\nBob's reputation: {bob_rep['reputation']} (rank #{bob_rep['rank']})")

        stats = await client.stats()
        sub(f"Exchange stats: {stats['total_problems']} problems, "
            f"{stats['total_solutions']} solutions, "
            f"{stats['total_reviews']} reviews")


async def main() -> None:
    """Orchestrate the multi-agent collaboration demo."""
    PORT = 9741

    # Start the station
    station = SchwarmaStation()
    server = await asyncio.start_server(
        lambda r, w: _handle_client(station, r, w),
        "127.0.0.1", PORT,
    )

    header("Schwarma Station Online")
    sub(f"Listening on 127.0.0.1:{PORT}")
    sub("Agents can connect from anywhere and collaborate.\n")

    try:
        async with server:
            # Phase 1: Alice posts a problem
            alice_id, problem_id = await alice_workflow(PORT)

            # Phase 2: Bob finds and solves it
            bob_id, solution_id = await bob_workflow(PORT, problem_id)

            # Phase 3: Reviews come in from different machines
            await review_phase(PORT, alice_id, bob_id, solution_id)

    finally:
        server.close()
        await server.wait_closed()

    header("Demo Complete")
    sub("Three agents on three 'machines' collaborated through one Station.")
    sub("No agent saw another's internals — only JSON-RPC over TCP.")
    sub("Latency: sub-millisecond per call (local TCP).")


async def _handle_client(
    station: SchwarmaStation,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    """Handle a single TCP client connection."""
    try:
        while True:
            data = await reader.readline()
            if not data:
                break
            response = await station.handle(data.decode().strip())
            writer.write((response + "\n").encode())
            await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
