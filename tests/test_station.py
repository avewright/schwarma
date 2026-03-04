"""Tests for SchwarmaStation JSON-RPC dispatch."""

import asyncio
import json
import pytest
from schwarma.station import SchwarmaStation, JSONRPC_VERSION, AUTH_REQUIRED
from schwarma.exchange import ExchangeConfig


def _req(method: str, params: dict | None = None, id: int = 1) -> str:
    return json.dumps({
        "jsonrpc": JSONRPC_VERSION,
        "id": id,
        "method": method,
        "params": params or {},
    })


def _parse(raw: str) -> dict:
    return json.loads(raw)


@pytest.fixture
def station() -> SchwarmaStation:
    config = ExchangeConfig(
        reviews_required_for_accept=2,
        auto_assign=False,
        auto_review=False,
        enable_content_guards=False,
        enable_staking=False,
        enable_skill_tracking=True,
    )
    return SchwarmaStation(config=config, require_auth=False)


# ── Protocol correctness ────────────────────────────────────────────────


class TestProtocol:
    @pytest.mark.asyncio
    async def test_parse_error(self, station):
        resp = _parse(await station.handle("not json"))
        assert resp["error"]["code"] == -32700

    @pytest.mark.asyncio
    async def test_invalid_request_no_version(self, station):
        resp = _parse(await station.handle('{"id":1,"method":"ping"}'))
        assert resp["error"]["code"] == -32600

    @pytest.mark.asyncio
    async def test_method_not_found(self, station):
        resp = _parse(await station.handle(_req("nonexistent")))
        assert resp["error"]["code"] == -32601

    @pytest.mark.asyncio
    async def test_invalid_params_type(self, station):
        raw = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": [1, 2]})
        resp = _parse(await station.handle(raw))
        assert resp["error"]["code"] == -32602

    @pytest.mark.asyncio
    async def test_ping(self, station):
        resp = _parse(await station.handle(_req("ping")))
        assert resp["result"]["pong"] is True

    @pytest.mark.asyncio
    async def test_response_has_id(self, station):
        resp = _parse(await station.handle(_req("ping", id=42)))
        assert resp["id"] == 42


# ── Agent registration ──────────────────────────────────────────────────


class TestRegister:
    @pytest.mark.asyncio
    async def test_register_agent(self, station):
        resp = _parse(await station.handle(_req("register", {
            "name": "Alice",
            "capabilities": ["CODE_GENERATION", "DEBUGGING"],
            "model_tier": "PREMIUM",
        })))
        result = resp["result"]
        assert result["name"] == "Alice"
        assert "agent_id" in result
        assert "CODE_GENERATION" in result["capabilities"]
        assert result["model_tier"] == "PREMIUM"

    @pytest.mark.asyncio
    async def test_register_defaults(self, station):
        resp = _parse(await station.handle(_req("register", {"name": "Bob"})))
        result = resp["result"]
        assert result["model_tier"] == "STANDARD"
        assert "GENERAL" in result["capabilities"]

    @pytest.mark.asyncio
    async def test_register_missing_name(self, station):
        resp = _parse(await station.handle(_req("register", {})))
        assert "error" in resp

    @pytest.mark.asyncio
    async def test_register_bad_capability(self, station):
        resp = _parse(await station.handle(_req("register", {
            "name": "X", "capabilities": ["TELEKINESIS"],
        })))
        assert "error" in resp

    @pytest.mark.asyncio
    async def test_list_agents(self, station):
        await station.handle(_req("register", {"name": "Alice"}))
        await station.handle(_req("register", {"name": "Bob"}))
        resp = _parse(await station.handle(_req("list_agents")))
        assert len(resp["result"]) == 2


# ── Full workflow ────────────────────────────────────────────────────────


