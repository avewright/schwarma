"""
Google OAuth 2.0 — login with Gmail, zero external dependencies.

Flow:
  1. ``GET /auth/google``         → redirect to Google consent screen
  2. ``GET /auth/google/callback`` → exchange code for tokens, upsert user,
                                     set session cookie, redirect to /
  3. ``GET /auth/me``             → return current user from cookie
  4. ``POST /auth/logout``        → delete session, clear cookie

Uses stdlib ``urllib.request`` for the token exchange and userinfo fetch,
``secrets`` for session token generation, and ``json`` for parsing.
"""

from __future__ import annotations

import json
import logging
import secrets
import smtplib
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, TYPE_CHECKING
from email.message import EmailMessage
from uuid import UUID

if TYPE_CHECKING:
    from schwarma.hub.config import HubConfig
    from schwarma.hub.database import Database

logger = logging.getLogger(__name__)

# ── Google endpoints ─────────────────────────────────────────────────────

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

# Scopes: basic profile + email
GOOGLE_SCOPES = "openid email profile"
GITHUB_AUTH_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_USER_URL = "https://api.github.com/user"
GITHUB_EMAILS_URL = "https://api.github.com/user/emails"


def google_login_url(config: "HubConfig", state: str = "") -> str:
    """Build the Google OAuth consent screen URL."""
    params = {
        "client_id": config.google_client_id,
        "redirect_uri": config.google_redirect_uri,
        "response_type": "code",
        "scope": GOOGLE_SCOPES,
        "access_type": "online",
        "prompt": "select_account",
    }
    if state:
        params["state"] = state
    return f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"


async def exchange_code_for_user(
    config: "HubConfig", code: str,
) -> dict[str, Any]:
    """Exchange the authorization code for access token, then fetch userinfo.

    Returns a dict with keys: ``sub``, ``email``, ``name``, ``picture``.
    Runs the HTTP calls in a thread to avoid blocking the event loop.
    """
    import asyncio

    def _blocking() -> dict[str, Any]:
        # 1. Exchange code → access token
        token_data = urllib.parse.urlencode({
            "code": code,
            "client_id": config.google_client_id,
            "client_secret": config.google_client_secret,
            "redirect_uri": config.google_redirect_uri,
            "grant_type": "authorization_code",
        }).encode()

        req = urllib.request.Request(
            GOOGLE_TOKEN_URL,
            data=token_data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                token_resp = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            # Google returns the real error detail in the response body
            error_body = exc.read().decode("utf-8", errors="replace")
            logger.error(
                "Google token exchange failed (HTTP %d): %s",
                exc.code, error_body,
            )
            raise RuntimeError(
                f"Google token exchange HTTP {exc.code}: {error_body}"
            ) from exc

        access_token = token_resp.get("access_token", "")
        if not access_token:
            logger.error("Google token response missing access_token: %s", token_resp)
            raise RuntimeError("Google token response missing access_token")

        # 2. Fetch userinfo
        userinfo_req = urllib.request.Request(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        with urllib.request.urlopen(userinfo_req, timeout=10) as resp:
            userinfo = json.loads(resp.read())

        return {
            "sub": userinfo["sub"],
            "email": userinfo.get("email", ""),
            "name": userinfo.get("name", ""),
            "picture": userinfo.get("picture", ""),
        }

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _blocking)


def generate_session_token() -> str:
    """Generate a cryptographically secure session token."""
    return secrets.token_urlsafe(48)


def is_google_configured(config: "HubConfig") -> bool:
    """Return True if Google OAuth credentials are set."""
    return bool(config.google_client_id and config.google_client_secret)


def github_login_url(config: "HubConfig", state: str = "") -> str:
    params = {
        "client_id": config.github_client_id,
        "redirect_uri": config.github_redirect_uri,
        "scope": "read:user user:email",
    }
    if state:
        params["state"] = state
    return f"{GITHUB_AUTH_URL}?{urllib.parse.urlencode(params)}"


def is_github_configured(config: "HubConfig") -> bool:
    return bool(config.github_client_id and config.github_client_secret)


async def exchange_github_code_for_user(config: "HubConfig", code: str) -> dict[str, Any]:
    import asyncio

    def _blocking() -> dict[str, Any]:
        token_data = urllib.parse.urlencode({
            "client_id": config.github_client_id,
            "client_secret": config.github_client_secret,
            "code": code,
            "redirect_uri": config.github_redirect_uri,
        }).encode()
        token_req = urllib.request.Request(
            GITHUB_TOKEN_URL,
            data=token_data,
            headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(token_req, timeout=10) as resp:
                token_resp = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            logger.error(
                "GitHub token exchange failed (HTTP %d): %s",
                exc.code, error_body,
            )
            raise RuntimeError(
                f"GitHub token exchange HTTP {exc.code}: {error_body}"
            ) from exc

        access_token = token_resp.get("access_token", "")
        if not access_token:
            error_desc = token_resp.get("error_description", token_resp.get("error", "unknown"))
            logger.error("GitHub token exchange returned no access_token: %s", token_resp)
            raise RuntimeError(f"GitHub token exchange failed: {error_desc}")

        user_req = urllib.request.Request(
            GITHUB_USER_URL,
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        )
        with urllib.request.urlopen(user_req, timeout=10) as resp:
            user = json.loads(resp.read())

        # Fetch emails — GitHub emails may be private, so we must use
        # the /user/emails endpoint and pick the primary+verified entry.
        emails_req = urllib.request.Request(
            GITHUB_EMAILS_URL,
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        )
        with urllib.request.urlopen(emails_req, timeout=10) as resp:
            emails = json.loads(resp.read())

        # Prefer the email that is both primary AND verified
        primary_verified = next(
            (e for e in emails if e.get("primary") and e.get("verified")),
            None,
        )
        # Fall back to any verified email, then any primary, then first
        if not primary_verified:
            primary_verified = (
                next((e for e in emails if e.get("verified")), None)
                or next((e for e in emails if e.get("primary")), None)
                or (emails[0] if emails else {})
            )

        email = primary_verified.get("email", user.get("email", ""))
        verified = bool(primary_verified.get("verified", False))

        return {
            "sub": str(user.get("id", "")),
            "email": email,
            "name": user.get("name") or user.get("login", ""),
            "picture": user.get("avatar_url", ""),
            "email_verified": verified,
        }

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _blocking)


async def send_verification_email(config: "HubConfig", to_email: str, code: str) -> bool:
    """Send email verification code via SMTP. Returns False when SMTP is not configured."""
    if not (config.smtp_host and config.smtp_from):
        logger.warning("SMTP not configured; verification code for %s is %s", to_email, code)
        return False

    msg = EmailMessage()
    msg["Subject"] = "Your Schwarma verification code"
    msg["From"] = config.smtp_from
    msg["To"] = to_email
    msg.set_content(f"Your verification code is: {code}\nIt expires in 15 minutes.")

    import asyncio
    def _send() -> None:
        with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=10) as s:
            s.starttls()
            if config.smtp_user:
                s.login(config.smtp_user, config.smtp_password)
            s.send_message(msg)
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _send)
    return True


