"""Stable short handles for resources.

short_id(resource_id) = "rid_" + base32(sha256(resource_id).digest())[:6].lower()

The 6-char base32 gives 30 bits of address space (~1B unique IDs before a
collision). On collision (UNIQUE constraint fail), extend to 8 chars.
"""

from __future__ import annotations

import base64
import hashlib
import sqlite3


def short_id(resource_id: str) -> str:
    """Return a stable 10-char short handle for a resource UUID.

    Format: "rid_" + first 6 chars of base32-encoded sha256 digest (lowercased).
    Total length: 10 chars.
    """
    digest = hashlib.sha256(resource_id.encode()).digest()
    return "rid_" + base64.b32encode(digest).decode().lower()[:6]


def resolve_handle(handle: str, project_id: str | None, conn: sqlite3.Connection) -> str | None:
    """Resolve any of: short-id, full UUID, or fuzzy title match.

    Priority order:
    1. Exact short-id match (computed on the fly by comparing short_id(r.id) for
       project resources — small scan, acceptable at project scale)
    2. Exact UUID (36-char)
    3. Fuzzy title match via rapidfuzz.partial_ratio >= 70 — returns the best
       match; callers decide whether to confirm with user.

    Returns resource_id (UUID) or None.
    """
    handle = handle.strip()

    # --- Step 1: Short-id match ---
    # Gather candidate resource IDs (project-scoped when possible, else global)
    if project_id is not None:
        rows = conn.execute(
            """
            SELECT DISTINCT n.id
            FROM nodes n
            JOIN project_effective_members pm ON pm.node_id = n.id
            WHERE pm.project_id = ?
            """,
            (project_id,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT id FROM raw_nodes").fetchall()

    for row in rows:
        rid = row[0]
        if short_id(rid) == handle:
            return rid

    # --- Step 2: Exact UUID ---
    # UUIDs are 36 chars (with hyphens) or 26-char ULIDs
    if len(handle) in (36, 26):
        row = conn.execute("SELECT id FROM raw_nodes WHERE id = ?", (handle,)).fetchone()
        if row is not None:
            return row[0]

    # --- Step 3: Fuzzy title match ---
    try:
        from rapidfuzz import fuzz
    except ImportError:
        return None

    if project_id is not None:
        title_rows = conn.execute(
            """
            SELECT n.id, n.title
            FROM nodes n
            JOIN project_effective_members pm ON pm.node_id = n.id
            WHERE pm.project_id = ?
            """,
            (project_id,),
        ).fetchall()
    else:
        title_rows = conn.execute("SELECT id, title FROM nodes").fetchall()

    best_id: str | None = None
    best_score: float = 0.0
    for row in title_rows:
        rid, title = row[0], row[1] or ""
        score = fuzz.partial_ratio(handle.lower(), title.lower())
        if score > best_score:
            best_score = score
            best_id = rid

    if best_score >= 70:
        return best_id

    return None
