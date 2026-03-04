"""
triage_demo.py — Demonstrates the triage router with different strategies.
"""

import asyncio

from schwarma import Agent, AgentCapability, Exchange, Problem, ProblemTag
from schwarma.exchange import ExchangeConfig
from schwarma.triage import TriageConfig, TriageStrategy


async def generic_solver(desc: str, ctx: dict) -> str:
    return f"Solved: {desc[:60]}"


async def main() -> None:
    # Use COMPOSITE triage (default) with custom weights
    triage_cfg = TriageConfig(
        strategy=TriageStrategy.COMPOSITE,
        w_capability=0.5,
        w_reputation=0.3,
        w_load=0.15,
        w_random=0.05,
    )
    config = ExchangeConfig(triage_config=triage_cfg, auto_assign=False)
    exchange = Exchange(config)

    # Create a diverse pool of agents
    agents = [
        Agent(name="CodeBot",    solver=generic_solver, capabilities={AgentCapability.CODE_GENERATION}),
        Agent(name="DebugBot",   solver=generic_solver, capabilities={AgentCapability.DEBUGGING}),
        Agent(name="SecBot",     solver=generic_solver, capabilities={AgentCapability.SECURITY_AUDIT}),
        Agent(name="DocBot",     solver=generic_solver, capabilities={AgentCapability.DOCUMENTATION, AgentCapability.PROOFREADING}),
        Agent(name="MathBot",    solver=generic_solver, capabilities={AgentCapability.MATH, AgentCapability.DATA_ANALYSIS}),
        Agent(name="ArchBot",    solver=generic_solver, capabilities={AgentCapability.ARCHITECTURE}),
        Agent(name="Generalist", solver=generic_solver, capabilities={AgentCapability.GENERAL}),
    ]
    for a in agents:
        exchange.register(a)

    # Post problems with different tags
    problems = [
        Problem(title="Fix null pointer",  author_id=agents[0].id, description="...", tags={ProblemTag.BUG}),
        Problem(title="Design microservice", author_id=agents[1].id, description="...", tags={ProblemTag.ARCHITECTURE}),
        Problem(title="Audit login flow",  author_id=agents[2].id, description="...", tags={ProblemTag.SECURITY}),
        Problem(title="Proofread README",  author_id=agents[3].id, description="...", tags={ProblemTag.PROOFREAD}),
    ]

    for p in problems:
        await exchange.post_problem(p)

    # Manually triage each problem
    print("--- Triage Results (COMPOSITE) ---")
    for p in problems:
        ranked = exchange.router.rank(p, exchange.agents, top_n=3)
        names = [a.name for a in ranked]
        print(f"  {p.title:25s} → {names}")

    # Compare with CAPABILITY_MATCH
    exchange.router.config.strategy = TriageStrategy.CAPABILITY_MATCH
    print("\n--- Triage Results (CAPABILITY_MATCH) ---")
    for p in problems:
        ranked = exchange.router.rank(p, exchange.agents, top_n=3)
        names = [a.name for a in ranked]
        print(f"  {p.title:25s} → {names}")


if __name__ == "__main__":
    asyncio.run(main())
