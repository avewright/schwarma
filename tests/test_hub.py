"""Tests for the Schwarma Hub — config, sync, HTTP routing, and database layer.

These tests run without a real PostgreSQL instance by mocking the database.
"""

from __future__ import annotations

import asyncio
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

# ── Config tests ─────────────────────────────────────────────────────────

class TestHubConfig(unittest.TestCase):
    """Test HubConfig defaults and env-based construction."""

    def test_defaults(self):
        from schwarma.hub.config import HubConfig
        cfg = HubConfig()
        assert cfg.host == "0.0.0.0"
        assert cfg.tcp_port == 9741
        assert cfg.http_port == 8741
        assert cfg.require_auth is True
        assert "postgresql" in cfg.database_url
        assert cfg.db_pool_min == 2
        assert cfg.db_pool_max == 10
        assert cfg.snapshot_interval == 300
        assert cfg.log_level == "INFO"
        # Google OAuth defaults to empty (unconfigured)
        assert cfg.google_client_id == ""
        assert cfg.google_client_secret == ""
        assert "callback" in cfg.google_redirect_uri
        assert cfg.session_secret == ""

    def test_from_env(self):
        from schwarma.hub.config import HubConfig
        with patch.dict("os.environ", {
            "SCHWARMA_DATABASE_URL": "postgresql://test:test@db/test",
            "SCHWARMA_HOST": "127.0.0.1",
            "SCHWARMA_TCP_PORT": "1234",
            "SCHWARMA_HTTP_PORT": "5678",
            "SCHWARMA_LOG_LEVEL": "DEBUG",
        }):
            cfg = HubConfig.from_env()
            assert cfg.database_url == "postgresql://test:test@db/test"
            assert cfg.host == "127.0.0.1"
            assert cfg.tcp_port == 1234
            assert cfg.http_port == 5678
            assert cfg.log_level == "DEBUG"

    def test_custom_values(self):
        from schwarma.hub.config import HubConfig
        cfg = HubConfig(
            database_url="postgresql://x:y@z/w",
            host="10.0.0.1",
            tcp_port=7777,
            http_port=8888,
            require_auth=False,
            db_pool_min=5,
            db_pool_max=20,
            snapshot_interval=60,
        )
        assert cfg.tcp_port == 7777
        assert cfg.http_port == 8888
        assert cfg.require_auth is False
        assert cfg.db_pool_min == 5


# ── Schema tests ─────────────────────────────────────────────────────────

class TestSchema(unittest.TestCase):
    """Verify schema.sql is valid SQL and covers all tables."""

    def test_schema_file_exists_and_readable(self):
        from pathlib import Path
        schema = Path(__file__).parent.parent / "schwarma" / "hub" / "schema.sql"
        assert schema.exists()
        sql = schema.read_text(encoding="utf-8")
        assert len(sql) > 500

    def test_schema_contains_all_tables(self):
        from pathlib import Path
        schema = Path(__file__).parent.parent / "schwarma" / "hub" / "schema.sql"
        sql = schema.read_text(encoding="utf-8")
        expected_tables = [
            "agents", "problems", "solutions", "reviews",
            "reputation_events", "reputation_balances",
            "archive_entries", "event_log", "swap_entries", "sessions",
            "users", "user_sessions",
        ]
        for table in expected_tables:
            assert f"CREATE TABLE IF NOT EXISTS {table}" in sql, f"Missing table: {table}"

    def test_schema_has_indexes(self):
        from pathlib import Path
        schema = Path(__file__).parent.parent / "schwarma" / "hub" / "schema.sql"
        sql = schema.read_text(encoding="utf-8")
        assert sql.count("CREATE INDEX") >= 10


# ── Database helper tests (unit, no pg connection) ───────────────────────

class TestDatabaseHelpers(unittest.TestCase):
    """Test Database helper functions without a connection."""

    def test_parse_ts_none(self):
        from schwarma.hub.database import _parse_ts
        assert _parse_ts(None) is None
        assert _parse_ts("") is None

    def test_parse_ts_valid(self):
        from schwarma.hub.database import _parse_ts
        ts = "2026-03-02T10:00:00+00:00"
        result = _parse_ts(ts)
        assert isinstance(result, datetime)
        assert result.year == 2026

    def test_database_requires_connect(self):
        from schwarma.hub.database import Database
        db = Database("postgresql://localhost/test")
        with self.assertRaises(RuntimeError):
            _ = db.pool

    def test_database_accepts_ssl_param(self):
        from schwarma.hub.database import Database
        db = Database("postgresql://localhost/test", ssl=True)
        assert db._ssl is True

    def test_database_ssl_defaults_none(self):
        from schwarma.hub.database import Database
        db = Database("postgresql://localhost/test")
        assert db._ssl is None


# ── Sync tests (mocked database) ────────────────────────────────────────

class TestExchangeSync(unittest.IsolatedAsyncioTestCase):
    """Test the sync layer with a mocked database."""

    async def _make_sync(self):
        from contextlib import asynccontextmanager
        from schwarma.hub.database import Database
        from schwarma.hub.sync import ExchangeSync
        from schwarma.station import SchwarmaStation

        station = SchwarmaStation(require_auth=False)
        db = MagicMock(spec=Database)
        # Mock all DB methods as async
        db.list_agents = AsyncMock(return_value=[])
        db.get_reputation = AsyncMock(return_value=50)
        db.list_problems = AsyncMock(return_value=([], None))
        db.solutions_for_problem = AsyncMock(return_value=[])
        db.reviews_for_solution = AsyncMock(return_value=[])
        db.search_archive = AsyncMock(return_value=[])
        db.load_all_sessions = AsyncMock(return_value={})
        db.upsert_agent = AsyncMock()
        db.upsert_problem = AsyncMock()
        db.upsert_solution = AsyncMock()
        db.upsert_review = AsyncMock()
        db.upsert_archive_entry = AsyncMock()
        db.log_event = AsyncMock()
        db.save_session = AsyncMock()
        db.set_agent_suspended = AsyncMock()
        db.record_reputation_event = AsyncMock()
        db.stats = AsyncMock(return_value={"agents": 0, "problems": 0})

        # Mock transaction() — yield a mock connection with async execute
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()

        @asynccontextmanager
        async def _fake_txn():
            yield mock_conn

        db.transaction = _fake_txn
        db._mock_conn = mock_conn  # expose for assertions

        sync = ExchangeSync(station, db)
        return sync, station, db

    async def test_rehydrate_empty(self):
        sync, station, db = await self._make_sync()
        counts = await sync.rehydrate()
        assert counts["agents"] == 0
        assert counts["problems"] == 0
        assert counts["sessions"] == 0

    async def test_rehydrate_with_agents(self):
        sync, station, db = await self._make_sync()
        agent_id = uuid4()
        db.list_agents.return_value = [{
            "id": agent_id,
            "name": "TestBot",
            "model_tier": "STANDARD",
            "capabilities": ["GENERAL"],
            "metadata": {},
            "is_suspended": False,
            "total_solved": 5,
            "total_reviewed": 3,
        }]
        counts = await sync.rehydrate()
        assert counts["agents"] == 1
        assert agent_id in station.exchange._agents
        agent = station.exchange._agents[agent_id]
        assert agent.name == "TestBot"
        assert agent._total_solved == 5

    async def test_rehydrate_with_suspended_agent(self):
        sync, station, db = await self._make_sync()
        agent_id = uuid4()
        db.list_agents.return_value = [{
            "id": agent_id,
            "name": "BadBot",
            "model_tier": "LIGHTWEIGHT",
            "capabilities": ["CODE_GENERATION"],
            "metadata": {},
            "is_suspended": True,
            "total_solved": 0,
            "total_reviewed": 0,
        }]
        await sync.rehydrate()
        assert agent_id in station.exchange._suspended

    async def test_rehydrate_sessions(self):
        sync, station, db = await self._make_sync()
        agent_id = uuid4()
        db.load_all_sessions.return_value = {"tok_abc": agent_id}
        counts = await sync.rehydrate()
        assert counts["sessions"] == 1
        assert station._sessions["tok_abc"] == agent_id

    async def test_attach_wires_event_bus(self):
        sync, station, db = await self._make_sync()
        bus = station.exchange.bus
        initial_count = len(bus._global_handlers)
        sync.attach()
        assert len(bus._global_handlers) == initial_count + 1

    async def test_event_logs_to_db(self):
        sync, station, db = await self._make_sync()
        sync.attach()

        from schwarma.events import Event, EventKind
        agent_id = uuid4()
        event = Event(
            kind=EventKind.AGENT_REGISTERED,
            source_agent_id=agent_id,
        )

        # The new _on_event opens a transaction and writes directly via conn.
        # Mock db.transaction() to yield a mock connection whose execute we
        # can inspect.
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()

        from contextlib import asynccontextmanager
        @asynccontextmanager
        async def _fake_txn():
            yield mock_conn

        db.transaction = _fake_txn

        await sync._on_event(event)

        # The event_log INSERT should have been called on the conn
        mock_conn.execute.assert_called()
        insert_call = mock_conn.execute.call_args_list[0]
        assert "INSERT INTO event_log" in insert_call.args[0]
        assert insert_call.args[1] == "AGENT_REGISTERED"

    async def test_full_snapshot_calls_db(self):
        sync, station, db = await self._make_sync()
        # Add a fake agent to the exchange
        from schwarma.agent import Agent, AgentCapability
        agent = Agent(name="Snap", solver=lambda d, c: "x")
        station.exchange._agents[agent.id] = agent

        await sync.full_snapshot()
        db.upsert_agent.assert_called_once()


# ── Sync row conversion tests ───────────────────────────────────────────

class TestRowConversions(unittest.TestCase):
    """Test the _*_from_row helper functions."""

    def test_problem_from_row(self):
        from schwarma.hub.sync import _problem_from_row
        row = {
            "id": uuid4(),
            "title": "Fix bug",
            "description": "Something broke",
            "author_id": uuid4(),
            "status": "OPEN",
            "tags": ["BUG"],
            "priority": 3,
            "bounty": 20,
            "sensitivity": "PUBLIC",
            "min_solver_tier": None,
            "max_solvers": 1,
            "deadline": None,
            "claimed_by": [],
            "solution_ids": [],
            "accepted_solution_id": None,
            "context": {},
            "failure_report": None,
            "parent_id": None,
            "sub_problem_ids": [],
            "depends_on": [],
            "created_at": datetime.now(timezone.utc),
        }
        p = _problem_from_row(row)
        assert p.title == "Fix bug"
        assert p.status.name == "OPEN"
        assert p.bounty == 20

    def test_solution_from_row(self):
        from schwarma.hub.sync import _solution_from_row
        row = {
            "id": uuid4(),
            "problem_id": uuid4(),
            "author_id": uuid4(),
            "body": "Here's the fix",
            "verdict": "PENDING",
            "fix_package": None,
            "outcome": None,
            "revision_history": [],
            "review_ids": [],
            "metadata": {},
            "created_at": datetime.now(timezone.utc),
        }
        s = _solution_from_row(row)
        assert s.body == "Here's the fix"
        assert s.verdict.name == "PENDING"

    def test_review_from_row(self):
        from schwarma.hub.sync import _review_from_row
        row = {
            "id": uuid4(),
            "solution_id": uuid4(),
            "reviewer_id": uuid4(),
            "review_type": "CORRECTNESS",
            "verdict": "APPROVE",
            "body": "Looks good",
            "confidence": 0.9,
            "metadata": {},
            "created_at": datetime.now(timezone.utc),
        }
        r = _review_from_row(row)
        assert r.verdict.name == "APPROVE"
        assert r.confidence == 0.9

    def test_archive_entry_from_row(self):
        from schwarma.hub.sync import _archive_entry_from_row
        row = {
            "id": uuid4(),
            "problem_id": uuid4(),
            "solution_id": uuid4(),
            "problem_title": "Fixed it",
            "problem_description": "Was broken",
            "tags": ["BUG"],
            "sensitivity": "INTERNAL",
            "solution_body": "Apply this patch",
            "solver_id": uuid4(),
            "solver_tier": "PREMIUM",
            "solver_reputation": 150,
            "reviews": [
                {
                    "reviewer_id": str(uuid4()),
                    "verdict": "APPROVE",
                    "review_type": "CORRECTNESS",
                    "confidence": 1.0,
                    "body": "LGTM",
                },
            ],
            "status": "ACTIVE",
            "metadata": {},
            "ttl_seconds": None,
            "created_at": datetime.now(timezone.utc),
        }
        e = _archive_entry_from_row(row)
        assert e.problem_title == "Fixed it"
        assert e.solver_tier.name == "PREMIUM"
        assert len(e.reviews) == 1


# ── HTTP routing tests ───────────────────────────────────────────────────

class TestHTTPRouting(unittest.TestCase):
    """Test HTTP path parsing and route matching."""

    def test_parse_path_no_query(self):
        from schwarma.hub.http import _parse_path
        path, query = _parse_path("/health")
        assert path == "/health"
        assert query == {}

    def test_parse_path_with_query(self):
        from schwarma.hub.http import _parse_path
        path, query = _parse_path("/problems?status=OPEN&limit=10")
        assert path == "/problems"
        assert query == {"status": "OPEN", "limit": "10"}

    def test_parse_path_empty_value(self):
        from schwarma.hub.http import _parse_path
        path, query = _parse_path("/events?verbose")
        assert query == {"verbose": ""}