class TestFullWorkflow:
    """End-to-end: register → post → claim → solve → review → accept."""

    @pytest.mark.asyncio
    async def test_complete_cycle(self, station):
        # Register agents
        alice = _parse(await station.handle(_req("register", {
            "name": "Alice", "capabilities": ["CODE_GENERATION"],
        })))["result"]
        bob = _parse(await station.handle(_req("register", {
            "name": "Bob", "capabilities": ["CODE_REVIEW"],
        })))["result"]
        carol = _parse(await station.handle(_req("register", {
            "name": "Carol", "capabilities": ["CODE_REVIEW"],
        })))["result"]

        # Post problem
        problem = _parse(await station.handle(_req("post_problem", {
            "title": "FizzBuzz",
            "description": "Write FizzBuzz in Python",
            "author_id": bob["agent_id"],
            "tags": ["FEATURE"],
            "bounty": 15,
        })))["result"]
        assert problem["title"] == "FizzBuzz"
        problem_id = problem["id"]

        # List problems
        problems = _parse(await station.handle(_req("list_problems")))["result"]
        assert len(problems) == 1

        # Claim
        claim_resp = _parse(await station.handle(_req("claim", {
            "problem_id": problem_id,
            "agent_id": alice["agent_id"],
        })))["result"]
        assert claim_resp["claimed"] is True

        # Solve (push body)
        solution = _parse(await station.handle(_req("solve", {
            "problem_id": problem_id,
            "agent_id": alice["agent_id"],
            "body": "def fizzbuzz():\n    for i in range(1,101):\n        print(i)",
        })))["result"]
        assert "id" in solution
        solution_id = solution["id"]

        # Check reviews needed
        needed = _parse(await station.handle(_req("list_reviews_needed", {
            "agent_id": bob["agent_id"],
        })))["result"]
        assert len(needed) == 1
        assert needed[0]["problem"]["title"] == "FizzBuzz"

        # Bob reviews
        r1 = _parse(await station.handle(_req("submit_review", {
            "solution_id": solution_id,
            "reviewer_id": bob["agent_id"],
            "verdict": "APPROVE",
            "confidence": 1.0,
        })))["result"]
        assert r1["verdict"] == "APPROVE"

        # Carol reviews
        r2 = _parse(await station.handle(_req("submit_review", {
            "solution_id": solution_id,
            "reviewer_id": carol["agent_id"],
            "verdict": "APPROVE",
            "confidence": 1.0,
        })))["result"]

        # Check solution is now accepted
        prob = _parse(await station.handle(_req("get_problem", {
            "problem_id": problem_id,
        })))["result"]
        assert prob["status"] == "CLOSED"

        # Leaderboard
        board = _parse(await station.handle(_req("leaderboard")))["result"]
        assert len(board) >= 3

        # My reputation
        rep = _parse(await station.handle(_req("my_reputation", {
            "agent_id": alice["agent_id"],
        })))["result"]
        assert rep["reputation"] > 50  # base + bounty

    @pytest.mark.asyncio
    async def test_claim_and_solve(self, station):
        """claim_and_solve combines both steps."""
        alice = _parse(await station.handle(_req("register", {
            "name": "Alice", "capabilities": ["CODE_GENERATION"],
        })))["result"]
        bob = _parse(await station.handle(_req("register", {"name": "Bob"})))["result"]

        problem = _parse(await station.handle(_req("post_problem", {
            "title": "T", "description": "D", "author_id": bob["agent_id"],
        })))["result"]

        sol = _parse(await station.handle(_req("claim_and_solve", {
            "problem_id": problem["id"],
            "agent_id": alice["agent_id"],
            "body": "print('hello')",
        })))["result"]
        assert "id" in sol

    @pytest.mark.asyncio
    async def test_get_reviews(self, station):
        alice = _parse(await station.handle(_req("register", {
            "name": "Alice", "capabilities": ["CODE_GENERATION"],
        })))["result"]
        bob = _parse(await station.handle(_req("register", {
            "name": "Bob", "capabilities": ["CODE_REVIEW"],
        })))["result"]

        p = _parse(await station.handle(_req("post_problem", {
            "title": "T", "description": "D", "author_id": bob["agent_id"],
        })))["result"]

        sol = _parse(await station.handle(_req("claim_and_solve", {
            "problem_id": p["id"],
            "agent_id": alice["agent_id"],
            "body": "x = 42",
        })))["result"]

        await station.handle(_req("submit_review", {
            "solution_id": sol["id"],
            "reviewer_id": bob["agent_id"],
            "verdict": "APPROVE",
        }))

        reviews = _parse(await station.handle(_req("get_reviews", {
            "solution_id": sol["id"],
        })))["result"]
        assert len(reviews) == 1


# ── Edge cases ──────────────────────────────────────────────────────────


class TestEdgeCases:
    @pytest.mark.asyncio
    async def test_solve_without_body_errors(self, station):
        alice = _parse(await station.handle(_req("register", {"name": "Alice"})))["result"]
        bob = _parse(await station.handle(_req("register", {"name": "Bob"})))["result"]

        p = _parse(await station.handle(_req("post_problem", {
            "title": "T", "description": "D", "author_id": bob["agent_id"],
        })))["result"]

        await station.handle(_req("claim", {
            "problem_id": p["id"], "agent_id": alice["agent_id"],
        }))

        resp = _parse(await station.handle(_req("solve", {
            "problem_id": p["id"], "agent_id": alice["agent_id"],
        })))
        assert "error" in resp  # body is required

    @pytest.mark.asyncio
    async def test_bad_uuid(self, station):
        resp = _parse(await station.handle(_req("get_problem", {
            "problem_id": "not-a-uuid",
        })))
        assert "error" in resp

    @pytest.mark.asyncio
    async def test_stats(self, station):
        resp = _parse(await station.handle(_req("stats")))
        assert "total_problems" in resp["result"]

    @pytest.mark.asyncio
    async def test_skill_summary(self, station):
        alice = _parse(await station.handle(_req("register", {"name": "Alice"})))["result"]
        resp = _parse(await station.handle(_req("skill_summary", {
            "agent_id": alice["agent_id"],
        })))
        assert "total_outcomes" in resp["result"]


# ── Token authentication ────────────────────────────────────────────────


@pytest.fixture
def auth_station() -> SchwarmaStation:
    """Station with require_auth=True for token tests."""
    config = ExchangeConfig(
        reviews_required_for_accept=2,
        auto_assign=False,
        auto_review=False,
        enable_content_guards=False,
        enable_staking=False,
        enable_skill_tracking=True,
    )
    return SchwarmaStation(config=config, require_auth=True)


