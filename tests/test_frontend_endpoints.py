"""Tests for the frontend-facing endpoints added for the loki-frontend extension.

Covers:
  - GET /projects (list endpoint)
  - GET /projects/:id/communities (latest snapshot)
  - GET /projects/:id/pinned (pinned membership read)
  - POST/GET /projects/:id/anchors (transient active-anchor set)
  - WS publish smoke (graph deltas + trace events on POST /nodes)
  - retrieve/draft anchor fallback semantics

These are the bare-minimum guards: they don't try to lock the whole protocol
down, just the bits the frontend was visibly missing.
"""

from __future__ import annotations

import asyncio
import json

from fastapi.testclient import TestClient

from loci.api import create_app

# ---------------------------------------------------------------------------
# Project list
# ---------------------------------------------------------------------------


def test_list_projects_orders_by_recency(loci_dir):
    with TestClient(create_app()) as c:
        r = c.get("/projects")
        assert r.status_code == 200
        assert r.json() == {"projects": []}

        # Three projects; the second one we touch last so it should bubble up.
        a = c.post("/projects", json={"slug": "a", "name": "A"}).json()["id"]
        b = c.post("/projects", json={"slug": "b", "name": "B"}).json()["id"]
        cc = c.post("/projects", json={"slug": "c", "name": "C"}).json()["id"]

        # Touch B by updating its profile (bumps last_active_at).
        c.patch(f"/projects/{b}/profile", json={"profile_md": "hi"})

        r = c.get("/projects")
        assert r.status_code == 200
        ids = [p["id"] for p in r.json()["projects"]]
        # B should be first (most-recently-active). The other two are
        # ordered by created_at DESC so cc, then a.
        assert ids[0] == b
        assert set(ids) == {a, b, cc}


# ---------------------------------------------------------------------------
# Pinned
# ---------------------------------------------------------------------------


def test_pinned_returns_pinned_node_ids(loci_dir, fake_embedder, tmp_path):
    src = tmp_path / "c"
    src.mkdir()
    (src / "x.md").write_text("alpha bravo charlie")
    with TestClient(create_app()) as c:
        pid = c.post("/projects", json={"slug": "p", "name": "P"}).json()["id"]
        ws = c.post("/workspaces", json={"slug": "ws-p", "name": "P"}).json()
        ws_id = ws["id"]
        c.post(f"/workspaces/{ws_id}/sources", json={"root": str(src)})
        c.post(f"/projects/{pid}/workspaces/{ws_id}", json={"role": "primary"})
        c.post(f"/workspaces/{ws_id}/scan")

        r = c.get(f"/projects/{pid}/pinned")
        assert r.status_code == 200
        assert r.json() == {"pinned_node_ids": []}

        ids = [n["id"] for n in c.post(
            f"/projects/{pid}/retrieve", json={"query": "alpha", "k": 1}
        ).json()["nodes"]]
        assert ids
        c.post(f"/nodes/{ids[0]}/pin", params={"project_id": pid})

        r = c.get(f"/projects/{pid}/pinned")
        assert r.status_code == 200
        assert r.json() == {"pinned_node_ids": [ids[0]]}


# ---------------------------------------------------------------------------
# Communities
# ---------------------------------------------------------------------------


def test_communities_empty_when_no_snapshot(loci_dir):
    with TestClient(create_app()) as c:
        pid = c.post("/projects", json={"slug": "p", "name": "P"}).json()["id"]
        r = c.get(f"/projects/{pid}/communities")
        assert r.status_code == 200
        assert r.json() == {"communities": [], "community_version": 0}


def test_communities_returns_latest_snapshot(loci_dir, conn):
    # Insert a project + two snapshots manually; expect only the latest back.
    with TestClient(create_app()) as c:
        pid = c.post("/projects", json={"slug": "p", "name": "P"}).json()["id"]

    from loci.db.connection import connect

    db = connect()
    db.execute(
        "INSERT INTO communities(id, project_id, snapshot_at, level, label, member_node_ids)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        ("01OLD" + "0" * 21, pid, "2026-04-23T00:00:00.000Z", 0, "old", json.dumps(["A", "B"])),
    )
    db.execute(
        "INSERT INTO communities(id, project_id, snapshot_at, level, label, member_node_ids)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        ("01NEW" + "0" * 21, pid, "2026-04-24T12:00:00.000Z", 0, "new", json.dumps(["C", "D"])),
    )
    db.close()

    with TestClient(create_app()) as c:
        r = c.get(f"/projects/{pid}/communities")
        assert r.status_code == 200
        body = r.json()
        assert len(body["communities"]) == 1
        assert body["communities"][0]["label"] == "new"
        assert body["communities"][0]["member_node_ids"] == ["C", "D"]
        assert body["community_version"] > 0