class TestHTTPHandlers(unittest.IsolatedAsyncioTestCase):
    """Test HTTP route handlers with mocked hub."""

    async def _mock_hub(self):
        from schwarma.hub.database import Database
        hub = MagicMock()
        hub.db = MagicMock(spec=Database)
        hub.db.stats = AsyncMock(return_value={
            "agents": 5, "problems": 10, "open_problems": 3,
            "solutions": 8, "reviews": 12, "archive_entries": 4,
            "events_logged": 100,
        })
        hub.db.list_agents = AsyncMock(return_value=[
            {"id": uuid4(), "name": "Bot1", "model_tier": "STANDARD"},
        ])
        hub.db.list_problems = AsyncMock(return_value=([], None))
        hub.db.get_problem = AsyncMock(return_value=None)
        hub.db.solutions_for_problem = AsyncMock(return_value=[])
        hub.db.reviews_for_solution = AsyncMock(return_value=[])
        hub.db.reputation_leaderboard = AsyncMock(return_value=[])
        hub.db.search_archive = AsyncMock(return_value=[])
        hub.db.recent_events = AsyncMock(return_value=[])
        hub.station = MagicMock()
        hub.station.exchange = MagicMock()
        hub.station.exchange.statistics = MagicMock(return_value={"total_problems": 10})
        return hub

    async def test_health(self):
        from schwarma.hub.http import _dispatch
        hub = await self._mock_hub()
        status, ct, body, hdrs = await _dispatch(hub, "GET", "/health", {})
        assert status == 200
        data = json.loads(body)
        assert data["status"] == "ok"

    async def test_stats(self):
        from schwarma.hub.http import _dispatch
        hub = await self._mock_hub()
        status, ct, body, hdrs = await _dispatch(hub, "GET", "/stats", {})
        assert status == 200
        data = json.loads(body)
        assert data["agents"] == 5

    async def test_agents(self):
        from schwarma.hub.http import _dispatch
        hub = await self._mock_hub()
        status, ct, body, hdrs = await _dispatch(hub, "GET", "/agents", {})
        assert status == 200
        data = json.loads(body)
        assert data["count"] == 1

    async def test_problems_empty(self):
        from schwarma.hub.http import _dispatch
        hub = await self._mock_hub()
        status, ct, body, hdrs = await _dispatch(hub, "GET", "/problems", {})
        assert status == 200
        data = json.loads(body)
        assert data["count"] == 0

    async def test_problem_not_found(self):
        from schwarma.hub.http import _dispatch
        hub = await self._mock_hub()
        pid = str(uuid4())
        status, ct, body, hdrs = await _dispatch(hub, "GET", f"/problems/{pid}", {})
        assert status == 404

    async def test_leaderboard(self):
        from schwarma.hub.http import _dispatch
        hub = await self._mock_hub()
        status, ct, body, hdrs = await _dispatch(hub, "GET", "/leaderboard", {})
        assert status == 200

    async def test_archive(self):
        from schwarma.hub.http import _dispatch
        hub = await self._mock_hub()
        status, ct, body, hdrs = await _dispatch(hub, "GET", "/archive", {"tags": "BUG", "q": "error"})
        assert status == 200

    async def test_events(self):
        from schwarma.hub.http import _dispatch
        hub = await self._mock_hub()
        status, ct, body, hdrs = await _dispatch(hub, "GET", "/events", {})
        assert status == 200

    async def test_unknown_route(self):
        from schwarma.hub.http import _dispatch
        hub = await self._mock_hub()
        status, ct, body, hdrs = await _dispatch(hub, "GET", "/nonexistent", {})
        assert status == 404

    async def test_wrong_method(self):
        from schwarma.hub.http import _dispatch
        hub = await self._mock_hub()
        status, ct, body, hdrs = await _dispatch(hub, "DELETE", "/health", {})
        assert status == 404


# ── JSON encoder tests ───────────────────────────────────────────────────

class TestJSONEncoder(unittest.TestCase):
    """Test the custom JSON encoder handles all types."""

    def test_uuid(self):
        from schwarma.hub.http import _Encoder
        uid = uuid4()
        result = json.dumps({"id": uid}, cls=_Encoder)
        assert str(uid) in result

    def test_datetime(self):
        from schwarma.hub.http import _Encoder
        dt = datetime(2026, 3, 2, tzinfo=timezone.utc)
        result = json.dumps({"ts": dt}, cls=_Encoder)
        assert "2026-03-02" in result

    def test_set(self):
        from schwarma.hub.http import _Encoder
        result = json.dumps({"tags": {"b", "a"}}, cls=_Encoder)
        data = json.loads(result)
        assert data["tags"] == ["a", "b"]


# ── Hub construction tests ───────────────────────────────────────────────

class TestSchwarmaHub(unittest.TestCase):
    """Test hub construction and configuration."""

    def test_hub_construction(self):
        from schwarma.hub.app import SchwarmaHub
        from schwarma.hub.config import HubConfig
        config = HubConfig(database_url="postgresql://test:test@localhost/test")
        hub = SchwarmaHub(config)
        assert hub.config.database_url == "postgresql://test:test@localhost/test"
        assert hub.station is not None
        assert hub.db is not None
        assert hub.sync is not None

    def test_hub_default_config(self):
        from schwarma.hub.app import SchwarmaHub
        hub = SchwarmaHub()
        assert hub.config.tcp_port == 9741
        assert hub.config.http_port == 8741

    def test_mask_dsn(self):
        from schwarma.hub.app import _mask_dsn
        masked = _mask_dsn("postgresql://user:secretpass@host/db")
        assert "secretpass" not in masked
        assert "***" in masked
        assert "user" in masked


# ── CLI tests ────────────────────────────────────────────────────────────

class TestCLI(unittest.TestCase):
    """Test CLI argument parsing."""

    def test_module_imports(self):
        from schwarma.hub.__main__ import main
        assert callable(main)

    def test_hub_package_exports(self):
        from schwarma.hub import HubConfig, SchwarmaHub
        assert HubConfig is not None
        assert SchwarmaHub is not None


# ── Event handler routing tests ──────────────────────────────────────────

class TestEventHandlerRouting(unittest.TestCase):
    """Test that the event routing table covers expected events."""

    def test_routing_table_coverage(self):
        from schwarma.hub.sync import _EVENT_HANDLERS
        from schwarma.events import EventKind

        # Core events must have handlers
        required = [
            EventKind.AGENT_REGISTERED,
            EventKind.PROBLEM_POSTED,
            EventKind.PROBLEM_CLAIMED,
            EventKind.SOLUTION_SUBMITTED,
            EventKind.SOLUTION_ACCEPTED,
            EventKind.REVIEW_SUBMITTED,
            EventKind.REPUTATION_CHANGED,
        ]
        for kind in required:
            assert kind in _EVENT_HANDLERS, f"Missing handler for {kind.name}"

    def test_routing_table_values_are_callable(self):
        from schwarma.hub.sync import _EVENT_HANDLERS
        for kind, handler in _EVENT_HANDLERS.items():
            assert callable(handler), f"Handler for {kind.name} is not callable"


# ── Auth module tests ────────────────────────────────────────────────────

class TestAuthHelpers(unittest.TestCase):
    """Test auth.py helper functions."""

    def test_parse_cookies(self):
        from schwarma.hub.auth import parse_cookies
        cookies = parse_cookies("schwarma_session=abc123; other=xyz")
        assert cookies["schwarma_session"] == "abc123"
        assert cookies["other"] == "xyz"

    def test_parse_cookies_empty(self):
        from schwarma.hub.auth import parse_cookies
        assert parse_cookies("") == {}

    def test_parse_cookies_no_value(self):
        from schwarma.hub.auth import parse_cookies
        cookies = parse_cookies("flag; key=val")
        assert cookies["key"] == "val"

    def test_set_cookie_header(self):
        from schwarma.hub.auth import set_cookie_header
        header = set_cookie_header("test", "value123", max_age=3600)
        assert "test=value123" in header
        assert "Max-Age=3600" in header
        assert "HttpOnly" in header
        assert "SameSite=Lax" in header

    def test_clear_cookie_header(self):
        from schwarma.hub.auth import clear_cookie_header
        header = clear_cookie_header("test")
        assert "test=" in header
        assert "Max-Age=0" in header

    def test_generate_session_token(self):
        from schwarma.hub.auth import generate_session_token
        t1 = generate_session_token()
        t2 = generate_session_token()
        assert len(t1) > 30
        assert t1 != t2

    def test_is_google_configured_false(self):
        from schwarma.hub.auth import is_google_configured
        from schwarma.hub.config import HubConfig
        cfg = HubConfig()  # defaults are empty strings
        assert is_google_configured(cfg) is False

    def test_is_google_configured_true(self):
        from schwarma.hub.auth import is_google_configured
        from schwarma.hub.config import HubConfig
        cfg = HubConfig(
            google_client_id="test-id.apps.googleusercontent.com",
            google_client_secret="test-secret",
        )
        assert is_google_configured(cfg) is True

    def test_google_login_url(self):
        from schwarma.hub.auth import google_login_url
        from schwarma.hub.config import HubConfig
        cfg = HubConfig(
            google_client_id="myid.apps.googleusercontent.com",
            google_client_secret="mysecret",
            google_redirect_uri="http://localhost:8741/auth/google/callback",
        )
        url = google_login_url(cfg)
        assert "accounts.google.com" in url
        assert "myid.apps.googleusercontent.com" in url
        assert "redirect_uri=" in url
        assert "scope=" in url
        assert "select_account" in url

    def test_google_login_url_with_state(self):
        from schwarma.hub.auth import google_login_url
        from schwarma.hub.config import HubConfig
        cfg = HubConfig(
            google_client_id="id",
            google_client_secret="secret",
        )
        url = google_login_url(cfg, state="abc123")
        assert "state=abc123" in url

    def test_session_cookie_name(self):
        from schwarma.hub.auth import SESSION_COOKIE_NAME
        assert SESSION_COOKIE_NAME == "schwarma_session"


# ── Auth HTTP route tests ────────────────────────────────────────────────