class TestAuth:
    """Token-based authentication tests."""

    @pytest.mark.asyncio
    async def test_register_returns_token(self, auth_station):
        resp = _parse(await auth_station.handle(_req("register", {"name": "Alice"})))
        result = resp["result"]
        assert "token" in result
        assert isinstance(result["token"], str)
        assert len(result["token"]) > 20  # url-safe base64, 32 bytes

    @pytest.mark.asyncio
    async def test_token_auth_post_problem(self, auth_station):
        """Token alone is sufficient — no agent_id needed."""
        alice = _parse(await auth_station.handle(_req("register", {"name": "Alice"})))["result"]
        resp = _parse(await auth_station.handle(_req("post_problem", {
            "title": "T",
            "description": "D",
            "token": alice["token"],
        })))
        assert "result" in resp
        assert resp["result"]["title"] == "T"

    @pytest.mark.asyncio
    async def test_missing_token_errors(self, auth_station):
        """Without token, authenticated methods fail."""
        alice = _parse(await auth_station.handle(_req("register", {"name": "Alice"})))["result"]
        resp = _parse(await auth_station.handle(_req("post_problem", {
            "title": "T",
            "description": "D",
            "author_id": alice["agent_id"],
        })))
        assert resp["error"]["code"] == AUTH_REQUIRED

    @pytest.mark.asyncio
    async def test_invalid_token_errors(self, auth_station):
        resp = _parse(await auth_station.handle(_req("register", {"name": "Alice"})))["result"]
        resp = _parse(await auth_station.handle(_req("post_problem", {
            "title": "T",
            "description": "D",
            "token": "bogus-token-abc",
        })))
        assert resp["error"]["code"] == AUTH_REQUIRED

    @pytest.mark.asyncio
    async def test_token_agent_id_mismatch(self, auth_station):
        """Token must match the agent_id if both are provided."""
        alice = _parse(await auth_station.handle(_req("register", {"name": "Alice"})))["result"]
        bob = _parse(await auth_station.handle(_req("register", {"name": "Bob"})))["result"]
        # Alice's token with Bob's agent_id
        resp = _parse(await auth_station.handle(_req("post_problem", {
            "title": "T",
            "description": "D",
            "token": alice["token"],
            "author_id": bob["agent_id"],
        })))
        assert resp["error"]["code"] == AUTH_REQUIRED

    @pytest.mark.asyncio
    async def test_full_workflow_with_tokens(self, auth_station):
        """Complete cycle using only tokens for identity."""
        # Register
        alice = _parse(await auth_station.handle(_req("register", {
            "name": "Alice", "capabilities": ["CODE_GENERATION"],
        })))["result"]
        bob = _parse(await auth_station.handle(_req("register", {
            "name": "Bob", "capabilities": ["CODE_REVIEW"],
        })))["result"]
        carol = _parse(await auth_station.handle(_req("register", {
            "name": "Carol", "capabilities": ["CODE_REVIEW"],
        })))["result"]

        # Post problem (token only, no author_id)
        problem = _parse(await auth_station.handle(_req("post_problem", {
            "title": "FizzBuzz",
            "description": "Write FizzBuzz",
            "token": bob["token"],
            "tags": ["FEATURE"],
        })))["result"]
        pid = problem["id"]

        # Claim and solve (token only)
        sol = _parse(await auth_station.handle(_req("claim_and_solve", {
            "problem_id": pid,
            "token": alice["token"],
            "body": "def fizzbuzz(): pass",
        })))["result"]
        sid = sol["id"]

        # Review with tokens
        r1 = _parse(await auth_station.handle(_req("submit_review", {
            "solution_id": sid,
            "token": bob["token"],
            "verdict": "APPROVE",
            "confidence": 1.0,
        })))["result"]
        assert r1["verdict"] == "APPROVE"

        r2 = _parse(await auth_station.handle(_req("submit_review", {
            "solution_id": sid,
            "token": carol["token"],
            "verdict": "APPROVE",
            "confidence": 1.0,
        })))["result"]

        # Check problem closed
        prob = _parse(await auth_station.handle(_req("get_problem", {
            "problem_id": pid,
        })))["result"]
        assert prob["status"] == "CLOSED"

        # My reputation with token
        rep = _parse(await auth_station.handle(_req("my_reputation", {
            "token": alice["token"],
        })))["result"]
        assert rep["reputation"] > 50

    @pytest.mark.asyncio
    async def test_public_methods_no_token_needed(self, auth_station):
        """ping, stats, list_agents, get_problem, list_problems are public."""
        # ping
        resp = _parse(await auth_station.handle(_req("ping")))
        assert resp["result"]["pong"] is True
        # stats
        resp = _parse(await auth_station.handle(_req("stats")))
        assert "total_problems" in resp["result"]
        # list_agents (no agents yet, but no error)
        resp = _parse(await auth_station.handle(_req("list_agents")))
        assert resp["result"] == []

    @pytest.mark.asyncio
    async def test_noauth_station_still_works_with_agent_id(self):
        """require_auth=False allows bare agent_id without token."""
        config = ExchangeConfig(
            auto_assign=False, auto_review=False,
            enable_content_guards=False, enable_staking=False,
        )
        station = SchwarmaStation(config=config, require_auth=False)
        alice = _parse(await station.handle(_req("register", {"name": "Alice"})))["result"]
        resp = _parse(await station.handle(_req("post_problem", {
            "title": "T",
            "description": "D",
            "author_id": alice["agent_id"],
        })))
        assert "result" in resp


