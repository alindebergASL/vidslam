"""Managing generated media: save a Studio image to any cast member as a
chosen type, delete Studio results and cast assets without breaking each
other, and retype an asset."""
from __future__ import annotations

from pathlib import Path

from tests.conftest import make_png_bytes


def _avatar(c, name="Ava") -> int:
    return c.post("/api/avatars", json={"name": name}).json()["id"]


def _ingredient(c, name="Rooftop") -> int:
    return c.post("/api/ingredients", json={"name": name, "kind": "scene"}).json()["id"]


def _image_job(c, kind="avatar", owner_id=None) -> dict:
    owner_id = owner_id if owner_id is not None else _avatar(c)
    r = c.post("/api/studio/generate-image", json={
        "owner_kind": kind, "owner_id": owner_id, "prompt": "portrait, warm light"})
    assert r.status_code == 202, r.text
    j = c.get(f"/api/studio/jobs/{r.json()['id']}").json()
    assert j["status"] == "completed"
    return j


def _db():
    from app.db import SessionLocal

    return SessionLocal()


def _asset_path(asset_id: int) -> str:
    from app import models

    db = _db()
    try:
        return db.get(models.Asset, asset_id).file_path
    finally:
        db.close()


def _job_row(job_id: int):
    from app import models

    db = _db()
    try:
        return db.get(models.AssetGenerationJob, job_id)
    finally:
        db.close()


def _gone(job_id: int) -> bool:
    """Deleted Studio jobs are tombstoned (kept, hidden) so their id is never reused."""
    row = _job_row(job_id)
    return row is None or (row.status == "deleted" and not row.result_path)


def test_save_to_another_character_with_a_chosen_type_makes_a_copy(auth_client):
    job = _image_job(auth_client)
    other = _avatar(auth_client, "Bo")
    r = auth_client.post(f"/api/studio/jobs/{job['id']}/save", json={
        "owner_kind": "avatar", "owner_id": other, "asset_type": "hero"})
    assert r.status_code == 200, r.text
    asset = r.json()
    assert (asset["avatar_id"], asset["asset_type"], asset["source"]) == (other, "hero", "generated")
    assert asset["generation_job_id"] == job["id"]
    assert [a["id"] for a in auth_client.get(f"/api/avatars/{other}/assets").json()] == [asset["id"]]

    # The asset owns its own file under uploads, not the Studio result.
    path = _asset_path(asset["id"])
    assert path != job["result_path"]
    assert f"/uploads/avatar/{other}/" in path
    assert Path(path).read_bytes() == Path(job["result_path"]).read_bytes()

    # The same result can go to a second character, and to an ingredient.
    third = _avatar(auth_client, "Cy")
    again = auth_client.post(f"/api/studio/jobs/{job['id']}/save",
                             json={"owner_kind": "avatar", "owner_id": third})
    assert again.status_code == 200 and again.json()["asset_type"] == "lifestyle"
    ing = _ingredient(auth_client)
    to_ing = auth_client.post(f"/api/studio/jobs/{job['id']}/save",
                              json={"owner_kind": "ingredient", "owner_id": ing})
    assert to_ing.status_code == 200
    assert (to_ing.json()["ingredient_id"], to_ing.json()["asset_type"]) == (ing, "reference")

    # Deleting the asset leaves the Studio preview working.
    assert auth_client.delete(f"/api/assets/{asset['id']}").status_code == 204
    assert not Path(path).exists()
    assert auth_client.get(f"/api/studio/jobs/{job['id']}/preview").status_code == 200


def test_save_query_param_still_works_and_body_wins(auth_client):
    job = _image_job(auth_client)
    r = auth_client.post(f"/api/studio/jobs/{job['id']}/save", params={"asset_type": "logo"})
    assert r.status_code == 200 and r.json()["asset_type"] == "logo"
    r = auth_client.post(f"/api/studio/jobs/{job['id']}/save", params={"asset_type": "logo"},
                         json={"asset_type": "outfit_sheet"})
    assert r.status_code == 200 and r.json()["asset_type"] == "outfit_sheet"


def test_ingredient_job_defaults_to_reference(auth_client):
    ing = _ingredient(auth_client)
    job = _image_job(auth_client, kind="ingredient", owner_id=ing)
    r = auth_client.post(f"/api/studio/jobs/{job['id']}/save")
    assert r.status_code == 200
    assert (r.json()["ingredient_id"], r.json()["asset_type"]) == (ing, "reference")