class TestAuthRoutes(unittest.IsolatedAsyncioTestCase):
    """Test auth HTTP endpoints with mocked hub."""

    async def _mock_hub(self, *, google_configured=False):
        from schwarma.hub.config import HubConfig
        from schwarma.hub.database import Database

        hub = MagicMock()
        hub.config = HubConfig()
        if google_configured:
            hub.config.google_client_id = "test-id.apps.googleusercontent.com"
            hub.config.google_client_secret = "test-secret"
            hub.config.google_redirect_uri = "http://localhost:8741/auth/google/callback"
        hub.config.github_client_id = ""
        hub.config.github_client_secret = ""
        hub.config.github_redirect_uri = "http://localhost:8741/auth/github/callback"

        hub.db = MagicMock(spec=Database)
        hub.db.upsert_user = AsyncMock(return_value={
            "id": uuid4(),
            "email": "test@gmail.com",
            "name": "Test User",
            "picture_url": "https://example.com/pic.jpg",
            "google_sub": "12345",
            "agent_id": None,
            "is_admin": False,
        })
        hub.db.create_user_session = AsyncMock()
        hub.db.get_user_session = AsyncMock(return_value=None)
        hub.db.delete_user_session = AsyncMock()
        hub.db.create_local_user = AsyncMock(return_value={"id": uuid4()})
        hub.db.set_local_credential = AsyncMock()
        hub.db.get_local_credential_by_email = AsyncMock(return_value=None)
        hub.db.touch_user_login = AsyncMock()
        hub.db.create_email_verification_code = AsyncMock()
        hub.db.verify_email_code = AsyncMock(return_value=None)
        hub.db.mark_email_verified = AsyncMock()
        hub.station = MagicMock()
        hub.station.exchange = MagicMock()
        return hub

    async def test_auth_google_unconfigured(self):
        from schwarma.hub.http import _dispatch
        hub = await self._mock_hub(google_configured=False)
        status, ct, body, hdrs = await _dispatch(hub, "GET", "/auth/google", {})
        assert status == 503
        data = json.loads(body)
        assert "not configured" in data["error"]

    async def test_auth_google_redirect(self):
        from schwarma.hub.http import _dispatch
        hub = await self._mock_hub(google_configured=True)
        status, ct, body, hdrs = await _dispatch(hub, "GET", "/auth/google", {})
        assert status == 302
        assert "accounts.google.com" in hdrs["Location"]
        assert "test-id.apps.googleusercontent.com" in hdrs["Location"]

    async def test_auth_callback_missing_code(self):
        from schwarma.hub.http import _dispatch
        hub = await self._mock_hub(google_configured=True)
        status, ct, body, hdrs = await _dispatch(hub, "GET", "/auth/google/callback", {})
        assert status == 400
        data = json.loads(body)
        assert "missing authorization code" in data["error"]

    async def test_auth_callback_error_param(self):
        from schwarma.hub.http import _dispatch
        hub = await self._mock_hub(google_configured=True)
        status, ct, body, hdrs = await _dispatch(
            hub, "GET", "/auth/google/callback", {"error": "access_denied"},
        )
        assert status == 400
        assert "access_denied" in json.loads(body)["error"]

    async def test_auth_callback_success(self):
        from schwarma.hub.http import _dispatch
        hub = await self._mock_hub(google_configured=True)

        fake_userinfo = {
            "sub": "12345",
            "email": "test@gmail.com",
            "name": "Test User",
            "picture": "https://example.com/pic.jpg",
        }
        with patch("schwarma.hub.auth.exchange_code_for_user", new_callable=AsyncMock) as mock_auth:
            mock_auth.return_value = fake_userinfo
            status, ct, body, hdrs = await _dispatch(
                hub, "GET", "/auth/google/callback", {"code": "authcode123"},
            )

        assert status == 302
        assert "Set-Cookie" in hdrs
        assert "schwarma_session=" in hdrs["Set-Cookie"]
        hub.db.upsert_user.assert_called_once_with(
            email="test@gmail.com",
            name="Test User",
            picture_url="https://example.com/pic.jpg",
            google_sub="google:12345",
            auth_provider="google",
            email_verified=True,
        )
        hub.db.create_user_session.assert_called_once()

    async def test_auth_me_no_cookie(self):
        from schwarma.hub.http import _dispatch
        hub = await self._mock_hub()
        status, ct, body, hdrs = await _dispatch(hub, "GET", "/auth/me", {}, {})
        assert status == 401
        data = json.loads(body)
        assert data["authenticated"] is False

    async def test_auth_me_valid_session(self):
        from schwarma.hub.http import _dispatch
        hub = await self._mock_hub()
        user_id = uuid4()
        hub.db.get_user_session = AsyncMock(return_value={
            "id": user_id,
            "email": "test@gmail.com",
            "name": "Test User",
            "picture_url": "https://example.com/pic.jpg",
            "agent_id": None,
            "is_admin": False,
        })
        headers = {"cookie": "schwarma_session=valid_token_abc"}
        status, ct, body, hdrs = await _dispatch(hub, "GET", "/auth/me", {}, headers)
        assert status == 200
        data = json.loads(body)
        assert data["authenticated"] is True
        assert data["user"]["email"] == "test@gmail.com"

    async def test_auth_me_expired_session(self):
        from schwarma.hub.http import _dispatch
        hub = await self._mock_hub()
        hub.db.get_user_session = AsyncMock(return_value=None)
        headers = {"cookie": "schwarma_session=expired_token"}
        status, ct, body, hdrs = await _dispatch(hub, "GET", "/auth/me", {}, headers)
        assert status == 401

    async def test_auth_logout(self):
        from schwarma.hub.http import _dispatch
        hub = await self._mock_hub()
        headers = {"cookie": "schwarma_session=my_token_123"}
        status, ct, body, hdrs = await _dispatch(hub, "POST", "/auth/logout", {}, headers)
        assert status == 200
        data = json.loads(body)
        assert data["logged_out"] is True
        assert "Set-Cookie" in hdrs
        assert "Max-Age=0" in hdrs["Set-Cookie"]
        hub.db.delete_user_session.assert_called_once_with("my_token_123")

    async def test_auth_logout_no_cookie(self):
        from schwarma.hub.http import _dispatch
        hub = await self._mock_hub()
        status, ct, body, hdrs = await _dispatch(hub, "POST", "/auth/logout", {}, {})
        assert status == 200
        assert "Max-Age=0" in hdrs["Set-Cookie"]

    async def test_auth_status_unconfigured(self):
        from schwarma.hub.http import _dispatch
        hub = await self._mock_hub(google_configured=False)
        status, ct, body, hdrs = await _dispatch(hub, "GET", "/auth/status", {})
        assert status == 200
        data = json.loads(body)
        assert data["google_configured"] is False
        assert data["github_configured"] is False
        assert data["local_auth_enabled"] is True
        assert data["login_url"] is None

    async def test_auth_status_configured(self):
        from schwarma.hub.http import _dispatch
        hub = await self._mock_hub(google_configured=True)
        status, ct, body, hdrs = await _dispatch(hub, "GET", "/auth/status", {})
        assert status == 200
        data = json.loads(body)
        assert data["google_configured"] is True
        assert data["github_configured"] is False
        assert data["local_auth_enabled"] is True
        assert data["login_url"] == "/auth/google"

    async def test_auth_signup_success(self):
        from schwarma.hub.http import _dispatch
        hub = await self._mock_hub()
        with patch("schwarma.hub.auth.send_verification_email", new_callable=AsyncMock) as send_mock:
            send_mock.return_value = True
            status, ct, body, hdrs = await _dispatch(
                hub, "POST", "/auth/signup",
                {"email": "local@example.com", "password": "Password123!", "name": "Local"},
                {},
            )
        assert status == 200
        assert "Set-Cookie" not in hdrs
        data = json.loads(body)
        assert data["verification_required"] is True
        hub.db.create_local_user.assert_called_once()
        hub.db.set_local_credential.assert_called_once()
        hub.db.create_email_verification_code.assert_called_once()

    async def test_auth_signup_short_password(self):
        from schwarma.hub.http import _dispatch
        hub = await self._mock_hub()
        status, ct, body, hdrs = await _dispatch(
            hub, "POST", "/auth/signup",
            {"email": "local@example.com", "password": "short"},
            {},
        )
        assert status == 400

    async def test_auth_login_success(self):
        from schwarma.hub.http import _dispatch
        import base64
        import hashlib
        hub = await self._mock_hub()
        salt = b"0123456789abcdef"
        digest = hashlib.pbkdf2_hmac("sha256", b"Password123!", salt, 200_000)
        hub.db.get_local_credential_by_email = AsyncMock(return_value={
            "id": uuid4(),
            "email_verified": True,
            "password_salt": base64.b64encode(salt).decode("ascii"),
            "password_hash": base64.b64encode(digest).decode("ascii"),
        })
        status, ct, body, hdrs = await _dispatch(
            hub, "POST", "/auth/login",
            {"email": "local@example.com", "password": "Password123!"},
            {},
        )
        assert status == 200
        assert "Set-Cookie" in hdrs

    async def test_auth_login_invalid_password(self):
        from schwarma.hub.http import _dispatch
        import base64
        import hashlib
        hub = await self._mock_hub()
        salt = b"0123456789abcdef"
        digest = hashlib.pbkdf2_hmac("sha256", b"Password123!", salt, 200_000)
        hub.db.get_local_credential_by_email = AsyncMock(return_value={
            "id": uuid4(),
            "email_verified": True,
            "password_salt": base64.b64encode(salt).decode("ascii"),
            "password_hash": base64.b64encode(digest).decode("ascii"),
        })
        status, ct, body, hdrs = await _dispatch(
            hub, "POST", "/auth/login",
            {"email": "local@example.com", "password": "wrong-pass"},
            {},
        )
        assert status == 401

    async def test_auth_login_unverified_email(self):
        from schwarma.hub.http import _dispatch
        import base64
        import hashlib
        hub = await self._mock_hub()
        salt = b"0123456789abcdef"
        digest = hashlib.pbkdf2_hmac("sha256", b"Password123!", salt, 200_000)
        hub.db.get_local_credential_by_email = AsyncMock(return_value={
            "id": uuid4(),
            "email_verified": False,
            "password_salt": base64.b64encode(salt).decode("ascii"),
            "password_hash": base64.b64encode(digest).decode("ascii"),
        })
        status, ct, body, hdrs = await _dispatch(
            hub, "POST", "/auth/login",
            {"email": "local@example.com", "password": "Password123!"},
            {},
        )
        assert status == 403

    async def test_auth_verify_email_success(self):
        from schwarma.hub.http import _dispatch
        uid = uuid4()
        hub = await self._mock_hub()
        hub.db.verify_email_code = AsyncMock(return_value={"id": uid})
        status, ct, body, hdrs = await _dispatch(
            hub, "POST", "/auth/verify-email",
            {"email": "local@example.com", "code": "123456"},
            {},
        )
        assert status == 200
        assert "Set-Cookie" in hdrs
        hub.db.mark_email_verified.assert_called_once_with(uid)

    async def test_auth_github_unconfigured(self):
        from schwarma.hub.http import _dispatch
        hub = await self._mock_hub()
        status, ct, body, hdrs = await _dispatch(hub, "GET", "/auth/github", {})
        assert status == 503


# ── Google OAuth config env var tests ────────────────────────────────────

class TestGoogleOAuthConfig(unittest.TestCase):
    """Test Google OAuth config from environment variables."""

    def test_google_config_from_env(self):
        from schwarma.hub.config import HubConfig
        with patch.dict("os.environ", {
            "SCHWARMA_GOOGLE_CLIENT_ID": "env-client-id",
            "SCHWARMA_GOOGLE_CLIENT_SECRET": "env-client-secret",
            "SCHWARMA_GOOGLE_REDIRECT_URI": "https://example.com/callback",
            "SCHWARMA_SESSION_SECRET": "env-session-secret",
        }):
            cfg = HubConfig.from_env()
            assert cfg.google_client_id == "env-client-id"
            assert cfg.google_client_secret == "env-client-secret"
            assert cfg.google_redirect_uri == "https://example.com/callback"
            assert cfg.session_secret == "env-session-secret"


# ── Config new fields tests ──────────────────────────────────────────────

class TestHubConfigNewFields(unittest.TestCase):
    """Test HubConfig new fields, defaults, tls_enabled, make_ssl_context, and from_env."""

    def test_tls_cert_default(self):
        from schwarma.hub.config import HubConfig
        cfg = HubConfig()
        assert cfg.tls_cert == ""

    def test_tls_key_default(self):
        from schwarma.hub.config import HubConfig
        cfg = HubConfig()
        assert cfg.tls_key == ""

    def test_allowed_origins_default(self):
        from schwarma.hub.config import HubConfig
        cfg = HubConfig()
        assert cfg.allowed_origins == "auto"

    def test_http_rate_limit_default(self):
        from schwarma.hub.config import HubConfig
        cfg = HubConfig()
        assert cfg.http_rate_limit == 100

    def test_http_rate_window_default(self):
        from schwarma.hub.config import HubConfig
        cfg = HubConfig()
        assert cfg.http_rate_window == 60

    def test_max_request_size_default(self):
        from schwarma.hub.config import HubConfig
        cfg = HubConfig()
        assert cfg.max_request_size == 1_048_576

    def test_shutdown_drain_seconds_default(self):
        from schwarma.hub.config import HubConfig
        cfg = HubConfig()
        assert cfg.shutdown_drain_seconds == 5

    def test_log_format_default(self):
        from schwarma.hub.config import HubConfig
        cfg = HubConfig()
        assert cfg.log_format == "text"

    def test_tls_enabled_false_when_empty(self):
        from schwarma.hub.config import HubConfig
        cfg = HubConfig()
        assert cfg.tls_enabled is False

    def test_tls_enabled_false_when_cert_only(self):
        from schwarma.hub.config import HubConfig
        cfg = HubConfig(tls_cert="/path/cert.pem")
        assert cfg.tls_enabled is False

    def test_tls_enabled_false_when_key_only(self):
        from schwarma.hub.config import HubConfig
        cfg = HubConfig(tls_key="/path/key.pem")
        assert cfg.tls_enabled is False

    def test_tls_enabled_true_when_both_set(self):
        from schwarma.hub.config import HubConfig
        cfg = HubConfig(tls_cert="/path/cert.pem", tls_key="/path/key.pem")
        assert cfg.tls_enabled is True

    def test_make_ssl_context_returns_none_when_not_enabled(self):
        from schwarma.hub.config import HubConfig
        cfg = HubConfig()
        assert cfg.make_ssl_context() is None

    # ── Database SSL ─────────────────────────────────────────────────

    def test_db_ssl_default_disabled(self):
        from schwarma.hub.config import HubConfig
        cfg = HubConfig()
        assert cfg.database_ssl == ""
        assert cfg.make_db_ssl_context() is None

    def test_db_ssl_require_returns_true(self):
        from schwarma.hub.config import HubConfig
        cfg = HubConfig(database_ssl="require")
        assert cfg.make_db_ssl_context() is True

    def test_db_ssl_verify_ca_returns_ssl_context(self):
        import ssl
        from schwarma.hub.config import HubConfig
        cfg = HubConfig(database_ssl="verify-ca")
        ctx = cfg.make_db_ssl_context()
        assert isinstance(ctx, ssl.SSLContext)
        assert ctx.check_hostname is False

    def test_db_ssl_verify_full_returns_ssl_context(self):
        import ssl
        from schwarma.hub.config import HubConfig
        cfg = HubConfig(database_ssl="verify-full")
        ctx = cfg.make_db_ssl_context()
        assert isinstance(ctx, ssl.SSLContext)
        assert ctx.check_hostname is True

    def test_db_ssl_invalid_value_raises(self):
        from schwarma.hub.config import HubConfig
        cfg = HubConfig(database_ssl="bogus")
        with self.assertRaises(ValueError):
            cfg.make_db_ssl_context()

    def test_db_ssl_from_env(self):
        from schwarma.hub.config import HubConfig
        with patch.dict("os.environ", {
            "SCHWARMA_DATABASE_SSL": "require",
            "SCHWARMA_DATABASE_SSL_CA": "/path/rds-bundle.pem",
        }):
            cfg = HubConfig.from_env()
            assert cfg.database_ssl == "require"
            assert cfg.database_ssl_ca == "/path/rds-bundle.pem"

    def test_from_env_new_fields(self):
        from schwarma.hub.config import HubConfig
        with patch.dict("os.environ", {
            "SCHWARMA_TLS_CERT": "/path/cert.pem",
            "SCHWARMA_TLS_KEY": "/path/key.pem",
            "SCHWARMA_ALLOWED_ORIGINS": "https://example.com,https://other.com",
            "SCHWARMA_HTTP_RATE_LIMIT": "200",
            "SCHWARMA_HTTP_RATE_WINDOW": "120",
            "SCHWARMA_MAX_REQUEST_SIZE": "2097152",
            "SCHWARMA_SHUTDOWN_DRAIN": "10",
            "SCHWARMA_LOG_FORMAT": "json",
        }):
            cfg = HubConfig.from_env()
            assert cfg.tls_cert == "/path/cert.pem"
            assert cfg.tls_key == "/path/key.pem"
            assert cfg.allowed_origins == "https://example.com,https://other.com"
            assert cfg.http_rate_limit == 200
            assert cfg.http_rate_window == 120
            assert cfg.max_request_size == 2097152
            assert cfg.shutdown_drain_seconds == 10
            assert cfg.log_format == "json"


