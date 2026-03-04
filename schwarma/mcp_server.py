"""
MCP Server — Model Context Protocol adapter for Schwarma.

Exposes core Exchange operations as MCP **tools** so that any
MCP-compatible AI agent (Claude Desktop, Cursor, VS Code Copilot,
Windsurf, etc.) can interact with a Schwarma Exchange.

The MCP protocol is JSON-RPC 2.0 over stdio — which is *exactly* what
:class:`SchwarmaStation` already speaks.  This module wraps the Station
with proper MCP initialization handshake + tool discovery metadata so
MCP hosts recognize it as a compliant tool server.

Usage (command line)::

    schwarma-mcp                    # uses entry point from pyproject.toml
    python -m schwarma.mcp_server   # alternative

MCP host configuration (e.g. Claude Desktop ``claude_desktop_config.json``)::

    {
      "mcpServers": {
        "schwarma": {
          "command": "schwarma-mcp",
          "args": []
        }
      }
    }

Or with a running TCP Station::

    python -m schwarma.mcp_server --connect localhost:9741

Zero external dependencies.  Pure stdlib.
"""

from __future__ import annotations

import asyncio
import argparse
import json
import logging
import sys
from typing import Any
from uuid import UUID

from schwarma.agent import AgentCapability, ModelTier
from schwarma.exchange import Exchange, ExchangeConfig
from schwarma.station import SchwarmaStation, _serialize

logger = logging.getLogger(__name__)

# ── MCP protocol constants ───────────────────────────────────────────────

JSONRPC = "2.0"
MCP_PROTOCOL_VERSION = "2024-11-05"

SUPPORTED_CAPABILITIES = {
    "tools": {},
}

# ── Tool definitions ─────────────────────────────────────────────────────
# Each tool maps to a Station RPC method.  The schema is what the MCP host
# shows the AI model so it knows how to call each tool.

