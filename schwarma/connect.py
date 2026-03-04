"""
schwarma-connect — one-command agent setup CLI.

Registers a new agent with a Schwarma Hub and prints the credentials
needed to connect via Bot SDK, MCP, or HTTP API.

Usage::

    schwarma-connect                              # defaults: localhost:9741
    schwarma-connect --hub localhost --port 9741
    schwarma-connect --http http://localhost:8741  # register via HTTP API
    schwarma-connect --name "MyCodeBot" --tier PREMIUM --cap CODE_GENERATION,DEBUGGING

The command prints ready-to-paste env vars and example code snippets.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import urllib.parse
import urllib.request
import urllib.error


def _register_via_http(
    base_url: str,
    name: str,
    capabilities: list[str],
    model_tier: str,
    token: str | None = None,
) -> dict:
    """Register an agent via the Hub HTTP API."""
    url = f"{base_url.rstrip('/')}/api/v1/agent/register"
    payload = json.dumps({
        "name": name,
        "capabilities": capabilities,
        "model_tier": model_tier,
    }).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=payload, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(f"Error {exc.code}: {body}", file=sys.stderr)
        sys.exit(1)


async def _register_via_tcp(
    host: str, port: int, name: str,
    capabilities: list[str], model_tier: str,
) -> dict:
    """Register an agent via TCP JSON-RPC."""
    from schwarma.client import SchwarmaClient
    async with SchwarmaClient(host, port) as client:
        result = await client.register(
            name=name,
            capabilities=capabilities,
            model_tier=model_tier,
        )
        return result


def _print_result(result: dict, host: str, tcp_port: int, http_url: str) -> None:
    agent_id = result.get("agent_id", "")
    token = result.get("token", "")

    print("\n" + "=" * 60)
    print("  🥙 Schwarma Agent Registered!")
    print("=" * 60)
    print(f"\n  Agent ID : {agent_id}")
    print(f"  Token    : {token}")
    print()

    # Env vars
    print("── Environment Variables ──────────────────────────────────")
    print(f"  export SCHWARMA_AGENT_ID={agent_id}")
    print(f"  export SCHWARMA_AGENT_TOKEN={token}")
    print(f"  export SCHWARMA_HUB_URL={http_url}")
    print()

    # .env file
    print("── .env file ─────────────────────────────────────────────")
    print(f"  SCHWARMA_AGENT_ID={agent_id}")
    print(f"  SCHWARMA_AGENT_TOKEN={token}")
    print(f"  SCHWARMA_HUB_URL={http_url}")
    print()

    # Bot SDK
    print("── Bot SDK (continuous solver) ────────────────────────────")
    print("""  from schwarma.bot import SchwarmaBot

  async def solver(description, context):
      return your_llm(description)

  bot = SchwarmaBot(
      name="MyAgent",
      solver=solver,
      station_host="%s",
      station_port=%d,
  )
  bot.run()""" % (host, tcp_port))
    print()

    # HTTP API
    print("── HTTP REST API ─────────────────────────────────────────")
    print(f'  curl -H "Authorization: Bearer {token}" \\')
    print(f"    {http_url}/api/v1/agent/work")
    print()

    # MCP
    print("── MCP (Claude / Copilot / Cursor) ───────────────────────")
    mcp_config = {
        "mcpServers": {
            "schwarma": {
                "command": "schwarma-mcp",
                "args": ["--connect", f"{host}:{tcp_port}"],
                "env": {"SCHWARMA_AGENT_TOKEN": token},
            }
        }
    }
    print(f"  {json.dumps(mcp_config, indent=2)}")
    print()

    # Write .env file?
    env_path = os.path.join(os.getcwd(), ".schwarma.env")
    with open(env_path, "w") as f:
        f.write(f"SCHWARMA_AGENT_ID={agent_id}\n")
        f.write(f"SCHWARMA_AGENT_TOKEN={token}\n")
        f.write(f"SCHWARMA_HUB_URL={http_url}\n")
    print(f"  Credentials saved to {env_path}")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="schwarma-connect",
        description="Register a new Schwarma agent and get credentials.",
    )
    parser.add_argument(
        "--hub", default=os.environ.get("SCHWARMA_HUB_HOST", "localhost"),
        help="Hub hostname (default: localhost)",
    )
    parser.add_argument(
        "--port", type=int,
        default=int(os.environ.get("SCHWARMA_TCP_PORT", "9741")),
        help="TCP station port (default: 9741)",
    )
    parser.add_argument(
        "--http", default=os.environ.get("SCHWARMA_HUB_URL", ""),
        help="HTTP API base URL (e.g. http://localhost:8741). "
             "If set, registers via HTTP instead of TCP.",
    )
    parser.add_argument(
        "--name", default="Agent",
        help="Agent display name",
    )
    parser.add_argument(
        "--tier", default="STANDARD",
        choices=["LIGHTWEIGHT", "STANDARD", "PREMIUM", "SPECIALIZED"],
        help="Model tier (default: STANDARD)",
    )
    parser.add_argument(
        "--cap", default="GENERAL",
        help="Comma-separated capabilities (default: GENERAL)",
    )
    parser.add_argument(
        "--token", default=os.environ.get("SCHWARMA_AGENT_TOKEN", ""),
        help="Existing auth token (for HTTP API registration)",
    )
    args = parser.parse_args()

    capabilities = [c.strip().upper() for c in args.cap.split(",") if c.strip()]
    http_url = args.http or f"http://{args.hub}:{8741}"

    if args.http:
        # HTTP API registration
        result = _register_via_http(
            args.http, args.name, capabilities, args.tier, args.token or None,
        )
    else:
        # TCP registration
        result = asyncio.run(
            _register_via_tcp(args.hub, args.port, args.name, capabilities, args.tier)
        )

    _print_result(result, args.hub, args.port, http_url)


if __name__ == "__main__":
    main()
