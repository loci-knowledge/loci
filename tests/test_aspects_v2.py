"""Tests for project-scoped aspects (project_resource_aspects table)."""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _insert_raw_node(conn, node_id: str, title: str = "Test Node") -> None:
    """Insert a minimal node + raw_node row for use in aspect tests."""
    content_hash = node_id[:16]  # use prefix as a unique-enough hash
    conn.execute(
        "INSERT INTO nodes(id, kind, subkind, title, body) VALUES (?,?,?,?,?)",
        (node_id, "raw", "md", title, "some body text"),
    )
    conn.execute(
        "INSERT INTO raw_nodes(id, content_hash, canonical_path, mime, size_bytes) "
        "VALUES (?,?,?,?,?)",
        (node_id, content_hash, f"/tmp/{node_id}.md", "text/markdown", 42),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_tag_resource_project(conn, project):
    """tag_resource(project_id=...) writes a row into project_resource_aspects."""
    from loci.graph.aspects import AspectRepository

    rid = "01AAAAAAAAAAAAAAAAAAAAAA00"
    _insert_raw_node(conn, rid)

    repo = AspectRepository(conn)
    repo.tag_resource(
        resource_id=rid,
        aspect_labels=["methodology"],
        source="llm",
        confidence=0.9,
        project_id=project.id,
    )

    row = conn.execute(
        "SELECT * FROM project_resource_aspects WHERE project_id = ? AND resource_id = ?",
        (project.id, rid),
    ).fetchone()
    assert row is not None, "expected a row in project_resource_aspects"
    assert row["source"] == "llm"
    assert abs(row["confidence"] - 0.9) < 1e-6


def test_aspects_for_returns_project_scoped(conn):
    """aspects_for with project_id returns only that project's aspects."""
    from loci.graph import Project, ProjectRepository
    from loci.graph.aspects import AspectRepository

    proj_repo = ProjectRepository(conn)
    p1 = proj_repo.create(Project(slug="proj-one", name="P1"))
    p2 = proj_repo.create(Project(slug="proj-two", name="P2"))

    rid = "01AAAAAAAAAAAAAAAAAAAAAA01"
    _insert_raw_node(conn, rid)

    repo = AspectRepository(conn)
    repo.tag_resource(rid, ["alpha"], source="user", project_id=p1.id)
    repo.tag_resource(rid, ["beta"], source="user", project_id=p2.id)

    p1_aspects = repo.aspects_for(rid, project_id=p1.id)
    p2_aspects = repo.aspects_for(rid, project_id=p2.id)

    p1_labels = {a.aspect_id for a in p1_aspects}
    p2_labels = {a.aspect_id for a in p2_aspects}

    # Fetch label names for assertion clarity
    def labels_for(aspect_list):
        return {
            conn.execute(
                "SELECT label FROM aspect_vocab WHERE id = ?", (a.aspect_id,)
            ).fetchone()["label"]
            for a in aspect_list
        }

    assert labels_for(p1_aspects) == {"alpha"}
    assert labels_for(p2_aspects) == {"beta"}
    assert p1_labels != p2_labels


def test_user_aspect_not_overwritten(conn, project):
    """A source='user' row must not be overwritten by a lower-confidence LLM tag.

    Uses tag_resource_project() which is the gold-protected UPSERT path used
    by infer_interpretation. The WHERE source != 'user' guard keeps user rows intact.
    """
    from loci.graph.aspects import AspectRepository

    rid = "01AAAAAAAAAAAAAAAAAAAAAA02"
    _insert_raw_node(conn, rid)

    repo = AspectRepository(conn)

    # Insert gold user label at high confidence via the gold-protected method
    repo.tag_resource_project(
        project_id=project.id,
        resource_id=rid,
        aspect_labels=["gold-label"],
        source="user",
        confidence=1.0,
    )

    # Attempt to overwrite with LLM at lower confidence
    repo.tag_resource_project(
        project_id=project.id,
        resource_id=rid,
        aspect_labels=["gold-label"],
        source="llm",
        confidence=0.4,
    )

    row = conn.execute(
        """
        SELECT pra.source, pra.confidence
        FROM project_resource_aspects pra
        JOIN aspect_vocab av ON av.id = pra.aspect_id
        WHERE pra.project_id = ? AND pra.resource_id = ? AND av.label = 'gold-label'
        """,
        (project.id, rid),
    ).fetchone()

    assert row is not None
    assert row["source"] == "user", f"expected 'user', got {row['source']!r}"
    assert abs(row["confidence"] - 1.0) < 1e-6, "user confidence should be unchanged"
