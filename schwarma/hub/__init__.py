"""
Schwarma Hub — deployable server with PostgreSQL persistence.

The Hub wraps a :class:`SchwarmaStation` and synchronises all exchange
state to PostgreSQL via the EventBus.  On restart it rehydrates the
Exchange from the database so no data is lost.

Usage::

    # docker compose up   (starts hub + postgres)

    # Or run directly:
    python -m schwarma.hub --tcp 0.0.0.0 9741 --database-url postgresql://...
"""

from schwarma.hub.config import HubConfig
from schwarma.hub.app import SchwarmaHub

__all__ = ["HubConfig", "SchwarmaHub"]
