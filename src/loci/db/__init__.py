"""SQLite storage layer.

The schema lives in `schema.sql` (one canonical file, applied via
`init_schema()`). Runtime connections come from `connection.py`.
"""

from loci.db.connection import connect, get_connection
from loci.db.schema import init_schema

__all__ = ["connect", "get_connection", "init_schema"]