def test_save_rejects_bad_type_clip_missing_owner_and_unfinished_jobs(auth_client):
    job = _image_job(auth_client)
    url = f"/api/studio/jobs/{job['id']}/save"
    assert auth_client.post(url, json={"asset_type": "reference"}).status_code == 422
    assert auth_client.post(url, params={"asset_type": "nonsense"}).status_code == 422
    ing = _ingredient(auth_client)
    assert auth_client.post(url, json={"owner_kind": "ingredient", "owner_id": ing,
                                       "asset_type": "lifestyle"}).status_code == 422
    assert auth_client.post(url, json={"owner_kind": "avatar", "owner_id": 9999}).status_code == 404
    assert auth_client.post("/api/studio/jobs/9999/save").status_code == 404

    aid = _avatar(auth_client)
    clip = auth_client.post("/api/studio/generate-clip", json={
        "owner_kind": "avatar", "owner_id": aid, "prompt": "slow push-in"}).json()
    assert auth_client.get(f"/api/studio/jobs/{clip['id']}").json()["status"] == "completed"
    assert auth_client.post(f"/api/studio/jobs/{clip['id']}/save").status_code == 400

    from app import models

    db = _db()
    try:
        pending = models.AssetGenerationJob(owner_kind="avatar", owner_id=aid, output_kind="image",
                                            prompt="x", status="pending")
        db.add(pending)
        db.commit()
        pid = pending.id
    finally:
        db.close()
    assert auth_client.post(f"/api/studio/jobs/{pid}/save").status_code == 400


def test_delete_job_removes_its_file_but_saved_assets_keep_working(auth_client):
    job = _image_job(auth_client)
    asset = auth_client.post(f"/api/studio/jobs/{job['id']}/save").json()
    assert auth_client.delete(f"/api/studio/jobs/{job['id']}").status_code == 204
    assert not Path(job["result_path"]).exists()
    assert auth_client.get(f"/api/studio/jobs/{job['id']}").status_code == 404
    assert job["id"] not in [j["id"] for j in auth_client.get("/api/studio/jobs").json()]
    assert auth_client.delete(f"/api/studio/jobs/{job['id']}").status_code == 404

    # The asset survives, still previewable, with the provenance link cleared.
    assert auth_client.get(f"/api/assets/{asset['id']}/preview").status_code == 200
    kept = [a for a in auth_client.get(f"/api/avatars/{asset['avatar_id']}/assets").json()
            if a["id"] == asset["id"]]
    assert kept and kept[0]["generation_job_id"] is None


def test_delete_job_keeps_a_file_a_legacy_asset_still_uses(auth_client):
    """Assets saved before saves made copies point at the job's own file."""
    from app import models

    job = _image_job(auth_client)
    db = _db()
    try:
        legacy = models.Asset(owner_kind="avatar", avatar_id=job["owner_id"], asset_type="lifestyle",
                              file_path=job["result_path"], mime_type="image/png",
                              source="generated", generation_job_id=job["id"])
        db.add(legacy)
        db.flush()
        row = db.get(models.AssetGenerationJob, job["id"])
        row.result_asset_id = legacy.id
        row.status = "saved"
        db.commit()
        legacy_id = legacy.id
    finally:
        db.close()

    # Deleting the legacy asset must not take the job's result with it.
    assert auth_client.delete(f"/api/assets/{legacy_id}").status_code == 204
    assert Path(job["result_path"]).exists()
    row = _job_row(job["id"])
    assert (row.status, row.result_asset_id) == ("completed", None)
    assert auth_client.get(f"/api/studio/jobs/{job['id']}/preview").status_code == 200

    # And the other way round: a job whose file a legacy asset shares.
    db = _db()
    try:
        legacy = models.Asset(owner_kind="avatar", avatar_id=job["owner_id"], asset_type="lifestyle",
                              file_path=job["result_path"], mime_type="image/png",
                              source="generated", generation_job_id=job["id"])
        db.add(legacy)
        db.commit()
        legacy_id = legacy.id
    finally:
        db.close()
    assert auth_client.delete(f"/api/studio/jobs/{job['id']}").status_code == 204
    assert Path(job["result_path"]).exists()
    assert auth_client.get(f"/api/assets/{legacy_id}/preview").status_code == 200


