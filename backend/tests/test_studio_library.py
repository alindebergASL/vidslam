"""Studio library: favorite + label columns (migration 0004), list filters
and paging, PATCH + bulk favorite/delete/save, per-job cost from the spend
ledger, retry with another model, and character sheets."""
from __future__ import annotations

from pathlib import Path

import pytest

from .conftest import make_png_bytes
from .test_provider_onoff import _off, live  # noqa: F401 — fixture


@pytest.fixture
def image_calls(monkeypatch):
    from app.providers import mock_image

    calls: list[dict] = []
    real = mock_image.MockImageProvider.generate

    def spy(self, **kw):
        calls.append(kw)
        return real(self, **kw)

    monkeypatch.setattr(mock_image.MockImageProvider, "generate", spy)
    return calls


def _avatar(client, name: str = "Mia", **extra) -> int:
    return client.post("/api/avatars", json={"name": name, **extra}).json()["id"]


def _ingredient(client) -> int:
    return client.post("/api/ingredients", json={"name": "Roof", "kind": "scene"}).json()["id"]


def _upload(client, aid: int, asset_type: str = "hero") -> int:
    r = client.post(
        f"/api/avatars/{aid}/assets",
        files={"file": ("h.png", make_png_bytes(), "image/png")},
        data={"asset_type": asset_type, "rights_confirmed": "true"},
    )
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _job(**kw) -> int:
    """A studio job row straight in the database (no provider call)."""
    from app import models
    from app.db import SessionLocal

    fields = {"owner_kind": "avatar", "owner_id": 1, "output_kind": "image", "prompt": "p",
              "status": "completed", **kw}
    with SessionLocal() as db:
        job = models.AssetGenerationJob(**fields)
        db.add(job)
        db.commit()
        return job.id


def _image(client, owner_id: int, prompt: str = "a picture", **extra) -> dict:
    r = client.post("/api/studio/generate-image", json={
        "owner_kind": "avatar", "owner_id": owner_id, "prompt": prompt, **extra})
    assert r.status_code == 202, r.text
    return client.get(f"/api/studio/jobs/{r.json()['id']}").json()


def _ids(client, **params) -> list[int]:
    r = client.get("/api/studio/jobs", params=params)
    assert r.status_code == 200, r.text
    return [j["id"] for j in r.json()]


# ---------- model + migration ----------


def test_new_jobs_default_to_not_favorite_and_no_label(auth_client):
    from app import models
    from app.db import SessionLocal

    jid = _job()
    with SessionLocal() as db:
        j = db.get(models.AssetGenerationJob, jid)
        assert j.favorite is False and j.label == ""
    out = auth_client.get(f"/api/studio/jobs/{jid}").json()
    assert out["favorite"] is False and out["label"] == ""
    assert out["cost_usd"] is None and out["cost_is_estimate"] is False


def test_migration_0004_adds_columns_with_defaults(tmp_path, monkeypatch):
    import sqlite3

    from alembic import command
    from sqlalchemy import inspect

    from app import config
    from app import db as dbmod

    url = f"sqlite:///{tmp_path / 'm.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    config.get_settings.cache_clear()
    dbmod.reset_engine(url)
    command.upgrade(dbmod._alembic_config(), "0003")
    con = sqlite3.connect(tmp_path / "m.db")
    con.execute("insert into asset_generation_jobs (owner_kind, owner_id, output_kind, duration_seconds,"
                " aspect_ratio, prompt, negative_prompt, reference_asset_ids_json, provider, provider_model,"
                " provider_job_id, status, result_path, error, created_at, updated_at) values"
                " ('avatar',1,'image',5,'','old','','[]','mock','m','','completed','','',"
                " datetime('now'),datetime('now'))")
    con.commit()
    con.close()
    command.upgrade(dbmod._alembic_config(), "0004")
    cols = {c["name"]: c for c in inspect(dbmod.engine).get_columns("asset_generation_jobs")}
    assert cols["favorite"]["nullable"] is False and cols["label"]["nullable"] is False
    con = sqlite3.connect(tmp_path / "m.db")
    assert con.execute("select favorite, label from asset_generation_jobs").fetchall() == [(0, "")]
    con.close()
    command.downgrade(dbmod._alembic_config(), "0003")
    names = {c["name"] for c in inspect(dbmod.engine).get_columns("asset_generation_jobs")}
    assert "favorite" not in names and "label" not in names