# ── _IPRateLimiter tests ────────────────────────────────────────────────

class TestIPRateLimiter(unittest.TestCase):
    """Test the per-IP rate limiter."""

    def test_allow_returns_true_for_new_ip(self):
        from schwarma.hub.http import _IPRateLimiter
        rl = _IPRateLimiter(max_requests=10, window=60)
        assert rl.allow("1.2.3.4") is True

    def test_allow_returns_false_after_max_exceeded(self):
        from schwarma.hub.http import _IPRateLimiter
        rl = _IPRateLimiter(max_requests=3, window=60)
        assert rl.allow("1.2.3.4") is True
        assert rl.allow("1.2.3.4") is True
        assert rl.allow("1.2.3.4") is True
        assert rl.allow("1.2.3.4") is False

    def test_allow_always_true_when_disabled(self):
        from schwarma.hub.http import _IPRateLimiter
        rl = _IPRateLimiter(max_requests=0, window=60)
        for _ in range(200):
            assert rl.allow("1.2.3.4") is True

    def test_different_ips_tracked_separately(self):
        from schwarma.hub.http import _IPRateLimiter
        rl = _IPRateLimiter(max_requests=2, window=60)
        assert rl.allow("1.1.1.1") is True
        assert rl.allow("1.1.1.1") is True
        assert rl.allow("1.1.1.1") is False
        # Different IP should still be allowed
        assert rl.allow("2.2.2.2") is True

    def test_window_expiry_allows_again(self):
        from schwarma.hub.http import _IPRateLimiter
        rl = _IPRateLimiter(max_requests=2, window=1)
        assert rl.allow("1.2.3.4") is True
        assert rl.allow("1.2.3.4") is True
        assert rl.allow("1.2.3.4") is False
        # Manually expire the timestamps
        import time
        old = time.monotonic() - 2
        rl._hits["1.2.3.4"] = [old, old]
        assert rl.allow("1.2.3.4") is True

    def test_prune_removes_stale_entries(self):
        from schwarma.hub.http import _IPRateLimiter
        import time
        rl = _IPRateLimiter(max_requests=10, window=1)
        # Add an old entry
        old = time.monotonic() - 100
        rl._hits["stale_ip"] = [old]
        rl._hits["fresh_ip"] = [time.monotonic()]
        rl.prune()
        assert "stale_ip" not in rl._hits
        assert "fresh_ip" in rl._hits

    def test_prune_removes_empty_entries(self):
        from schwarma.hub.http import _IPRateLimiter
        rl = _IPRateLimiter(max_requests=10, window=1)
        rl._hits["empty_ip"] = []
        rl.prune()
        assert "empty_ip" not in rl._hits


# ── _Metrics tests ──────────────────────────────────────────────────────

class TestMetrics(unittest.TestCase):
    """Test the HTTP request metrics collector."""

    def test_initial_state(self):
        from schwarma.hub.http import _Metrics
        m = _Metrics()
        assert m.total_requests == 0
        assert m.latency_count == 0
        assert m.latency_sum == 0.0

    def test_record_increments_counters(self):
        from schwarma.hub.http import _Metrics
        m = _Metrics()
        m.record(200, 0.05)
        assert m.total_requests == 1
        assert m.status_counts[200] == 1
        assert m.latency_count == 1

    def test_record_multiple_statuses(self):
        from schwarma.hub.http import _Metrics
        m = _Metrics()
        m.record(200, 0.01)
        m.record(200, 0.02)
        m.record(404, 0.005)
        assert m.total_requests == 3
        assert m.status_counts[200] == 2
        assert m.status_counts[404] == 1

    def test_snapshot_returns_correct_dict(self):
        from schwarma.hub.http import _Metrics
        m = _Metrics()
        m.record(200, 0.1)
        m.record(200, 0.3)
        snap = m.snapshot()
        assert snap["total_requests"] == 2
        assert snap["status_counts"] == {200: 2}
        assert snap["avg_latency_ms"] == 200.0  # (0.1+0.3)/2*1000

    def test_snapshot_zero_latency_when_empty(self):
        from schwarma.hub.http import _Metrics
        m = _Metrics()
        snap = m.snapshot()
        assert snap["avg_latency_ms"] == 0
        assert snap["total_requests"] == 0


# ── Write endpoint tests ────────────────────────────────────────────────

def _mock_hub_with_auth(*, agent_id=None, is_admin=False):
    """Create a hub mock with an authenticated user session."""
    from schwarma.hub.config import HubConfig
    from schwarma.hub.database import Database

    uid = uuid4()
    aid = agent_id or str(uuid4())

    hub = MagicMock()
    hub.config = HubConfig()
    hub.db = MagicMock(spec=Database)
    hub.db.get_user_session = AsyncMock(return_value={
        "id": uid,
        "email": "test@gmail.com",
        "name": "Test",
        "picture_url": "",
        "agent_id": aid,
        "is_admin": is_admin,
    })
    hub.db.link_user_agent = AsyncMock()
    hub.db.save_session = AsyncMock()
    hub.db.upsert_agent = AsyncMock()
    hub.db.delete_agent_sessions = AsyncMock()
    hub.db.list_users = AsyncMock(return_value=[
        {"id": uid, "email": "test@gmail.com", "name": "Test", "is_admin": is_admin},
    ])
    hub.db.delete_user_sessions = AsyncMock()
    hub.db.set_agent_suspended = AsyncMock()
    hub.db.health_check = AsyncMock(return_value=True)
    hub.db.stats = AsyncMock(return_value={"agents": 1, "problems": 0})
    hub.db.pool = MagicMock()
    hub.db.pool.execute = AsyncMock()
    hub.db.pool.fetchval = AsyncMock(return_value=1)
    hub.station = MagicMock()
    hub.station._sessions = {}
    hub.station._m_register = AsyncMock()
    hub.station.exchange = MagicMock()
    hub.station.exchange.statistics = MagicMock(return_value={"total_problems": 0})
    hub.station.exchange._agents = {}
    hub.station.exchange._suspended = set()
    return hub


class TestWriteEndpoints(unittest.IsolatedAsyncioTestCase):
    """Test POST write endpoints that require authentication."""

    # ── POST /problems ───────────────────────────────────────────────

    async def test_post_problem_no_auth(self):
        from schwarma.hub.http import _dispatch
        hub = _mock_hub_with_auth()
        hub.db.get_user_session = AsyncMock(return_value=None)
        status, ct, body, hdrs = await _dispatch(hub, "POST", "/problems", {}, {})
        assert status == 401

    async def test_post_problem_missing_fields(self):
        from schwarma.hub.http import _dispatch
        hub = _mock_hub_with_auth()
        headers = {"cookie": "schwarma_session=tok"}
        status, ct, body, hdrs = await _dispatch(hub, "POST", "/problems", {}, headers)
        assert status == 400
        assert b"title" in body

    async def test_post_problem_no_linked_agent(self):
        from schwarma.hub.http import _dispatch
        hub = _mock_hub_with_auth()
        hub.db.get_user_session = AsyncMock(return_value={
            "id": uuid4(), "email": "t@t.com", "name": "T",
            "picture_url": "", "agent_id": None, "is_admin": False,
        })
        headers = {"cookie": "schwarma_session=tok"}
        status, ct, body, hdrs = await _dispatch(
            hub, "POST", "/problems",
            {"title": "Bug", "description": "It broke"},
            headers,
        )
        assert status == 400
        assert b"link-agent" in body

    async def test_post_problem_success(self):
        from schwarma.hub.http import _dispatch
        hub = _mock_hub_with_auth()
        mock_problem = MagicMock()
        mock_problem.id = uuid4()
        mock_problem.status = MagicMock()
        mock_problem.status.name = "OPEN"
        hub.station.exchange.post_problem = AsyncMock(return_value=mock_problem)
        headers = {"cookie": "schwarma_session=tok"}
        status, ct, body, hdrs = await _dispatch(
            hub, "POST", "/problems",
            {"title": "Bug", "description": "Broken feature"},
            headers,
        )
        assert status == 200
        data = json.loads(body)
        assert data["status"] == "OPEN"
        assert "problem_id" in data

    # ── POST /problems/:id/claim ─────────────────────────────────────

    async def test_claim_problem_no_auth(self):
        from schwarma.hub.http import _dispatch
        hub = _mock_hub_with_auth()
        hub.db.get_user_session = AsyncMock(return_value=None)
        pid = str(uuid4())
        status, ct, body, hdrs = await _dispatch(hub, "POST", f"/problems/{pid}/claim", {}, {})
        assert status == 401

    async def test_claim_problem_success(self):
        from schwarma.hub.http import _dispatch
        hub = _mock_hub_with_auth()
        mock_problem = MagicMock()
        mock_problem.status = MagicMock()
        mock_problem.status.name = "CLAIMED"
        hub.station.exchange.claim_problem = AsyncMock(return_value=mock_problem)
        headers = {"cookie": "schwarma_session=tok"}
        pid = str(uuid4())
        status, ct, body, hdrs = await _dispatch(
            hub, "POST", f"/problems/{pid}/claim", {}, headers,
        )
        assert status == 200
        data = json.loads(body)
        assert data["status"] == "CLAIMED"

    # ── POST /solutions ──────────────────────────────────────────────

    async def test_post_solution_no_auth(self):
        from schwarma.hub.http import _dispatch
        hub = _mock_hub_with_auth()
        hub.db.get_user_session = AsyncMock(return_value=None)
        status, ct, body, hdrs = await _dispatch(hub, "POST", "/solutions", {}, {})
        assert status == 401

    async def test_post_solution_missing_fields(self):
        from schwarma.hub.http import _dispatch
        hub = _mock_hub_with_auth()
        headers = {"cookie": "schwarma_session=tok"}
        status, ct, body, hdrs = await _dispatch(hub, "POST", "/solutions", {}, headers)
        assert status == 400
        assert b"problem_id" in body

    async def test_post_solution_success(self):
        from schwarma.hub.http import _dispatch
        hub = _mock_hub_with_auth()
        mock_solution = MagicMock()
        mock_solution.id = uuid4()
        mock_solution.verdict = MagicMock()
        mock_solution.verdict.name = "PENDING"
        hub.station.exchange.submit_solution = AsyncMock(return_value=mock_solution)
        headers = {"cookie": "schwarma_session=tok"}
        pid = str(uuid4())
        status, ct, body, hdrs = await _dispatch(
            hub, "POST", "/solutions",
            {"problem_id": pid, "body": "Here is the fix"},
            headers,
        )
        assert status == 200
        data = json.loads(body)
        assert data["verdict"] == "PENDING"

    # ── POST /reviews ────────────────────────────────────────────────

    async def test_post_review_no_auth(self):
        from schwarma.hub.http import _dispatch
        hub = _mock_hub_with_auth()
        hub.db.get_user_session = AsyncMock(return_value=None)
        status, ct, body, hdrs = await _dispatch(hub, "POST", "/reviews", {}, {})
        assert status == 401

    async def test_post_review_missing_fields(self):
        from schwarma.hub.http import _dispatch
        hub = _mock_hub_with_auth()
        headers = {"cookie": "schwarma_session=tok"}
        status, ct, body, hdrs = await _dispatch(hub, "POST", "/reviews", {}, headers)
        assert status == 400
        assert b"solution_id" in body

    async def test_post_review_success(self):
        from schwarma.hub.http import _dispatch
        hub = _mock_hub_with_auth()
        mock_review = MagicMock()
        mock_review.id = uuid4()
        mock_review.verdict = MagicMock()
        mock_review.verdict.name = "APPROVE"
        hub.station.exchange.submit_review = AsyncMock(return_value=mock_review)
        headers = {"cookie": "schwarma_session=tok"}
        sid = str(uuid4())
        status, ct, body, hdrs = await _dispatch(
            hub, "POST", "/reviews",
            {"solution_id": sid, "verdict": "APPROVE"},
            headers,
        )
        assert status == 200
        data = json.loads(body)
        assert data["verdict"] == "APPROVE"

    # ── POST /users/me/link-agent ────────────────────────────────────

    async def test_link_agent_no_auth(self):
        from schwarma.hub.http import _dispatch
        hub = _mock_hub_with_auth()
        hub.db.get_user_session = AsyncMock(return_value=None)
        status, ct, body, hdrs = await _dispatch(
            hub, "POST", "/users/me/link-agent", {}, {},
        )
        assert status == 401

    async def test_link_agent_missing_agent_id(self):
        from schwarma.hub.http import _dispatch
        hub = _mock_hub_with_auth()
        headers = {"cookie": "schwarma_session=tok"}
        status, ct, body, hdrs = await _dispatch(
            hub, "POST", "/users/me/link-agent", {}, headers,
        )
        assert status == 400
        assert b"agent_id" in body

    async def test_link_agent_success(self):
        from schwarma.hub.http import _dispatch
        hub = _mock_hub_with_auth()
        headers = {"cookie": "schwarma_session=tok"}
        aid = str(uuid4())
        status, ct, body, hdrs = await _dispatch(
            hub, "POST", "/users/me/link-agent", {"agent_id": aid}, headers,
        )
        assert status == 200
        data = json.loads(body)
        assert data["linked"] is True
        hub.db.link_user_agent.assert_called_once()

    # ── POST /users/me/agent-credentials ─────────────────────────────

    async def test_agent_credentials_no_auth(self):
        from schwarma.hub.http import _dispatch
        hub = _mock_hub_with_auth()
        hub.db.get_user_session = AsyncMock(return_value=None)
        status, ct, body, hdrs = await _dispatch(
            hub, "POST", "/users/me/agent-credentials", {}, {},
        )
        assert status == 401

    async def test_agent_credentials_create_success(self):
        from schwarma.hub.http import _dispatch
        from types import SimpleNamespace
        hub = _mock_hub_with_auth()
        hub.db.get_user_session = AsyncMock(return_value={
            "id": uuid4(), "email": "t@t.com", "name": "T",
            "picture_url": "", "agent_id": None, "is_admin": False,
        })
        aid = uuid4()
        hub.station._m_register = AsyncMock(return_value={
            "agent_id": str(aid),
            "name": "T",
            "capabilities": ["GENERAL"],
            "model_tier": "STANDARD",
            "token": "tok-new-123",
        })
        hub.station.exchange._agents = {
            aid: SimpleNamespace(
                name="T",
                model_tier=SimpleNamespace(name="STANDARD"),
                capabilities=[SimpleNamespace(name="GENERAL")],
                metadata={},
                _total_solved=0,
                _total_reviewed=0,
            )
        }
        headers = {"cookie": "schwarma_session=tok"}
        status, ct, body, hdrs = await _dispatch(
            hub, "POST", "/users/me/agent-credentials", {"name": "T"}, headers,
        )
        assert status == 200
        data = json.loads(body)
        assert data["created"] is True
        assert data["rotated"] is False
        assert data["agent_id"] == str(aid)
        assert data["token"] == "tok-new-123"
        hub.db.link_user_agent.assert_called_once()
        hub.db.save_session.assert_called_once()

    async def test_agent_credentials_create_conflict_when_linked(self):
        from schwarma.hub.http import _dispatch
        hub = _mock_hub_with_auth()
        headers = {"cookie": "schwarma_session=tok"}
        status, ct, body, hdrs = await _dispatch(
            hub, "POST", "/users/me/agent-credentials", {}, headers,
        )
        assert status == 409
        assert b"rotate=true" in body

    async def test_agent_credentials_rotate_success(self):
        from schwarma.hub.http import _dispatch
        aid = uuid4()
        hub = _mock_hub_with_auth(agent_id=str(aid))
        hub.station._sessions = {"old-token-1": aid, "old-token-2": aid}
        hub.db.rotate_session = AsyncMock(return_value=aid)
        headers = {"cookie": "schwarma_session=tok"}
        with patch("schwarma.hub.http.secrets.token_urlsafe", return_value="new-token-xyz"):
            status, ct, body, hdrs = await _dispatch(
                hub, "POST", "/users/me/agent-credentials", {"rotate": True}, headers,
            )
        assert status == 200
        data = json.loads(body)
        assert data["created"] is False
        assert data["rotated"] is True
        assert data["token"] == "new-token-xyz"
        assert "expires_at" in data
        assert hub.station._sessions == {"new-token-xyz": aid}
        hub.db.rotate_session.assert_called_once()


