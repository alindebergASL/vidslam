"""Studio picture operations: remove background and upscale, from a Studio
picture or a Cast picture (migration 0010, model kinds and routing, the
catalog's prices, the estimate, the endpoints and their guards, the worker,
Runware's request and spend, retry, and smaller references)."""
from __future__ import annotations

import io
import json
from pathlib import Path

import httpx
import pytest
import respx
from PIL import Image

from .conftest import make_png_bytes

API = "https://api.runware.ai/v1"
KEY = "rw-test-abcdefgh1234"
OUT_URL = "https://im.runware.ai/image/out.png"


def _go_live(monkeypatch, *, runware_key: bool = True) -> None:
    """Providers live (fake Runware key, nothing sleeps)."""
    from app import config
    from app.providers import runware
    from app.services import provider_keys

    monkeypatch.setenv("MOCK_PROVIDERS", "false")
    monkeypatch.setenv("RUNWARE_API_KEY", KEY if runware_key else "")
    monkeypatch.setattr(runware, "_sleep", lambda s: None)
    config.get_settings.cache_clear()
    provider_keys.invalidate()


def _avatar(client, name: str = "Mia") -> int:
    r = client.post("/api/avatars", json={"name": name})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _upload(client, aid: int, size=(64, 48), asset_type: str = "hero") -> int:
    r = client.post(
        f"/api/avatars/{aid}/assets",
        files={"file": ("h.png", make_png_bytes(size), "image/png")},
        data={"asset_type": asset_type, "rights_confirmed": "true"},
    )
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _picture(client, aid: int) -> dict:
    """A finished Studio picture (mock: 1024 × 1024 PNG)."""
    r = client.post("/api/studio/generate-image", json={"owner_kind": "avatar", "owner_id": aid, "prompt": "a cat"})
    assert r.status_code == 202, r.text
    job = client.get(f"/api/studio/jobs/{r.json()['id']}").json()
    assert job["status"] == "completed", job
    return job


def _job_with_file(tmp_path: Path, size=(64, 64), *, mode: str = "RGB", **kw) -> int:
    """A Studio job row with a picture file of `size` (no provider call)."""
    from app import models
    from app.db import SessionLocal

    path = tmp_path / f"src_{size[0]}x{size[1]}.png"
    Image.new(mode, size, (10, 120, 200) if mode == "RGB" else (10, 120, 200, 255)).save(path, format="PNG")
    fields = {"owner_kind": "avatar", "owner_id": 1, "output_kind": "image", "prompt": "p",
              "status": "completed", "result_path": str(path), **kw}
    with SessionLocal() as db:
        job = models.AssetGenerationJob(**fields)
        db.add(job)
        db.commit()
        return job.id


def _open(path: str) -> Image.Image:
    img = Image.open(path)
    img.load()
    return img


# ---------- migration 0010 ----------


def test_migration_0010_adds_operation_columns_with_defaults(tmp_path, monkeypatch):
    from alembic import command
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from sqlalchemy import inspect, text

    from app import config
    from app import db as dbmod

    url = f"sqlite:///{tmp_path / 'm.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    config.get_settings.cache_clear()
    dbmod.reset_engine(url)
    command.upgrade(dbmod._alembic_config(), "0009")
    assert "operation" not in {c["name"] for c in inspect(dbmod.engine).get_columns("asset_generation_jobs")}
    with dbmod.engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO asset_generation_jobs (owner_kind, owner_id, output_kind, duration_seconds, prompt, "
            "negative_prompt, provider, provider_model, provider_job_id, status, result_path, error, "
            "created_at, updated_at) VALUES ('avatar', 1, 'image', 5, 'old', '', '', '', '', 'completed', '', '', "
            "'2026-01-01', '2026-01-01')"))
    command.upgrade(dbmod._alembic_config(), "head")
    insp = inspect(dbmod.engine)
    cols = {c["name"] for c in insp.get_columns("asset_generation_jobs")}
    assert {"operation", "source_job_id", "source_asset_id", "options_json"} <= cols
    fks = [fk for fk in insp.get_foreign_keys("asset_generation_jobs")
           if fk["constrained_columns"] == ["source_asset_id"]]
    assert fks and fks[0]["referred_table"] == "assets" and fks[0]["options"].get("ondelete") == "SET NULL"
    with dbmod.engine.connect() as conn:
        row = conn.execute(text("SELECT operation, source_job_id, source_asset_id, options_json "
                                "FROM asset_generation_jobs")).one()
        assert tuple(row) == ("", None, None, None)
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0010"
        diff = compare_metadata(MigrationContext.configure(conn), dbmod.Base.metadata)
    assert diff == [], diff  # same as `alembic check`
    command.downgrade(dbmod._alembic_config(), "0009")
    assert "operation" not in {c["name"] for c in inspect(dbmod.engine).get_columns("asset_generation_jobs")}