# ── Agent admin ──────────────────────────────────────────────────────────


class TestAgentAdmin:
    @pytest.fixture
    def admin_station(self) -> SchwarmaStation:
        config = ExchangeConfig(
            auto_assign=False, auto_review=False,
            enable_content_guards=False, enable_staking=False,
            require_approval=True,
        )
        return SchwarmaStation(config=config, require_auth=False)

    @pytest.mark.asyncio
    async def test_get_agent(self, station):
        alice = _parse(await station.handle(_req("register", {"name": "Alice"})))["result"]
        resp = _parse(await station.handle(_req("get_agent", {
            "agent_id": alice["agent_id"],
        })))
        assert resp["result"]["name"] == "Alice"

    @pytest.mark.asyncio
    async def test_pending_approve_reject(self, admin_station):
        """Agents go to pending, can be approved or rejected."""
        a = _parse(await admin_station.handle(_req("register", {"name": "A"})))["result"]
        b = _parse(await admin_station.handle(_req("register", {"name": "B"})))["result"]

        pending = _parse(await admin_station.handle(_req("pending_agents")))["result"]
        assert len(pending) == 2

        # Approve A
        resp = _parse(await admin_station.handle(_req("approve_agent", {
            "agent_id": a["agent_id"],
        })))
        assert resp["result"]["approved"] is True

        # Reject B
        resp = _parse(await admin_station.handle(_req("reject_agent", {
            "agent_id": b["agent_id"],
        })))
        assert resp["result"]["rejected"] is True

        # Pending should be empty now
        pending = _parse(await admin_station.handle(_req("pending_agents")))["result"]
        assert len(pending) == 0

    @pytest.mark.asyncio
    async def test_suspend_unsuspend(self, station):
        alice = _parse(await station.handle(_req("register", {"name": "Alice"})))["result"]
        aid = alice["agent_id"]

        # Suspend
        resp = _parse(await station.handle(_req("suspend_agent", {
            "agent_id": aid, "reason": "testing",
        })))
        assert resp["result"]["suspended"] is True

        # Check
        resp = _parse(await station.handle(_req("is_suspended", {"agent_id": aid})))
        assert resp["result"]["suspended"] is True

        # Unsuspend
        resp = _parse(await station.handle(_req("unsuspend_agent", {"agent_id": aid})))
        assert resp["result"]["suspended"] is False

        # Check again
        resp = _parse(await station.handle(_req("is_suspended", {"agent_id": aid})))
        assert resp["result"]["suspended"] is False


# ── Batch / Decompose ────────────────────────────────────────────────────


class TestBatchDecompose:
    @pytest.mark.asyncio
    async def test_post_problems_batch(self, station):
        alice = _parse(await station.handle(_req("register", {"name": "Alice"})))["result"]
        resp = _parse(await station.handle(_req("post_problems", {
            "author_id": alice["agent_id"],
            "problems": [
                {"title": "P1", "description": "D1", "tags": ["FEATURE"]},
                {"title": "P2", "description": "D2"},
            ],
        })))
        assert len(resp["result"]) == 2
        assert resp["result"][0]["title"] == "P1"
        assert resp["result"][1]["title"] == "P2"

    @pytest.mark.asyncio
    async def test_decompose_problem(self, station):
        alice = _parse(await station.handle(_req("register", {"name": "Alice"})))["result"]
        parent = _parse(await station.handle(_req("post_problem", {
            "title": "Big task",
            "description": "Multi-step",
            "author_id": alice["agent_id"],
        })))["result"]
        pid = parent["id"]

        subs = _parse(await station.handle(_req("decompose_problem", {
            "parent_id": pid,
            "author_id": alice["agent_id"],
            "sub_problems": [
                {"title": "Step 1", "description": "Do part 1"},
                {"title": "Step 2", "description": "Do part 2"},
            ],
            "sequential": True,
        })))["result"]
        assert len(subs) == 2

        # sub_problems query
        listed = _parse(await station.handle(_req("sub_problems", {
            "parent_id": pid,
        })))["result"]
        assert len(listed) == 2

    @pytest.mark.asyncio
    async def test_dependencies_met(self, station):
        alice = _parse(await station.handle(_req("register", {"name": "Alice"})))["result"]
        parent = _parse(await station.handle(_req("post_problem", {
            "title": "Parent",
            "description": "Parent",
            "author_id": alice["agent_id"],
        })))["result"]
        subs = _parse(await station.handle(_req("decompose_problem", {
            "parent_id": parent["id"],
            "author_id": alice["agent_id"],
            "sub_problems": [
                {"title": "A", "description": "A"},
                {"title": "B", "description": "B"},
            ],
            "sequential": True,
        })))["result"]

        # First sub should have deps met (no dependencies)
        r = _parse(await station.handle(_req("dependencies_met", {
            "problem_id": subs[0]["id"],
        })))["result"]
        assert r["met"] is True

        # Second sub depends on first (sequential) — not met yet
        r = _parse(await station.handle(_req("dependencies_met", {
            "problem_id": subs[1]["id"],
        })))["result"]
        assert r["met"] is False


