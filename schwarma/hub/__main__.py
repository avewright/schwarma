"""
CLI entry point for the Schwarma Hub.

Usage::

    python -m schwarma.hub
    python -m schwarma.hub --tcp-port 9741 --http-port 8741
    python -m schwarma.hub --database-url postgresql://user:pass@host/db
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from schwarma.hub.config import HubConfig
from schwarma.hub.app import SchwarmaHub


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="schwarma-hub",
        description="Schwarma Hub — deployable server with PostgreSQL persistence",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="PostgreSQL connection string (default: $SCHWARMA_DATABASE_URL or localhost)",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="Bind address (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--tcp-port",
        type=int,
        default=None,
        help="TCP Station port (default: 9741)",
    )
    parser.add_argument(
        "--http-port",
        type=int,
        default=None,
        help="HTTP API port (default: 8741)",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level (default: INFO)",
    )
    parser.add_argument(
        "--no-auth",
        action="store_true",
        help="Disable token authentication (development only)",
    )
    parser.add_argument(
        "--google-client-id",
        default=None,
        help="Google OAuth 2.0 client ID (or set $SCHWARMA_GOOGLE_CLIENT_ID)",
    )
    parser.add_argument(
        "--google-client-secret",
        default=None,
        help="Google OAuth 2.0 client secret (or set $SCHWARMA_GOOGLE_CLIENT_SECRET)",
    )
    parser.add_argument(
        "--google-redirect-uri",
        default=None,
        help="OAuth redirect URI (default: http://localhost:8741/auth/google/callback)",
    )

    args = parser.parse_args()

    config = HubConfig.from_env()

    # CLI overrides
    if args.database_url:
        config.database_url = args.database_url
    if args.host:
        config.host = args.host
    if args.tcp_port:
        config.tcp_port = args.tcp_port
    if args.http_port:
        config.http_port = args.http_port
    if args.log_level:
        config.log_level = args.log_level
    if args.no_auth:
        config.require_auth = False
    if args.google_client_id:
        config.google_client_id = args.google_client_id
    if args.google_client_secret:
        config.google_client_secret = args.google_client_secret
    if args.google_redirect_uri:
        config.google_redirect_uri = args.google_redirect_uri

    hub = SchwarmaHub(config)

    try:
        asyncio.run(hub.run())
    except KeyboardInterrupt:
        print("\nShutdown requested", file=sys.stderr)


if __name__ == "__main__":
    main()