# ---------- list filters + paging ----------


def test_list_filters_each_and_combined(auth_client):
    aid, other = _avatar(auth_client), _avatar(auth_client, "Bo")
    iid = _ingredient(auth_client)
    a = _job(owner_id=aid, prompt="Mia on a ROOFTOP at dusk", favorite=True)
    b = _job(owner_id=aid, prompt="Mia in a kitchen", status="failed")
    c = _job(owner_id=aid, prompt="studio shot", label="Front view", status="saved", favorite=True)
    d = _job(owner_id=other, prompt="rooftop again", output_kind="video_clip", status="polling")
    e = _job(owner_kind="ingredient", owner_id=iid, prompt="rooftop scene", status="pending")
    _job(owner_id=aid, prompt="rooftop gone", status="deleted")

    assert _ids(auth_client) == [e, d, c, b, a]  # newest first, deleted hidden
    assert _ids(auth_client, q="  rooftop ") == [e, d, a]  # case-insensitive, trimmed
    assert _ids(auth_client, q="front") == [c]  # matches the label
    assert _ids(auth_client, q="   ") == [e, d, c, b, a]
    assert _ids(auth_client, q="100%") == []  # wildcards are literal
    assert _ids(auth_client, favorite="true") == [c, a]
    assert _ids(auth_client, favorite="false") == [e, d, c, b, a]
    assert _ids(auth_client, output_kind="video_clip") == [d]
    assert _ids(auth_client, output_kind="image") == [e, c, b, a]
    assert _ids(auth_client, owner_kind="avatar", owner_id=aid) == [c, b, a]
    assert _ids(auth_client, owner_kind="ingredient", owner_id=iid) == [e]
    assert _ids(auth_client, status="completed") == [c, a]  # saved counts as completed
    assert _ids(auth_client, status="failed") == [b]
    assert _ids(auth_client, status="active") == [e, d]
    # Combined.
    assert _ids(auth_client, q="rooftop", owner_kind="avatar", owner_id=aid, status="completed",
                favorite="true", output_kind="image") == [a]
    assert _ids(auth_client, q="rooftop", status="active", output_kind="image") == [e]


def test_list_pages_with_before_id_and_limit(auth_client):
    ids = [_job(prompt=f"p{i}") for i in range(7)]
    newest_first = ids[::-1]
    page = _ids(auth_client, limit=3)
    assert page == newest_first[:3]
    page2 = _ids(auth_client, limit=3, before_id=page[-1])
    assert page2 == newest_first[3:6]
    assert _ids(auth_client, limit=3, before_id=page2[-1]) == newest_first[6:]
    assert _ids(auth_client, before_id=ids[0]) == []
    for bad in (0, 201):
        assert auth_client.get("/api/studio/jobs", params={"limit": bad}).status_code == 422
    assert len(_ids(auth_client, limit=200)) == 7


def test_list_default_limit_is_50(auth_client):
    from app import models
    from app.db import SessionLocal

    with SessionLocal() as db:
        db.add_all([models.AssetGenerationJob(owner_kind="avatar", owner_id=1, prompt=str(i),
                                              status="completed") for i in range(55)])
        db.commit()
    assert len(_ids(auth_client)) == 50


def test_owner_filter_needs_both_halves(auth_client):
    for params in ({"owner_kind": "avatar"}, {"owner_id": 1}):
        assert auth_client.get("/api/studio/jobs", params=params).status_code == 422
    assert auth_client.get("/api/studio/jobs", params={"owner_kind": "robot", "owner_id": 1}).status_code == 422
    assert auth_client.get("/api/studio/jobs", params={"status": "nope"}).status_code == 422


# ---------- favorite + label ----------