# ── Cookie helpers ───────────────────────────────────────────────────────

def set_cookie_header(
    name: str,
    value: str,
    *,
    max_age: int = 30 * 86400,
    path: str = "/",
    http_only: bool = True,
    same_site: str = "Lax",
    secure: bool = False,
) -> str:
    """Build a Set-Cookie header value."""
    parts = [f"{name}={value}", f"Path={path}", f"Max-Age={max_age}",
             f"SameSite={same_site}"]
    if http_only:
        parts.append("HttpOnly")
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def clear_cookie_header(name: str, path: str = "/") -> str:
    """Build a Set-Cookie header that expires the cookie immediately."""
    return f"{name}=; Path={path}; Max-Age=0; HttpOnly; SameSite=Lax"


def parse_cookies(header_value: str) -> dict[str, str]:
    """Parse a ``Cookie:`` header into a dict."""
    cookies: dict[str, str] = {}
    for pair in header_value.split(";"):
        pair = pair.strip()
        if "=" in pair:
            k, v = pair.split("=", 1)
            cookies[k.strip()] = v.strip()
    return cookies


SESSION_COOKIE_NAME = "schwarma_session"


async def probe_smtp(config: "HubConfig") -> bool:
    """Attempt a non-sending SMTP connection to validate config at startup.

    Returns ``True`` if the SMTP server responds (EHLO succeeds), or
    ``False`` if SMTP is not configured.  Raises on connection failure
    so the caller can log/warn.
    """
    if not (config.smtp_host and config.smtp_from):
        return False

    import asyncio

    def _probe() -> None:
        with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=10) as s:
            s.ehlo()
            s.starttls()
            s.ehlo()
            if config.smtp_user:
                s.login(config.smtp_user, config.smtp_password)
            # NOOP to confirm the connection is healthy without sending
            s.noop()

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _probe)
    return True


def log_oauth_env_status(config: "HubConfig") -> None:
    """Log which OAuth env vars are present/missing (without printing values)."""
    checks = {
        "GOOGLE_CLIENT_ID": bool(config.google_client_id),
        "GOOGLE_CLIENT_SECRET": bool(config.google_client_secret),
        "GITHUB_CLIENT_ID": bool(config.github_client_id),
        "GITHUB_CLIENT_SECRET": bool(config.github_client_secret),
    }
    present = [k for k, v in checks.items() if v]
    missing = [k for k, v in checks.items() if not v]
    if present:
        logger.info("OAuth env vars present: %s", ", ".join(present))
    if missing:
        logger.warning("OAuth env vars MISSING: %s", ", ".join(missing))
    if config.google_client_id and config.google_client_secret:
        logger.info("Google OAuth: ENABLED (redirect_uri=%s)", config.google_redirect_uri)
    else:
        logger.warning("Google OAuth: DISABLED")
    if config.github_client_id and config.github_client_secret:
        logger.info("GitHub OAuth: ENABLED (redirect_uri=%s)", config.github_redirect_uri)
    else:
        logger.warning("GitHub OAuth: DISABLED")
