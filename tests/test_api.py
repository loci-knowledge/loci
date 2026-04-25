"""End-to-end REST API tests via FastAPI's TestClient."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_health(loci_dir):
    from loci.api import create_app
    with TestClient(create_app()) as c:
        r = c.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


def test_create_project_and_duplicate(loci_dir):
    from loci.api import create_app
    with TestClient(create_app()) as c:
        r = c.post("/projects", json={"slug": "x", "name": "X", "profile_md": ""})
        assert r.status_code == 201
        r = c.post("/projects", json={"slug": "x", "name": "Y", "profile_md": ""})
        assert r.status_code == 409


def test_full_loop_scan_retrieve_pin(loci_dir, fake_embedder, tmp_path):
    from loci.api import create_app
    src = tmp_path / "c"
    src.mkdir()
    (src / "a.md").write_text("rotary embeddings encode position via projection rotation")
    with TestClient(create_app()) as c:
        r = c.post("/projects", json={"slug": "p", "name": "P", "profile_md": ""})
        pid = r.json()["id"]

        r = c.post(f"/projects/{pid}/sources/scan", json={"root": str(src)})
        assert r.status_code == 200
        assert r.json()["new_raw"] == 1

        r = c.post(f"/projects/{pid}/retrieve", json={"query": "rotary", "k": 3})
        assert r.status_code == 200
        ids = [n["id"] for n in r.json()["nodes"]]
        assert ids

        # Pin first node
        r = c.post(f"/nodes/{ids[0]}/pin", params={"project_id": pid})
        assert r.status_code == 200

        # Graph view should show the pinned role
        r = c.get(f"/projects/{pid}/graph")
        roles = {n["id"]: n["role"] for n in r.json()["nodes"]}
        assert roles[ids[0]] == "pinned"


def test_create_interp_and_edge(loci_dir, fake_embedder, tmp_path):
    from loci.api import create_app
    with TestClient(create_app()) as c:
        pid = c.post("/projects", json={"slug": "p", "name": "P", "profile_md": ""}).json()["id"]
        n1 = c.post("/nodes", json={
            "project_id": pid, "subkind": "pattern", "title": "T1", "body": "b1",
            "origin": "user_explicit_create",
        }).json()["node_id"]
        n2 = c.post("/nodes", json={
            "project_id": pid, "subkind": "pattern", "title": "T2", "body": "b2",
            "origin": "user_explicit_create",
        }).json()["node_id"]
        r = c.post("/edges", json={"src": n1, "dst": n2, "type": "reinforces"})
        assert r.status_code == 201
        assert len(r.json()["edges"]) == 2  # symmetric → reciprocal

        r = c.get(f"/nodes/{n1}")
        assert r.status_code == 200
        edges_out = [e["type"] for e in r.json()["edges_out"]]
        assert "reinforces" in edges_out


def test_response_expansion(loci_dir, fake_embedder, tmp_path):
    from loci.api import create_app
    src = tmp_path / "c"
    src.mkdir()
    (src / "a.md").write_text("hello world")
    with TestClient(create_app()) as c:
        pid = c.post("/projects", json={"slug": "p", "name": "P", "profile_md": ""}).json()["id"]
        c.post(f"/projects/{pid}/sources/scan", json={"root": str(src)})
        r = c.post(f"/projects/{pid}/retrieve", json={"query": "hello", "k": 1})
        rid = r.json()["trace_id"]
        # Expand the response
        r = c.get(f"/responses/{rid}")
        assert r.status_code == 200
        assert "request" in r.json()
        # 404 on bogus
        r = c.get("/responses/01ZZZZZZZZZZZZZZZZZZZZZZZZ")
        assert r.status_code == 404