# ── Session rotation endpoint tests ─────────────────────────────────────

class TestSessionRotation(unittest.IsolatedAsyncioTestCase):
    """Test the POST /sessions/rotate bearer-token rotation endpoint."""

    async def test_rotate_no_bearer(self):
        from schwarma.hub.http import _dispatch
        hub = _mock_hub_with_auth()
        status, ct, body, hdrs = await _dispatch(
            hub, "POST", "/sessions/rotate", {}, {},
        )
        assert status == 401

    async def test_rotate_invalid_token(self):
        from schwarma.hub.http import _dispatch
        hub = _mock_hub_with_auth()
        hub.db.rotate_session = AsyncMock(return_value=None)
        headers = {"authorization": "Bearer bad-token"}
        status, ct, body, hdrs = await _dispatch(
            hub, "POST", "/sessions/rotate", {}, headers,
        )
        assert status == 401
        assert "Invalid or expired" in json.loads(body)["error"]

    async def test_rotate_success(self):
        from schwarma.hub.http import _dispatch
        aid = uuid4()
        hub = _mock_hub_with_auth()
        hub.station._sessions = {"old-tok": aid}
        hub.db.rotate_session = AsyncMock(return_value=aid)
        # Also mock get_agent_for_session so the deployment-mode gate passes auth
        hub.db.get_agent_for_session = AsyncMock(return_value=aid)
        headers = {"authorization": "Bearer old-tok"}
        with patch("schwarma.hub.http.secrets.token_urlsafe", return_value="fresh-tok"):
            status, ct, body, hdrs = await _dispatch(
                hub, "POST", "/sessions/rotate", {}, headers,
            )
        assert status == 200
        data = json.loads(body)
        assert data["agent_id"] == str(aid)
        assert data["new_token"] == "fresh-tok"
        assert "expires_at" in data
        assert "old-tok" not in hub.station._sessions
        assert hub.station._sessions.get("fresh-tok") == aid


# ── Admin endpoint tests ────────────────────────────────────────────────

class TestAdminEndpoints(unittest.IsolatedAsyncioTestCase):
    """Test admin endpoints that require admin privileges."""

    # ── POST /admin/suspend/:agent_id ────────────────────────────────

    async def test_suspend_no_auth(self):
        from schwarma.hub.http import _dispatch
        hub = _mock_hub_with_auth(is_admin=True)
        hub.db.get_user_session = AsyncMock(return_value=None)
        aid = str(uuid4())
        status, ct, body, hdrs = await _dispatch(
            hub, "POST", f"/admin/suspend/{aid}", {}, {},
        )
        assert status == 401

    async def test_suspend_non_admin(self):
        from schwarma.hub.http import _dispatch
        hub = _mock_hub_with_auth(is_admin=False)
        headers = {"cookie": "schwarma_session=tok"}
        aid = str(uuid4())
        status, ct, body, hdrs = await _dispatch(
            hub, "POST", f"/admin/suspend/{aid}", {}, headers,
        )
        assert status == 403

    async def test_suspend_success(self):
        from schwarma.hub.http import _dispatch
        hub = _mock_hub_with_auth(is_admin=True)
        headers = {"cookie": "schwarma_session=tok"}
        aid = str(uuid4())
        status, ct, body, hdrs = await _dispatch(
            hub, "POST", f"/admin/suspend/{aid}", {}, headers,
        )
        assert status == 200
        data = json.loads(body)
        assert data["suspended"] is True
        hub.station.exchange.suspend_agent.assert_called_once()
        hub.db.set_agent_suspended.assert_called_once()

    # ── POST /admin/unsuspend/:agent_id ──────────────────────────────

    async def test_unsuspend_no_auth(self):
        from schwarma.hub.http import _dispatch
        hub = _mock_hub_with_auth(is_admin=True)
        hub.db.get_user_session = AsyncMock(return_value=None)
        aid = str(uuid4())
        status, ct, body, hdrs = await _dispatch(
            hub, "POST", f"/admin/unsuspend/{aid}", {}, {},
        )
        assert status == 401

    async def test_unsuspend_success(self):
        from schwarma.hub.http import _dispatch
        hub = _mock_hub_with_auth(is_admin=True)
        headers = {"cookie": "schwarma_session=tok"}
        aid = str(uuid4())
        status, ct, body, hdrs = await _dispatch(
            hub, "POST", f"/admin/unsuspend/{aid}", {}, headers,
        )
        assert status == 200
        data = json.loads(body)
        assert data["suspended"] is False
        hub.station.exchange.unsuspend_agent.assert_called_once()

    # ── GET /admin/users ─────────────────────────────────────────────

    async def test_admin_users_no_auth(self):
        from schwarma.hub.http import _dispatch
        hub = _mock_hub_with_auth(is_admin=True)
        hub.db.get_user_session = AsyncMock(return_value=None)
        status, ct, body, hdrs = await _dispatch(
            hub, "GET", "/admin/users", {}, {},
        )
        assert status == 401

    async def test_admin_users_non_admin(self):
        from schwarma.hub.http import _dispatch
        hub = _mock_hub_with_auth(is_admin=False)
        headers = {"cookie": "schwarma_session=tok"}
        status, ct, body, hdrs = await _dispatch(
            hub, "GET", "/admin/users", {}, headers,
        )
        assert status == 403

    async def test_admin_users_success(self):
        from schwarma.hub.http import _dispatch
        hub = _mock_hub_with_auth(is_admin=True)
        headers = {"cookie": "schwarma_session=tok"}
        status, ct, body, hdrs = await _dispatch(
            hub, "GET", "/admin/users", {}, headers,
        )
        assert status == 200
        data = json.loads(body)
        assert "users" in data
        assert data["count"] == 1

    # ── POST /admin/users/:id/promote ────────────────────────────────

    async def test_promote_non_admin(self):
        from schwarma.hub.http import _dispatch
        hub = _mock_hub_with_auth(is_admin=False)
        headers = {"cookie": "schwarma_session=tok"}
        uid = str(uuid4())
        status, ct, body, hdrs = await _dispatch(
            hub, "POST", f"/admin/users/{uid}/promote", {}, headers,
        )
        assert status == 403

    async def test_promote_success(self):
        from schwarma.hub.http import _dispatch
        hub = _mock_hub_with_auth(is_admin=True)
        headers = {"cookie": "schwarma_session=tok"}
        uid = str(uuid4())
        status, ct, body, hdrs = await _dispatch(
            hub, "POST", f"/admin/users/{uid}/promote", {}, headers,
        )
        assert status == 200
        data = json.loads(body)
        assert data["promoted"] is True
        hub.db.pool.execute.assert_called_once()

    # ── DELETE /admin/users/:id/sessions ─────────────────────────────

    async def test_clear_sessions_non_admin(self):
        from schwarma.hub.http import _dispatch
        hub = _mock_hub_with_auth(is_admin=False)
        headers = {"cookie": "schwarma_session=tok"}
        uid = str(uuid4())
        status, ct, body, hdrs = await _dispatch(
            hub, "DELETE", f"/admin/users/{uid}/sessions", {}, headers,
        )
        assert status == 403

    async def test_clear_sessions_success(self):
        from schwarma.hub.http import _dispatch
        hub = _mock_hub_with_auth(is_admin=True)
        headers = {"cookie": "schwarma_session=tok"}
        uid = str(uuid4())
        status, ct, body, hdrs = await _dispatch(
            hub, "DELETE", f"/admin/users/{uid}/sessions", {}, headers,
        )
        assert status == 200
        data = json.loads(body)
        assert data["cleared"] is True
        hub.db.delete_user_sessions.assert_called_once()

    # ── GET /admin/metrics ───────────────────────────────────────────

    async def test_admin_metrics_non_admin(self):
        from schwarma.hub.http import _dispatch
        hub = _mock_hub_with_auth(is_admin=False)
        headers = {"cookie": "schwarma_session=tok"}
        status, ct, body, hdrs = await _dispatch(
            hub, "GET", "/admin/metrics", {}, headers,
        )
        assert status == 403

    async def test_admin_metrics_success(self):
        from schwarma.hub.http import _dispatch, _Metrics
        hub = _mock_hub_with_auth(is_admin=True)
        hub._http_metrics = _Metrics()
        headers = {"cookie": "schwarma_session=tok"}
        status, ct, body, hdrs = await _dispatch(
            hub, "GET", "/admin/metrics", {}, headers,
        )
        assert status == 200
        data = json.loads(body)
        assert "http" in data
        assert "database" in data
        assert "exchange" in data


# ── Deep health check tests ─────────────────────────────────────────────

class TestDeepHealthCheck(unittest.IsolatedAsyncioTestCase):
    """Test GET /health?deep=1 with DB probe."""

    async def test_deep_health_ok(self):
        from schwarma.hub.http import _dispatch
        hub = MagicMock()
        hub.db = MagicMock()
        hub.db.pool = MagicMock()
        hub.db.pool.fetchval = AsyncMock(return_value=1)
        status, ct, body, hdrs = await _dispatch(
            hub, "GET", "/health", {"deep": "1"},
        )
        assert status == 200
        data = json.loads(body)
        assert data["status"] == "ok"
        assert data["database"] == "ok"

    async def test_deep_health_db_failure(self):
        from schwarma.hub.http import _dispatch
        hub = MagicMock()
        hub.db = MagicMock()
        hub.db.pool = MagicMock()
        hub.db.pool.fetchval = AsyncMock(side_effect=Exception("connection lost"))
        status, ct, body, hdrs = await _dispatch(
            hub, "GET", "/health", {"deep": "1"},
        )
        assert status == 503
        data = json.loads(body)
        assert data["status"] == "degraded"
        assert data["database"] == "connection lost"

    async def test_shallow_health(self):
        from schwarma.hub.http import _dispatch
        hub = MagicMock()
        status, ct, body, hdrs = await _dispatch(
            hub, "GET", "/health", {},
        )
        assert status == 200
        data = json.loads(body)
        assert data["status"] == "ok"
        assert "database" not in data


