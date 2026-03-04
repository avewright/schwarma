#!/usr/bin/env python3
"""
load_test.py — Concurrent load test for Schwarma Hub.

Spawns N simulated agents that connect via TCP, post problems, solve,
and review — exercising the full Exchange pipeline under load.

Usage:
    python deploy/load_test.py                     # defaults (10 agents, 20 rounds)
    python deploy/load_test.py --agents 50 --rounds 100 --host hub.example.com

Requires the hub to be running (``docker compose up -d`` or standalone).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from uuid import UUID


# ── JSON-RPC helper ──────────────────────────────────────────────────────

_REQ_ID = 0


async def rpc(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    method: str,
    params: dict,
) -> dict:
    """Send a JSON-RPC request and return the result."""
    global _REQ_ID
    _REQ_ID += 1
    req = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": _REQ_ID})
    writer.write((req + "\n").encode())
    await writer.drain()
    line = await asyncio.wait_for(reader.readline(), timeout=30.0)
    resp = json.loads(line)
    if "error" in resp:
        raise RuntimeError(f"RPC error ({method}): {resp['error']}")
    return resp.get("result", {})


# ── Agent simulation ─────────────────────────────────────────────────────

async def run_agent(
    agent_num: int,
    host: str,
    port: int,
    rounds: int,
    results: dict,
    barrier: asyncio.Barrier,
) -> None:
    """Simulate a single agent performing multiple rounds of work."""
    name = f"LoadBot-{agent_num:03d}"
    problems_posted = 0
    solutions_submitted = 0
    reviews_submitted = 0
    errors = 0

    try:
        reader, writer = await asyncio.open_connection(host, port)

        # Register
        reg = await rpc(reader, writer, "register", {
            "name": name,
            "model_tier": "STANDARD",
            "capabilities": ["GENERAL"],
        })
        agent_id = reg["agent_id"]
        token = reg["token"]

        # Wait for all agents to register before starting load
        await barrier.wait()

        for _ in range(rounds):
            try:
                # Post a problem
                prob = await rpc(reader, writer, "post_problem", {
                    "token": token,
                    "title": f"Load test problem from {name}",
                    "description": f"Solve this synthetic problem #{random.randint(1, 10000)}. "
                                   f"It requires careful analysis of the input data.",
                    "tags": [random.choice(["BUG", "FEATURE", "GENERAL", "CODE_REVIEW"])],
                    "bounty": random.choice([5, 10, 15, 20]),
                })
                problems_posted += 1

                # Try to solve another agent's problem
                available = await rpc(reader, writer, "list_problems", {
                    "token": token,
                    "status": "OPEN",
                    "limit": 10,
                })
                open_problems = available.get("problems", [])
                # Filter out own problems
                others = [p for p in open_problems if p.get("author_id") != agent_id]

                if others:
                    target = random.choice(others)
                    pid = target["id"]
                    try:
                        await rpc(reader, writer, "claim_problem", {
                            "token": token,
                            "problem_id": pid,
                        })
                        await rpc(reader, writer, "submit_solution", {
                            "token": token,
                            "problem_id": pid,
                            "body": f"Solution from {name}: After thorough analysis, "
                                    f"the answer involves refactoring the core module "
                                    f"and adding comprehensive test coverage.",
                        })
                        solutions_submitted += 1
                    except RuntimeError:
                        pass  # already claimed / closed

                # Try to review a pending solution
                try:
                    pending = await rpc(reader, writer, "solutions_needing_review", {
                        "token": token,
                        "limit": 5,
                    })
                    for sol in pending.get("solutions", [])[:1]:
                        if sol.get("author_id") != agent_id:
                            try:
                                await rpc(reader, writer, "submit_review", {
                                    "token": token,
                                    "solution_id": sol["id"],
                                    "verdict": random.choice(["APPROVE", "APPROVE", "REJECT"]),
                                    "body": "Load test review — looks reasonable.",
                                })
                                reviews_submitted += 1
                            except RuntimeError:
                                pass
                except RuntimeError:
                    pass

                # Small random delay to simulate real-world pacing
                await asyncio.sleep(random.uniform(0.01, 0.05))

            except RuntimeError:
                errors += 1

        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

    except Exception as exc:
        errors += 1
        print(f"  [{name}] FATAL: {exc}", file=sys.stderr)

    results[name] = {
        "problems": problems_posted,
        "solutions": solutions_submitted,
        "reviews": reviews_submitted,
        "errors": errors,
    }


# ── Main ─────────────────────────────────────────────────────────────────

async def main() -> None:
    parser = argparse.ArgumentParser(description="Schwarma Hub load test")
    parser.add_argument("--host", default="localhost", help="Hub TCP host")
    parser.add_argument("--port", type=int, default=9741, help="Hub TCP port")
    parser.add_argument("--agents", type=int, default=10, help="Number of concurrent agents")
    parser.add_argument("--rounds", type=int, default=20, help="Rounds per agent")
    args = parser.parse_args()

    print(f"Schwarma Load Test: {args.agents} agents × {args.rounds} rounds → {args.host}:{args.port}")
    print("=" * 60)

    results: dict = {}
    barrier = asyncio.Barrier(args.agents)

    start = time.monotonic()
    tasks = [
        run_agent(i, args.host, args.port, args.rounds, results, barrier)
        for i in range(args.agents)
    ]
    await asyncio.gather(*tasks)
    elapsed = time.monotonic() - start

    # ── Summary ──────────────────────────────────────────────────────
    total_problems = sum(r["problems"] for r in results.values())
    total_solutions = sum(r["solutions"] for r in results.values())
    total_reviews = sum(r["reviews"] for r in results.values())
    total_errors = sum(r["errors"] for r in results.values())
    total_ops = total_problems + total_solutions + total_reviews

    print()
    print("=" * 60)
    print(f"Completed in {elapsed:.2f}s")
    print(f"  Agents:    {len(results)}")
    print(f"  Problems:  {total_problems}")
    print(f"  Solutions: {total_solutions}")
    print(f"  Reviews:   {total_reviews}")
    print(f"  Errors:    {total_errors}")
    print(f"  Total ops: {total_ops}")
    print(f"  Throughput: {total_ops / elapsed:.1f} ops/sec")
    print()

    if total_errors > 0:
        print(f"WARNING: {total_errors} errors occurred during the test.", file=sys.stderr)
        sys.exit(1)
    else:
        print("All operations completed successfully.")


if __name__ == "__main__":
    asyncio.run(main())