# ── Solutions / Revision ─────────────────────────────────────────────────


async def _setup_solved(station) -> dict:
    """Helper: register 3 agents, post problem, claim & solve. Returns ids."""
    alice = _parse(await station.handle(_req("register", {"name": "Alice"})))["result"]
    bob = _parse(await station.handle(_req("register", {"name": "Bob"})))["result"]
    carol = _parse(await station.handle(_req("register", {"name": "Carol"})))["result"]

    problem = _parse(await station.handle(_req("post_problem", {
        "title": "Test",
        "description": "Test problem",
        "author_id": alice["agent_id"],
    })))["result"]

    sol = _parse(await station.handle(_req("claim_and_solve", {
        "problem_id": problem["id"],
        "agent_id": bob["agent_id"],
        "body": "solution code",
    })))["result"]

    return {
        "alice": alice["agent_id"],
        "bob": bob["agent_id"],
        "carol": carol["agent_id"],
        "problem_id": problem["id"],
        "solution_id": sol["id"],
    }


class TestSolutions:
    @pytest.mark.asyncio
    async def test_get_solution(self, station):
        ids = await _setup_solved(station)
        resp = _parse(await station.handle(_req("get_solution", {
            "solution_id": ids["solution_id"],
        })))
        assert resp["result"]["id"] == ids["solution_id"]
        assert resp["result"]["body"] == "solution code"

    @pytest.mark.asyncio
    async def test_solutions_for_problem(self, station):
        ids = await _setup_solved(station)
        resp = _parse(await station.handle(_req("solutions_for_problem", {
            "problem_id": ids["problem_id"],
        })))
        assert len(resp["result"]) == 1
        assert resp["result"][0]["id"] == ids["solution_id"]


class TestRevision:
    @pytest.mark.asyncio
    async def test_request_and_revise(self, station):
        ids = await _setup_solved(station)

        # Request revision
        resp = _parse(await station.handle(_req("request_revision", {
            "solution_id": ids["solution_id"],
            "reviewer_id": ids["carol"],
            "reason": "Needs improvement",
        })))
        assert resp["result"]["revision_requested"] is True

        # Revise
        resp = _parse(await station.handle(_req("revise_solution", {
            "solution_id": ids["solution_id"],
            "agent_id": ids["bob"],
            "body": "improved code",
        })))
        result = resp["result"]
        assert result["body"] == "improved code"

    @pytest.mark.asyncio
    async def test_request_revision_missing_reason(self, station):
        ids = await _setup_solved(station)
        resp = _parse(await station.handle(_req("request_revision", {
            "solution_id": ids["solution_id"],
            "reviewer_id": ids["carol"],
        })))
        assert "error" in resp


# ── Swap ─────────────────────────────────────────────────────────────────


class TestSwap:
    @pytest.mark.asyncio
    async def test_submit_swap(self, station):
        """Submit a problem to swap pool."""
        alice = _parse(await station.handle(_req("register", {
            "name": "Alice", "capabilities": ["CODE_GENERATION"],
        })))["result"]
        p = _parse(await station.handle(_req("post_problem", {
            "title": "Stuck",
            "description": "Can't figure this out",
            "author_id": alice["agent_id"],
        })))["result"]

        # Claim first
        _parse(await station.handle(_req("claim", {
            "problem_id": p["id"],
            "agent_id": alice["agent_id"],
        })))

        resp = _parse(await station.handle(_req("submit_swap", {
            "problem_id": p["id"],
            "agent_id": alice["agent_id"],
        })))
        assert resp["result"]["submitted"] is True

    @pytest.mark.asyncio
    async def test_run_swaps_empty(self, station):
        """run_swaps with no submissions returns empty list."""
        resp = _parse(await station.handle(_req("run_swaps")))
        assert resp["result"] == []


# ── Challenge ────────────────────────────────────────────────────────────


class TestChallenge:
    @pytest.fixture
    def challenge_station(self) -> SchwarmaStation:
        config = ExchangeConfig(
            reviews_required_for_accept=2,
            auto_assign=False,
            auto_review=False,
            enable_content_guards=False,
            enable_staking=True,
        )
        return SchwarmaStation(config=config, require_auth=False)

    @pytest.mark.asyncio
    async def test_challenge_accepted_solution(self, challenge_station):
        """Challenge a solution that was accepted via reviews."""
        s = challenge_station

        # Register agents
        alice = _parse(await s.handle(_req("register", {"name": "Alice"})))["result"]
        bob = _parse(await s.handle(_req("register", {"name": "Bob"})))["result"]
        carol = _parse(await s.handle(_req("register", {"name": "Carol"})))["result"]
        dave = _parse(await s.handle(_req("register", {"name": "Dave"})))["result"]

        # Post + solve
        p = _parse(await s.handle(_req("post_problem", {
            "title": "Q", "description": "D", "author_id": alice["agent_id"],
        })))["result"]
        sol = _parse(await s.handle(_req("claim_and_solve", {
            "problem_id": p["id"], "agent_id": bob["agent_id"], "body": "answer",
        })))["result"]

        # Two approvals to close
        _parse(await s.handle(_req("submit_review", {
            "solution_id": sol["id"], "reviewer_id": carol["agent_id"],
            "verdict": "APPROVE", "confidence": 1.0,
        })))
        _parse(await s.handle(_req("submit_review", {
            "solution_id": sol["id"], "reviewer_id": alice["agent_id"],
            "verdict": "APPROVE", "confidence": 1.0,
        })))

        # Challenge
        resp = _parse(await s.handle(_req("challenge_solution", {
            "solution_id": sol["id"],
            "challenger_id": dave["agent_id"],
            "reason": "This is wrong",
        })))
        assert "result" in resp
        result = resp["result"]
        assert result["status"] == "SOLVED"  # re-opened for re-review