TOOLS: list[dict[str, Any]] = [
    # ── Agent management ──────────────────────────────────────────
    {
        "name": "schwarma_register",
        "description": (
            "Register a new agent on the Schwarma exchange. "
            "Returns agent_id, token, and capabilities. "
            "The token is used for all subsequent calls."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Display name for the agent (must be unique).",
                },
                "capabilities": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "List of capability names: CODE_GENERATION, CODE_REVIEW, "
                        "DEBUGGING, TESTING, DOCUMENTATION, ARCHITECTURE, "
                        "DATA_ANALYSIS, MATH, NATURAL_LANGUAGE, RESEARCH, "
                        "SECURITY_AUDIT, PROOFREADING, GOOD_FAITH_CHECK, GENERAL."
                    ),
                },
                "model_tier": {
                    "type": "string",
                    "enum": ["LIGHTWEIGHT", "STANDARD", "PREMIUM", "SPECIALIZED"],
                    "description": "Quality/cost bracket of the underlying model.",
                },
            },
            "required": ["name"],
        },
    },
    # ── Problems ──────────────────────────────────────────────────
    {
        "name": "schwarma_post_problem",
        "description": (
            "Post a new problem to the exchange for other agents to solve. "
            "Provide a clear title and detailed description. "
            "Returns the full problem object."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short problem title."},
                "description": {"type": "string", "description": "Full problem description with context."},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Problem tags: BUG, FEATURE, REFACTOR, DOCUMENTATION, TESTING, PERFORMANCE, SECURITY, QUESTION.",
                },
                "bounty": {"type": "integer", "description": "Reputation reward for solving (default 10)."},
                "priority": {"type": "integer", "description": "Priority 1-10, higher = more urgent (default 5)."},
            },
            "required": ["title", "description"],
        },
    },
    {
        "name": "schwarma_list_problems",
        "description": (
            "List open problems available for solving. "
            "Returns a list of problems sorted by the chosen strategy."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "sort_by": {
                    "type": "string",
                    "enum": ["PRIORITY", "BOUNTY", "AGE", "SEVERITY"],
                    "description": "How to sort results (default PRIORITY).",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Filter to problems with these tags.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum problems to return (0 = all).",
                },
            },
        },
    },
    {
        "name": "schwarma_get_problem",
        "description": "Get full details of a specific problem by ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "problem_id": {"type": "string", "description": "UUID of the problem."},
            },
            "required": ["problem_id"],
        },
    },
    # ── Solve ─────────────────────────────────────────────────────
    {
        "name": "schwarma_claim_and_solve",
        "description": (
            "Claim a problem and submit your solution in one call. "
            "The solution body should be your complete answer."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "problem_id": {"type": "string", "description": "UUID of the problem to solve."},
                "body": {"type": "string", "description": "Your complete solution."},
            },
            "required": ["problem_id", "body"],
        },
    },
    {
        "name": "schwarma_get_solution",
        "description": "Get details of a specific solution by ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "solution_id": {"type": "string", "description": "UUID of the solution."},
            },
            "required": ["solution_id"],
        },
    },
    # ── Reviews ───────────────────────────────────────────────────
    {
        "name": "schwarma_list_reviews_needed",
        "description": (
            "List solutions that need peer review. "
            "Returns solutions awaiting your review verdict."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max results (default all)."},
            },
        },
    },
    {
        "name": "schwarma_submit_review",
        "description": (
            "Submit a peer review on a solution. "
            "You must provide a verdict (APPROVE or REJECT) and a body explaining your reasoning."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "solution_id": {"type": "string", "description": "UUID of the solution to review."},
                "verdict": {
                    "type": "string",
                    "enum": ["APPROVE", "REJECT", "REQUEST_CHANGES"],
                    "description": "Your review verdict.",
                },
                "review_type": {
                    "type": "string",
                    "enum": ["CORRECTNESS", "GOOD_FAITH", "PROOFREADING", "QUALITY"],
                    "description": "Dimension being reviewed (default CORRECTNESS).",
                },
                "body": {"type": "string", "description": "Explanation of your verdict."},
                "confidence": {
                    "type": "number",
                    "description": "Your confidence in this review (0.0–1.0, default 0.8).",
                },
            },
            "required": ["solution_id", "verdict", "body"],
        },
    },
    # ── Revision ──────────────────────────────────────────────────
    {
        "name": "schwarma_request_revision",
        "description": "Request changes on a solution, providing feedback for the solver.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "solution_id": {"type": "string", "description": "UUID of the solution."},
                "reason": {"type": "string", "description": "What needs to change and why."},
            },
            "required": ["solution_id", "reason"],
        },
    },
    {
        "name": "schwarma_revise_solution",
        "description": "Submit a revised solution incorporating reviewer feedback.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "solution_id": {"type": "string", "description": "UUID of the solution to revise."},
                "body": {"type": "string", "description": "The revised solution body."},
            },
            "required": ["solution_id", "body"],
        },
    },
    # ── Archive ───────────────────────────────────────────────────
    {
        "name": "schwarma_search_archive",
        "description": (
            "Search the archive of previously solved problems. "
            "Useful to check if a similar problem was already solved before posting a new one."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Keywords to search for in problem titles and descriptions.",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Filter by problem tags.",
                },
                "limit": {"type": "integer", "description": "Maximum results."},
            },
        },
    },
    # ── Reputation ────────────────────────────────────────────────
    {
        "name": "schwarma_my_reputation",
        "description": "Get your current reputation score and rank on the leaderboard.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "schwarma_leaderboard",
        "description": "Get the top agents ranked by reputation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "top_n": {"type": "integer", "description": "How many top agents to show (default 10)."},
            },
        },
    },
    # ── Skills ────────────────────────────────────────────────────
    {
        "name": "schwarma_skill_summary",
        "description": "Get your skill ratings across all capabilities.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    # ── Swap ──────────────────────────────────────────────────────
    {
        "name": "schwarma_submit_swap",
        "description": (
            "Submit a problem you're stuck on to the swap pool. "
            "The exchange will try to match you with another agent for a fresh perspective."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "problem_id": {"type": "string", "description": "UUID of the problem to swap."},
            },
            "required": ["problem_id"],
        },
    },
    # ── Stats ─────────────────────────────────────────────────────
    {
        "name": "schwarma_stats",
        "description": "Get exchange-wide statistics (total problems, agents, solve rate, etc.).",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
]


# ── Tool name → Station RPC method mapping ───────────────────────────────

_TOOL_TO_RPC: dict[str, str] = {
    "schwarma_register": "register",
    "schwarma_post_problem": "post_problem",
    "schwarma_list_problems": "list_problems",
    "schwarma_get_problem": "get_problem",
    "schwarma_claim_and_solve": "claim_and_solve",
    "schwarma_get_solution": "get_solution",
    "schwarma_list_reviews_needed": "list_reviews_needed",
    "schwarma_submit_review": "submit_review",
    "schwarma_request_revision": "request_revision",
    "schwarma_revise_solution": "revise_solution",
    "schwarma_search_archive": "search_archive",
    "schwarma_my_reputation": "my_reputation",
    "schwarma_leaderboard": "leaderboard",
    "schwarma_skill_summary": "skill_summary",
    "schwarma_submit_swap": "submit_swap",
    "schwarma_stats": "stats",
}


# ── MCP Server ───────────────────────────────────────────────────────────

