"""Tests for schwarma.mcp_server — MCP protocol adapter."""

from __future__ import annotations

import json

import pytest

from schwarma.mcp_server import (
    MCP_PROTOCOL_VERSION,
    TOOLS,
    SchwarmaMCPServer,
    _TOOL_TO_RPC,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_server() -> SchwarmaMCPServer:
    return SchwarmaMCPServer(server_name="test-schwarma", server_version="0.0.1")


async def _call(server: SchwarmaMCPServer, method: str, params: dict | None = None, req_id: int = 1) -> dict:
    """Send a JSON-RPC request and parse the response."""
    msg = json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}})
    raw = await server.handle_message(msg)
    assert raw is not None, f"Expected response for {method}"
    return json.loads(raw)


async def _call_tool(server: SchwarmaMCPServer, tool_name: str, arguments: dict | None = None) -> dict:
    """Invoke a tool via tools/call and return the parsed MCP result."""
    resp = await _call(server, "tools/call", {"name": tool_name, "arguments": arguments or {}})
    return resp["result"]


# ---------------------------------------------------------------------------
# Tests: MCP initialize
# ---------------------------------------------------------------------------

class TestInitialize:

    async def test_initialize_returns_server_info(self):
        server = _make_server()
        resp = await _call(server, "initialize", {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1.0"},
        })
        result = resp["result"]
        assert result["protocolVersion"] == MCP_PROTOCOL_VERSION
        assert result["serverInfo"]["name"] == "test-schwarma"
        assert result["serverInfo"]["version"] == "0.0.1"
        assert "tools" in result["capabilities"]

    async def test_initialize_no_error(self):
        server = _make_server()
        resp = await _call(server, "initialize")
        assert "error" not in resp


# ---------------------------------------------------------------------------
# Tests: tools/list
# ---------------------------------------------------------------------------

class TestToolsList:

    async def test_returns_all_tools(self):
        server = _make_server()
        resp = await _call(server, "tools/list")
        tools = resp["result"]["tools"]
        assert isinstance(tools, list)
        assert len(tools) == len(TOOLS)

    async def test_tools_have_required_fields(self):
        server = _make_server()
        resp = await _call(server, "tools/list")
        for tool in resp["result"]["tools"]:
            assert "name" in tool
            assert "description" in tool
            assert "inputSchema" in tool
            assert tool["inputSchema"]["type"] == "object"

    async def test_all_tools_map_to_rpc(self):
        """Every tool in TOOLS has a corresponding RPC mapping."""
        for tool in TOOLS:
            assert tool["name"] in _TOOL_TO_RPC, f"{tool['name']} missing from _TOOL_TO_RPC"


# ---------------------------------------------------------------------------
# Tests: tools/call — register
# ---------------------------------------------------------------------------

class TestToolRegister:

    async def test_register_creates_agent(self):
        server = _make_server()
        result = await _call_tool(server, "schwarma_register", {
            "name": "TestAgent",
            "capabilities": ["CODE_GENERATION"],
            "model_tier": "STANDARD",
        })
        assert not result.get("isError", False)
        content = json.loads(result["content"][0]["text"])
        assert "agent_id" in content
        assert content["name"] == "TestAgent"
        assert "token" in content

    async def test_register_stores_session(self):
        server = _make_server()
        await _call_tool(server, "schwarma_register", {"name": "SessionAgent"})
        assert server._session_agent_id is not None
        assert server._session_token is not None

    async def test_register_defaults(self):
        server = _make_server()
        result = await _call_tool(server, "schwarma_register", {"name": "MinAgent"})
        content = json.loads(result["content"][0]["text"])
        # Should get GENERAL capability by default
        assert "GENERAL" in content["capabilities"]


# ---------------------------------------------------------------------------
# Tests: tools/call — problems
# ---------------------------------------------------------------------------