# ── Archive ──────────────────────────────────────────────────────────────


class TestArchive:
    @pytest.mark.asyncio
    async def test_search_archive_empty(self, station):
        resp = _parse(await station.handle(_req("search_archive")))
        assert resp["result"] == []

    @pytest.mark.asyncio
    async def test_search_archive_after_close(self, station):
        """Archive should contain an entry after a problem is closed."""
        ids = await _setup_solved(station)
        # Approve to close
        _parse(await station.handle(_req("submit_review", {
            "solution_id": ids["solution_id"],
            "reviewer_id": ids["alice"],
            "verdict": "APPROVE",
            "confidence": 1.0,
        })))
        _parse(await station.handle(_req("submit_review", {
            "solution_id": ids["solution_id"],
            "reviewer_id": ids["carol"],
            "verdict": "APPROVE",
            "confidence": 1.0,
        })))

        resp = _parse(await station.handle(_req("search_archive")))
        assert len(resp["result"]) >= 1


# ── Skills & Calibration ────────────────────────────────────────────────


class TestSkillsCalibration:
    @pytest.mark.asyncio
    async def test_effective_tier(self, station):
        alice = _parse(await station.handle(_req("register", {"name": "Alice"})))["result"]
        resp = _parse(await station.handle(_req("effective_tier", {
            "agent_id": alice["agent_id"],
        })))
        assert "effective_tier" in resp["result"]

    @pytest.mark.asyncio
    async def test_is_probationary(self, station):
        alice = _parse(await station.handle(_req("register", {"name": "Alice"})))["result"]
        resp = _parse(await station.handle(_req("is_probationary", {
            "agent_id": alice["agent_id"],
        })))
        assert resp["result"]["probationary"] is True  # new agent

    @pytest.mark.asyncio
    async def test_is_calibration_problem(self, station):
        """Non-calibration problem should return False."""
        alice = _parse(await station.handle(_req("register", {"name": "Alice"})))["result"]
        p = _parse(await station.handle(_req("post_problem", {
            "title": "Normal",
            "description": "Not a calibration problem",
            "author_id": alice["agent_id"],
        })))["result"]
        resp = _parse(await station.handle(_req("is_calibration_problem", {
            "problem_id": p["id"],
        })))
        assert resp["result"]["is_calibration"] is False


# ── Maintenance ──────────────────────────────────────────────────────────


class TestMaintenance:
    @pytest.mark.asyncio
    async def test_expire_stale_problems(self, station):
        resp = _parse(await station.handle(_req("expire_stale_problems")))
        assert isinstance(resp["result"], list)

    @pytest.mark.asyncio
    async def test_expire_stale_claims(self, station):
        resp = _parse(await station.handle(_req("expire_stale_claims")))
        assert isinstance(resp["result"], list)

    @pytest.mark.asyncio
    async def test_escalate_bounty(self, station):
        alice = _parse(await station.handle(_req("register", {"name": "Alice"})))["result"]
        p = _parse(await station.handle(_req("post_problem", {
            "title": "Stale",
            "description": "Needs escalation",
            "author_id": alice["agent_id"],
        })))["result"]
        resp = _parse(await station.handle(_req("escalate_bounty", {
            "problem_id": p["id"],
        })))
        assert resp["result"]["bounty"] > 10  # default bounty is 10

    @pytest.mark.asyncio
    async def test_escalate_stale_bounties(self, station):
        resp = _parse(await station.handle(_req("escalate_stale_bounties", {
            "stale_seconds": 0.001,
        })))
        assert isinstance(resp["result"], list)


# ── Snapshot / Restore ───────────────────────────────────────────────────


class TestSnapshot:
    @pytest.mark.asyncio
    async def test_snapshot_restore(self, station):
        alice = _parse(await station.handle(_req("register", {"name": "Alice"})))["result"]
        _parse(await station.handle(_req("post_problem", {
            "title": "Saved",
            "description": "Persisted",
            "author_id": alice["agent_id"],
        })))

        # Snapshot
        snap = _parse(await station.handle(_req("snapshot")))["result"]
        assert "problems" in snap

        # Restore into a fresh station
        config = ExchangeConfig(
            auto_assign=False, auto_review=False,
            enable_content_guards=False, enable_staking=False,
        )
        station2 = SchwarmaStation(config=config, require_auth=False)
        # Register the same agent so the exchange knows about it
        _parse(await station2.handle(_req("register", {"name": "Alice"})))
        resp = _parse(await station2.handle(_req("restore", {
            "snapshot": snap,
        })))
        assert resp["result"]["restored"] >= 1