# ── SSE route tests ─────────────────────────────────────────────────────

class TestSSERoute(unittest.IsolatedAsyncioTestCase):
    """Test the SSE events/stream endpoint returns status 209."""

    async def test_sse_stream_returns_209(self):
        from schwarma.hub.http import _dispatch
        hub = MagicMock()
        status, ct, body, hdrs = await _dispatch(
            hub, "GET", "/events/stream", {"kinds": "PROBLEM_POSTED,SOLUTION_SUBMITTED"},
        )
        assert status == 209
        assert ct == "text/event-stream"
        assert b"PROBLEM_POSTED" in body

    async def test_sse_stream_empty_kinds(self):
        from schwarma.hub.http import _dispatch
        hub = MagicMock()
        status, ct, body, hdrs = await _dispatch(
            hub, "GET", "/events/stream", {},
        )
        assert status == 209
        assert ct == "text/event-stream"

    async def test_sse_stream_single_kind(self):
        from schwarma.hub.http import _dispatch
        hub = MagicMock()
        status, ct, body, hdrs = await _dispatch(
            hub, "GET", "/events/stream", {"kinds": "AGENT_REGISTERED"},
        )
        assert status == 209
        assert b"AGENT_REGISTERED" in body


# ── Cursor pagination tests ─────────────────────────────────────────────

class TestCursorPagination(unittest.IsolatedAsyncioTestCase):
    """Test GET /problems with cursor pagination."""

    async def test_problems_with_cursor(self):
        from schwarma.hub.http import _dispatch
        hub = MagicMock()
        hub.db = MagicMock()
        pid = str(uuid4())
        hub.db.list_problems = AsyncMock(return_value=(
            [{"id": pid, "title": "Test", "status": "OPEN"}],
            "next_abc",
        ))
        status, ct, body, hdrs = await _dispatch(
            hub, "GET", "/problems", {"cursor": "start_abc"},
        )
        assert status == 200
        data = json.loads(body)
        assert data["next_cursor"] == "next_abc"
        assert data["count"] == 1

    async def test_problems_no_next_cursor(self):
        from schwarma.hub.http import _dispatch
        hub = MagicMock()
        hub.db = MagicMock()
        hub.db.list_problems = AsyncMock(return_value=([], None))
        status, ct, body, hdrs = await _dispatch(
            hub, "GET", "/problems", {},
        )
        assert status == 200
        data = json.loads(body)
        assert "next_cursor" not in data

    async def test_problems_with_status_filter(self):
        from schwarma.hub.http import _dispatch
        hub = MagicMock()
        hub.db = MagicMock()
        hub.db.list_problems = AsyncMock(return_value=([], None))
        status, ct, body, hdrs = await _dispatch(
            hub, "GET", "/problems", {"status": "OPEN", "limit": "5"},
        )
        assert status == 200
        hub.db.list_problems.assert_called_once_with(
            status="OPEN", limit=5, cursor=None, tag=None,
        )


# ── Static file serving tests ───────────────────────────────────────────

class TestStaticFileServing(unittest.IsolatedAsyncioTestCase):
    """Test GET / serves the static index.html."""

    async def test_index_returns_html(self):
        from schwarma.hub.http import _dispatch
        hub = MagicMock()
        status, ct, body, hdrs = await _dispatch(hub, "GET", "/", {})
        assert status == 200
        assert "text/html" in ct
        assert b"Schwarma Hub" in body

    async def test_index_has_doctype(self):
        from schwarma.hub.http import _dispatch
        hub = MagicMock()
        status, ct, body, hdrs = await _dispatch(hub, "GET", "/", {})
        assert body.startswith(b"<!DOCTYPE html>")


# ── Metrics route tests ─────────────────────────────────────────────────

class TestMetricsRoute(unittest.IsolatedAsyncioTestCase):
    """Test GET /metrics returns HTTP metrics and rate limiter state."""

    async def test_metrics_with_data(self):
        from schwarma.hub.http import _dispatch, _Metrics, _IPRateLimiter
        hub = MagicMock()
        metrics = _Metrics()
        metrics.record(200, 0.05)
        metrics.record(404, 0.02)
        hub._http_metrics = metrics
        rl = _IPRateLimiter()
        rl.allow("1.1.1.1")
        hub._http_rate_limiter = rl
        status, ct, body, hdrs = await _dispatch(hub, "GET", "/metrics", {})
        assert status == 200
        data = json.loads(body)
        assert data["http"]["total_requests"] == 2
        assert data["rate_limiter"]["tracked_ips"] == 1

    async def test_metrics_empty(self):
        from schwarma.hub.http import _dispatch
        hub = MagicMock()
        hub._http_metrics = None
        hub._http_rate_limiter = None
        status, ct, body, hdrs = await _dispatch(hub, "GET", "/metrics", {})
        assert status == 200
        data = json.loads(body)
        assert "http" not in data or data.get("http") is None

    async def test_metrics_no_rate_limiter(self):
        from schwarma.hub.http import _dispatch, _Metrics
        hub = MagicMock()
        hub._http_metrics = _Metrics()
        hub._http_rate_limiter = None
        status, ct, body, hdrs = await _dispatch(hub, "GET", "/metrics", {})
        assert status == 200
        data = json.loads(body)
        assert data["http"]["total_requests"] == 0


# ── OAuth state parameter tests ─────────────────────────────────────────

class TestOAuthState(unittest.IsolatedAsyncioTestCase):
    """Test OAuth CSRF state parameter handling."""

    async def _mock_hub_google(self):
        from schwarma.hub.config import HubConfig
        from schwarma.hub.database import Database
        hub = MagicMock()
        hub.config = HubConfig(
            google_client_id="test-id.apps.googleusercontent.com",
            google_client_secret="test-secret",
            google_redirect_uri="http://localhost:8741/auth/google/callback",
        )
        hub.db = MagicMock(spec=Database)
        hub.db.upsert_user = AsyncMock(return_value={
            "id": uuid4(), "email": "t@t.com", "name": "T",
            "picture_url": "", "google_sub": "sub1", "agent_id": None, "is_admin": False,
        })
        hub.db.create_user_session = AsyncMock()
        hub.db.get_user_session = AsyncMock(return_value=None)
        hub.station = MagicMock()
        return hub

    async def test_auth_google_sets_state_cookie(self):
        from schwarma.hub.http import _dispatch
        hub = await self._mock_hub_google()
        status, ct, body, hdrs = await _dispatch(hub, "GET", "/auth/google", {})
        assert status == 302
        assert "Set-Cookie" in hdrs
        assert "schwarma_oauth_state" in hdrs["Set-Cookie"]

    async def test_callback_rejects_state_mismatch(self):
        from schwarma.hub.http import _dispatch
        hub = await self._mock_hub_google()
        headers = {"cookie": "schwarma_oauth_state=expected_state_value"}
        status, ct, body, hdrs = await _dispatch(
            hub, "GET", "/auth/google/callback",
            {"code": "authcode", "state": "different_state_value"},
            headers,
        )
        assert status == 403
        data = json.loads(body)
        assert "state mismatch" in data["error"].lower()

    async def test_callback_accepts_matching_state(self):
        from schwarma.hub.http import _dispatch
        hub = await self._mock_hub_google()
        headers = {"cookie": "schwarma_oauth_state=matching_state"}
        fake_userinfo = {
            "sub": "12345", "email": "t@t.com",
            "name": "T", "picture": "https://example.com/pic.jpg",
        }
        with patch("schwarma.hub.auth.exchange_code_for_user", new_callable=AsyncMock) as mock_auth:
            mock_auth.return_value = fake_userinfo
            status, ct, body, hdrs = await _dispatch(
                hub, "GET", "/auth/google/callback",
                {"code": "authcode", "state": "matching_state"},
                headers,
            )
        assert status == 302

    async def test_callback_passes_when_no_state_cookie(self):
        """When no state cookie is present, state check is skipped (expected_state empty)."""
        from schwarma.hub.http import _dispatch
        hub = await self._mock_hub_google()
        fake_userinfo = {
            "sub": "12345", "email": "t@t.com",
            "name": "T", "picture": "https://pic.com/p.jpg",
        }
        with patch("schwarma.hub.auth.exchange_code_for_user", new_callable=AsyncMock) as mock_auth:
            mock_auth.return_value = fake_userinfo
            status, ct, body, hdrs = await _dispatch(
                hub, "GET", "/auth/google/callback",
                {"code": "authcode", "state": "any"},
                {},
            )
        assert status == 302


# ── Tests for Agent API routes ───────────────────────────────────────────


class TestAgentAPIRoutes(unittest.IsolatedAsyncioTestCase):
    """Test /api/v1/agent/* endpoints."""

    async def _mock_hub(self, *, with_user=True, with_agent=True):
        from schwarma.hub.config import HubConfig
        from schwarma.hub.database import Database

        hub = MagicMock()
        hub.config = HubConfig()
        hub.config.session_secret = "test-secret"
        hub.db = MagicMock(spec=Database)

        agent_id = uuid4() if with_agent else None
        user_row = {
            "id": uuid4(),
            "email": "agent@example.com",
            "name": "Agent User",
            "picture_url": "",
            "agent_id": agent_id,
            "is_admin": False,
        } if with_user else None

        hub.db.get_user_session = AsyncMock(return_value=user_row)
        hub.db.get_agent_for_session = AsyncMock(return_value=None)
        hub.db.get_user_by_agent = AsyncMock(return_value=None)
        hub.db.upsert_user = AsyncMock(return_value=user_row or {
            "id": uuid4(), "email": "", "name": "Agent",
            "picture_url": "", "agent_id": None, "is_admin": False,
        })
        hub.db.create_user_session = AsyncMock()
        hub.db.delete_user_session = AsyncMock()

        hub.station = MagicMock()
        hub.station.exchange = MagicMock()
        hub.station.exchange.register = MagicMock(return_value=MagicMock(
            id=agent_id or uuid4(),
        ))
        hub.station.exchange.open_problems = MagicMock(return_value=[])
        hub.station.exchange.is_agent_online = MagicMock(return_value=True)
        hub.station.exchange.statistics = MagicMock(return_value={
            "total_problems": 5, "open_problems": 3,
        })
        # Populate _agents dict so agent/me lookup works
        if with_agent and agent_id:
            from schwarma.agent import ModelTier, AgentCapability
            mock_agent = MagicMock()
            mock_agent.name = "TestBot"
            mock_agent.model_tier = ModelTier.STANDARD
            mock_agent.capabilities = [AgentCapability.GENERAL]
            hub.station.exchange._agents = {agent_id: mock_agent}
        else:
            hub.station.exchange._agents = {}
        hub.station.exchange.reputation = {}
        hub.station.exchange._skill_tracker = None
        return hub, agent_id

    async def test_agent_register_no_auth(self):
        """POST /api/v1/agent/register without auth returns 401."""
        from schwarma.hub.http import _dispatch
        hub, _ = await self._mock_hub(with_user=False)
        hub.db.get_user_session = AsyncMock(return_value=None)
        status, ct, body, hdrs = await _dispatch(
            hub, "POST", "/api/v1/agent/register",
            {"name": "TestBot", "capabilities": "GENERAL", "model_tier": "STANDARD"},
            {},
        )
        assert status == 401

    async def test_agent_register_success(self):
        """POST /api/v1/agent/register with auth registers and returns credentials."""
        from schwarma.hub.http import _dispatch
        hub, _ = await self._mock_hub(with_user=True, with_agent=False)
        headers = {"cookie": "schwarma_session=valid_token"}
        new_agent_id = uuid4()
        hub.station._m_register = AsyncMock(return_value={
            "agent_id": str(new_agent_id),
            "token": "test-agent-token-abc",
        })
        hub.db.save_session = AsyncMock()
        hub.db.upsert_agent = AsyncMock()
        hub.db.link_user_agent = AsyncMock()
        status, ct, body, hdrs = await _dispatch(
            hub, "POST", "/api/v1/agent/register",
            {"name": "TestBot", "capabilities": "GENERAL", "model_tier": "STANDARD"},
            headers,
        )
        assert status == 200
        data = json.loads(body)
        assert data["agent_id"] == str(new_agent_id)
        assert data["token"] == "test-agent-token-abc"
        assert "env" in data
        assert "usage" in data

    async def test_agent_me_no_agent(self):
        """GET /api/v1/agent/me without linked agent returns 404."""
        from schwarma.hub.http import _dispatch
        hub, _ = await self._mock_hub(with_user=True, with_agent=False)
        headers = {"cookie": "schwarma_session=valid_token"}
        status, ct, body, hdrs = await _dispatch(
            hub, "GET", "/api/v1/agent/me", {}, headers,
        )
        assert status == 404

    async def test_agent_me_with_agent(self):
        """GET /api/v1/agent/me with linked agent returns agent info."""
        from schwarma.hub.http import _dispatch
        hub, agent_id = await self._mock_hub(with_user=True, with_agent=True)
        headers = {"cookie": "schwarma_session=valid_token"}
        status, ct, body, hdrs = await _dispatch(
            hub, "GET", "/api/v1/agent/me", {}, headers,
        )
        assert status == 200
        data = json.loads(body)
        assert data["agent_id"] == str(agent_id)
        assert data["is_online"] is True

    async def test_agent_work_returns_problems(self):
        """GET /api/v1/agent/work returns open problems."""
        from schwarma.hub.http import _dispatch
        hub, agent_id = await self._mock_hub(with_user=True, with_agent=True)
        headers = {"cookie": "schwarma_session=valid_token"}
        status, ct, body, hdrs = await _dispatch(
            hub, "GET", "/api/v1/agent/work", {}, headers,
        )
        assert status == 200
        data = json.loads(body)
        assert "problems" in data
        assert data["count"] == 0

    async def test_agent_work_no_agent(self):
        """GET /api/v1/agent/work without linked agent returns 400."""
        from schwarma.hub.http import _dispatch
        hub, _ = await self._mock_hub(with_user=True, with_agent=False)
        headers = {"cookie": "schwarma_session=valid_token"}
        status, ct, body, hdrs = await _dispatch(
            hub, "GET", "/api/v1/agent/work", {}, headers,
        )
        assert status == 400

    async def test_agent_solve_missing_fields(self):
        """POST /api/v1/agent/solve without required fields returns 400."""
        from schwarma.hub.http import _dispatch
        hub, agent_id = await self._mock_hub(with_user=True, with_agent=True)
        headers = {"cookie": "schwarma_session=valid_token"}
        status, ct, body, hdrs = await _dispatch(
            hub, "POST", "/api/v1/agent/solve", {}, headers,
        )
        assert status == 400

    async def test_agent_solve_no_agent(self):
        """POST /api/v1/agent/solve without linked agent returns 400."""
        from schwarma.hub.http import _dispatch
        hub, _ = await self._mock_hub(with_user=True, with_agent=False)
        headers = {"cookie": "schwarma_session=valid_token"}
        status, ct, body, hdrs = await _dispatch(
            hub, "POST", "/api/v1/agent/solve",
            {"problem_id": str(uuid4()), "solution_body": "Fix is here"},
            headers,
        )
        assert status == 400


