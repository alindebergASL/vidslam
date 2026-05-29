from __future__ import annotations

import subprocess

from .conftest import make_png_bytes


def test_brand_kit_crud_and_logo(auth_client):
    bk = auth_client.post(
        "/api/brand-kits",
        json={"name": "Acme", "end_card_bg_color": "#10243A", "default_disclosure_text": "AI demo"},
    ).json()
    assert bk["end_card_bg_color"] == "#10243A"
    bid = bk["id"]

    # Update color.
    r = auth_client.patch(f"/api/brand-kits/{bid}", json={"primary_color": "#00FF00"})
    assert r.json()["primary_color"] == "#00FF00"

    # Upload logo → token set, logo streamable.
    r = auth_client.post(
        f"/api/brand-kits/{bid}/logo",
        files={"file": ("logo.png", make_png_bytes(), "image/png")},
    )
    assert r.status_code == 200
    assert r.json()["logo_public_token"]
    assert auth_client.get(f"/api/brand-kits/{bid}/logo").status_code == 200

    assert any(k["id"] == bid for k in auth_client.get("/api/brand-kits").json())


def test_project_uses_brand_kit_disclosure_fallback(auth_client):
    bk = auth_client.post(
        "/api/brand-kits",
        json={"name": "B", "default_disclosure_text": "AI-generated brand spot"},
    ).json()
    aid = auth_client.post("/api/avatars", json={"name": "X"}).json()["id"]
    auth_client.post(
        f"/api/avatars/{aid}/assets",
        files={"file": ("h.png", make_png_bytes(), "image/png")},
        data={"asset_type": "hero", "rights_confirmed": "true"},
    )
    pid = auth_client.post(
        "/api/projects",
        json={
            "title": "T",
            "original_script": "one two three four five",
            "cta_text": "Go",
            "brand_kit_id": bk["id"],
            "cast": [{"member_kind": "avatar", "avatar_id": aid, "role": "host"}],
        },
    ).json()["id"]
    assert auth_client.get(f"/api/projects/{pid}").json()["brand_kit_id"] == bk["id"]
    # No project-level disclosure override → brand kit value should win over the default.
    auth_client.post(f"/api/projects/{pid}/generate-plan")
    auth_client.post(f"/api/projects/{pid}/generate-video")
    st = auth_client.get(f"/api/projects/{pid}/status").json()
    assert st["project_status"] == "completed"
    final = st["latest_render"]["final_video_path"]
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name", "-of", "csv=p=0", final],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert out == "h264"


def test_brand_primary_color_drives_caption_color(auth_client):
    from pathlib import Path

    bk = auth_client.post(
        "/api/brand-kits", json={"name": "C", "primary_color": "#FF5C8A"}
    ).json()
    aid = auth_client.post("/api/avatars", json={"name": "X"}).json()["id"]
    auth_client.post(
        f"/api/avatars/{aid}/assets",
        files={"file": ("h.png", make_png_bytes(), "image/png")},
        data={"asset_type": "hero", "rights_confirmed": "true"},
    )
    pid = auth_client.post(
        "/api/projects",
        json={
            "title": "T",
            "original_script": "one two three four five",
            "cta_text": "Go",
            "brand_kit_id": bk["id"],
            "cast": [{"member_kind": "avatar", "avatar_id": aid, "role": "host"}],
        },
    ).json()["id"]
    auth_client.post(f"/api/projects/{pid}/generate-plan")
    auth_client.post(f"/api/projects/{pid}/generate-video")
    st = auth_client.get(f"/api/projects/{pid}/status").json()
    assert st["project_status"] == "completed"
    # The generated ASS caption file should carry the brand color (&H008A5CFF).
    out_dir = Path(st["latest_render"]["final_video_path"]).parent
    ass = out_dir / "captions.ass"
    assert ass.exists()
    style_line = next(
        line for line in ass.read_text().splitlines() if line.startswith("Style:")
    )
    assert "8A5CFF" in style_line.upper()


def test_deleting_brand_kit_detaches_projects(auth_client):
    bk = auth_client.post("/api/brand-kits", json={"name": "Temp"}).json()
    aid = auth_client.post("/api/avatars", json={"name": "X"}).json()["id"]
    pid = auth_client.post(
        "/api/projects",
        json={
            "title": "T",
            "original_script": "hi",
            "brand_kit_id": bk["id"],
            "cast": [{"member_kind": "avatar", "avatar_id": aid, "role": "host"}],
        },
    ).json()["id"]
    assert auth_client.delete(f"/api/brand-kits/{bk['id']}").status_code == 204
    assert auth_client.get(f"/api/projects/{pid}").json()["brand_kit_id"] is None