# ---------- model kinds and routing ----------


def test_kinds_defaults_and_routing_never_openrouter(client, monkeypatch):
    from app.providers import get_image_op
    from app.providers.local_upscale import LocalUpscaleProvider
    from app.providers.mock_image_ops import MockImageOpProvider
    from app.providers.runware import RunwareImageOpProvider
    from app.services import model_registry as reg
    from app.services.provider_keys import ProviderUnavailable

    assert {"bg_removal", "upscale"} <= set(reg.KINDS)
    assert reg.MOCK_DEFAULTS["bg_removal"] == "mock/bg-removal" and reg.MOCK_DEFAULTS["upscale"] == "mock/upscale"
    assert reg.env_default("bg_removal") == "runware/runware:112@5"
    assert reg.env_default("upscale") == "runware/runware:504@1"
    snap = reg.snapshot()
    assert snap["upscale"]["model"] == "runware/runware:504@1"
    # Mock mode: the mock serves Runware ids; the free upscale always runs here.
    assert reg.route_info("upscale")[0] == "mock"
    assert reg.route_info("upscale", "local/lanczos") == ("local", None)
    assert reg.route_info("bg_removal", "local/lanczos")[0] == "unavailable"
    assert isinstance(get_image_op("upscale"), MockImageOpProvider)
    assert isinstance(get_image_op("upscale", "local/lanczos"), LocalUpscaleProvider)

    _go_live(monkeypatch)
    assert reg.route_info("bg_removal") == ("runware", None)
    assert reg.route_info("upscale", "runware/runware:500@1") == ("runware", None)
    assert reg.route_info("upscale", "mock/upscale") == ("mock", None)
    assert reg.route_info("upscale", "local/lanczos") == ("local", None)
    for kind in ("bg_removal", "upscale"):
        for model in ("openai/gpt-image-1", "sogni/flux", "some-model"):
            assert reg.route_info(kind, model)[0] == "unavailable", (kind, model)
    with pytest.raises(ProviderUnavailable, match="supported"):
        reg.require("upscale", "openai/gpt-image-1")
    assert isinstance(get_image_op("bg_removal"), RunwareImageOpProvider)
    with pytest.raises(ProviderUnavailable):
        get_image_op("bg_removal", "local/lanczos")

    # Without a Runware key: refused, never faked or sent elsewhere.
    _go_live(monkeypatch, runware_key=False)
    assert reg.route_info("bg_removal")[0] == "unavailable"


# ---------- catalog and prices ----------


def test_catalog_lists_operation_models_with_prices(client):
    from app.providers import runware
    from app.services import model_catalog

    entries = {e["id"]: e for e in runware.catalog_entries(True) if e["kind"] in ("bg_removal", "upscale")}
    for model, kind in (("runware/runware:112@5", "bg_removal"), ("runware/runware:109@1", "bg_removal"),
                        ("runware/bria:2@1", "bg_removal"), ("runware/runware:504@1", "upscale"),
                        ("runware/runware:500@1", "upscale"), ("runware/topazlabs:wonder@4", "upscale")):
        assert entries[model]["kind"] == kind and entries[model]["suggested"], model
        assert entries[model]["price_text"].startswith("about $"), model
    assert entries["runware/bria:2@1"]["pricing"]["image"] == 0.018
    esrgan = entries["runware/runware:504@1"]["pricing"]
    assert esrgan["x4"] > esrgan["x2"]  # priced by factor, not the ×2 measurement
    assert (esrgan["x2"], esrgan["x4"]) == (0.0006, 0.0013)
    topaz = entries["runware/topazlabs:wonder@4"]["pricing"]
    assert topaz["x4"] > topaz["x2"]  # by output megapixels
    assert runware.op_cost_usd("runware/topazlabs:wonder@4", "upscale", factor=4, out_megapixels=16.0) == 0.0856
    assert runware.op_cost_usd("runware/topazlabs:wonder@4", "upscale", factor=2, out_megapixels=3.0) == 0.0428
    assert runware.kind_of(["image-to-image", "remove-background"]) == "bg_removal"
    assert runware.kind_of(["image-to-image", "upscale"]) == "upscale"
    assert runware.kind_of(["text-to-image"]) == "image"
    local = model_catalog._local_entries()[0]
    assert (local["id"], local["kind"], local["provider"]) == ("local/lanczos", "upscale", "local")
    assert local["name"] == "Basic upscale (free, no AI)"


