"""
Hub configuration — everything the hub needs from the environment.

All settings can come from environment variables with the ``SCHWARMA_``
prefix, or be passed directly via the HubConfig dataclass.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class HubConfig:
    """Configuration for the Schwarma Hub server."""

    # ── Database ─────────────────────────────────────────────────────
    database_url: str = field(
        default_factory=lambda: os.environ.get(
            "SCHWARMA_DATABASE_URL",
            "postgresql://schwarma:schwarma@localhost:5432/schwarma",
        )
    )
    # Database SSL mode for secure connections (e.g. AWS RDS).
    # Values: "" (disabled), "require", "verify-ca", "verify-full".
    # For "verify-ca" / "verify-full", set database_ssl_ca to the CA
    # bundle path (e.g. the RDS combined CA bundle).
    database_ssl: str = field(
        default_factory=lambda: os.environ.get("SCHWARMA_DATABASE_SSL", "")
    )
    database_ssl_ca: str = field(
        default_factory=lambda: os.environ.get("SCHWARMA_DATABASE_SSL_CA", "")
    )
    db_pool_min: int = 2
    db_pool_max: int = 10

    # ── Station / TCP ────────────────────────────────────────────────
    host: str = field(
        default_factory=lambda: os.environ.get("SCHWARMA_HOST", "0.0.0.0")
    )
    tcp_port: int = field(
        default_factory=lambda: int(os.environ.get("SCHWARMA_TCP_PORT", "9741"))
    )

    # ── HTTP API (health, stats, dashboard) ──────────────────────────
    http_port: int = field(
        default_factory=lambda: int(os.environ.get("SCHWARMA_HTTP_PORT", "8741"))
    )

    # ── Auth ─────────────────────────────────────────────────────────
    require_auth: bool = True

    # ── Deployment mode ──────────────────────────────────────────────
    # Controls privacy posture, registration policy, and feature flags.
    #   PRIVATE  — single-team, full privacy, agents must be manually registered.
    #   TEAM     — semi-open, self-registration with review, INTERNAL default.
    #   PUBLIC   — community platform, open registration, open challenges,
    #              globs, public leaderboard, and external score ingestion.
    deployment_mode: str = field(
        default_factory=lambda: os.environ.get("SCHWARMA_DEPLOYMENT_MODE", "PRIVATE").upper()
    )

    # ── Open challenge ingestion ──────────────────────────────────────
    # Used only when deployment_mode == PUBLIC
    kaggle_username: str = field(
        default_factory=lambda: os.environ.get("SCHWARMA_KAGGLE_USERNAME", "")
    )
    kaggle_key: str = field(
        default_factory=lambda: os.environ.get("SCHWARMA_KAGGLE_KEY", "")
    )
    arxiv_query: str = field(
        default_factory=lambda: os.environ.get("SCHWARMA_ARXIV_QUERY", "open problems")
    )
    arxiv_category: str = field(
        default_factory=lambda: os.environ.get("SCHWARMA_ARXIV_CATEGORY", "")
    )
    challenge_ingest_interval: int = field(
        default_factory=lambda: int(os.environ.get("SCHWARMA_CHALLENGE_INGEST_INTERVAL", "3600"))
    )  # seconds between ingestion runs

    # ── Google OAuth ─────────────────────────────────────────────────
    google_client_id: str = field(
        default_factory=lambda: (
            os.environ.get("SCHWARMA_GOOGLE_CLIENT_ID")
            or os.environ.get("GOOGLE_CLIENT_ID", "")
        )
    )
    google_client_secret: str = field(
        default_factory=lambda: (
            os.environ.get("SCHWARMA_GOOGLE_CLIENT_SECRET")
            or os.environ.get("GOOGLE_CLIENT_SECRET", "")
        )
    )
    google_redirect_uri: str = field(
        default_factory=lambda: os.environ.get(
            "SCHWARMA_GOOGLE_REDIRECT_URI", "http://localhost:8741/auth/google/callback"
        )
    )
    github_client_id: str = field(
        default_factory=lambda: (
            os.environ.get("SCHWARMA_GITHUB_CLIENT_ID")
            or os.environ.get("GITHUB_CLIENT_ID")
            or os.environ.get("GH_CLIENT_ID", "")
        )
    )
    github_client_secret: str = field(
        default_factory=lambda: (
            os.environ.get("SCHWARMA_GITHUB_CLIENT_SECRET")
            or os.environ.get("GITHUB_CLIENT_SECRET")
            or os.environ.get("GH_CLIENT_SECRET", "")
        )
    )
    github_redirect_uri: str = field(
        default_factory=lambda: os.environ.get(
            "SCHWARMA_GITHUB_REDIRECT_URI", "http://localhost:8741/auth/github/callback"
        )
    )
    # Secret used to sign browser session cookies.  Generate a random
    # value for production; leave blank to auto-generate at startup.
    session_secret: str = field(
        default_factory=lambda: os.environ.get("SCHWARMA_SESSION_SECRET", "")
    )
    smtp_host: str = field(default_factory=lambda: os.environ.get("SCHWARMA_SMTP_HOST", ""))
    smtp_port: int = field(default_factory=lambda: int(os.environ.get("SCHWARMA_SMTP_PORT", "587")))
    smtp_user: str = field(default_factory=lambda: os.environ.get("SCHWARMA_SMTP_USER", ""))
    smtp_password: str = field(default_factory=lambda: os.environ.get("SCHWARMA_SMTP_PASSWORD", ""))
    smtp_from: str = field(default_factory=lambda: os.environ.get("SCHWARMA_SMTP_FROM", ""))

    # ── TLS ──────────────────────────────────────────────────────────
    # Paths to PEM-encoded certificate and key files for HTTPS/TLS.
    # Leave empty to run plain HTTP (suitable behind a reverse proxy).
    tls_cert: str = field(
        default_factory=lambda: os.environ.get("SCHWARMA_TLS_CERT", "")
    )
    tls_key: str = field(
        default_factory=lambda: os.environ.get("SCHWARMA_TLS_KEY", "")
    )

    # ── CORS ─────────────────────────────────────────────────────────
    # Comma-separated allowed origins for CORS.
    # "auto" (default) — only localhost:<http_port> (safe for dev & CI).
    # "*"              — allow all origins (disables CSRF; NEVER use in production).
    # "https://my.domain.com" — explicit production origin(s).
    allowed_origins: str = field(
        default_factory=lambda: os.environ.get("SCHWARMA_ALLOWED_ORIGINS", "auto")
    )

    # ── Rate limiting ────────────────────────────────────────────────
    # Max requests per IP per window.  0 = disabled.
    http_rate_limit: int = field(
        default_factory=lambda: int(os.environ.get("SCHWARMA_HTTP_RATE_LIMIT", "100"))
    )
    http_rate_window: int = field(
        default_factory=lambda: int(os.environ.get("SCHWARMA_HTTP_RATE_WINDOW", "60"))
    )

    # ── Request limits ───────────────────────────────────────────────
    max_request_size: int = field(
        default_factory=lambda: int(os.environ.get("SCHWARMA_MAX_REQUEST_SIZE", str(1024 * 1024)))
    )

    # ── Shutdown ─────────────────────────────────────────────────────
    shutdown_drain_seconds: int = field(
        default_factory=lambda: int(os.environ.get("SCHWARMA_SHUTDOWN_DRAIN", "5"))
    )

    # ── Logging ──────────────────────────────────────────────────────
    log_level: str = field(
        default_factory=lambda: os.environ.get("SCHWARMA_LOG_LEVEL", "INFO")
    )
    log_format: str = field(
        default_factory=lambda: os.environ.get("SCHWARMA_LOG_FORMAT", "text")
    )

    # ── Persistence ──────────────────────────────────────────────────
    # How often to flush a full snapshot (seconds).  Event-level writes
    # happen immediately; this is a safety net.
    snapshot_interval: int = 300

    def make_db_ssl_context(self):
        """Create an ``ssl.SSLContext`` for database connections.

        Returns ``None`` when *database_ssl* is empty (disabled).
        For ``require`` mode, returns ``True`` (asyncpg will use default
        TLS without certificate verification).
        For ``verify-ca`` / ``verify-full`` mode, returns a full
        ``ssl.SSLContext`` loaded with the CA bundle.
        """
        mode = self.database_ssl.strip().lower()
        if not mode:
            return None
        if mode == "require":
            # asyncpg interprets ssl=True as "use TLS, skip CA verify"
            return True
        if mode in ("verify-ca", "verify-full"):
            import ssl as _ssl
            ctx = _ssl.create_default_context(
                cafile=self.database_ssl_ca or None,
            )
            if mode == "verify-ca":
                # Verify the server cert is signed by the CA, but don't
                # check hostname (matches psql verify-ca semantics).
                ctx.check_hostname = False
            return ctx
        raise ValueError(
            f"Invalid SCHWARMA_DATABASE_SSL value: {self.database_ssl!r}.  "
            "Expected '', 'require', 'verify-ca', or 'verify-full'."
        )

    @property
    def tls_enabled(self) -> bool:
        """True if TLS cert and key are both set."""
        return bool(self.tls_cert and self.tls_key)

    def make_ssl_context(self):
        """Create an ``ssl.SSLContext`` from the configured cert/key.

        Returns ``None`` if TLS is not configured.
        """
        if not self.tls_enabled:
            return None
        import ssl
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.tls_cert, self.tls_key)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        return ctx

    @classmethod
    def from_env(cls) -> "HubConfig":
        """Build a config entirely from environment variables."""
        return cls()

    def validate(self, *, strict: bool = False) -> list[str]:
        """Check configuration and return a list of warnings.

        When *strict* is True (intended for production), raises
        ``RuntimeError`` on any critical misconfiguration.

        Warnings are always emitted via the ``logging`` module.
        """
        import logging as _log
        _logger = _log.getLogger("schwarma.hub.config")
        warnings: list[str] = []

        # Session secret
        if not self.session_secret:
            msg = (
                "SCHWARMA_SESSION_SECRET is not set — a random secret will be "
                "generated, but all sessions will be lost on restart.  "
                "Set it to a random 64-char value for production."
            )
            warnings.append(msg)
            _logger.warning(msg)

        # CORS
        if self.allowed_origins.strip() == "*":
            msg = (
                "SCHWARMA_ALLOWED_ORIGINS='*' disables CSRF protection.  "
                "Set explicit origins for production."
            )
            warnings.append(msg)
            _logger.warning(msg)
        elif self.allowed_origins.strip().lower() == "auto":
            msg = (
                "SCHWARMA_ALLOWED_ORIGINS='auto' — only localhost is allowed.  "
                "Set your production domain(s) before going live."
            )
            warnings.append(msg)
            _logger.info(msg)

        # Database password
        if "schwarma:schwarma@" in self.database_url:
            msg = (
                "Database is using the default password 'schwarma'.  "
                "Use a strong password in production."
            )
            warnings.append(msg)
            _logger.warning(msg)

        # SMTP
        if not self.smtp_host:
            msg = (
                "SMTP is not configured (SCHWARMA_SMTP_HOST).  "
                "Local-signup users will not be able to verify their email."
            )
            warnings.append(msg)
            _logger.info(msg)

        # TLS
        if not self.tls_enabled and strict:
            msg = (
                "TLS is not configured.  In production, deploy behind a "
                "reverse proxy (nginx/Caddy) with TLS termination."
            )
            warnings.append(msg)
            _logger.warning(msg)

        # OAuth
        if not (self.google_client_id or self.github_client_id):
            msg = (
                "No OAuth provider configured.  Users can only sign up "
                "with local email/password."
            )
            warnings.append(msg)
            _logger.info(msg)

        if strict:
            critical = [w for w in warnings if "CSRF" in w or "default password" in w.lower()]
            if critical:
                raise RuntimeError(
                    "Production configuration errors:\n" +
                    "\n".join(f"  - {c}" for c in critical)
                )

        return warnings
