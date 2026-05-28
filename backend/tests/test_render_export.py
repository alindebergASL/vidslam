from __future__ import annotations

import io
import zipfile

from .conftest import make_png_bytes


def _rendered_project(auth_client, title: str = "Kissmet — Specific Detail!") -> int:
    aid = auth_client.post("/api/avatars", json={"name": "X"}).json()["id"]
    auth_client.post(
        f"/api/avatars/{aid}/assets",
        files={"file": ("h.png", make_png_bytes(), "image/png")},
        data={"asset_type": "hero", "rights_confirmed": "true"},
    )
    pid = auth_client.post(
        "/api/projects",
        json={
            "title": title,
            "original_script": "one two three four five",
            "cta_text": "Go",
            "cast": [{"member_kind": "avatar", "avatar_id": aid, "role": "host"}],
        },
    ).json()["id"]
    auth_client.post(f"/api/projects/{pid}/generate-plan")
    auth_client.post(f"/api/projects/{pid}/generate-video")
    return pid


def test_export_renders_zip_contains_each_version(auth_client):
    pid = _rendered_project(auth_client)
    auth_client.post(f"/api/projects/{pid}/recompose")  # second version

    r = auth_client.get(f"/api/projects/{pid}/renders/export")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert "renders.zip" in r.headers["content-disposition"]

    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = z.namelist()
    # Two completed renders → v1 + v2, each MP4 + thumbnail; slugified title.
    assert "kissmet-specific-detail_v1.mp4" in names
    assert "kissmet-specific-detail_v2.mp4" in names
    assert "kissmet-specific-detail_v1.jpg" in names
    # MP4 entries are non-trivial.
    assert z.getinfo("kissmet-specific-detail_v2.mp4").file_size > 1000


def test_export_renders_404_when_no_completed_renders(auth_client):
    aid = auth_client.post("/api/avatars", json={"name": "X"}).json()["id"]
    pid = auth_client.post(
        "/api/projects",
        json={
            "title": "Empty",
            "original_script": "x",
            "cast": [{"member_kind": "avatar", "avatar_id": aid, "role": "host"}],
        },
    ).json()["id"]
    assert auth_client.get(f"/api/projects/{pid}/renders/export").status_code == 404


def test_export_renders_404_for_missing_project(auth_client):
    assert auth_client.get("/api/projects/999999/renders/export").status_code == 404
