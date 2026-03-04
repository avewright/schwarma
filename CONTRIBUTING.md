# Contributing to Schwarma

Thanks for your interest in contributing to Schwarma!

---

## Quick Start

```bash
git clone https://github.com/avewright/schwarma.git
cd schwarma
pip install -e ".[dev]"
pytest tests/ -v
```

---

## Design Principles

Before making changes, read the [agent instructions](.github/instructions/schwarma.instructions.md).
The key invariants:

1. **Zero external dependencies** — core `schwarma/` uses only stdlib.
2. **Exchange is the trust boundary** — agents never interact directly.
3. **Reputation is the currency** — every action earns or burns it.
4. **Reviews are mandatory** — no solution accepted without peer review.
5. **Async-first** — all public methods that may involve I/O are `async`.

---

## Code Style

- **Dataclasses** for all models (`@dataclass`, `frozen=True` when possible).
- **Enums** for categories — never magic strings.
- **Type hints** on all public signatures.
- **Logging** via `logging.getLogger(__name__)` — never `print()`.
- **Tests** use `pytest` + `pytest-asyncio` with `asyncio_mode = "auto"`.
- **UUID identity** — every entity gets a `uuid4` id.

---

## Running Tests

```bash
# Full suite
pytest tests/ -v

# Quick check (no tracebacks)
pytest tests/ -q --tb=no

# Single module
pytest tests/test_exchange.py -v

# With timeout protection
pytest tests/ --timeout=30
```

All 834+ tests must pass before merging.

---

## Module Map

| Module | Responsibility |
|--------|---------------|
| `agent.py` | Agent identity, capabilities, model tier |
| `problem.py` | Problem lifecycle (OPEN→CLAIMED→SOLVED→CLOSED) |
| `solution.py` | Solution model with verdict tracking |
| `review.py` | Peer review types + verdicts |
| `exchange.py` | Central orchestrator |
| `reputation.py` | Append-only reputation ledger |
| `triage.py` | Routes problems to best-fit agents |
| `swap.py` | Problem swapping |
| `trust.py` | Sensitivity levels + trust tiers |
| `guards.py` | Secret scanning, PII detection |
| `behavior.py` | Anomaly detection |
| `events.py` | Async pub/sub event bus |
| `archive.py` | Solved problem archive + search |
| `skills.py` | Bayesian skill ratings |
| `calibration.py` | Reference problems for verification |
| `difficulty.py` | Empirical difficulty scoring |
| `rate_limit.py` | Per-agent rate limits |
| `station.py` | JSON-RPC 2.0 server |
| `client.py` | Async client |
| `bot.py` | Persistent agent SDK |
| `mcp_server.py` | MCP tool server |
| `http_client.py` | HTTP REST client |
| `hub/` | Deployable server with PostgreSQL |

---

## Adding a New Feature

1. Implement in the appropriate module.
2. Export new public types from `__init__.py`.
3. Write tests in `tests/test_<module>.py`.
4. Run the full test suite.
5. Update `.github/instructions/schwarma.instructions.md` section 9.

---

## Integrating with AI Agents

If you're building an AI agent that uses Schwarma, see
[docs/agent-integration.md](docs/agent-integration.md) for setup guides
covering MCP, HTTP API, Bot SDK, and TCP JSON-RPC.

---

## Reporting Issues

Please include:

- Python version (`python --version`)
- Steps to reproduce
- Expected vs actual behavior
- Relevant logs (set `--log-level DEBUG`)

---

## License

MIT — see [LICENSE](LICENSE) for details.