def test_operation_models_endpoint(auth_client):
    r = auth_client.get("/api/studio/operations/models", params={"op": "upscale"})
    assert r.status_code == 200, r.text
    by_id = {m["id"]: m for m in r.json()}
    assert by_id["runware/runware:504@1"]["active"] and by_id["runware/runware:504@1"]["factors"] == [2, 4]
    assert by_id["local/lanczos"]["free"] and by_id["local/lanczos"]["price_text"] == "free"
    r = auth_client.get("/api/studio/operations/models", params={"op": "remove_background"})
    ids = [m["id"] for m in r.json()]
    assert "runware/runware:112@5" in ids and "local/lanczos" not in ids


def test_studio_pickers_carry_price_text(auth_client):
    for path in ("/api/providers/openrouter/image-models", "/api/providers/openrouter/video-models"):
        r = auth_client.get(path)
        assert r.status_code == 200, r.text
        assert all("price_text" in e for e in r.json()), path
    from app.services.prices import price_text

    assert price_text({"pricing": {"image": 0.04}}, "image") == "about $0.04 per image"
    assert price_text({"price_per_second": 0.06}, "video") == "about $0.30 per 5 s"
    assert price_text({"pricing_skus": {"duration_seconds": "0.05"}}, "video") == "about $0.25 per 5 s"
    assert price_text({}, "image") == ""


# ---------- estimate ----------


def test_estimate_free_local_priced_runware_and_too_large(auth_client, tmp_path):
    small = _job_with_file(tmp_path, (1000, 800))
    r = auth_client.get("/api/studio/operations/estimate",
                        params={"op": "upscale", "model": "local/lanczos", "factor": 4, "source_job_id": small})
    assert r.status_code == 200, r.text
    e = r.json()
    assert e["free"] and e["cost_usd"] == 0 and e["provider"] == "local"
    assert (e["output_width"], e["output_height"], e["too_large"]) == (4000, 3200, False)
    assert e["message"] == "Free · 4000 × 3200 px"
    e = auth_client.get("/api/studio/operations/estimate",
                        params={"op": "upscale", "factor": 2, "source_job_id": small}).json()
    assert e["model"] == "runware/runware:504@1" and e["model_name"] == "Real-ESRGAN"
    assert e["cost_usd"] == 0.0006 and not e["free"] and e["is_estimate"]
    assert e["message"].startswith("About $0.0006")
    e = auth_client.get("/api/studio/operations/estimate",
                        params={"op": "remove_background", "source_job_id": small}).json()
    assert (e["output_width"], e["output_height"], e["factor"]) == (1000, 800, None)
    big = _job_with_file(tmp_path, (2500, 1200))
    e = auth_client.get("/api/studio/operations/estimate",
                        params={"op": "upscale", "factor": 2, "source_job_id": big}).json()
    assert e["too_large"] and e["output_width"] == 5000 and "4096" in e["message"]
    # Clarity takes ~1.15 MP: the picture is scaled down first, then ×2.
    e = auth_client.get("/api/studio/operations/estimate", params={
        "op": "upscale", "factor": 2, "model": "runware/runware:500@1", "source_job_id": big}).json()
    assert e["output_width"] * e["output_height"] <= 1153433 * 4 and not e["too_large"]
    assert e["output_width"] < 5000
    r = auth_client.get("/api/studio/operations/estimate",
                        params={"op": "upscale", "factor": 3, "source_job_id": small})
    assert r.status_code == 422
    # Nothing given: priced for a 1 MP picture, no size.
    e = auth_client.get("/api/studio/operations/estimate", params={"op": "remove_background"}).json()
    assert e["output_width"] is None and e["cost_usd"] == 0.0006
    # A model that can't do the operation.
    r = auth_client.get("/api/studio/operations/estimate",
                        params={"op": "remove_background", "model": "runware/runware:504@1"})
    assert r.status_code == 422
    r = auth_client.get("/api/studio/operations/estimate",
                        params={"op": "remove_background", "model": "local/lanczos"})
    assert r.status_code == 422


# ---------- endpoints and guards ----------