def test_graph_view_includes_community_version(loci_dir):
    with TestClient(create_app()) as c:
        pid = c.post("/projects", json={"slug": "p", "name": "P"}).json()["id"]
        r = c.get(f"/projects/{pid}/graph")
        assert r.status_code == 200
        assert "community_version" in r.json()
        assert r.json()["community_version"] == 0


# ---------------------------------------------------------------------------
# Anchors (active-anchor set)
# ---------------------------------------------------------------------------


def test_anchors_post_then_get(loci_dir):
    with TestClient(create_app()) as c:
        pid = c.post("/projects", json={"slug": "p", "name": "P"}).json()["id"]

        # Initially empty.
        r = c.get(f"/projects/{pid}/anchors")
        assert r.status_code == 200
        assert r.json() == {"node_ids": [], "expires_at": None}

        # Set a dummy node id (need not exist — anchors store is opaque).
        nid = "01" + "ABCD" * 6  # 26 chars
        r = c.post(
            f"/projects/{pid}/anchors",
            json={"node_ids": [nid], "ttl_sec": 600},
        )
        assert r.status_code == 200
        assert r.json()["node_ids"] == [nid]
        assert r.json()["expires_at"] is not None

        r = c.get(f"/projects/{pid}/anchors")
        assert r.status_code == 200
        assert r.json()["node_ids"] == [nid]


def test_anchors_rejects_bad_id_shape(loci_dir):
    with TestClient(create_app()) as c:
        pid = c.post("/projects", json={"slug": "p", "name": "P"}).json()["id"]
        r = c.post(
            f"/projects/{pid}/anchors",
            json={"node_ids": ["nope"], "ttl_sec": 60},
        )
        assert r.status_code == 400


def test_anchors_expiry_returns_empty(loci_dir, monkeypatch):
    """Past-expiry entries should round-trip as empty."""
    from loci.api.routes import anchors as anchors_mod

    with TestClient(create_app()) as c:
        pid = c.post("/projects", json={"slug": "p", "name": "P"}).json()["id"]
        # Fake-set an already-expired entry directly via the helper — easier
        # than waiting on real-time expiry.
        anchors_mod._store[pid] = anchors_mod._Entry(
            ["01" + "X" * 24], expires_at_epoch=0.0,
        )
        r = c.get(f"/projects/{pid}/anchors")
        assert r.status_code == 200
        assert r.json() == {"node_ids": [], "expires_at": None}


def test_retrieve_falls_back_to_active_anchors(loci_dir, fake_embedder, tmp_path, monkeypatch):
    """If the request omits `anchors`, the active anchor set seeds retrieval.

    We don't assert on retrieval results (that would couple to the retriever's
    behaviour); we just assert the request reaches the retriever with the
    fallback anchors filled in. Use a monkeypatch on Retriever to capture.
    """
    from loci.api.routes import anchors as anchors_mod
    from loci.retrieve import pipeline

    src = tmp_path / "c"
    src.mkdir()
    (src / "x.md").write_text("alpha")

    captured_requests: list = []

    real_retrieve = pipeline.Retriever.retrieve

    def spy(self, req, *a, **kw):
        captured_requests.append(req)
        return real_retrieve(self, req, *a, **kw)

    monkeypatch.setattr(pipeline.Retriever, "retrieve", spy)

    with TestClient(create_app()) as c:
        pid = c.post("/projects", json={"slug": "p", "name": "P"}).json()["id"]
        ws = c.post("/workspaces", json={"slug": "ws-p", "name": "P"}).json()
        ws_id = ws["id"]
        c.post(f"/workspaces/{ws_id}/sources", json={"root": str(src)})
        c.post(f"/projects/{pid}/workspaces/{ws_id}", json={"role": "primary"})
        c.post(f"/workspaces/{ws_id}/scan")

        # Pre-set active anchors.
        anchor_id = "01" + "Y" * 24
        anchors_mod.set_active_anchors(pid, [anchor_id], ttl_sec=600)

        # Omit `anchors` from the request body; expect fallback.
        c.post(f"/projects/{pid}/retrieve", json={"query": "alpha"})
        assert captured_requests
        assert captured_requests[-1].anchors == [anchor_id]

        # Explicit empty list should NOT pick up the fallback.
        c.post(f"/projects/{pid}/retrieve", json={"query": "alpha", "anchors": []})
        assert captured_requests[-1].anchors == []