class TestToolProblems:

    async def _register(self, server: SchwarmaMCPServer) -> str:
        result = await _call_tool(server, "schwarma_register", {
            "name": "ProblemAgent",
            "capabilities": ["CODE_GENERATION"],
        })
        content = json.loads(result["content"][0]["text"])
        return content["agent_id"]

    async def test_post_and_list_problems(self):
        server = _make_server()
        agent_id = await self._register(server)

        # Post a problem
        result = await _call_tool(server, "schwarma_post_problem", {
            "title": "Fix the auth module",
            "description": "The JWT validation is broken when tokens expire.",
            "tags": ["BUG"],
            "bounty": 15,
        })
        assert not result.get("isError", False)
        problem = json.loads(result["content"][0]["text"])
        assert problem["title"] == "Fix the auth module"
        pid = problem["id"]

        # List problems
        result = await _call_tool(server, "schwarma_list_problems")
        problems = json.loads(result["content"][0]["text"])
        assert isinstance(problems, list)
        assert len(problems) >= 1

    async def test_get_problem(self):
        server = _make_server()
        await self._register(server)

        post_result = await _call_tool(server, "schwarma_post_problem", {
            "title": "Test Prob",
            "description": "Details here.",
        })
        pid = json.loads(post_result["content"][0]["text"])["id"]

        result = await _call_tool(server, "schwarma_get_problem", {"problem_id": pid})
        assert not result.get("isError", False)
        problem = json.loads(result["content"][0]["text"])
        assert problem["id"] == pid


# ---------------------------------------------------------------------------
# Tests: tools/call — solve & review
# ---------------------------------------------------------------------------

class TestToolSolveAndReview:

    async def test_claim_and_solve(self):
        server = _make_server()
        # Register poster
        r = await _call_tool(server, "schwarma_register", {"name": "Poster", "capabilities": ["GENERAL"]})
        poster_id = json.loads(r["content"][0]["text"])["agent_id"]

        # Post problem
        r = await _call_tool(server, "schwarma_post_problem", {
            "title": "Solve this", "description": "FizzBuzz in Python",
        })
        pid = json.loads(r["content"][0]["text"])["id"]

        # Register solver (new session)
        server._session_token = None
        server._session_agent_id = None
        r = await _call_tool(server, "schwarma_register", {
            "name": "Solver", "capabilities": ["CODE_GENERATION"],
        })
        solver_id = json.loads(r["content"][0]["text"])["agent_id"]

        # Solve
        r = await _call_tool(server, "schwarma_claim_and_solve", {
            "problem_id": pid,
            "body": "def fizzbuzz(): ...",
        })
        assert not r.get("isError", False)
        sol = json.loads(r["content"][0]["text"])
        assert "id" in sol


# ---------------------------------------------------------------------------
# Tests: tools/call — archive & reputation
# ---------------------------------------------------------------------------

class TestToolMisc:

    async def test_search_archive_empty(self):
        server = _make_server()
        result = await _call_tool(server, "schwarma_search_archive", {"keywords": ["nonexistent"]})
        entries = json.loads(result["content"][0]["text"])
        assert isinstance(entries, list)

    async def test_leaderboard(self):
        server = _make_server()
        result = await _call_tool(server, "schwarma_leaderboard", {"top_n": 5})
        board = json.loads(result["content"][0]["text"])
        assert isinstance(board, list)

    async def test_stats(self):
        server = _make_server()
        result = await _call_tool(server, "schwarma_stats")
        stats = json.loads(result["content"][0]["text"])
        assert isinstance(stats, dict)


# ---------------------------------------------------------------------------
# Tests: error handling
# ---------------------------------------------------------------------------

class TestErrorHandling:

    async def test_unknown_tool(self):
        server = _make_server()
        result = await _call_tool(server, "schwarma_nonexistent")
        assert result["isError"] is True
        assert "Unknown tool" in result["content"][0]["text"]

    async def test_unknown_method(self):
        server = _make_server()
        resp = await _call(server, "unknown/method")
        assert "error" in resp

    async def test_parse_error(self):
        server = _make_server()
        raw = await server.handle_message("not json at all")
        resp = json.loads(raw)
        assert resp["error"]["code"] == -32700

    async def test_notification_returns_none(self):
        server = _make_server()
        msg = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        result = await server.handle_message(msg)
        assert result is None


# ---------------------------------------------------------------------------
# Tests: ping
# ---------------------------------------------------------------------------

class TestPing:

    async def test_ping(self):
        server = _make_server()
        resp = await _call(server, "ping")
        assert "error" not in resp
