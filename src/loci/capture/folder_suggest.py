"""Folder suggestion via fuzzy text similarity.

Given a new resource (title + abstract snippet), returns the top-k existing
folder names from the project, scored by text similarity. No embeddings — the
goal is a fast, lightweight first-pass that can run synchronously during
ingest.

Score formula:
  - title_score  = fuzz.token_set_ratio(title, folder_name) / 100   [0..1]
  - content_score = fuzz.partial_ratio(abstract_text[:200], recent_titles) / 100
  - final = 0.6 * title_score + 0.4 * content_score

`recent_titles` is the concatenation of the most-recently-ingested resource
titles in that folder (up to 5), giving the folder a "characteristic" string
that the abstract can match against.
"""

from __future__ import annotations

import logging
import sqlite3

log = logging.getLogger(__name__)

_TITLE_WEIGHT = 0.6
_CONTENT_WEIGHT = 0.4
_MAX_RECENT_TITLES = 5


def suggest_folders(
    title: str,
    abstract_text: str,
    conn: sqlite3.Connection,
    project_id: str,
    top_k: int = 3,
) -> list[tuple[str, float]]:
    """Return (folder_path, score) pairs, score in [0, 1], sorted descending.

    Folders come from resource_provenance.folder for resources in the project's
    effective membership. Returns [] if no folders have been recorded yet.
    """
    try:
        from rapidfuzz import fuzz
    except ImportError:
        log.warning("suggest_folders: rapidfuzz not installed; returning empty list")
        return []

    # Fetch all (folder, title) pairs for this project's resources.
    rows = conn.execute(
        """
        SELECT rp.folder, n.title
        FROM resource_provenance rp
        JOIN nodes n ON n.id = rp.resource_id
        JOIN project_effective_members pm ON pm.node_id = rp.resource_id
        WHERE pm.project_id = ?
          AND rp.folder IS NOT NULL
          AND rp.folder != ''
        ORDER BY n.created_at DESC
        """,
        (project_id,),
    ).fetchall()

    if not rows:
        return []

    # Group by folder, keeping recent titles for content scoring.
    folder_titles: dict[str, list[str]] = {}
    for row in rows:
        folder = row["folder"]
        node_title = row["title"] or ""
        if folder not in folder_titles:
            folder_titles[folder] = []
        if len(folder_titles[folder]) < _MAX_RECENT_TITLES:
            folder_titles[folder].append(node_title)

    abstract_snippet = abstract_text[:200]
    scores: list[tuple[str, float]] = []

    for folder, recent_titles in folder_titles.items():
        # Score the new resource's title against the folder name.
        folder_basename = folder.rstrip("/").split("/")[-1]
        title_score = fuzz.token_set_ratio(title, folder_basename) / 100.0

        # Score the abstract against a joined string of recent titles in that folder.
        combined_titles = " ".join(recent_titles)
        content_score = fuzz.partial_ratio(abstract_snippet, combined_titles) / 100.0

        blended = _TITLE_WEIGHT * title_score + _CONTENT_WEIGHT * content_score
        scores.append((folder, round(blended, 4)))

    scores.sort(key=lambda t: t[1], reverse=True)
    return scores[:top_k]