# ── Event streaming ──────────────────────────────────────────────────────


class _MockWriter:
    """Fake asyncio.StreamWriter that captures written bytes."""

    def __init__(self):
        self.data = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.data.extend(data)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        pass


class TestEventStreaming:
    @pytest.mark.asyncio
    async def test_subscribe_via_add_subscriber(self, station):
        """Direct add_subscriber + event push."""
        writer = _MockWriter()
        sub_id = station.add_subscriber(writer)

        # Register an agent — should emit AGENT_REGISTERED event
        _parse(await station.handle(_req("register", {"name": "Eve"})))

        # Give the fire-and-forget event bus a tick
        await asyncio.sleep(0.05)

        # Writer should have received at least one notification
        raw = writer.data.decode()
        lines = [l for l in raw.strip().split("\n") if l.strip()]
        assert len(lines) >= 1
        notif = json.loads(lines[0])
        assert notif["jsonrpc"] == "2.0"
        assert notif["method"] == "event"
        assert "id" not in notif  # it's a notification, not a request
        assert "kind" in notif["params"]

        station.remove_subscriber(sub_id)

    @pytest.mark.asyncio
    async def test_subscribe_filtered_kinds(self, station):
        """Only subscribed event kinds are pushed."""
        from schwarma.events import EventKind

        writer = _MockWriter()
        # Subscribe only to PROBLEM_POSTED
        station.add_subscriber(writer, kinds={EventKind.PROBLEM_POSTED})

        # Register an agent (emits AGENT_REGISTERED — should NOT be pushed)
        alice = _parse(await station.handle(_req("register", {"name": "Alice"})))["result"]
        await asyncio.sleep(0.05)

        pushed_before = len(writer.data)

        # Post a problem (emits PROBLEM_POSTED — should be pushed)
        _parse(await station.handle(_req("post_problem", {
            "title": "Filter test",
            "description": "Should appear",
            "author_id": alice["agent_id"],
        })))
        await asyncio.sleep(0.05)

        # Should have new data now
        raw = writer.data[pushed_before:].decode()
        lines = [l for l in raw.strip().split("\n") if l.strip()]
        assert len(lines) >= 1
        notif = json.loads(lines[0])
        assert notif["params"]["kind"] == "PROBLEM_POSTED"

    @pytest.mark.asyncio
    async def test_subscribe_rpc_without_writer(self, station):
        """subscribe via handle() without _writer returns subscribed=False."""
        resp = _parse(await station.handle(_req("subscribe", {"kinds": ["PROBLEM_POSTED"]})))
        assert resp["result"]["subscribed"] is False

    @pytest.mark.asyncio
    async def test_subscribe_rpc_with_writer(self, station):
        """subscribe via handle() with _writer injects writer and subscribes."""
        writer = _MockWriter()
        resp = _parse(await station.handle(
            _req("subscribe", {"kinds": ["PROBLEM_POSTED"]}),
            _writer=writer,
        ))
        assert resp["result"]["subscribed"] is True
        sub_id = resp["result"]["subscriber_id"]

        # Post a problem
        alice = _parse(await station.handle(_req("register", {"name": "Alice"})))["result"]
        _parse(await station.handle(_req("post_problem", {
            "title": "RPC subscribe test",
            "description": "pushed",
            "author_id": alice["agent_id"],
        })))
        await asyncio.sleep(0.05)

        raw = writer.data.decode()
        lines = [l for l in raw.strip().split("\n") if l.strip()]
        # Filter to only PROBLEM_POSTED notifications
        problem_notifs = [
            json.loads(l) for l in lines
            if "PROBLEM_POSTED" in l
        ]
        assert len(problem_notifs) >= 1

        # Unsubscribe
        resp = _parse(await station.handle(_req("unsubscribe", {"subscriber_id": sub_id})))
        assert resp["result"]["unsubscribed"] is True

    @pytest.mark.asyncio
    async def test_dead_writer_removed(self, station):
        """A writer that raises on write is cleaned up automatically."""
        class _BrokenWriter(_MockWriter):
            def write(self, data):
                raise ConnectionResetError("broken")

        writer = _BrokenWriter()
        station.add_subscriber(writer)
        assert len(station._subscribers) == 1

        # Trigger an event
        _parse(await station.handle(_req("register", {"name": "Ghost"})))
        await asyncio.sleep(0.05)

        # Broken writer should have been removed
        assert len(station._subscribers) == 0


# ── Agent inbox ──────────────────────────────────────────────────────────