def test_deleting_the_saved_asset_reopens_the_job(auth_client):
    job = _image_job(auth_client)
    asset = auth_client.post(f"/api/studio/jobs/{job['id']}/save").json()
    j = auth_client.get(f"/api/studio/jobs/{job['id']}").json()
    assert (j["status"], j["result_asset_id"]) == ("saved", asset["id"])
    assert auth_client.delete(f"/api/assets/{asset['id']}").status_code == 204
    j = auth_client.get(f"/api/studio/jobs/{job['id']}").json()
    assert (j["status"], j["result_asset_id"]) == ("completed", None)
    # Savable again.
    assert auth_client.post(f"/api/studio/jobs/{job['id']}/save").status_code == 200


def test_delete_asset_keeps_a_file_another_asset_shares(auth_client):
    from app import models

    aid = _avatar(auth_client)
    up = auth_client.post(f"/api/avatars/{aid}/assets", data={"asset_type": "hero", "rights_confirmed": "true"},
                          files={"file": ("a.png", make_png_bytes(), "image/png")}).json()
    path = _asset_path(up["id"])
    db = _db()
    try:
        twin = models.Asset(owner_kind="avatar", avatar_id=aid, asset_type="other",
                            file_path=path, mime_type="image/png")
        db.add(twin)
        db.commit()
        twin_id = twin.id
    finally:
        db.close()
    assert auth_client.delete(f"/api/assets/{up['id']}").status_code == 204
    assert Path(path).exists()
    assert auth_client.delete(f"/api/assets/{twin_id}").status_code == 204
    assert not Path(path).exists()


def test_patch_asset_type_validates_per_owner_kind(auth_client):
    aid = _avatar(auth_client)
    up = auth_client.post(f"/api/avatars/{aid}/assets", data={"asset_type": "other", "rights_confirmed": "true"},
                          files={"file": ("a.png", make_png_bytes(), "image/png")}).json()
    r = auth_client.patch(f"/api/assets/{up['id']}", json={"asset_type": "expression_sheet"})
    assert r.status_code == 200 and r.json()["asset_type"] == "expression_sheet"
    assert auth_client.get(f"/api/avatars/{aid}/assets").json()[0]["asset_type"] == "expression_sheet"
    assert auth_client.patch(f"/api/assets/{up['id']}", json={"asset_type": "reference"}).status_code == 422
    assert auth_client.patch("/api/assets/9999", json={"asset_type": "hero"}).status_code == 404

    ing = _ingredient(auth_client)
    iu = auth_client.post(f"/api/ingredients/{ing}/assets", data={"rights_confirmed": "true"},
                          files={"file": ("a.png", make_png_bytes(), "image/png")}).json()
    assert auth_client.patch(f"/api/assets/{iu['id']}", json={"asset_type": "reference"}).status_code == 200
    assert auth_client.patch(f"/api/assets/{iu['id']}", json={"asset_type": "lifestyle"}).status_code == 422


def test_media_routes_need_auth(client):
    assert client.delete("/api/studio/jobs/1").status_code == 401
    assert client.patch("/api/assets/1", json={"asset_type": "hero"}).status_code == 401


def test_queued_job_deleted_before_it_runs_is_a_no_op(auth_client, monkeypatch):
    from app import models
    from app.workers import jobs

    aid = _avatar(auth_client)
    db = _db()
    try:
        row = models.AssetGenerationJob(owner_kind="avatar", owner_id=aid, output_kind="image",
                                        prompt="x", status="pending")
        db.add(row)
        db.commit()
        jid = row.id
    finally:
        db.close()
    assert auth_client.delete(f"/api/studio/jobs/{jid}").status_code == 204
    jobs.generate_asset_job(jid)  # must not raise
    assert _gone(jid)


def test_job_deleted_while_generating_is_a_no_op(auth_client, monkeypatch):
    from app import models
    from app.providers import mock_image
    from app.workers import jobs

    aid = _avatar(auth_client)
    db = _db()
    try:
        row = models.AssetGenerationJob(owner_kind="avatar", owner_id=aid, output_kind="image",
                                        prompt="x", status="pending")
        db.add(row)
        db.commit()
        jid = row.id
    finally:
        db.close()

    newer: list[int] = []

    def _generate(self, **kw):
        # The user deletes the job while the provider is still working, and
        # starts another one (on SQLite a hard delete would hand it this id).
        assert auth_client.delete(f"/api/studio/jobs/{jid}").status_code == 204
        d = _db()
        try:
            other = models.AssetGenerationJob(owner_kind="avatar", owner_id=aid, output_kind="image",
                                              prompt="y", status="pending")
            d.add(other)
            d.commit()
            newer.append(other.id)
        finally:
            d.close()
        return make_png_bytes()

    monkeypatch.setattr(mock_image.MockImageProvider, "generate", _generate)
    jobs.generate_asset_job(jid)  # must not raise
    assert _gone(jid)
    # Review: the deleted job's image must not land in the newer job.
    assert newer[0] != jid
    other = _job_row(newer[0])
    assert (other.status, other.result_path or "") == ("pending", "")
    assert auth_client.get(f"/api/studio/jobs/{jid}").status_code == 404
    assert jid not in [j["id"] for j in auth_client.get("/api/studio/jobs").json()]