# ---------------------------------------------------------------------------
# WS publish smoke — graph deltas and trace events
# ---------------------------------------------------------------------------


def test_publish_node_upsert_on_create(loci_dir, fake_embedder):
    """POSTing a node should publish a graph-delta upsert event."""
    from loci.api.pubsub import bus

    async def runner():
        with TestClient(create_app()) as c:
            pid = c.post("/projects", json={"slug": "p", "name": "P"}).json()["id"]
            # Subscribe AFTER project creation but BEFORE node creation.
            q = await bus.subscribe(f"project:{pid}")
            try:
                resp = c.post("/nodes", json={
                    "project_id": pid, "subkind": "decision",
                    "title": "T", "body": "b",
                    "origin": "user_explicit_create",
                })
                assert resp.status_code == 201
                node_id = resp.json()["node_id"]
                # Drain at most a couple of events; ensure the upsert is
                # among them. We tolerate other events on the channel
                # because the route also publishes a `pinned` trace, etc.
                seen_upsert = False
                for _ in range(5):
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=1.0)
                    except TimeoutError:
                        break
                    if (
                        ev.get("op") == "upsert"
                        and ev.get("entity") == "node"
                        and ev.get("payload", {}).get("id") == node_id
                    ):
                        seen_upsert = True
                        assert "seq" in ev
                        assert ev["seq"] >= 1
                        break
                assert seen_upsert, "no node upsert event was published"
            finally:
                await bus.unsubscribe(f"project:{pid}", q)

    asyncio.run(runner())


def test_publish_trace_on_pin(loci_dir, fake_embedder, tmp_path):
    """Pinning a node writes a trace and publishes it."""
    from loci.api.pubsub import bus

    async def runner():
        src = tmp_path / "c"
        src.mkdir()
        (src / "x.md").write_text("alpha")
        with TestClient(create_app()) as c:
            pid = c.post("/projects", json={"slug": "p", "name": "P"}).json()["id"]
            ws = c.post("/workspaces", json={"slug": "ws-p", "name": "P"}).json()
            ws_id = ws["id"]
            c.post(f"/workspaces/{ws_id}/sources", json={"root": str(src)})
            c.post(f"/projects/{pid}/workspaces/{ws_id}", json={"role": "primary"})
            c.post(f"/workspaces/{ws_id}/scan")
            ids = [n["id"] for n in c.post(
                f"/projects/{pid}/retrieve", json={"query": "alpha"}
            ).json()["nodes"]]
            assert ids

            q = await bus.subscribe(f"project:{pid}")
            try:
                c.post(f"/nodes/{ids[0]}/pin", params={"project_id": pid})
                seen_trace = False
                for _ in range(10):
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=1.0)
                    except TimeoutError:
                        break
                    if ev.get("kind") == "trace" and ev.get("action") == "pinned":
                        seen_trace = True
                        assert ev.get("node_id") == ids[0]
                        break
                assert seen_trace, "no trace event was published for pin"
            finally:
                await bus.unsubscribe(f"project:{pid}", q)

    asyncio.run(runner())


def test_seq_is_monotonic_per_project(loci_dir, fake_embedder):
    from loci.api.pubsub import bus

    async def runner():
        with TestClient(create_app()) as c:
            pid = c.post("/projects", json={"slug": "p", "name": "P"}).json()["id"]
            q = await bus.subscribe(f"project:{pid}")
            try:
                # Create two nodes; collect upsert events; verify seq strictly
                # increasing.
                for i in range(2):
                    c.post("/nodes", json={
                        "project_id": pid, "subkind": "decision",
                        "title": f"T{i}", "body": "x",
                        "origin": "user_explicit_create",
                    })
                seqs = []
                for _ in range(10):
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=1.0)
                    except TimeoutError:
                        break
                    if "seq" in ev:
                        seqs.append(ev["seq"])
                assert len(seqs) >= 2
                assert seqs == sorted(seqs)
                assert len(set(seqs)) == len(seqs)
            finally:
                await bus.unsubscribe(f"project:{pid}", q)

    asyncio.run(runner())
