"""Image uploads: iPhone HEIC/HEIF (also sent as octet-stream or with no
type), 50 MB limit with a clear 413, large photos scaled to 4096 px, and the
decompression-bomb guard — for Cast assets, ambient scene pictures and
brand logos."""
from __future__ import annotations

import io
import struct
import zlib
from pathlib import Path

import pytest
from PIL import Image

from .conftest import make_png_bytes


def make_heic_bytes(size=(1200, 900), color=(30, 140, 90)) -> bytes:
    pillow_heif = pytest.importorskip("pillow_heif")
    pillow_heif.register_heif_opener()
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="HEIF", quality=80)
    data = buf.getvalue()
    assert data[4:12] in (b"ftypheic", b"ftypmif1", b"ftypheix")  # a real HEIF container
    return data


def _avatar(client) -> int:
    return client.post("/api/avatars", json={"name": "N"}).json()["id"]


def _upload_asset(client, aid, name, data, mime):
    return client.post(f"/api/avatars/{aid}/assets", files={"file": (name, data, mime)},
                       data={"asset_type": "hero", "rights_confirmed": "true"})


def _asset_path(asset_id: int) -> str:
    from app import models
    from app.db import SessionLocal

    with SessionLocal() as db:
        return db.get(models.Asset, asset_id).file_path


def _scene(client) -> int:
    from .test_ambient import SHORT

    r = client.post("/api/ambient", json={"aspect_ratio": "16:9", "config": SHORT, "generate": False})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _png_with_claimed_size(w: int, h: int) -> bytes:
    """A tiny PNG whose header claims w×h (a decompression bomb's shape)."""
    raw = make_png_bytes((8, 8))
    ihdr = raw[8:33]
    body = struct.pack(">II", w, h) + ihdr[16:21]
    chunk = b"IHDR" + body
    new = struct.pack(">I", 13) + chunk + struct.pack(">I", zlib.crc32(chunk) & 0xFFFFFFFF)
    return raw[:8] + new + raw[33:]


# ---------- HEIC ----------


@pytest.mark.parametrize("name,mime", [
    ("IMG_0001.HEIC", "image/heic"),
    ("IMG_0001.heif", "image/heif"),
    ("IMG_0001.heic", "application/octet-stream"),  # Chrome/Windows
    ("IMG_0001.heic", ""),                          # no type at all
])
def test_heic_cast_upload_comes_back_as_jpeg(auth_client, name, mime):
    aid = _avatar(auth_client)
    r = _upload_asset(auth_client, aid, name, make_heic_bytes(), mime)
    assert r.status_code == 201, r.text
    a = r.json()
    assert a["mime_type"] == "image/jpeg"
    assert (a["width"], a["height"]) == (1200, 900)
    assert a["original_filename"] == name
    assert Path(_asset_path(a["id"])).suffix == ".jpg"
    got = auth_client.get(f"/api/assets/{a['id']}/preview")
    assert got.status_code == 200
    assert got.headers["content-type"] == "image/jpeg"
    with Image.open(io.BytesIO(got.content)) as img:
        assert img.format == "JPEG" and img.size == (1200, 900)
        assert not img.getexif()  # metadata stripped


def test_octet_stream_needs_an_image_name(auth_client):
    aid = _avatar(auth_client)
    r = _upload_asset(auth_client, aid, "notes.txt", make_heic_bytes(), "application/octet-stream")
    assert r.status_code == 400
    assert "HEIC/PNG/JPEG/WebP only" in r.json()["detail"]
    assert "application/octet-stream" in r.json()["detail"]


def test_unsupported_type_message_names_the_type_not_the_contents(auth_client):
    aid = _avatar(auth_client)
    r = _upload_asset(auth_client, aid, "x.pdf", b"%PDF-1.4 secret-contents", "application/pdf")
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail == "HEIC/PNG/JPEG/WebP only — this looks like application/pdf."
    assert "secret" not in detail


def test_gif_bytes_are_named_in_the_rejection(auth_client):
    buf = io.BytesIO()
    Image.new("RGB", (16, 16)).save(buf, format="GIF")
    aid = _avatar(auth_client)
    r = _upload_asset(auth_client, aid, "a.png", buf.getvalue(), "image/png")
    assert r.status_code == 400
    assert "this looks like a GIF image" in r.json()["detail"]


