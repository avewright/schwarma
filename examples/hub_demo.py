"""
hub_demo.py — Multi-agent demo running against the live Schwarma Hub.

Four MiniMax-backed agents connect to the hub over TCP, exchange problems,
review each other's solutions, and earn reputation.  Everything is visible
in the hub dashboard — no local web server needed.

Watch the action at:  http://localhost:8741

Usage
-----
    # Hub must be running first:
    docker compose up -d

    # Then run the demo (reads .env automatically):
    python examples/hub_demo.py

    # Override the hub address or model:
    python examples/hub_demo.py --host localhost --port 9741 --model MiniMax-M2.5
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import textwrap
from pathlib import Path
from typing import Any

# ── Ensure the package is importable from the repo root ──────────────────
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _load_dotenv() -> None:
    """Load .env from the repo root into os.environ (no external deps)."""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    with env_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())


# ── Console helpers ───────────────────────────────────────────────────────

def _phase(text: str) -> None:
    print(f"\n{'-'*60}\n  {text}\n{'-'*60}")


def _log(text: str) -> None:
    print(f"  >>  {text}")


# ── LLM solver factory ────────────────────────────────────────────────────

def make_solver(name: str, role: str, *, api_key: str, base_url: str | None, model: str):
    """Return an async solver that calls the LLM API via the Anthropic SDK."""
    try:
        import anthropic as _anthropic
    except ImportError:
        print("[ERROR] 'anthropic' package not found.  Install with: pip install anthropic")
        sys.exit(1)

    kwargs: dict[str, Any] = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    llm = _anthropic.Anthropic(**kwargs)

    system_prompt = textwrap.dedent(f"""
        You are {name}, an AI agent participating in the Schwarma peer-review exchange.
        Your role: {role}
        Be concise — limit responses to 3-6 sentences. No markdown unless the problem explicitly asks for it.
    """).strip()

    async def solver(description: str, ctx: dict) -> str:
        revision = ctx.get("revision_feedback", "")
        content = description
        if revision:
            content += f"\n\n[Revision feedback]: {revision}"

        loop = asyncio.get_event_loop()

        def _call() -> str:
            resp = llm.messages.create(
                model=model,
                max_tokens=600,
                system=system_prompt,
                messages=[{"role": "user", "content": content}],
            )
            return resp.content[0].text

        try:
            return await loop.run_in_executor(None, _call)
        except Exception as exc:
            return f"[LLM error: {exc}]"

    solver.__name__ = name
    return solver


# ── Demo ──────────────────────────────────────────────────────────────────

async def run_demo(
    *,
    host: str,
    port: int,
    api_key: str,
    base_url: str | None,
    model: str,
    http_port: int,
) -> None:
    from schwarma.client import SchwarmaClient

    agent_configs = [
        (
            "Alice",
            "You are a senior software architect. Solve problems clearly and correctly.",
            ["CODE_GENERATION", "DEBUGGING"],
        ),
        (
            "Bob",
            "You are a backend engineer specialising in Python. Write clean, working code.",
            ["CODE_GENERATION"],
        ),
        (
            "Carol",
            "You are a thorough code reviewer. Reply with exactly APPROVE or REJECT "
            "followed by a one-sentence reason.",
            ["CODE_REVIEW"],
        ),
        (
            "Dave",
            "You are a meticulous code reviewer. Reply with APPROVE or REJECT and a brief reason.",
            ["CODE_REVIEW"],
        ),
    ]

    # ── Connect and register all agents ──────────────────────────────
    _phase("Connecting agents to the hub")

    # One persistent TCP connection per agent
    clients: dict[str, SchwarmaClient] = {}
    agent_ids: dict[str, str] = {}
    solvers: dict[str, Any] = {}

    contexts: list[Any] = []
    for aname, role, caps in agent_configs:
        ctx = SchwarmaClient.tcp(host, port)
        client = await ctx.__aenter__()
        contexts.append((ctx, client))
        clients[aname] = client

        result = await client.register(aname, capabilities=caps, model_tier="STANDARD")
        agent_ids[aname] = result["agent_id"]
        solvers[aname] = make_solver(aname, role, api_key=api_key, base_url=base_url, model=model)
        _log(f"Registered: {aname}  (id={result['agent_id'][:8]}…)")

    alice_id = agent_ids["Alice"]
    bob_id   = agent_ids["Bob"]
    carol_id = agent_ids["Carol"]
    dave_id  = agent_ids["Dave"]

    alice_client = clients["Alice"]
    bob_client   = clients["Bob"]
    carol_client = clients["Carol"]
    dave_client  = clients["Dave"]

    # ── Post problems ─────────────────────────────────────────────────
    _phase("Alice posting problems to the hub")

    problem_defs = [
        {
            "title": "Write a retry decorator",
            "description": (
                "Write a Python decorator called `retry(max_attempts, delay_seconds)` "
                "that retries a failing function up to max_attempts times, "
                "waiting delay_seconds between attempts. Include a docstring."
            ),
            "tags": ["FEATURE"],
            "bounty": 20,
        },
        {
            "title": "Explain async generators",
            "description": (
                "Explain Python async generators in 3-4 sentences suitable for a "
                "developer who knows sync generators but is new to async/await. "
                "Include one short code example."
            ),
            "tags": ["QUESTION"],
            "bounty": 15,
        },
        {
            "title": "Find the bug in this binary search",
            "description": (
                "Here is a binary search implementation that sometimes returns -1 "
                "even when the target is present:\n\n"
                "def binary_search(arr, target):\n"
                "    lo, hi = 0, len(arr)\n"
                "    while lo < hi:\n"
                "        mid = (lo + hi) // 2\n"
                "        if arr[mid] == target: return mid\n"
                "        elif arr[mid] < target: lo = mid\n"
                "        else: hi = mid - 1\n"
                "    return -1\n\n"
                "Find the bug and provide the corrected version."
            ),
            "tags": ["BUG"],
            "bounty": 25,
        },
    ]

    posted: list[dict] = []
    for pdef in problem_defs:
        result = await alice_client.post_problem(
            pdef["title"],
            pdef["description"],
            alice_id,
            tags=pdef["tags"],
            bounty=pdef["bounty"],
        )
        # Merge original description into result for later use
        result["_description"] = pdef["description"]
        posted.append(result)
        _log(f"Posted: '{result['title']}'  (id={result['id'][:8]}…, bounty={pdef['bounty']})")
        await asyncio.sleep(0.3)

    # ── Solve problems ────────────────────────────────────────────────
    _phase("Bob and Alice claiming and solving via LLM")

    # Bob solves problems 0 and 2; Alice solves problem 1
    solver_pairs = [
        (posted[0], "Bob", bob_client, bob_id),
        (posted[1], "Alice", alice_client, alice_id),
        (posted[2], "Bob", bob_client, bob_id),
    ]

    solutions: list[dict] = []
    for pdata, sname, sclient, sid in solver_pairs:
        pid = pdata["id"]          # from Problem.to_dict()
        description = pdata["_description"]

        _log(f"{sname} solving '{pdata['title']}' via {model}…")
        solution_text = await solvers[sname](description, {})

        result = await sclient.claim_and_solve(pid, sid, solution_text)
        result["_problem_title"] = pdata["title"]
        result["_problem_description"] = description
        solutions.append(result)
        _log(f"  → solution {result['id'][:8]}… submitted ({result['verdict']})")
        await asyncio.sleep(0.3)

    # ── Reviews ───────────────────────────────────────────────────────
    _phase("Carol and Dave reviewing all solutions")

    for sol in solutions:
        sol_id = sol["id"]          # from Solution.to_dict()
        prob_desc   = sol["_problem_description"]
        ptitle      = sol["_problem_title"]
        sol_body    = sol.get("body", "")

        for rname, rclient, rid in [
            ("Carol", carol_client, carol_id),
            ("Dave",  dave_client,  dave_id),
        ]:
            _log(f"{rname} reviewing solution for '{ptitle}'…")

            review_prompt = (
                f"Review the solution to this problem.\n\n"
                f"PROBLEM: {prob_desc}\n\n"
                f"SOLUTION:\n{sol_body}\n\n"
                f"Reply with exactly APPROVE or REJECT followed by a one-sentence reason."
            )
            review_text = await solvers[rname](review_prompt, {})

            upper = review_text.upper()
            verdict = "APPROVE" if "APPROVE" in upper else "REJECT"

            await rclient.submit_review(
                sol_id,
                rid,
                verdict,
                review_type="CORRECTNESS",
                body=review_text[:500],
                confidence=0.9,
            )
            _log(f"  → {rname}: {verdict}")
            await asyncio.sleep(0.4)

    # ── Final leaderboard ─────────────────────────────────────────────
    _phase("Final standings")
    await asyncio.sleep(1.0)  # let hub settle

    board = await alice_client.leaderboard(top_n=10)
    print(f"\n{'='*60}")
    print("  REPUTATION LEADERBOARD (from hub)")
    print(f"{'='*60}")
    for rank, entry in enumerate(board, 1):
        aname = entry.get("name") or str(entry.get("agent_id", "?"))[:8]
        score = entry.get("reputation", entry.get("score", 0))
        print(f"  #{rank:<3} {aname:<16} {score:>5} pts")

    print(f"\n  Hub dashboard:  http://{host if host != '0.0.0.0' else 'localhost'}:{http_port}")
    print()

    # ── Close all TCP connections ─────────────────────────────────────
    for ctx, client in contexts:
        try:
            await ctx.__aexit__(None, None, None)
        except Exception:
            pass


# ── Entry point ───────────────────────────────────────────────────────────

def main() -> None:
    os.environ.setdefault("PYTHONUTF8", "1")
    _load_dotenv()

    parser = argparse.ArgumentParser(
        description="Schwarma hub demo — 4 LLM agents exchange problems on the live hub",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
            Requirements
            ------------
            Hub must be running:
              docker compose up -d

            Then run:
              python examples/hub_demo.py

            Or with explicit credentials:
              python examples/hub_demo.py \\
                --api-key YOUR_KEY \\
                --base-url https://api.minimaxi.chat/v1 \\
                --model MiniMax-M2.5
        """),
    )
    parser.add_argument("--host",      default="localhost",     help="Hub TCP host (default: localhost)")
    parser.add_argument("--port",      type=int, default=9741,  help="Hub TCP port (default: 9741)")
    parser.add_argument("--http-port", type=int, default=8741,  help="Hub HTTP port for dashboard link (default: 8741)")
    parser.add_argument("--api-key",   default=os.environ.get("MINIMAX_API_KEY"),
                        help="LLM API key (default: $MINIMAX_API_KEY)")
    parser.add_argument("--base-url",  default=os.environ.get("MINIMAX_BASE_URL"),
                        help="LLM base URL (default: $MINIMAX_BASE_URL)")
    parser.add_argument("--model",     default=os.environ.get("MINIMAX_MODEL", "MiniMax-M2.5"),
                        help="LLM model (default: $MINIMAX_MODEL or MiniMax-M2.5)")
    args = parser.parse_args()

    if not args.api_key:
        parser.error("--api-key is required (or set MINIMAX_API_KEY in .env)")

    print(f"\n  Schwarma Hub Demo")
    print(f"  Hub:   tcp://{args.host}:{args.port}")
    print(f"  Model: {args.model}")
    if args.base_url:
        print(f"  API:   {args.base_url}")
    print(f"\n  Watch live at:  http://localhost:{args.http_port}\n")

    asyncio.run(run_demo(
        host=args.host,
        port=args.port,
        api_key=args.api_key,
        base_url=args.base_url,
        model=args.model,
        http_port=args.http_port,
    ))


if __name__ == "__main__":
    main()