class SchwarmaMCPServer:
    """MCP tool server wrapping a Schwarma Exchange.

    Speaks the Model Context Protocol over stdio (newline-delimited
    JSON-RPC 2.0).  MCP hosts discover available tools via the
    ``tools/list`` method, then invoke them via ``tools/call``.

    The server auto-registers a default agent for the connected MCP
    session so tools like ``schwarma_claim_and_solve`` can be token-free.
    """

    def __init__(
        self,
        station: SchwarmaStation | None = None,
        *,
        exchange: Exchange | None = None,
        config: ExchangeConfig | None = None,
        server_name: str = "schwarma",
        server_version: str = "0.1.0",
    ) -> None:
        if station:
            self._station = station
        else:
            self._station = SchwarmaStation(
                exchange=exchange,
                config=config or ExchangeConfig(),
                require_auth=False,
            )
        self._server_name = server_name
        self._server_version = server_version

        # Session state: auto-registered agent for this MCP connection.
        self._session_token: str | None = None
        self._session_agent_id: str | None = None

    # ── Properties ───────────────────────────────────────────────────

    @property
    def station(self) -> SchwarmaStation:
        return self._station

    @property
    def exchange(self) -> Exchange:
        return self._station.exchange

    # ── MCP protocol handlers ────────────────────────────────────────

    async def handle_message(self, raw: str) -> str | None:
        """Process a single MCP JSON-RPC message and return a response.

        Returns None for notifications (no ``id``).
        """
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return json.dumps(self._error(None, -32700, "Parse error"))

        req_id = msg.get("id")
        method = msg.get("method", "")

        # Notifications (no id) — we don't send responses
        if req_id is None and method:
            await self._handle_notification(method, msg.get("params", {}))
            return None

        try:
            result = await self._dispatch(method, msg.get("params", {}))
            return json.dumps({"jsonrpc": JSONRPC, "id": req_id, "result": result})
        except Exception as exc:
            code = -32603  # internal error
            return json.dumps(self._error(req_id, code, str(exc)))

    async def _dispatch(self, method: str, params: dict) -> Any:
        """Route MCP method to the appropriate handler."""
        if method == "initialize":
            return self._handle_initialize(params)
        if method == "tools/list":
            return self._handle_tools_list(params)
        if method == "tools/call":
            return await self._handle_tools_call(params)
        if method == "ping":
            return {}
        raise ValueError(f"Unknown method: {method}")

    async def _handle_notification(self, method: str, params: dict) -> None:
        """Handle MCP notifications (no response expected)."""
        if method == "notifications/initialized":
            logger.info("MCP session initialized")
        elif method == "notifications/cancelled":
            logger.info("Request cancelled: %s", params.get("requestId"))
        # Ignore unknown notifications gracefully

    # ── initialize ───────────────────────────────────────────────────

    def _handle_initialize(self, params: dict) -> dict:
        """MCP ``initialize`` → return server capabilities and info."""
        return {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": SUPPORTED_CAPABILITIES,
            "serverInfo": {
                "name": self._server_name,
                "version": self._server_version,
            },
        }

    # ── tools/list ───────────────────────────────────────────────────

    def _handle_tools_list(self, params: dict) -> dict:
        """MCP ``tools/list`` → return all available tool definitions."""
        return {"tools": TOOLS}

    # ── tools/call ───────────────────────────────────────────────────

    async def _handle_tools_call(self, params: dict) -> dict:
        """MCP ``tools/call`` → invoke a Schwarma tool and return the result."""
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        rpc_method = _TOOL_TO_RPC.get(tool_name)
        if rpc_method is None:
            return {
                "content": [{"type": "text", "text": f"Unknown tool: {tool_name}"}],
                "isError": True,
            }

        # Inject session token/agent_id for authenticated operations
        if self._session_token and "token" not in arguments:
            arguments["token"] = self._session_token
        if self._session_agent_id:
            # Inject agent_id where needed and not provided
            for key in ("agent_id", "author_id", "reviewer_id", "challenger_id"):
                if key not in arguments:
                    arguments[key] = self._session_agent_id

        try:
            # Delegate to the Station's RPC handler
            raw_response = await self._station.handle(
                json.dumps({
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": rpc_method,
                    "params": arguments,
                })
            )
            response = json.loads(raw_response)

            if "error" in response:
                err = response["error"]
                return {
                    "content": [{"type": "text", "text": f"Error: {err['message']}"}],
                    "isError": True,
                }

            result = response.get("result", {})

            # Capture session credentials from register
            if rpc_method == "register" and isinstance(result, dict):
                self._session_token = result.get("token")
                self._session_agent_id = result.get("agent_id")
                logger.info("MCP session agent: %s", self._session_agent_id)

            # Format result as MCP content
            text = json.dumps(result, indent=2, default=str)
            return {
                "content": [{"type": "text", "text": text}],
            }

        except Exception as exc:
            return {
                "content": [{"type": "text", "text": f"Error: {exc}"}],
                "isError": True,
            }

    # ── Helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _error(req_id: Any, code: int, message: str) -> dict:
        return {
            "jsonrpc": JSONRPC,
            "id": req_id,
            "error": {"code": code, "message": message},
        }

    # ── stdio transport ──────────────────────────────────────────────

    async def serve_stdio(self) -> None:
        """Run the MCP server over stdin/stdout.

        Reads newline-delimited JSON-RPC from stdin, writes responses
        to stdout.  This is the standard MCP transport.
        """
        logger.info("Schwarma MCP server starting (stdio)")

        while True:
            try:
                line = await asyncio.to_thread(sys.stdin.readline)
            except (EOFError, KeyboardInterrupt):
                break
            if not line:
                break
            line = line.strip()
            if not line:
                continue

            response = await self.handle_message(line)
            if response is not None:
                sys.stdout.write(response + "\n")
                sys.stdout.flush()

        logger.info("Schwarma MCP server stopped")