def test_patch_favorite_and_label(auth_client):
    jid = _job()
    r = auth_client.patch(f"/api/studio/jobs/{jid}", json={"favorite": True, "label": "  Hero pick  "})
    assert r.status_code == 200, r.text
    assert r.json()["favorite"] is True and r.json()["label"] == "Hero pick"
    # A field left out stays as is.
    r = auth_client.patch(f"/api/studio/jobs/{jid}", json={"favorite": False})
    assert r.json()["favorite"] is False and r.json()["label"] == "Hero pick"
    assert auth_client.patch(f"/api/studio/jobs/{jid}", json={"label": "x" * 81}).status_code == 422
    # Stripped before the length check.
    assert auth_client.patch(f"/api/studio/jobs/{jid}", json={"label": " " + "x" * 80 + " "}).status_code == 200
    assert auth_client.patch("/api/studio/jobs/99999", json={"favorite": True}).status_code == 404
    gone = _job(status="deleted")
    assert auth_client.patch(f"/api/studio/jobs/{gone}", json={"favorite": True}).status_code == 404


def test_bulk_favorite(auth_client):
    a, b = _job(), _job()
    gone = _job(status="deleted")
    r = auth_client.post("/api/studio/jobs/bulk-favorite", json={"ids": [a, b, gone, 99999], "favorite": True})
    assert r.status_code == 200, r.text
    assert r.json() == {"updated": [a, b], "missing": [gone, 99999]}
    assert _ids(auth_client, favorite="true") == [b, a]
    auth_client.post("/api/studio/jobs/bulk-favorite", json={"ids": [a], "favorite": False})
    assert _ids(auth_client, favorite="true") == [b]


def test_bulk_ids_are_validated(auth_client):
    for ids in ([], [1, 1], list(range(1, 202))):
        r = auth_client.post("/api/studio/jobs/bulk-delete", json={"ids": ids})
        assert r.status_code == 422, ids
    assert auth_client.post("/api/studio/jobs/bulk-favorite", json={"ids": [1]}).status_code == 422


def test_worker_leaves_favorite_and_label_alone(auth_client):
    from app.db import SessionLocal
    from app.services.pipeline import run_studio_job

    aid = _avatar(auth_client)
    jid = _job(owner_id=aid, status="pending", favorite=True, label="Front view")
    with SessionLocal() as db:
        run_studio_job(db, jid)
    j = auth_client.get(f"/api/studio/jobs/{jid}").json()
    assert j["status"] == "completed" and j["favorite"] is True and j["label"] == "Front view"


# ---------- bulk delete ----------


def test_bulk_delete_tombstones_hides_and_reports_missing(auth_client):
    from app import models
    from app.db import SessionLocal

    aid = _avatar(auth_client)
    a = _image(auth_client, aid)
    b = _image(auth_client, aid)
    keep = _image(auth_client, aid)
    clip = _job(owner_id=aid, output_kind="video_clip", status="polling")
    with SessionLocal() as db:
        db.add(models.VideoJob(purpose="studio", ref_id=clip))
        db.commit()
    gone = _job(status="deleted")
    r = auth_client.post("/api/studio/jobs/bulk-delete", json={"ids": [a["id"], b["id"], clip, gone, 99999]})
    assert r.status_code == 200, r.text
    assert r.json() == {"deleted": [a["id"], b["id"], clip], "missing": [gone, 99999]}
    assert _ids(auth_client) == [keep["id"]]
    for jid in (a["id"], b["id"], clip):
        assert auth_client.get(f"/api/studio/jobs/{jid}").status_code == 404
    assert not Path(a["result_path"]).exists() and not Path(b["result_path"]).exists()
    assert Path(keep["result_path"]).exists()
    with SessionLocal() as db:
        rows = {j.id: j for j in db.query(models.AssetGenerationJob).all()}
        assert rows[a["id"]].status == "deleted" and rows[a["id"]].result_path == ""
        assert db.query(models.VideoJob).one().ref_id == 0


