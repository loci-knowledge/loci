"""SQLite storage layer.

PLAN §Storage commits us to SQLite as the single source of truth for the graph.
The schema lives in `migrations/`; runtime connections come from `connection.py`.
"""

from loci.db.connection import connect, get_connection
from loci.db.migrate import migrate

__all__ = ["connect", "get_connection", "migrate"]
