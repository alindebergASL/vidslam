"""Asset image types come from the file's bytes, never from a label.

Found on the live server: images saved before types were read from the
bytes could be JPEG data recorded as image/png, so the public asset URL
served them as PNG and HeyGen rejected them ("Content type not match
image/png != image/jpeg").
"""
from __future__ import annotations

import io
from pathlib import Path

from PIL import Image


def _jpeg(size=(96, 64)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (40, 120, 200)).save(buf, format="JPEG")
    return buf.getvalue()


def _avatar(client) -> int:
    return client.post("/api/avatars", json={"name": "A"}).json()["id"]


def test_upload_is_typed_by_its_contents(auth_client):
    aid = _avatar(auth_client)
    r = auth_client.post(f"/api/avatars/{aid}/assets",
                         files={"file": ("photo.png", _jpeg(), "image/png")},  # a JPEG labelled PNG
                         data={"asset_type": "hero", "rights_confirmed": "true"})
    assert r.status_code in (200, 201), r.text
    a = r.json()
    assert a["mime_type"] == "image/jpeg"
    from app import models
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        row = db.get(models.Asset, a["id"])
        assert Path(row.file_path).suffix == ".jpg"
        token = row.public_token
    finally:
        db.close()
    got = auth_client.get(f"/api/public-assets/{token}")
    assert got.status_code == 200 and got.headers["content-type"] == "image/jpeg"


def _broken_asset(tmp_path: Path, aid: int, **kw):
    from app import models
    from app.db import SessionLocal

    path = tmp_path / "old.png"
    path.write_bytes(_jpeg())  # JPEG data saved as .png, as before the fix
    db = SessionLocal()
    try:
        row = models.Asset(owner_kind="avatar", avatar_id=aid, file_path=str(path),
                           **{"mime_type": "image/png", "width": 0, "height": 0, **kw})
        db.add(row)
        db.commit()
        return row.id, row.public_token
    finally:
        db.close()


def test_repair_fixes_old_records_once(auth_client, tmp_path):
    from app import models
    from app.db import SessionLocal
    from app.services import storage

    aid = _avatar(auth_client)
    bad, token = _broken_asset(tmp_path, aid)
    audio = tmp_path / "voice.mp3"
    audio.write_bytes(b"ID3 not an image")
    db = SessionLocal()
    try:
        db.add(models.Asset(owner_kind="avatar", avatar_id=aid, file_path=str(audio), mime_type="audio/mpeg"))
        db.add(models.Asset(owner_kind="avatar", avatar_id=aid, file_path=str(tmp_path / "gone.png"),
                            mime_type="image/png"))
        db.commit()
        assert storage.repair_image_types(db) == 1
        row = db.get(models.Asset, bad)
        assert (row.mime_type, row.width, row.height) == ("image/jpeg", 96, 64)
        assert storage.repair_image_types(db) == 0  # already right: nothing to do
        others = {a.mime_type for a in db.query(models.Asset).filter(models.Asset.id != bad)}
        assert others == {"audio/mpeg", "image/png"}  # non-image and missing file untouched
    finally:
        db.close()
    assert auth_client.get(f"/api/public-assets/{token}").headers["content-type"] == "image/jpeg"


def test_startup_repairs_existing_assets(auth_client, tmp_path):
    from fastapi.testclient import TestClient

    from app import models
    from app.db import SessionLocal
    from app.main import app

    bad, _ = _broken_asset(tmp_path, _avatar(auth_client))
    with TestClient(app):  # runs the lifespan (startup) again
        pass
    db = SessionLocal()
    try:
        assert db.get(models.Asset, bad).mime_type == "image/jpeg"
    finally:
        db.close()