def test_remove_background_from_a_studio_picture(auth_client):
    aid = _avatar(auth_client)
    src = _picture(auth_client, aid)
    r = auth_client.post(f"/api/studio/jobs/{src['id']}/remove-background")
    assert r.status_code == 202, r.text
    out = r.json()
    assert out["batch_ids"] == [out["id"]]
    job = auth_client.get(f"/api/studio/jobs/{out['id']}").json()
    assert job["status"] == "completed", job
    assert (job["operation"], job["source_job_id"], job["source_asset_id"], job["options"]) == (
        "remove_background", src["id"], None, {})
    assert (job["owner_kind"], job["owner_id"], job["output_kind"]) == ("avatar", aid, "image")
    assert job["prompt"] == f"Background removed from #{src['id']}" and job["label"] == "No background"
    assert job["provider"] == "mock" and job["provider_model"] == "runware/runware:112@5"
    img = _open(job["result_path"])
    assert img.format == "PNG" and img.mode == "RGBA"
    assert img.getpixel((0, 0))[3] == 0  # outside: transparent
    assert img.getpixel((img.width // 2, img.height // 3))[3] == 255
    assert (job["result_width"], job["result_height"]) == img.size
    r = auth_client.get(f"/api/studio/jobs/{job['id']}/preview")
    assert r.headers["content-type"] == "image/png"
    # Its own events, like any Studio job.
    from app import models
    from app.db import SessionLocal

    with SessionLocal() as db:
        ev = db.query(models.Event).filter_by(entity_type="studio_job", entity_id=job["id"]).all()
        assert [e.status for e in ev] == ["completed"]


def test_upscale_mock_and_local(auth_client):
    aid = _avatar(auth_client)
    src = _picture(auth_client, aid)
    job = auth_client.post(f"/api/studio/jobs/{src['id']}/upscale", json={"factor": 2}).json()
    job = auth_client.get(f"/api/studio/jobs/{job['id']}").json()
    assert job["status"] == "completed" and job["options"] == {"factor": 2}
    assert job["prompt"] == f"Upscaled ×2 from #{src['id']}" and job["label"] == "Upscaled ×2"
    assert _open(job["result_path"]).size == (2048, 2048)
    small = auth_client.post(f"/api/studio/assets/{_upload(auth_client, aid, (300, 200))}/upscale",
                             json={"factor": 4, "model": "local/lanczos"}).json()
    small = auth_client.get(f"/api/studio/jobs/{small['id']}").json()
    assert small["status"] == "completed" and small["provider"] == "local"
    assert _open(small["result_path"]).size == (1200, 800)
    from app import models
    from app.db import SessionLocal

    with SessionLocal() as db:
        assert db.query(models.SpendEntry).count() == 0  # mock and local: nothing billed


def test_source_checks(auth_client, tmp_path):
    aid = _avatar(auth_client)
    src = _picture(auth_client, aid)
    clip = _job_with_file(tmp_path, output_kind="video_clip")
    running = _job_with_file(tmp_path, status="pending")
    missing = _job_with_file(tmp_path, result_path=str(tmp_path / "gone.png"))
    too_big = _job_with_file(tmp_path, (2500, 2000))
    assert auth_client.post("/api/studio/jobs/99999/remove-background").status_code == 404
    assert auth_client.delete(f"/api/studio/jobs/{src['id']}").status_code == 204
    r = auth_client.post(f"/api/studio/jobs/{src['id']}/upscale", json={"factor": 2})
    assert r.status_code == 404 and r.json()["detail"] == "That picture was deleted"
    r = auth_client.post(f"/api/studio/jobs/{clip}/remove-background")
    assert r.status_code == 422 and "not clips" in r.json()["detail"]
    r = auth_client.post(f"/api/studio/jobs/{running}/remove-background")
    assert r.status_code == 409 and r.json()["detail"] == "That picture isn't finished yet"
    r = auth_client.post(f"/api/studio/jobs/{missing}/remove-background")
    assert r.status_code == 409 and "missing" in r.json()["detail"]
    r = auth_client.post(f"/api/studio/jobs/{too_big}/upscale", json={"factor": 2})
    assert r.status_code == 422 and "4096" in r.json()["detail"]
    r = auth_client.post(f"/api/studio/jobs/{too_big}/upscale", json={"factor": 3})
    assert r.status_code == 422  # only ×2 or ×4
    r = auth_client.post(f"/api/studio/jobs/{too_big}/remove-background", json={"model": "runware/runware:504@1"})
    assert r.status_code == 422 and "can't remove backgrounds" in r.json()["detail"]
    assert auth_client.post("/api/studio/assets/99999/upscale", json={"factor": 2}).status_code == 404
    from app import models
    from app.db import SessionLocal

    with SessionLocal() as db:
        n_ops = db.query(models.AssetGenerationJob).filter(models.AssetGenerationJob.operation != "").count()
    assert n_ops == 0  # nothing was queued


def test_budget_provider_switch_and_rate_limit_guards(auth_client, monkeypatch, tmp_path):
    from app import config, models
    from app.db import SessionLocal
    from app.services import provider_keys, ratelimit

    src = _job_with_file(tmp_path, owner_id=_avatar(auth_client))
    _go_live(monkeypatch)
    # The provider switched off in Settings: 409 before anything is queued.
    provider_keys.set_enabled("runware", False)
    r = auth_client.post(f"/api/studio/jobs/{src}/remove-background")
    assert r.status_code == 409 and "Runware" in r.json()["detail"]
    provider_keys.set_enabled("runware", True)
    # Over the monthly budget's hard cap: 402.
    auth_client.put("/api/spend/budget", json={"monthly_usd": 1, "hard_stop": True, "alert_pcts": []})
    with SessionLocal() as db:
        db.add(models.SpendEntry(provider="runware", kind="image", model="m", cost_usd=2.0))
        db.commit()
    for url, body in ((f"/api/studio/jobs/{src}/remove-background", None),
                      (f"/api/studio/jobs/{src}/upscale", {"factor": 2})):
        r = auth_client.post(url, json=body)
        assert r.status_code == 402, (url, r.text)
    # Throttled like every paid endpoint.
    monkeypatch.setenv("MOCK_PROVIDERS", "true")
    monkeypatch.setenv("RATE_LIMIT_GENERATION_BURST", "1")
    monkeypatch.setenv("RATE_LIMIT_GENERATION_PER_MINUTE", "1")
    config.get_settings.cache_clear()
    ratelimit._limiter = None
    assert auth_client.post(f"/api/studio/jobs/{src}/upscale", json={"factor": 2}).status_code == 202
    r = auth_client.post(f"/api/studio/jobs/{src}/upscale", json={"factor": 2})
    assert r.status_code == 429


def test_cast_picture_result_belongs_to_that_cast_member(auth_client):
    aid = _avatar(auth_client)
    _avatar(auth_client, "Other")
    asset = _upload(auth_client, aid, (80, 60))
    r = auth_client.post(f"/api/studio/assets/{asset}/remove-background")
    assert r.status_code == 202, r.text
    job = auth_client.get(f"/api/studio/jobs/{r.json()['id']}").json()
    assert (job["owner_kind"], job["owner_id"]) == ("avatar", aid)
    assert (job["source_asset_id"], job["source_job_id"]) == (asset, None)
    assert job["prompt"] == f"Background removed from Cast picture #{asset}"
    # "Save back to this slot": an ordinary save, same asset type.
    r = auth_client.post(f"/api/studio/jobs/{job['id']}/save", json={"asset_type": "hero"})
    assert r.status_code == 200, r.text
    assert r.json()["mime_type"] == "image/png" and r.json()["asset_type"] == "hero"


def test_retry_reruns_the_operation(auth_client):
    aid = _avatar(auth_client)
    src = _picture(auth_client, aid)
    first = auth_client.post(f"/api/studio/jobs/{src['id']}/upscale", json={"factor": 2}).json()
    r = auth_client.post(f"/api/studio/jobs/{first['id']}/retry", json={"prompt": "ignored"})
    assert r.status_code == 202, r.text
    again = auth_client.get(f"/api/studio/jobs/{r.json()['id']}").json()
    assert again["id"] != first["id"]
    assert (again["operation"], again["source_job_id"], again["options"]) == ("upscale", src["id"], {"factor": 2})
    assert again["prompt"] == first["prompt"] and again["status"] == "completed"
    assert _open(again["result_path"]).size == (2048, 2048)
    # "Try another model": the same operation with the model given.
    r = auth_client.post(f"/api/studio/jobs/{first['id']}/retry", json={"model": "local/lanczos"})
    other = auth_client.get(f"/api/studio/jobs/{r.json()['id']}").json()
    assert (other["provider_model"], other["provider"], other["operation"]) == ("local/lanczos", "local", "upscale")
    # The model must fit the operation.
    cut = auth_client.post(f"/api/studio/jobs/{src['id']}/remove-background").json()
    r = auth_client.post(f"/api/studio/jobs/{cut['id']}/retry", json={"model": "local/lanczos"})
    assert r.status_code == 422
    # The source deleted since: the retry fails plainly.
    auth_client.delete(f"/api/studio/jobs/{src['id']}")
    r = auth_client.post(f"/api/studio/jobs/{first['id']}/retry")
    assert r.status_code == 404


def test_source_deleted_while_queued_fails_the_job(auth_client):
    from app import models
    from app.db import SessionLocal
    from app.services import pipeline

    aid = _avatar(auth_client)
    src = _picture(auth_client, aid)
    with SessionLocal() as db:
        job = models.AssetGenerationJob(owner_kind="avatar", owner_id=aid, output_kind="image", prompt="p",
                                        operation="upscale", source_job_id=src["id"], options_json={"factor": 2},
                                        status="pending")
        db.add(job)
        db.commit()
        jid = job.id
    auth_client.delete(f"/api/studio/jobs/{src['id']}")
    with SessionLocal() as db, pytest.raises(ValueError):
        pipeline.run_studio_job(db, jid)
    job = auth_client.get(f"/api/studio/jobs/{jid}").json()
    assert job["status"] == "failed" and "deleted" in job["error"]


def test_generated_jobs_report_no_operation(auth_client):
    aid = _avatar(auth_client)
    job = _picture(auth_client, aid)
    assert (job["operation"], job["source_job_id"], job["source_asset_id"], job["options"]) == ("", None, None, {})


# ---------- Runware ----------


def _png(size=(40, 30), mode="RGB") -> bytes:
    buf = io.BytesIO()
    Image.new(mode, size, (1, 2, 3) if mode == "RGB" else (1, 2, 3, 0)).save(buf, format="PNG")
    return buf.getvalue()


def test_runware_request_bodies_and_spend(auth_client, monkeypatch):
    from app import models
    from app.db import SessionLocal

    aid = _avatar(auth_client)
    asset = _upload(auth_client, aid, (40, 30))
    _go_live(monkeypatch)
    sent: list[dict] = []
    costs = iter([0.0006, None])

    def answer(request):
        task = json.loads(request.content)[0]
        sent.append(task)
        row = {"taskType": task["taskType"], "taskUUID": task["taskUUID"], "imageURL": OUT_URL}
        cost = next(costs)
        if cost is not None:
            row["cost"] = cost
        return httpx.Response(200, json={"data": [row]})

    with respx.mock() as router:
        router.post(API).mock(side_effect=answer)
        router.get(OUT_URL).mock(return_value=httpx.Response(200, content=_png((40, 30), "RGBA")))
        cut = auth_client.post(f"/api/studio/assets/{asset}/remove-background").json()
        big = auth_client.post(f"/api/studio/assets/{asset}/upscale", json={"factor": 4}).json()
    rb, up = sent
    assert rb["taskType"] == "removeBackground" and rb["model"] == "runware:112@5"
    assert rb["inputs"]["image"].startswith("data:image/png;base64,")
    assert rb["outputFormat"] == "PNG" and rb["includeCost"] is True and rb["deliveryMethod"] == "sync"
    assert "positivePrompt" not in rb and "safety" not in rb  # no prompt; a Cast picture is allowed
    assert up["taskType"] == "upscale" and up["model"] == "runware:504@1"
    assert up["upscaleFactor"] == 4 and up["outputFormat"] == "JPG"  # opaque picture
    with SessionLocal() as db:
        rows = {r.ref: r for r in db.query(models.SpendEntry).all()}
        a, b = rows[f"studio_job:{cut['id']}"], rows[f"studio_job:{big['id']}"]
        assert (a.provider, a.kind, a.cost_usd, a.is_estimate, a.note) == (
            "runware", "image", 0.0006, False, "remove background")
        assert a.generation_id == rb["taskUUID"]
        assert (b.cost_usd, b.is_estimate) == (0.0013, True) and b.note.startswith("upscale")
    job = auth_client.get(f"/api/studio/jobs/{cut['id']}").json()
    assert job["status"] == "completed" and job["provider"] == "runware"
    assert job["cost_usd"] == 0.0006 and job["cost_is_estimate"] is False


def test_runware_keeps_transparency_and_fits_input_limits(client, monkeypatch):
    from app.providers import runware

    _go_live(monkeypatch)
    t = runware.build_operation_task("runware/runware:504@1", "upscale", _png(mode="RGBA"), "image/png",
                                     {"factor": 2}, keep_alpha=True)
    assert t["outputFormat"] == "PNG" and t["upscaleFactor"] == 2
    with pytest.raises(runware.RunwareError, match="×3"):
        runware.build_operation_task("runware/runware:504@1", "upscale", _png(), "image/png", {"factor": 3})
    with pytest.raises(runware.RunwareError, match="can't remove backgrounds"):
        runware.build_operation_task("runware/runware:504@1", "remove_background", _png(), "image/png")
    data, mime, w, h, alpha = runware._prepared_input("runware/runware:500@1", _png((2000, 1500)), "image/png")
    assert w * h <= 1153433 and not alpha and _open_bytes(data).size == (w, h)


def _open_bytes(raw: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(raw))
    img.load()
    return img


# ---------- references sent to providers ----------


def test_big_cast_pictures_go_out_scaled_down(client, tmp_path):
    from app.services import storage

    big = tmp_path / "big.png"
    Image.new("RGB", (4096, 2048), (5, 5, 5)).save(big)
    small = storage.reference_copy(big)
    assert small != big and Image.open(small).size == (2048, 1024)
    assert Image.open(big).size == (4096, 2048)  # the stored picture is unchanged
    assert storage.reference_copy(big) == small  # cached
    tiny = tmp_path / "tiny.png"
    Image.new("RGB", (100, 100)).save(tiny)
    assert storage.reference_copy(tiny) == tiny


# ---------- review fixes ----------


def test_openrouter_image_prices_are_not_per_input_image_and_never_zero():
    from app.services.prices import money, price_text

    # OpenRouter's pricing.image is per INPUT image; output is token-billed.
    gemini = {"provider": "openrouter", "pricing": {"image": 0.0012, "image_output": 3e-05}}
    assert price_text(gemini, "image") == ""
    assert price_text({"provider": "openrouter", "pricing": {"image_output": 3e-05}}, "image") == ""
    # Built locally (Runware, Sogni): a real per-picture price.
    assert price_text({"provider": "sogni", "pricing": {"image": 0.04}}, "image") == "about $0.04 per image"
    assert price_text({"provider": "runware", "pricing": {"image_output": 3e-05}}, "image") == ""
    assert money(0.00004) == "" and money(0.0006) == "$0.0006" and money(0) == "$0"
    assert price_text({"provider": "runware", "pricing": {"image": 0.00001}}, "image") == ""


def test_measured_upscale_prices_follow_picture_size():
    from app.providers import runware

    one = runware.op_cost_usd("runware/runware:504@1", "upscale", factor=2, out_megapixels=1024 * 1024 * 4 / 1e6)
    four = runware.op_cost_usd("runware/runware:504@1", "upscale", factor=2, out_megapixels=2048 * 2048 * 4 / 1e6)
    assert one == 0.0006 and four == 0.0024
    small = runware.op_cost_usd("runware/runware:504@1", "upscale", factor=4, out_megapixels=512 * 512 * 16 / 1e6)
    assert small == 0.0013  # never below the measured price


def test_topaz_tiers_count_binary_megapixels():
    from app.providers import runware

    # 1024 px square ×4 = 4096 × 4096 = the 16 MP tier, not 24.
    assert runware.op_cost_usd("runware/topazlabs:wonder@4", "upscale", factor=4,
                               out_megapixels=4096 * 4096 / 1e6) == 0.0856
    assert runware.op_cost_usd("runware/topazlabs:wonder@4", "upscale", factor=4,
                               out_megapixels=5000 * 5000 / 1e6) == 0.1284
    topaz = {e["id"]: e for e in runware.catalog_entries(True)}["runware/topazlabs:wonder@4"]["pricing"]
    assert topaz["x4"] == 0.0856


def test_estimate_scales_with_source_size(auth_client, tmp_path):
    big = _job_with_file(tmp_path, (2048, 2048))
    e = auth_client.get("/api/studio/operations/estimate",
                        params={"op": "upscale", "model": "runware/runware:504@1", "factor": 2,
                                "source_job_id": big}).json()
    assert e["cost_usd"] == 0.0024, e


def test_public_asset_full_size_for_the_app_reference_copy_for_providers(auth_client, tmp_path):
    from app.services import storage

    aid = _avatar(auth_client)
    r = auth_client.post(
        f"/api/avatars/{aid}/assets",
        files={"file": ("h.png", make_png_bytes((3000, 1500)), "image/png")},
        data={"asset_type": "hero", "rights_confirmed": "true"},
    )
    assert r.status_code in (200, 201), r.text
    token = r.json()["public_token"]
    full = auth_client.get(f"/api/public-assets/{token}")
    assert full.status_code == 200 and _open_bytes(full.content).size == (3000, 1500)
    url = storage.public_url_for_token(token)
    assert url.endswith(f"/api/public-assets/{token}?ref=1")
    small = auth_client.get(f"/api/public-assets/{token}?ref=1")
    assert small.status_code == 200 and _open_bytes(small.content).size == (2048, 1024)


def test_reference_copies_are_cleaned_up(client, tmp_path):
    import os
    import time

    from app.services import storage

    big = tmp_path / "big.png"
    Image.new("RGB", (3000, 1000)).save(big)
    copy = storage.reference_copy(big)
    assert copy != big and copy.exists()
    storage.drop_reference_copy(big)
    assert not copy.exists()
    copy = storage.reference_copy(big)
    old = time.time() - storage.REFERENCE_CACHE_MAX_AGE_SECONDS - 60
    os.utime(copy, (old, old))
    assert storage.prune_reference_cache() >= 1 and not copy.exists()
    storage.drop_reference_copy(tmp_path / "gone.png")  # no file: nothing happens


def test_deleting_a_cast_picture_drops_its_reference_copy(auth_client):
    from pathlib import Path as P

    from app import models
    from app.db import SessionLocal
    from app.services import storage

    aid = _avatar(auth_client)
    r = auth_client.post(
        f"/api/avatars/{aid}/assets",
        files={"file": ("h.png", make_png_bytes((2600, 1300)), "image/png")},
        data={"asset_type": "hero", "rights_confirmed": "true"},
    )
    asset_id = r.json()["id"]
    with SessionLocal() as db:
        path = P(db.get(models.Asset, asset_id).file_path)
    copy = storage.reference_copy(path)
    assert copy != path and copy.exists()
    assert auth_client.delete(f"/api/assets/{asset_id}").status_code in (200, 204)
    assert not copy.exists()


def test_upscale_puts_back_transparency_the_upscaler_dropped(client, monkeypatch):
    from app.providers import runware

    _go_live(monkeypatch)
    src = Image.new("RGBA", (40, 30), (200, 10, 10, 255))
    for x in range(20):
        for y in range(30):
            src.putpixel((x, y), (0, 0, 0, 0))
    buf = io.BytesIO()
    src.save(buf, format="PNG")
    opaque = io.BytesIO()
    Image.new("RGB", (80, 60), (255, 255, 255)).save(opaque, format="PNG")
    assert not runware.keeps_transparency("runware/runware:504@1")
    with respx.mock() as router:
        router.post(API).mock(side_effect=lambda req: httpx.Response(200, json={"data": [
            {"taskType": "upscale", "taskUUID": json.loads(req.content)[0]["taskUUID"], "imageURL": OUT_URL,
             "cost": 0.0006}]}))
        router.get(OUT_URL).mock(return_value=httpx.Response(200, content=opaque.getvalue()))
        out = runware.RunwareImageOpProvider(api_key=KEY).run(
            "upscale", "runware/runware:504@1", buf.getvalue(), "image/png", {"factor": 2})
    img = _open_bytes(out)
    assert img.mode == "RGBA" and img.size == (80, 60)
    assert img.getpixel((5, 30))[3] == 0 and img.getpixel((75, 30))[3] == 255
    # A result that kept its transparency is left as it is.
    kept = runware.restore_alpha(buf.getvalue(), _png((80, 60), "RGBA"))
    assert kept == _png((80, 60), "RGBA")


def test_free_upscale_ignores_the_budget_cap(auth_client, tmp_path, monkeypatch):
    from app import models
    from app.db import SessionLocal

    src = _job_with_file(tmp_path, owner_id=_avatar(auth_client))
    _go_live(monkeypatch)
    auth_client.put("/api/spend/budget", json={"monthly_usd": 1, "hard_stop": True, "alert_pcts": []})
    with SessionLocal() as db:
        db.add(models.SpendEntry(provider="runware", kind="image", model="m", cost_usd=2.0))
        db.commit()
    r = auth_client.post(f"/api/studio/jobs/{src}/upscale", json={"factor": 2, "model": "local/lanczos"})
    assert r.status_code == 202, r.text
    r = auth_client.post(f"/api/studio/jobs/{r.json()['id']}/retry")
    assert r.status_code == 202, r.text
    r = auth_client.post(f"/api/studio/jobs/{src}/upscale", json={"factor": 2})
    assert r.status_code == 402, r.text


def test_operation_models_say_whether_they_run_and_keep_the_active_price(auth_client, monkeypatch):
    from app.services import image_ops, provider_keys

    # Mock mode: Runware's models run through the placeholder, so they're
    # listed, usable and priced even with Runware off.
    provider_keys.set_enabled("runware", False)
    by_id = {m["id"]: m for m in image_ops.models_for("upscale")}
    assert by_id["runware/runware:504@1"]["usable"] and by_id["runware/runware:504@1"]["price_text"]
    provider_keys.set_enabled("runware", True)
    # Live without a Runware key: the active one is still priced but can't run.
    _go_live(monkeypatch, runware_key=False)
    models_ = image_ops.models_for("upscale")
    active = next(m for m in models_ if m["active"])
    assert active["id"] == "runware/runware:504@1"
    assert active["price_text"].startswith("about $") and active["usable"] is False
    assert next(m for m in models_ if m["id"] == "local/lanczos")["usable"]
    rb = next(m for m in image_ops.models_for("remove_background") if m["active"])
    assert rb["usable"] is False and rb["price_text"].startswith("about $")