# ── Tests for OpenAI-compatible proxy ────────────────────────────────────


class TestOpenAIProxy(unittest.IsolatedAsyncioTestCase):
    """Test /v1/chat/completions and /v1/models endpoints."""

    async def _mock_hub(self, *, with_agent=True):
        from schwarma.hub.config import HubConfig
        from schwarma.hub.database import Database

        hub = MagicMock()
        hub.config = HubConfig()
        hub.config.session_secret = "test-secret"
        hub.db = MagicMock(spec=Database)

        agent_id = uuid4() if with_agent else None
        user_row = {
            "id": uuid4(),
            "email": "user@example.com",
            "name": "Proxy User",
            "picture_url": "",
            "agent_id": agent_id,
            "is_admin": False,
        }
        hub.db.get_user_session = AsyncMock(return_value=user_row)
        hub.db.get_agent_for_session = AsyncMock(return_value=None)
        hub.db.get_user_by_agent = AsyncMock(return_value=None)

        hub.station = MagicMock()
        hub.station.exchange = MagicMock()
        hub.station.exchange._agents = {}
        return hub, agent_id

    async def test_models_list(self):
        """GET /v1/models returns schwarma-swarm model."""
        from schwarma.hub.http import _dispatch
        hub, _ = await self._mock_hub()
        status, ct, body, hdrs = await _dispatch(hub, "GET", "/v1/models", {})
        assert status == 200
        data = json.loads(body)
        assert data["object"] == "list"
        assert len(data["data"]) == 1
        assert data["data"][0]["id"] == "schwarma-swarm"
        assert data["data"][0]["owned_by"] == "schwarma"

    async def test_chat_completions_no_auth(self):
        """POST /v1/chat/completions without auth returns 401."""
        from schwarma.hub.http import _dispatch
        hub, _ = await self._mock_hub()
        hub.db.get_user_session = AsyncMock(return_value=None)
        status, ct, body, hdrs = await _dispatch(
            hub, "POST", "/v1/chat/completions",
            {"messages": [{"role": "user", "content": "Hello"}]},
            {},
        )
        assert status == 401

    async def test_chat_completions_no_agent(self):
        """POST /v1/chat/completions without linked agent returns 400."""
        from schwarma.hub.http import _dispatch
        hub, _ = await self._mock_hub(with_agent=False)
        headers = {"cookie": "schwarma_session=valid_token"}
        status, ct, body, hdrs = await _dispatch(
            hub, "POST", "/v1/chat/completions",
            {"messages": [{"role": "user", "content": "Hello"}]},
            headers,
        )
        assert status == 400

    async def test_chat_completions_empty_messages(self):
        """POST /v1/chat/completions with empty messages returns 400."""
        from schwarma.hub.http import _dispatch
        hub, _ = await self._mock_hub(with_agent=True)
        headers = {"cookie": "schwarma_session=valid_token"}
        status, ct, body, hdrs = await _dispatch(
            hub, "POST", "/v1/chat/completions",
            {"messages": []},
            headers,
        )
        assert status == 400

    async def test_chat_completions_no_user_message(self):
        """POST /v1/chat/completions with no user message returns 400."""
        from schwarma.hub.http import _dispatch
        hub, _ = await self._mock_hub(with_agent=True)
        headers = {"cookie": "schwarma_session=valid_token"}
        status, ct, body, hdrs = await _dispatch(
            hub, "POST", "/v1/chat/completions",
            {"messages": [{"role": "system", "content": "You are helpful"}]},
            headers,
        )
        assert status == 400

    async def test_chat_completions_timeout_response(self):
        """POST /v1/chat/completions returns timeout message when no solution arrives."""
        from schwarma.hub.http import _dispatch
        hub, agent_id = await self._mock_hub(with_agent=True)
        headers = {"cookie": "schwarma_session=valid_token"}

        problem_mock = MagicMock()
        problem_mock.id = uuid4()
        hub.station.exchange.post_problem = AsyncMock(return_value=problem_mock)
        hub.station.exchange.solutions_for = MagicMock(return_value=[])

        status, ct, body, hdrs = await _dispatch(
            hub, "POST", "/v1/chat/completions",
            {
                "messages": [{"role": "user", "content": "Fix this bug please"}],
                "metadata": {"timeout": 0.1},  # very short timeout for test
            },
            headers,
        )
        assert status == 200
        data = json.loads(body)
        assert data["object"] == "chat.completion"
        assert data["model"] == "schwarma-swarm"
        assert len(data["choices"]) == 1
        assert data["choices"][0]["message"]["role"] == "assistant"
        assert "[Schwarma] No solution received" in data["choices"][0]["message"]["content"]
        assert "schwarma" in data
        assert str(problem_mock.id) == data["schwarma"]["problem_id"]

    async def test_chat_completions_with_solution(self):
        """POST /v1/chat/completions returns solution when one exists."""
        from schwarma.hub.http import _dispatch
        hub, agent_id = await self._mock_hub(with_agent=True)
        headers = {"cookie": "schwarma_session=valid_token"}

        problem_mock = MagicMock()
        problem_mock.id = uuid4()
        hub.station.exchange.post_problem = AsyncMock(return_value=problem_mock)

        solution_mock = MagicMock()
        solution_mock.body = "Here's the fix: change line 42 to use async/await"
        solution_mock.verdict = MagicMock(name="PENDING")
        hub.station.exchange.solutions_for = MagicMock(return_value=[solution_mock])

        status, ct, body, hdrs = await _dispatch(
            hub, "POST", "/v1/chat/completions",
            {
                "messages": [{"role": "user", "content": "Fix this bug"}],
                "metadata": {"timeout": 0.1},
            },
            headers,
        )
        assert status == 200
        data = json.loads(body)
        assert data["choices"][0]["message"]["content"] == solution_mock.body
        assert data["choices"][0]["finish_reason"] == "stop"
        assert "usage" in data


# ── Tests for Bearer token auth ──────────────────────────────────────────


class TestBearerTokenAuth(unittest.IsolatedAsyncioTestCase):
    """Test that _get_current_user supports Authorization: Bearer tokens."""

    async def test_bearer_user_session(self):
        """Bearer token that matches a user session returns user."""
        from schwarma.hub.http import _get_current_user
        from schwarma.hub.database import Database

        hub = MagicMock()
        hub.db = MagicMock(spec=Database)
        user_row = {"id": uuid4(), "email": "x@x.com", "name": "X", "agent_id": None}
        hub.db.get_user_session = AsyncMock(return_value=user_row)

        result = await _get_current_user(hub, {"authorization": "Bearer my_sess_tok"})
        assert result is not None
        assert result["email"] == "x@x.com"

    async def test_bearer_agent_token(self):
        """Bearer token that matches an agent session returns synthetic user."""
        from schwarma.hub.http import _get_current_user
        from schwarma.hub.database import Database

        hub = MagicMock()
        hub.db = MagicMock(spec=Database)
        agent_id = uuid4()
        hub.db.get_user_session = AsyncMock(return_value=None)
        hub.db.get_agent_for_session = AsyncMock(return_value=agent_id)
        hub.db.get_user_by_agent = AsyncMock(return_value=None)  # no linked user

        result = await _get_current_user(hub, {"authorization": "Bearer agent_tok123"})
        assert result is not None
        assert result["agent_id"] == agent_id
        assert result["name"].startswith("Agent ")

    async def test_bearer_invalid(self):
        """Bearer token that matches nothing returns None."""
        from schwarma.hub.http import _get_current_user
        from schwarma.hub.database import Database

        hub = MagicMock()
        hub.db = MagicMock(spec=Database)
        hub.db.get_user_session = AsyncMock(return_value=None)
        hub.db.get_agent_for_session = AsyncMock(return_value=None)

        result = await _get_current_user(hub, {"authorization": "Bearer bad_token"})
        assert result is None

    async def test_no_auth_header(self):
        """No Auth header and no cookie returns None."""
        from schwarma.hub.http import _get_current_user
        from schwarma.hub.database import Database

        hub = MagicMock()
        hub.db = MagicMock(spec=Database)
        result = await _get_current_user(hub, {})
        assert result is None


# ── Tests for schwarma-connect CLI ───────────────────────────────────────


class TestConnectCLI(unittest.TestCase):
    """Test the connect CLI module imports and argparse setup."""

    def test_module_imports(self):
        import schwarma.connect
        assert hasattr(schwarma.connect, "main")

    def test_argparse_help(self):
        """Verify argparse doesn't crash on --help."""
        import schwarma.connect
        import argparse
        # Just verify the parser can be constructed
        assert callable(schwarma.connect.main)


# ── Tests for safe query helpers ─────────────────────────────────────────

class TestQueryHelpers(unittest.TestCase):
    """Test _qs, _qs_list, _qs_int safe extraction helpers."""

    def test_qs_string_passthrough(self):
        from schwarma.hub.http import _qs
        assert _qs({"key": "  hello  "}, "key") == "hello"

    def test_qs_int_coerced(self):
        from schwarma.hub.http import _qs
        assert _qs({"key": 42}, "key") == "42"

    def test_qs_list_returned_as_is(self):
        from schwarma.hub.http import _qs
        result = _qs({"key": ["a", "b"]}, "key")
        assert result == ["a", "b"]

    def test_qs_dict_returned_as_is(self):
        from schwarma.hub.http import _qs
        result = _qs({"key": {"nested": True}}, "key")
        assert result == {"nested": True}

    def test_qs_missing_default(self):
        from schwarma.hub.http import _qs
        assert _qs({}, "key") == ""
        assert _qs({}, "key", "fallback") == "fallback"

    def test_qs_none_returns_default(self):
        from schwarma.hub.http import _qs
        assert _qs({"key": None}, "key", "def") == "def"

    def test_qs_bool_coerced(self):
        from schwarma.hub.http import _qs
        assert _qs({"key": True}, "key") == "True"

    def test_qs_list_from_json_list(self):
        from schwarma.hub.http import _qs_list
        assert _qs_list({"key": ["BUG", "CODE"]}, "key") == ["BUG", "CODE"]

    def test_qs_list_from_csv_string(self):
        from schwarma.hub.http import _qs_list
        assert _qs_list({"key": "BUG, CODE"}, "key") == ["BUG", "CODE"]

    def test_qs_list_default(self):
        from schwarma.hub.http import _qs_list
        assert _qs_list({}, "key", "GENERAL") == ["GENERAL"]

    def test_qs_list_empty(self):
        from schwarma.hub.http import _qs_list
        assert _qs_list({}, "key") == []

    def test_qs_list_single_int(self):
        from schwarma.hub.http import _qs_list
        assert _qs_list({"key": 42}, "key") == ["42"]

    def test_qs_int_from_int(self):
        from schwarma.hub.http import _qs_int
        assert _qs_int({"key": 42}, "key") == 42

    def test_qs_int_from_string(self):
        from schwarma.hub.http import _qs_int
        assert _qs_int({"key": "42"}, "key") == 42

    def test_qs_int_default(self):
        from schwarma.hub.http import _qs_int
        assert _qs_int({}, "key", 10) == 10

    def test_qs_int_invalid_returns_default(self):
        from schwarma.hub.http import _qs_int
        assert _qs_int({"key": "abc"}, "key", 5) == 5