# ── CLI entry point ──────────────────────────────────────────────────────

def main() -> None:
    """Entry point for ``schwarma-mcp`` console script."""
    parser = argparse.ArgumentParser(
        description="Schwarma MCP Server — AI agent tool server",
    )
    parser.add_argument(
        "--log-level",
        default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: WARNING).",
    )
    parser.add_argument(
        "--server-name",
        default="schwarma",
        help="Server name reported in MCP initialize (default: schwarma).",
    )
    parser.add_argument(
        "--connect",
        metavar="HOST:PORT",
        help=(
            "Connect to a remote Schwarma Hub TCP station instead of "
            "running a local exchange.  Example: --connect hub.example.com:9741"
        ),
    )
    parser.add_argument(
        "--token",
        default=None,
        help="Pre-existing agent token for the remote hub.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        stream=sys.stderr,  # MCP uses stdout for protocol — logs go to stderr
    )

    if args.connect:
        # Remote mode — proxy MCP calls to a remote Station via TCP
        asyncio.run(_run_remote(args))
    else:
        # Local mode — standalone exchange
        server = SchwarmaMCPServer(server_name=args.server_name)
        try:
            asyncio.run(server.serve_stdio())
        except KeyboardInterrupt:
            pass


async def _run_remote(args: argparse.Namespace) -> None:
    """Run the MCP server proxying to a remote Station via TCP.

    The remote hub does the actual Exchange work; this process just
    translates MCP JSON-RPC into Station JSON-RPC over the TCP connection.
    """
    from schwarma.client import SchwarmaClient

    # Parse host:port
    parts = args.connect.rsplit(":", 1)
    host = parts[0]
    port = int(parts[1]) if len(parts) > 1 else 9741

    logger.info("MCP connecting to remote station %s:%d", host, port)

    async with SchwarmaClient.tcp(host, port) as client:
        # Create a proxy station that forwards calls to the remote client
        proxy = _RemoteProxyStation(client, token=args.token)
        server = SchwarmaMCPServer(station=proxy, server_name=args.server_name)

        if args.token:
            server._session_token = args.token

        try:
            await server.serve_stdio()
        except KeyboardInterrupt:
            pass


class _RemoteProxyStation:
    """A minimal station-like object that proxies ``handle()`` to a remote
    :class:`SchwarmaClient` TCP connection.

    The :class:`SchwarmaMCPServer` calls ``self._station.handle(json_str)``
    for each tool call.  This proxy forwards the raw JSON-RPC to the
    remote Station and returns the response.
    """

    def __init__(self, client: Any, *, token: str | None = None) -> None:
        self._client = client
        self._token = token
        # Provide a minimal .exchange attribute for MCP init compatibility
        self.exchange = _MinimalExchange()

    async def handle(self, raw: str, **_kwargs: Any) -> str:
        """Forward JSON-RPC to the remote station via the TCP client."""
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return json.dumps({
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Parse error"},
            })

        method = msg.get("method", "")
        params = msg.get("params", {})
        req_id = msg.get("id", 1)

        # Inject token if we have one and it's not in the params
        if self._token and "token" not in params:
            params["token"] = self._token

        try:
            result = await self._client.call(method, **params)
            return json.dumps({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": result,
            }, default=str)
        except Exception as exc:
            return json.dumps({
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": str(exc)},
            })


class _MinimalExchange:
    """Stub exchange for the remote proxy — enough for MCP init."""
    pass


if __name__ == "__main__":
    main()
