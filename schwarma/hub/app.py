"""
SchwarmaHub — the deployable server that ties Station + PostgreSQL together.

Runs three concurrent services:
  1. TCP Station  — JSON-RPC for agent connections (port 9741)
  2. HTTP API     — health, stats, dashboard endpoints (port 8741)
  3. Sync engine  — EventBus → PostgreSQL write-through + periodic snapshots
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from schwarma.exchange import Exchange, ExchangeConfig
from schwarma.hub.config import HubConfig
from schwarma.hub.database import Database
from schwarma.hub.http import create_http_server
from schwarma.hub.sync import ExchangeSync
from schwarma.station import SchwarmaStation

logger = logging.getLogger(__name__)


class SchwarmaHub:
    """Deployable Schwarma server with PostgreSQL persistence.

    Usage::

        hub = SchwarmaHub(HubConfig.from_env())
        asyncio.run(hub.run())
    """

    def __init__(self, config: HubConfig | None = None) -> None:
        self.config = config or HubConfig()
        self.db = Database(
            self.config.database_url,
            min_size=self.config.db_pool_min,
            max_size=self.config.db_pool_max,
            ssl=self.config.make_db_ssl_context(),
        )
        self.station = SchwarmaStation(
            config=ExchangeConfig(),
            require_auth=self.config.require_auth,
        )
        self.sync = ExchangeSync(self.station, self.db)
        self._snapshot_task: asyncio.Task | None = None
        self._cleanup_task: asyncio.Task | None = None
        self._shutdown_event = asyncio.Event()

    async def start(self) -> None:
        """Start database, rehydrate, and attach sync."""
        _setup_logging(self.config.log_level, self.config.log_format)

        # Validate configuration and warn about insecure defaults
        self.config.validate()

        # Auto-generate session secret if not set (ephemeral — lost on restart)
        if not self.config.session_secret:
            import secrets as _secrets
            self.config.session_secret = _secrets.token_urlsafe(48)
            logger.warning(
                "Auto-generated session secret (will not persist across restarts). "
                "Set SCHWARMA_SESSION_SECRET for production."
            )

        # Log OAuth env var status at startup
        from schwarma.hub.auth import log_oauth_env_status, probe_smtp
        log_oauth_env_status(self.config)

        # Validate SMTP connectivity if configured
        try:
            smtp_ok = await probe_smtp(self.config)
            if smtp_ok:
                logger.info("SMTP probe OK (%s:%d)", self.config.smtp_host, self.config.smtp_port)
            else:
                logger.info("SMTP not configured — email verification will log codes to console")
        except Exception as exc:
            logger.error("SMTP probe FAILED (%s:%d): %s", self.config.smtp_host, self.config.smtp_port, exc)
            logger.error("Email verification will not work until SMTP is fixed")

        logger.info("Starting Schwarma Hub...")
        logger.info("  Database: %s", _mask_dsn(self.config.database_url))
        logger.info("  TCP:      %s:%d", self.config.host, self.config.tcp_port)
        logger.info("  HTTP:     %s:%d", self.config.host, self.config.http_port)

        # 1. Connect to PostgreSQL and run migrations
        await self.db.connect()

        # 2. Rehydrate exchange from database
        counts = await self.sync.rehydrate()
        logger.info("Rehydrated: %s", counts)

        # 3. Attach event-driven sync (Exchange → DB)
        self.sync.attach()

        # 4. Start periodic snapshot task
        self._snapshot_task = asyncio.create_task(
            self._periodic_snapshot(), name="snapshot"
        )

        # 5. Start session cleanup task (every 1 hour)
        self._cleanup_task = asyncio.create_task(
            self._periodic_session_cleanup(), name="session-cleanup"
        )

        logger.info("Hub started successfully")

    async def stop(self) -> None:
        """Gracefully shut down."""
        logger.info("Shutting down Hub...")

        # Cancel periodic tasks
        for task in (self._snapshot_task, self._cleanup_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        # Final snapshot before exit
        try:
            await self.sync.full_snapshot()
        except Exception:
            logger.exception("Failed to write final snapshot")

        # Close database
        await self.db.close()
        logger.info("Hub stopped")

    async def run(self) -> None:
        """Main entry point — start all services and wait for shutdown."""
        await self.start()

        # Set up signal handlers
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._shutdown_event.set)
            except NotImplementedError:
                # Windows doesn't support add_signal_handler
                pass

        try:
            # Run TCP station and HTTP server concurrently
            await asyncio.gather(
                self._run_tcp(),
                self._run_http(),
                self._wait_for_shutdown(),
            )
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()

    async def _run_tcp(self) -> None:
        """Start the JSON-RPC TCP server."""
        async def _handle_client(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            addr = writer.get_extra_info("peername")
            logger.info("TCP client connected: %s", addr)
            sub_id = id(writer)
            try:
                while True:
                    data = await reader.readline()
                    if not data:
                        break
                    response = await self.station.handle(
                        data.decode().strip(), _writer=writer,
                    )
                    writer.write((response + "\n").encode())
                    await writer.drain()
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Error handling TCP client %s", addr)
            finally:
                self.station.remove_subscriber(sub_id)
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
                logger.info("TCP client disconnected: %s", addr)

        ssl_ctx = self.config.make_ssl_context()
        server = await asyncio.start_server(
            _handle_client,
            self.config.host,
            self.config.tcp_port,
            ssl=ssl_ctx,
        )
        addrs = ", ".join(str(s.getsockname()) for s in server.sockets)
        proto = "TLS" if ssl_ctx else "TCP"
        logger.info("%s Station listening on %s", proto, addrs)
        print(f"Schwarma Hub {proto} on {addrs}", file=sys.stderr)

        async with server:
            await self.station.scheduler.start()
            try:
                await server.serve_forever()
            except asyncio.CancelledError:
                pass
            finally:
                await self.station.scheduler.stop()

    async def _run_http(self) -> None:
        """Start the HTTP API server."""
        handler = create_http_server(self)
        ssl_ctx = self.config.make_ssl_context()
        server = await asyncio.start_server(
            handler,
            self.config.host,
            self.config.http_port,
            ssl=ssl_ctx,
        )
        addrs = ", ".join(str(s.getsockname()) for s in server.sockets)
        proto = "HTTPS" if ssl_ctx else "HTTP"
        logger.info("%s API listening on %s", proto, addrs)
        print(f"Schwarma Hub {proto} on {addrs}", file=sys.stderr)

        async with server:
            try:
                await server.serve_forever()
            except asyncio.CancelledError:
                pass

    async def _wait_for_shutdown(self) -> None:
        """Wait for shutdown signal, drain in-flight requests, cancel tasks."""
        await self._shutdown_event.wait()
        drain = self.config.shutdown_drain_seconds
        logger.info("Shutdown signal received, draining for %ds...", drain)
        if drain > 0:
            await asyncio.sleep(drain)
        # Cancel all tasks in the current group
        for task in asyncio.all_tasks():
            if task is not asyncio.current_task():
                task.cancel()

    async def _periodic_snapshot(self) -> None:
        """Periodically write a full snapshot as a safety net."""
        interval = self.config.snapshot_interval
        while True:
            await asyncio.sleep(interval)
            try:
                await self.sync.full_snapshot()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Periodic snapshot failed")

    async def _periodic_session_cleanup(self) -> None:
        """Periodically delete expired user sessions."""
        while True:
            await asyncio.sleep(3600)  # every hour
            try:
                await self.db.cleanup_expired_sessions()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Session cleanup failed")


# ── Helpers ──────────────────────────────────────────────────────────────

def _setup_logging(level: str, log_format: str = "text") -> None:
    log_level = getattr(logging, level.upper(), logging.INFO)
    if log_format == "json":
        # Structured JSON logging for production
        import json as _json

        class _JSONFormatter(logging.Formatter):
            def format(self, record: logging.LogRecord) -> str:
                entry = {
                    "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
                    "level": record.levelname,
                    "logger": record.name,
                    "msg": record.getMessage(),
                }
                if record.exc_info and record.exc_info[1]:
                    entry["exception"] = self.formatException(record.exc_info)
                return _json.dumps(entry)

        handler = logging.StreamHandler()
        handler.setFormatter(_JSONFormatter())
        logging.root.handlers.clear()
        logging.root.addHandler(handler)
        logging.root.setLevel(log_level)
    else:
        logging.basicConfig(
            level=log_level,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )


def _mask_dsn(dsn: str) -> str:
    """Mask password in a DSN for safe logging."""
    import re
    return re.sub(r"://([^:]+):([^@]+)@", r"://\1:***@", dsn)