def test_bulk_delete_keeps_a_file_an_asset_still_uses(auth_client):
    from app import models
    from app.db import SessionLocal

    aid = _avatar(auth_client)
    a = _image(auth_client, aid)
    # An asset saved before saves made copies points at the job's own file.
    with SessionLocal() as db:
        db.add(models.Asset(owner_kind="avatar", avatar_id=aid, file_path=a["result_path"],
                            mime_type="image/png", generation_job_id=a["id"]))
        db.commit()
    r = auth_client.post("/api/studio/jobs/bulk-delete", json={"ids": [a["id"]]})
    assert r.json()["deleted"] == [a["id"]]
    assert Path(a["result_path"]).exists()
    with SessionLocal() as db:
        assert db.query(models.Asset).one().generation_job_id is None


def test_single_delete_still_works(auth_client):
    aid = _avatar(auth_client)
    a = _image(auth_client, aid)
    assert auth_client.delete(f"/api/studio/jobs/{a['id']}").status_code == 204
    assert not Path(a["result_path"]).exists()
    assert auth_client.delete(f"/api/studio/jobs/{a['id']}").status_code == 404


# ---------- bulk save ----------


def test_bulk_save_saves_images_and_skips_the_rest(auth_client):
    aid, target = _avatar(auth_client), _avatar(auth_client, "Bo")
    a = _image(auth_client, aid)
    b = _image(auth_client, aid)
    clip = _job(owner_id=aid, output_kind="video_clip")
    failed = _job(owner_id=aid, status="failed")
    no_file = _job(owner_id=aid, result_path="/nowhere/x.png")
    gone = _job(status="deleted")
    r = auth_client.post("/api/studio/jobs/bulk-save", json={
        "ids": [a["id"], clip, b["id"], failed, no_file, gone, 99999],
        "owner_kind": "avatar", "owner_id": target, "asset_type": "reference_sheet"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert [s["generation_job_id"] for s in out["saved"]] == [a["id"], b["id"]]
    assert all(s["avatar_id"] == target and s["asset_type"] == "reference_sheet" for s in out["saved"])
    reasons = {s["id"]: s["reason"] for s in out["skipped"]}
    assert set(reasons) == {clip, failed, no_file, gone, 99999}
    assert "image" in reasons[clip]
    assert "not completed" in reasons[failed] and "not completed" in reasons[no_file]
    assert reasons[gone] == reasons[99999] == "not found"
    assets = auth_client.get(f"/api/avatars/{target}/assets").json()
    assert {x["id"] for x in assets} == {s["id"] for s in out["saved"]}
    assert auth_client.get(f"/api/studio/jobs/{a['id']}").json()["status"] == "saved"


def test_bulk_save_default_type_bad_type_and_missing_owner(auth_client):
    aid = _avatar(auth_client)
    a = _image(auth_client, aid)
    r = auth_client.post("/api/studio/jobs/bulk-save", json={
        "ids": [a["id"]], "owner_kind": "avatar", "owner_id": aid, "asset_type": "reference"})
    assert r.status_code == 422  # an ingredient type, not an avatar one
    r = auth_client.post("/api/studio/jobs/bulk-save", json={
        "ids": [a["id"]], "owner_kind": "avatar", "owner_id": 99999, "asset_type": None})
    assert r.status_code == 404
    iid = _ingredient(auth_client)
    r = auth_client.post("/api/studio/jobs/bulk-save", json={
        "ids": [a["id"]], "owner_kind": "ingredient", "owner_id": iid, "asset_type": None})
    assert r.status_code == 200
    saved = r.json()["saved"]
    assert len(saved) == 1 and saved[0]["ingredient_id"] == iid and saved[0]["asset_type"] == "reference"


# ---------- cost ----------


def _spend(ref: str, cost: float, estimate: bool = False) -> None:
    from app import models
    from app.db import SessionLocal

    with SessionLocal() as db:
        db.add(models.SpendEntry(provider="openrouter", kind="image", ref=ref, cost_usd=cost,
                                 is_estimate=estimate))
        db.commit()


def _video_job(ref_id: int, purpose: str = "studio") -> int:
    from app import models
    from app.db import SessionLocal

    with SessionLocal() as db:
        v = models.VideoJob(purpose=purpose, ref_id=ref_id)
        db.add(v)
        db.commit()
        return v.id


def test_cost_for_image_jobs(auth_client):
    paid, estimated, free, none = _job(), _job(), _job(), _job()
    _spend(f"studio_job:{paid}", 0.03)
    _spend(f"studio_job:{paid}", 0.01)
    _spend(f"studio_job:{estimated}", 0.04, estimate=True)
    _spend(f"studio_job:{estimated}", 0.01)
    _spend(f"studio_job:{free}", 0.0)
    _spend(f"studio_job:{none}1", 9.0)  # another job's ref sharing a prefix
    jobs = {j["id"]: j for j in auth_client.get("/api/studio/jobs").json()}
    assert jobs[paid]["cost_usd"] == pytest.approx(0.04) and jobs[paid]["cost_is_estimate"] is False
    assert jobs[estimated]["cost_usd"] == pytest.approx(0.05) and jobs[estimated]["cost_is_estimate"] is True
    assert jobs[free]["cost_usd"] == 0.0
    assert jobs[none]["cost_usd"] is None and jobs[none]["cost_is_estimate"] is False
    one = auth_client.get(f"/api/studio/jobs/{estimated}").json()
    assert one["cost_usd"] == pytest.approx(0.05) and one["cost_is_estimate"] is True


def test_cost_for_clip_jobs(auth_client):
    clip, other, waiting = (_job(output_kind="video_clip") for _ in range(3))
    v1, v2 = _video_job(clip), _video_job(clip)
    _spend(f"video_job:{v1}", 0.50)
    _spend(f"video_job:{v2}", 0.25, estimate=True)
    v3 = _video_job(other)
    _spend(f"video_job:{v3}", 1.0)
    # A shot's video job with the same ref_id is not this clip's spend.
    shot = _video_job(clip, purpose="shot")
    _spend(f"video_job:{shot}", 7.0)
    _video_job(waiting)  # submitted, nothing billed yet
    jobs = {j["id"]: j for j in auth_client.get("/api/studio/jobs").json()}
    assert jobs[clip]["cost_usd"] == pytest.approx(0.75) and jobs[clip]["cost_is_estimate"] is True
    assert jobs[other]["cost_usd"] == pytest.approx(1.0) and jobs[other]["cost_is_estimate"] is False
    assert jobs[waiting]["cost_usd"] is None
    assert auth_client.get(f"/api/studio/jobs/{clip}").json()["cost_usd"] == pytest.approx(0.75)


def test_list_costs_take_a_fixed_number_of_queries(auth_client):
    from sqlalchemy import event

    from app import db as dbmod

    def count_for(n: int) -> int:
        for _ in range(n):
            jid = _job()
            _spend(f"studio_job:{jid}", 0.01)
            cid = _job(output_kind="video_clip")
            _spend(f"video_job:{_video_job(cid)}", 0.1)
        seen: list[str] = []

        def hook(conn, cursor, statement, *a):
            seen.append(statement)

        event.listen(dbmod.engine, "before_cursor_execute", hook)
        try:
            jobs = auth_client.get("/api/studio/jobs").json()
        finally:
            event.remove(dbmod.engine, "before_cursor_execute", hook)
        assert all(j["cost_usd"] is not None for j in jobs)
        return len(seen)

    assert count_for(2) == count_for(6)


# ---------- retry ----------


def test_retry_copies_the_job(auth_client, image_calls):
    aid = _avatar(auth_client)
    ref = _upload(auth_client, aid)
    src = _job(owner_id=aid, prompt="Mia at the beach", negative_prompt="hats", aspect_ratio="1:1",
               reference_asset_ids_json=[ref], provider_model="mock/image-default", status="failed",
               error="boom", label="Front view", favorite=True)
    r = auth_client.post(f"/api/studio/jobs/{src}/retry", json={})
    assert r.status_code == 202, r.text
    new = r.json()
    assert new["id"] != src and new["batch_ids"] == [new["id"]]
    j = auth_client.get(f"/api/studio/jobs/{new['id']}").json()
    assert j["status"] == "completed" and j["error"] == ""
    assert (j["owner_kind"], j["owner_id"], j["output_kind"]) == ("avatar", aid, "image")
    assert j["prompt"] == "Mia at the beach" and j["negative_prompt"] == "hats"
    assert j["aspect_ratio"] == "1:1" and j["reference_asset_ids"] == [ref]
    assert j["label"] == "Front view" and j["favorite"] is False
    assert j["provider_model"] == "mock/image-default"
    assert image_calls[-1]["prompt"] == "Mia at the beach"
    # The source is untouched.
    assert auth_client.get(f"/api/studio/jobs/{src}").json()["status"] == "failed"
    # No body at all works too.
    assert auth_client.post(f"/api/studio/jobs/{src}/retry").status_code == 202


def test_retry_with_overrides_and_another_model(auth_client, image_calls):
    aid = _avatar(auth_client)
    src = _image(auth_client, aid, prompt="old prompt", negative_prompt="hats", aspect_ratio="9:16",
                 model="mock/image-default")
    r = auth_client.post(f"/api/studio/jobs/{src['id']}/retry", json={
        "model": "mock/image-other", "prompt": "new prompt", "negative_prompt": "",
        "aspect_ratio": "16:9"})
    assert r.status_code == 202, r.text
    j = auth_client.get(f"/api/studio/jobs/{r.json()['id']}").json()
    assert j["prompt"] == "new prompt" and j["negative_prompt"] == "" and j["aspect_ratio"] == "16:9"
    assert j["provider_model"] == "mock/image-other"
    assert image_calls[-1]["model"] == "mock/image-other"
    # "" keeps the source's model.
    r = auth_client.post(f"/api/studio/jobs/{src['id']}/retry", json={"model": ""})
    assert auth_client.get(f"/api/studio/jobs/{r.json()['id']}").json()["provider_model"] == "mock/image-default"


def test_retry_a_clip(auth_client):
    iid = _ingredient(auth_client)
    src = _job(owner_kind="ingredient", owner_id=iid, output_kind="video_clip", duration_seconds=8.0,
               status="failed")
    r = auth_client.post(f"/api/studio/jobs/{src}/retry", json={})
    assert r.status_code == 202, r.text
    j = auth_client.get(f"/api/studio/jobs/{r.json()['id']}").json()
    assert j["output_kind"] == "video_clip" and j["duration_seconds"] == 8.0


def test_retry_refuses_missing_sources_and_owners(auth_client):
    assert auth_client.post("/api/studio/jobs/99999/retry", json={}).status_code == 404
    gone = _job(status="deleted")
    assert auth_client.post(f"/api/studio/jobs/{gone}/retry", json={}).status_code == 404
    orphan = _job(owner_id=99999)
    assert auth_client.post(f"/api/studio/jobs/{orphan}/retry", json={}).status_code == 404
    aid = _avatar(auth_client)
    src = _job(owner_id=aid)
    assert auth_client.post(f"/api/studio/jobs/{src}/retry", json={"prompt": ""}).status_code == 422
    assert auth_client.post(f"/api/studio/jobs/{src}/retry", json={"aspect_ratio": "4:3"}).status_code == 422


def test_retry_refused_when_the_models_provider_is_off(auth_client, live, monkeypatch):  # noqa: F811
    from app import models
    from app.api import studio as studio_api
    from app.db import SessionLocal

    queued: list = []
    monkeypatch.setattr(studio_api, "enqueue", lambda *a, **k: queued.append(a))
    with SessionLocal() as db:
        av = models.Avatar(name="Mia")
        db.add(av)
        db.commit()
        aid = av.id
    src = _job(owner_id=aid, status="failed", provider_model="sogni/krea-2-turbo")
    _off("sogni")
    r = auth_client.post(f"/api/studio/jobs/{src}/retry", json={})
    assert r.status_code == 409 and r.json()["provider"] == "sogni"
    r = auth_client.post(f"/api/studio/jobs/{src}/retry", json={"model": "sogni/krea-2-turbo"})
    assert r.status_code == 409
    with SessionLocal() as db:
        assert db.query(models.AssetGenerationJob).count() == 1 and not queued


# ---------- character sheet ----------


def test_character_sheet_views_endpoint(auth_client):
    views = auth_client.get("/api/studio/character-sheet/views").json()
    assert [v["id"] for v in views] == ["front", "three_quarter", "profile", "back", "portrait", "smile",
                                        "action", "seated"]
    by_id = {v["id"]: v for v in views}
    assert by_id["front"] == {"id": "front", "label": "Front view", "aspect_ratio": "9:16"}
    assert by_id["portrait"]["aspect_ratio"] == "1:1" and by_id["smile"]["label"] == "Smiling close-up"


def test_character_sheet_default_views(auth_client, image_calls):
    aid = _avatar(auth_client, "Mia", visual_identity="red bob haircut, denim jacket")
    old_hero = _upload(auth_client, aid, "hero")
    lifestyle = _upload(auth_client, aid, "lifestyle")
    sheet = _upload(auth_client, aid, "reference_sheet")
    new_hero = _upload(auth_client, aid, "hero")
    _upload(auth_client, aid, "logo")
    r = auth_client.post("/api/studio/character-sheet", json={"avatar_id": aid})
    assert r.status_code == 202, r.text
    out = r.json()
    assert len(out["batch_ids"]) == 4 and [j["id"] for j in out["jobs"]] == out["batch_ids"]
    assert [j["label"] for j in out["jobs"]] == ["Front view", "Three-quarter view", "Side profile", "Back view"]
    assert {j["aspect_ratio"] for j in out["jobs"]} == {"9:16"}
    for j in out["jobs"]:
        # Hero pictures first (newest first), then the reference sheet; never the logo.
        assert j["reference_asset_ids"] == [new_hero, old_hero, sheet, lifestyle]
        assert (j["owner_kind"], j["owner_id"], j["output_kind"]) == ("avatar", aid, "image")
        assert "the same person as in the reference pictures" in j["prompt"].lower()
        assert "Mia" in j["prompt"] and "red bob haircut" in j["prompt"]
        assert "background" in j["prompt"]
    assert len(image_calls) == 4
    assert "facing the camera" in out["jobs"][0]["prompt"] and "from behind" in out["jobs"][3]["prompt"]
    assert all(auth_client.get(f"/api/studio/jobs/{i}").json()["status"] == "completed"
               for i in out["batch_ids"])
    assert _ids(auth_client, q="side profile") == [out["batch_ids"][2]]


def test_character_sheet_options(auth_client, image_calls):
    aid = _avatar(auth_client)
    ref = _upload(auth_client, aid, "lifestyle")
    r = auth_client.post("/api/studio/character-sheet", json={
        "avatar_id": aid, "views": ["portrait", "action"], "reference_asset_ids": [ref],
        "model": "mock/image-other", "style": "film photo, beach background", "negative_prompt": "hats"})
    assert r.status_code == 202, r.text
    jobs = r.json()["jobs"]
    assert [(j["label"], j["aspect_ratio"]) for j in jobs] == [("Close-up portrait", "1:1"), ("Walking", "9:16")]
    for j in jobs:
        assert j["reference_asset_ids"] == [ref] and j["negative_prompt"] == "hats"
        assert "beach background" in j["prompt"] and "grey background" not in j["prompt"]
        assert j["provider_model"] == "mock/image-other"
    assert image_calls[-1]["settings"]["aspect_ratio"] == "9:16"


def test_character_sheet_refusals(auth_client):
    aid = _avatar(auth_client, "Mia")
    r = auth_client.post("/api/studio/character-sheet", json={"avatar_id": aid})
    assert r.status_code == 422 and r.json()["detail"] == "add a picture of Mia first"
    _upload(auth_client, aid, "logo")  # a logo isn't a picture of her
    assert auth_client.post("/api/studio/character-sheet", json={"avatar_id": aid}).status_code == 422
    _upload(auth_client, aid)
    for views in (["front", "upside_down"], [], ["front", "front"]):
        r = auth_client.post("/api/studio/character-sheet", json={"avatar_id": aid, "views": views})
        assert r.status_code == 422, views
    r = auth_client.post("/api/studio/character-sheet", json={"avatar_id": aid, "style": "x" * 301})
    assert r.status_code == 422
    assert auth_client.post("/api/studio/character-sheet", json={"avatar_id": 99999}).status_code == 404
    assert _ids(auth_client) == []
