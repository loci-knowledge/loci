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


def test_create_workspace_and_scan(loci_dir, fake_embedder, tmp_path):
    from loci.api import create_app
    src = tmp_path / "c"
    src.mkdir()
    (src / "a.md").write_text("rotary embeddings encode position via projection rotation")
    with TestClient(create_app()) as c:
        r = c.post("/projects", json={"slug": "p", "name": "P", "profile_md": ""})
        pid = r.json()["id"]

        ws = c.post("/workspaces", json={"slug": "ws-p", "name": "P"}).json()
        ws_id = ws["id"]
        c.post(f"/workspaces/{ws_id}/sources", json={"root": str(src)})
        c.post(f"/projects/{pid}/workspaces/{ws_id}", json={"role": "primary"})
        r = c.post(f"/workspaces/{ws_id}/scan")
        assert r.status_code == 200
        assert r.json()["new_raw"] == 1

        # Sources endpoint should list the scanned file
        r = c.get(f"/projects/{pid}/sources")
        assert r.status_code == 200
        assert len(r.json()) == 1


def test_aspects_crud(loci_dir, fake_embedder, tmp_path):
    from loci.api import create_app
    src = tmp_path / "c"
    src.mkdir()
    (src / "b.md").write_text("methodology section describes the experiment design")
    with TestClient(create_app()) as c:
        pid = c.post("/projects", json={"slug": "q", "name": "Q", "profile_md": ""}).json()["id"]
        ws = c.post("/workspaces", json={"slug": "ws-q", "name": "Q"}).json()
        ws_id = ws["id"]
        c.post(f"/workspaces/{ws_id}/sources", json={"root": str(src)})
        c.post(f"/projects/{pid}/workspaces/{ws_id}", json={"role": "primary"})
        c.post(f"/workspaces/{ws_id}/scan")

        sources = c.get(f"/projects/{pid}/sources").json()
        rid = sources[0]["id"]

        # Tag with an aspect
        r = c.post(f"/projects/{pid}/aspects/resources/{rid}/tags",
                   json={"labels": ["methodology"], "source": "user"})
        assert r.status_code == 200

        # List aspects for resource
        r = c.get(f"/projects/{pid}/aspects/resources/{rid}")
        assert r.status_code == 200
        labels = [a["label"] for a in r.json()]
        assert "methodology" in labels