def test_late_clip_completion_for_a_deleted_job_is_a_no_op(auth_client):
    from app import models
    from app.services import video_jobs

    aid = _avatar(auth_client)
    db = _db()
    try:
        row = models.AssetGenerationJob(owner_kind="avatar", owner_id=aid, output_kind="video_clip",
                                        prompt="x", status="polling")
        db.add(row)
        db.commit()
        jid = row.id
    finally:
        db.close()
    assert auth_client.delete(f"/api/studio/jobs/{jid}").status_code == 204

    from app.config import get_settings

    clip = get_settings().data_path / "studio" / "clips" / "late.mp4"
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    db = _db()
    try:
        vjob = models.VideoJob(purpose="studio", ref_id=jid, status="completed",
                               result_path=str(clip))
        db.add(vjob)
        db.commit()
        video_jobs.dispatch_completed(db, vjob)
        assert not clip.exists()  # the orphaned download is cleaned up, not left on disk
        vjob.error = "late"
        video_jobs.dispatch_failed(db, vjob)
    finally:
        db.close()
    assert _gone(jid)


def _video_jobs_for(job_id: int):
    from app import models

    db = _db()
    try:
        return db.query(models.VideoJob).filter(models.VideoJob.purpose == "studio",
                                                models.VideoJob.ref_id == job_id).all()
    finally:
        db.close()


def test_late_clip_for_a_deleted_job_does_not_fill_a_new_job_reusing_its_id(auth_client, monkeypatch):
    from app.services import video_jobs

    # Mock clips finish at submit time; keep them in flight like a real provider.
    real_check = video_jobs.check
    monkeypatch.setattr(video_jobs, "check", lambda db, job_id: "submitted")
    aid = _avatar(auth_client)
    r = auth_client.post("/api/studio/generate-clip", json={
        "owner_kind": "avatar", "owner_id": aid, "prompt": "walks in", "duration_seconds": 4})
    assert r.status_code == 202, r.text
    old_id = r.json()["id"]
    assert _job_row(old_id).status == "polling"
    [old_vjob] = _video_jobs_for(old_id)
    assert old_vjob.status in video_jobs.ACTIVE

    assert auth_client.delete(f"/api/studio/jobs/{old_id}").status_code == 204
    r = auth_client.post("/api/studio/generate-clip", json={
        "owner_kind": "avatar", "owner_id": aid, "prompt": "waves", "duration_seconds": 4})
    new_id = r.json()["id"]
    assert new_id != old_id  # deleted jobs are tombstoned, so ids are never reused
    [new_vjob] = _video_jobs_for(new_id)
    assert new_vjob.id != old_vjob.id

    # The old clip finishes later: it is still billed but fills nothing.
    monkeypatch.setattr(video_jobs, "check", real_check)
    db = _db()
    try:
        assert video_jobs.complete(db, old_vjob.id) == "completed"
    finally:
        db.close()
    new = _job_row(new_id)
    assert (new.status, new.result_path or "") == ("polling", "")

    # A late failure for the old clip leaves the new job alone too.
    db = _db()
    try:
        from app import models

        stale = models.VideoJob(purpose="studio", ref_id=0, status="completed", error="late")
        db.add(stale)
        db.commit()
        video_jobs.dispatch_failed(db, stale)
    finally:
        db.close()
    assert _job_row(new_id).status == "polling"


def test_stale_queued_task_does_not_rerun_a_job_that_reused_its_id(auth_client, monkeypatch):
    from app.providers import mock_image
    from app.workers import jobs

    job = _image_job(auth_client)
    calls = []
    real = mock_image.MockImageProvider.generate

    def _generate(self, **kw):
        calls.append(1)
        return real(self, **kw)

    monkeypatch.setattr(mock_image.MockImageProvider, "generate", _generate)
    # The task queued for the (deleted) job with this id arrives after the
    # new job already ran: it must not generate (and pay) again.
    jobs.generate_asset_job(job["id"])
    assert calls == []
    assert _job_row(job["id"]).status == "completed"