def test_heic_ambient_picture_upload(auth_client):
    sid = _scene(auth_client)
    for mime in ("image/heic", "application/octet-stream"):
        r = auth_client.post(f"/api/ambient/{sid}/picture",
                             files={"file": ("IMG_0002.HEIC", make_heic_bytes((1600, 900)), mime)})
        assert r.status_code == 200, r.text
        assert r.json()["picture_ready"] is True
        got = auth_client.get(f"/api/ambient/{sid}/picture")
        assert got.status_code == 200
        with Image.open(io.BytesIO(got.content)) as img:
            assert img.size == (1600, 900)


def test_heic_brand_logo_upload(auth_client):
    bid = auth_client.post("/api/brand-kits", json={"name": "B"}).json()["id"]
    r = auth_client.post(f"/api/brand-kits/{bid}/logo",
                         files={"file": ("logo.heic", make_heic_bytes((300, 200)), "")})
    assert r.status_code == 200, r.text
    got = auth_client.get(f"/api/brand-kits/{bid}/logo")
    assert got.status_code == 200
    with Image.open(io.BytesIO(got.content)) as img:
        assert img.format == "JPEG" and img.size == (300, 200)


# ---------- size limits ----------


def test_default_limit_is_50_mb():
    from app.config import Settings

    assert Settings().max_upload_bytes == 50 * 1024 * 1024


def test_oversize_upload_is_413_with_a_clear_message(auth_client, monkeypatch):
    from app.services import storage

    monkeypatch.setattr(storage.settings, "max_upload_bytes", 1024 * 1024)
    aid = _avatar(auth_client)
    big = make_png_bytes((8, 8)) + b"\0" * (3 * 1024 * 1024)
    r = _upload_asset(auth_client, aid, "big.png", big, "image/png")
    assert r.status_code == 413
    assert r.json()["detail"] == "That file is 3 MB; the limit is 1 MB."


def test_too_large_message_formats():
    from app.services.storage import too_large_message

    mb = 1024 * 1024
    assert too_large_message(62 * mb, 50 * mb) == "That file is 62 MB; the limit is 50 MB."
    # Just over: never "50 MB; the limit is 50 MB".
    assert too_large_message(50 * mb + 40_000, 50 * mb) == "That file is 50.0 MB; the limit is 50 MB."


def test_huge_dimensions_are_downscaled_to_4096(auth_client):
    aid = _avatar(auth_client)
    r = _upload_asset(auth_client, aid, "pano.png", make_png_bytes((6000, 1500)), "image/png")
    assert r.status_code == 201, r.text
    a = r.json()
    assert (a["width"], a["height"]) == (4096, 1024)
    with Image.open(_asset_path(a["id"])) as img:
        assert img.size == (4096, 1024)


def test_huge_heic_is_downscaled(auth_client):
    aid = _avatar(auth_client)
    r = _upload_asset(auth_client, aid, "big.heic", make_heic_bytes((3000, 5000)), "image/heic")
    assert r.status_code == 201, r.text
    assert max(r.json()["width"], r.json()["height"]) == 4096


def test_small_images_keep_their_size_and_format(auth_client):
    aid = _avatar(auth_client)
    r = _upload_asset(auth_client, aid, "s.png", make_png_bytes((640, 480)), "image/png")
    assert r.status_code == 201
    assert (r.json()["width"], r.json()["height"], r.json()["mime_type"]) == (640, 480, "image/png")


def test_decompression_bomb_is_refused_before_decoding(auth_client):
    aid = _avatar(auth_client)
    bomb = _png_with_claimed_size(40_000, 40_000)
    r = _upload_asset(auth_client, aid, "bomb.png", bomb, "image/png")
    assert r.status_code == 413
    assert "too many pixels" in r.json()["detail"] or "megapixels" in r.json()["detail"]
    # Just over our own cap but under Pillow's: still refused, from the header.
    r = _upload_asset(auth_client, aid, "bomb2.png", _png_with_claimed_size(11_000, 10_000), "image/png")
    assert r.status_code == 413
    assert "11000×10000 px" in r.json()["detail"]