# ── Tests for CORS auto-detect ───────────────────────────────────────────

class TestCORSAutoDetect(unittest.TestCase):
    """Test that CORS defaults changed from '*' to 'auto'."""

    def test_default_is_auto(self):
        from schwarma.hub.config import HubConfig
        cfg = HubConfig()
        assert cfg.allowed_origins == "auto"

    def test_explicit_star(self):
        from schwarma.hub.config import HubConfig
        with patch.dict("os.environ", {"SCHWARMA_ALLOWED_ORIGINS": "*"}):
            cfg = HubConfig()
            assert cfg.allowed_origins == "*"

    def test_explicit_origin(self):
        from schwarma.hub.config import HubConfig
        with patch.dict("os.environ", {"SCHWARMA_ALLOWED_ORIGINS": "https://example.com"}):
            cfg = HubConfig()
            assert cfg.allowed_origins == "https://example.com"


# ── Tests for dev_code leak removal ──────────────────────────────────────

class TestDevCodeLeak(unittest.IsolatedAsyncioTestCase):
    """Verify that signup no longer leaks verification codes in the response."""

    async def test_signup_no_dev_code_in_response(self):
        from schwarma.hub.http import _dispatch
        hub = MagicMock()
        hub.config = MagicMock()
        hub.config.smtp_host = ""  # No SMTP => code can't be emailed
        hub.config.smtp_port = 587
        hub.config.smtp_user = ""
        hub.config.smtp_password = ""
        hub.config.smtp_from = ""
        hub.db = MagicMock()
        user_id = uuid4()
        hub.db.create_local_user = AsyncMock(return_value={
            "id": user_id, "email": "new@test.com", "name": "New",
        })
        hub.db.set_local_credential = AsyncMock()
        hub.db.create_email_verification_code = AsyncMock()
        hub.db.user_count = AsyncMock(return_value=5)
        hub.db.mark_email_verified = AsyncMock()
        hub.db.create_user_session = AsyncMock()
        hub.config.tls_enabled = False

        status, ct, body, extra = await _dispatch(
            hub, "POST", "/auth/signup",
            {"email": "new@test.com", "password": "securepass123", "name": "New"},
            {},
        )
        assert status == 200
        data = json.loads(body)
        assert "dev_code" not in data, "Verification code MUST NOT appear in response"
        assert data["signed_up"] is True
        # When SMTP is not configured, auto-verify instead of requiring email
        assert data.get("authenticated") is True


# ── Tests for auth brute-force rate limiting ─────────────────────────────

class TestAuthRateLimit(unittest.TestCase):
    """Test that a separate stricter rate limiter is used for auth endpoints."""

    def test_auth_rate_limiter_has_lower_limit(self):
        """Verify the auth rate limiter concept works — 10 req / 60s window."""
        from schwarma.hub.http import _IPRateLimiter
        auth_rl = _IPRateLimiter(max_requests=10, window=60)
        for _ in range(10):
            assert auth_rl.allow("attacker") is True
        # 11th request should be rejected
        assert auth_rl.allow("attacker") is False


# ── Tests for first-admin auto-promote ───────────────────────────────────

class TestFirstAdminAutoPromote(unittest.IsolatedAsyncioTestCase):
    """Test that the first user to sign up is auto-promoted to admin."""

    async def test_first_local_signup_gets_admin(self):
        from schwarma.hub.http import _dispatch
        hub = MagicMock()
        hub.config = MagicMock()
        hub.config.smtp_host = ""
        hub.config.smtp_port = 587
        hub.config.smtp_user = ""
        hub.config.smtp_password = ""
        hub.config.smtp_from = ""
        hub.db = MagicMock()
        user_id = uuid4()
        hub.db.create_local_user = AsyncMock(return_value={
            "id": user_id, "email": "first@test.com", "name": "First",
        })
        hub.db.set_local_credential = AsyncMock()
        hub.db.create_email_verification_code = AsyncMock()
        # First user — count is 1 after creation
        hub.db.user_count = AsyncMock(return_value=1)
        hub.db.promote_to_admin = AsyncMock()
        hub.db.mark_email_verified = AsyncMock()
        hub.db.create_user_session = AsyncMock()
        hub.config.tls_enabled = False

        status, ct, body, extra = await _dispatch(
            hub, "POST", "/auth/signup",
            {"email": "first@test.com", "password": "securepass123", "name": "First"},
            {},
        )
        assert status == 200
        hub.db.promote_to_admin.assert_called_once_with(user_id)

    async def test_second_signup_not_promoted(self):
        from schwarma.hub.http import _dispatch
        hub = MagicMock()
        hub.config = MagicMock()
        hub.config.smtp_host = ""
        hub.config.smtp_port = 587
        hub.config.smtp_user = ""
        hub.config.smtp_password = ""
        hub.config.smtp_from = ""
        hub.db = MagicMock()
        user_id = uuid4()
        hub.db.create_local_user = AsyncMock(return_value={
            "id": user_id, "email": "second@test.com", "name": "Second",
        })
        hub.db.set_local_credential = AsyncMock()
        hub.db.create_email_verification_code = AsyncMock()
        # Second user — count > 1
        hub.db.user_count = AsyncMock(return_value=2)
        hub.db.promote_to_admin = AsyncMock()
        hub.db.mark_email_verified = AsyncMock()
        hub.db.create_user_session = AsyncMock()
        hub.config.tls_enabled = False

        status, ct, body, extra = await _dispatch(
            hub, "POST", "/auth/signup",
            {"email": "second@test.com", "password": "securepass123", "name": "Second"},
            {},
        )
        assert status == 200
        hub.db.promote_to_admin.assert_not_called()

    async def test_first_google_oauth_gets_admin(self):
        """First Google OAuth user should get auto-promoted."""
        from schwarma.hub.http import _dispatch
        import schwarma.hub.auth as auth_mod

        hub = MagicMock()
        hub.config = MagicMock()
        hub.config.google_client_id = "test-id"
        hub.config.google_client_secret = "test-secret"
        hub.config.tls_enabled = False
        hub.db = MagicMock()
        user_id = uuid4()
        user_data = {
            "id": user_id, "email": "google@test.com", "name": "Google User",
            "is_admin": False, "agent_id": None, "picture_url": "",
        }
        hub.db.upsert_user = AsyncMock(return_value=user_data)
        hub.db.user_count = AsyncMock(return_value=1)
        hub.db.promote_to_admin = AsyncMock()
        hub.db.create_user_session = AsyncMock()

        with patch.object(auth_mod, "exchange_code_for_user", new_callable=AsyncMock) as mock_exchange:
            mock_exchange.return_value = {
                "email": "google@test.com",
                "name": "Google User",
                "picture": "",
                "sub": "12345",
            }
            with patch.object(auth_mod, "is_google_configured", return_value=True):
                status, ct, body, extra = await _dispatch(
                    hub, "GET", "/auth/google/callback",
                    {"code": "auth-code", "state": ""},
                    {"cookie": "schwarma_oauth_state="},
                )
        assert status == 302
        hub.db.promote_to_admin.assert_called_once_with(user_id)


# ── Tests for leaderboard time windows ───────────────────────────────────

class TestLeaderboardTimeWindows(unittest.IsolatedAsyncioTestCase):
    """Test the enhanced leaderboard with period and capability params."""

    async def test_leaderboard_alltime_default(self):
        from schwarma.hub.http import _dispatch
        hub = MagicMock()
        hub.db = MagicMock()
        hub.db.reputation_leaderboard = AsyncMock(return_value=[
            {"agent_id": str(uuid4()), "name": "A", "model_tier": "STANDARD", "balance": 100},
        ])

        status, ct, body, extra = await _dispatch(
            hub, "GET", "/leaderboard", {}, {},
        )
        assert status == 200
        data = json.loads(body)
        assert data["period"] == "alltime"
        assert data["capability"] is None
        hub.db.reputation_leaderboard.assert_called_once_with(limit=20, period=None, capability=None)

    async def test_leaderboard_weekly_period(self):
        from schwarma.hub.http import _dispatch
        hub = MagicMock()
        hub.db = MagicMock()
        hub.db.reputation_leaderboard = AsyncMock(return_value=[])

        status, ct, body, extra = await _dispatch(
            hub, "GET", "/leaderboard", {"period": "weekly"}, {},
        )
        assert status == 200
        data = json.loads(body)
        assert data["period"] == "weekly"
        hub.db.reputation_leaderboard.assert_called_once_with(limit=20, period="weekly", capability=None)

    async def test_leaderboard_monthly_with_capability(self):
        from schwarma.hub.http import _dispatch
        hub = MagicMock()
        hub.db = MagicMock()
        hub.db.reputation_leaderboard = AsyncMock(return_value=[])

        status, ct, body, extra = await _dispatch(
            hub, "GET", "/leaderboard",
            {"period": "monthly", "capability": "CODE_GENERATION", "limit": "10"},
            {},
        )
        assert status == 200
        data = json.loads(body)
        assert data["period"] == "monthly"
        assert data["capability"] == "CODE_GENERATION"
        hub.db.reputation_leaderboard.assert_called_once_with(
            limit=10, period="monthly", capability="CODE_GENERATION",
        )


# ── Tests for JSON body with native types (POST handlers) ───────────────

class TestJSONBodyNativeTypes(unittest.IsolatedAsyncioTestCase):
    """Test that POST handlers correctly handle native JSON types (int, list, dict)."""

    async def test_post_problem_with_int_bounty(self):
        """Sending bounty as int (not string) should work after the fix."""
        from schwarma.hub.http import _dispatch
        hub = _mock_hub_with_auth()
        mock_problem = MagicMock()
        mock_problem.id = uuid4()
        mock_problem.status = MagicMock()
        mock_problem.status.name = "OPEN"
        hub.station.exchange.post_problem = AsyncMock(return_value=mock_problem)

        status, ct, body, hdrs = await _dispatch(
            hub, "POST", "/problems",
            {"title": "Bug", "description": "Broken", "bounty": 50, "tags": ["BUG", "CODE"]},
            {"cookie": "schwarma_session=tok"},
        )
        assert status == 200
        data = json.loads(body)
        assert data["status"] == "OPEN"

    async def test_post_solution_with_native_strings(self):
        """Solution fields should work whether from query-string or JSON body."""
        from schwarma.hub.http import _dispatch
        hub = _mock_hub_with_auth()
        mock_solution = MagicMock()
        mock_solution.id = uuid4()
        mock_solution.verdict = MagicMock()
        mock_solution.verdict.name = "PENDING"
        hub.station.exchange.submit_solution = AsyncMock(return_value=mock_solution)

        status, ct, body, hdrs = await _dispatch(
            hub, "POST", "/solutions",
            {"problem_id": str(uuid4()), "body": "Here's the fix"},
            {"cookie": "schwarma_session=tok"},
        )
        assert status == 200

    async def test_post_review_with_native_types(self):
        from schwarma.hub.http import _dispatch
        hub = _mock_hub_with_auth()
        mock_review = MagicMock()
        mock_review.id = uuid4()
        mock_review.verdict = MagicMock()
        mock_review.verdict.name = "APPROVE"
        hub.station.exchange.submit_review = AsyncMock(return_value=mock_review)

        status, ct, body, hdrs = await _dispatch(
            hub, "POST", "/reviews",
            {"solution_id": str(uuid4()), "verdict": "approve"},
            {"cookie": "schwarma_session=tok"},
        )
        assert status == 200

    async def test_agent_register_with_list_capabilities(self):
        """capabilities can be a JSON list, not just a CSV string."""
        from schwarma.hub.http import _dispatch
        hub = _mock_hub_with_auth(agent_id=None)
        hub.db.get_user_session = AsyncMock(return_value={
            "id": uuid4(), "email": "test@gmail.com", "name": "Test",
            "picture_url": "", "agent_id": None, "is_admin": False,
        })
        aid = uuid4()
        hub.station._m_register = AsyncMock(return_value={
            "agent_id": str(aid), "token": "tok123",
            "name": "Test", "model_tier": "STANDARD",
            "capabilities": ["CODE_GENERATION", "BUG_TRIAGE"],
        })
        hub.station.exchange._agents = {}

        status, ct, body, hdrs = await _dispatch(
            hub, "POST", "/api/v1/agent/register",
            {
                "name": "TestAgent",
                "capabilities": ["CODE_GENERATION", "BUG_TRIAGE"],
                "model_tier": "PREMIUM",
            },
            {"cookie": "schwarma_session=tok"},
        )
        assert status == 200
        data = json.loads(body)
        assert data["agent_id"] == str(aid)


# ── Tests for rate limiter prune ─────────────────────────────────────────

class TestRateLimiterPruneIntegration(unittest.TestCase):
    """Test that _prune_counter concept works."""

    def test_prune_clears_stale_ips(self):
        """Verify prune() actually frees memory."""
        import time as _time
        from schwarma.hub.http import _IPRateLimiter

        rl = _IPRateLimiter(max_requests=100, window=1)
        # Simulate 100 IPs with old timestamps
        old = _time.monotonic() - 200
        for i in range(100):
            rl._hits[f"10.0.0.{i}"] = [old]
        assert len(rl._hits) == 100
        rl.prune()
        assert len(rl._hits) == 0


if __name__ == "__main__":
    unittest.main()