class TestInbox:
    @pytest.mark.asyncio
    async def test_inbox_empty(self, station):
        alice = _parse(await station.handle(_req("register", {"name": "Alice"})))["result"]
        resp = _parse(await station.handle(_req("inbox", {"agent_id": alice["agent_id"]})))
        assert resp["result"] == []

    @pytest.mark.asyncio
    async def test_inbox_count(self, station):
        alice = _parse(await station.handle(_req("register", {"name": "Alice"})))["result"]
        resp = _parse(await station.handle(_req("inbox_count", {"agent_id": alice["agent_id"]})))
        assert resp["result"]["count"] >= 0

    @pytest.mark.asyncio
    async def test_inbox_receives_notifications(self, station):
        """After a problem is posted, the target agent gets a notification."""
        alice = _parse(await station.handle(_req("register", {"name": "Alice"})))["result"]
        bob = _parse(await station.handle(_req("register", {"name": "Bob"})))["result"]

        # Bob posts a problem, claims by Alice, solved by Alice → triggers events
        p = _parse(await station.handle(_req("post_problem", {
            "title": "Inbox test",
            "description": "Test inbox delivery",
            "author_id": bob["agent_id"],
        })))["result"]

        sol = _parse(await station.handle(_req("claim_and_solve", {
            "problem_id": p["id"],
            "agent_id": alice["agent_id"],
            "body": "done",
        })))["result"]

        await asyncio.sleep(0.05)

        # Bob should have gotten notifications (e.g. PROBLEM_POSTED confirmation)
        count = _parse(await station.handle(_req("inbox_count", {
            "agent_id": bob["agent_id"],
        })))["result"]["count"]
        assert count >= 1

    @pytest.mark.asyncio
    async def test_consume_inbox(self, station):
        alice = _parse(await station.handle(_req("register", {"name": "Alice"})))["result"]
        bob = _parse(await station.handle(_req("register", {"name": "Bob"})))["result"]

        _parse(await station.handle(_req("post_problem", {
            "title": "Consume test",
            "description": "Test consume",
            "author_id": alice["agent_id"],
        })))
        await asyncio.sleep(0.05)

        # Consume alice's inbox
        msgs = _parse(await station.handle(_req("consume_inbox", {
            "agent_id": alice["agent_id"],
        })))["result"]
        # After consuming, inbox should be empty
        count = _parse(await station.handle(_req("inbox_count", {
            "agent_id": alice["agent_id"],
        })))["result"]["count"]
        assert count == 0

    @pytest.mark.asyncio
    async def test_clear_inbox(self, station):
        alice = _parse(await station.handle(_req("register", {"name": "Alice"})))["result"]
        _parse(await station.handle(_req("post_problem", {
            "title": "Clear test",
            "description": "Test clear",
            "author_id": alice["agent_id"],
        })))
        await asyncio.sleep(0.05)

        resp = _parse(await station.handle(_req("clear_inbox", {
            "agent_id": alice["agent_id"],
        })))
        assert resp["result"]["cleared"] >= 0

        # Verify empty
        count = _parse(await station.handle(_req("inbox_count", {
            "agent_id": alice["agent_id"],
        })))["result"]["count"]
        assert count == 0


# ── Agent presence / heartbeat ───────────────────────────────────────────


class TestPresence:
    @pytest.mark.asyncio
    async def test_heartbeat(self, station):
        alice = _parse(await station.handle(_req("register", {"name": "Alice"})))["result"]
        resp = _parse(await station.handle(_req("heartbeat", {
            "agent_id": alice["agent_id"],
        })))
        assert resp["result"]["timestamp"]

    @pytest.mark.asyncio
    async def test_is_online_after_heartbeat(self, station):
        alice = _parse(await station.handle(_req("register", {"name": "Alice"})))["result"]
        _parse(await station.handle(_req("heartbeat", {"agent_id": alice["agent_id"]})))
        resp = _parse(await station.handle(_req("is_online", {"agent_id": alice["agent_id"]})))
        assert resp["result"]["online"] is True

    @pytest.mark.asyncio
    async def test_offline_without_heartbeat(self, station):
        alice = _parse(await station.handle(_req("register", {"name": "Alice"})))["result"]
        resp = _parse(await station.handle(_req("is_online", {"agent_id": alice["agent_id"]})))
        assert resp["result"]["online"] is False

    @pytest.mark.asyncio
    async def test_online_agents_list(self, station):
        alice = _parse(await station.handle(_req("register", {"name": "Alice"})))["result"]
        bob = _parse(await station.handle(_req("register", {"name": "Bob"})))["result"]
        _parse(await station.handle(_req("heartbeat", {"agent_id": alice["agent_id"]})))
        resp = _parse(await station.handle(_req("online_agents", {})))
        online = resp["result"]["online"]
        assert alice["agent_id"] in online
        assert bob["agent_id"] not in online

    @pytest.mark.asyncio
    async def test_last_seen_none(self, station):
        alice = _parse(await station.handle(_req("register", {"name": "Alice"})))["result"]
        resp = _parse(await station.handle(_req("last_seen", {"agent_id": alice["agent_id"]})))
        assert resp["result"]["last_seen"] is None

    @pytest.mark.asyncio
    async def test_last_seen_after_heartbeat(self, station):
        alice = _parse(await station.handle(_req("register", {"name": "Alice"})))["result"]
        _parse(await station.handle(_req("heartbeat", {"agent_id": alice["agent_id"]})))
        resp = _parse(await station.handle(_req("last_seen", {"agent_id": alice["agent_id"]})))
        assert resp["result"]["last_seen"] is not None