def test_ambient_picture_bomb_is_refused(auth_client):
    sid = _scene(auth_client)
    r = auth_client.post(f"/api/ambient/{sid}/picture",
                         files={"file": ("b.png", _png_with_claimed_size(11_000, 10_000), "image/png")})
    assert r.status_code == 413
    assert "megapixels" in r.json()["detail"]


# ---------- ambient picture: the same error contract ----------


def test_ambient_picture_oversize_is_413_with_the_exact_message(auth_client, monkeypatch):
    from app.services import storage

    monkeypatch.setattr(storage.settings, "max_upload_bytes", 1024 * 1024)
    sid = _scene(auth_client)
    big = make_png_bytes((8, 8)) + b"\0" * (3 * 1024 * 1024)
    r = auth_client.post(f"/api/ambient/{sid}/picture", files={"file": ("big.png", big, "image/png")})
    assert r.status_code == 413
    assert r.json()["detail"] == "That file is 3 MB; the limit is 1 MB."


def test_ambient_picture_unsupported_type_is_415_naming_the_type(auth_client):
    sid = _scene(auth_client)
    r = auth_client.post(f"/api/ambient/{sid}/picture",
                         files={"file": ("doc.pdf", b"%PDF-1.4 secret", "application/pdf")})
    assert r.status_code == 415
    assert r.json()["detail"] == "HEIC/PNG/JPEG/WebP only — this looks like application/pdf."
    assert "secret" not in r.text


@pytest.mark.parametrize("mime", ["application/octet-stream", ""])
def test_ambient_picture_untyped_non_image_name_is_refused_before_decoding(auth_client, monkeypatch, mime):
    from app.services import ambient as amb

    def boom(*a, **k):  # pragma: no cover - must not be reached
        raise AssertionError("decoded a file the type check should have refused")

    monkeypatch.setattr(amb, "_crop_to_aspect", boom)
    sid = _scene(auth_client)
    r = auth_client.post(f"/api/ambient/{sid}/picture",
                         files={"file": ("notes.txt", make_png_bytes((800, 600)), mime)})
    assert r.status_code == 415


def test_config_no_longer_lists_untyped_mimes():
    from app.config import Settings

    mimes = Settings().allowed_image_mimes
    assert "application/octet-stream" not in mimes and "" not in mimes


# ---------- MPO (phone JPEGs with an MPF block) ----------


def make_mpo_bytes(size=(1200, 900)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 60, 40)).save(
        buf, format="MPO", save_all=True, append_images=[Image.new("RGB", size, (10, 10, 10))])
    data = buf.getvalue()
    with Image.open(io.BytesIO(data)) as img:
        assert img.format == "MPO"
    return data


def test_mpo_phone_jpeg_uploads_to_cast_as_jpeg(auth_client):
    aid = _avatar(auth_client)
    r = _upload_asset(auth_client, aid, "IMG_0100.JPG", make_mpo_bytes(), "image/jpeg")
    assert r.status_code == 201, r.text
    a = r.json()
    assert a["mime_type"] == "image/jpeg" and (a["width"], a["height"]) == (1200, 900)
    with Image.open(_asset_path(a["id"])) as img:
        assert img.format == "JPEG"
        assert img.getpixel((10, 10))[0] > 150  # the first frame, not the second


def test_mpo_normalizes_to_jpeg():
    from app.services import storage

    out, ext, mime, w, h = storage.normalize_uploaded_image(make_mpo_bytes((640, 480)))
    assert (ext, mime, w, h) == ("jpg", "image/jpeg", 640, 480)
    with Image.open(io.BytesIO(out)) as img:
        assert img.format == "JPEG"


def test_mpo_ambient_picture_upload(auth_client):
    sid = _scene(auth_client)
    r = auth_client.post(f"/api/ambient/{sid}/picture",
                         files={"file": ("IMG_0101.jpg", make_mpo_bytes((1600, 900)), "image/jpeg")})
    assert r.status_code == 200, r.text
    assert r.json()["picture_ready"] is True


def test_panorama_scene_picture_is_cropped_before_the_size_cap():
    """A 10000×1000 panorama keeps its full height: the 16:9 crop is taken
    from the original, not from a frame first shrunk to 4096 px wide."""
    from app.services.ambient import _crop_to_aspect

    out = Image.open(io.BytesIO(_crop_to_aspect(make_png_bytes((10000, 1000)), "16:9")))
    assert out.size == (1778, 1000)
