"""Studio: aspect ratio, 1–4 variations per run, Remix fields on jobs, the
asset_generation_jobs.aspect_ratio column (migration 0003), and where the
Settings negative-prompt defaults apply (Studio + storyboard shots, never
ambient)."""
from __future__ import annotations

import pytest

from .conftest import make_png_bytes

IMAGE_TOKEN = "zzimage-default-token"
VIDEO_TOKEN = "zzvideo-default-token"


def _avatar(client) -> int:
    return client.post("/api/avatars", json={"name": "N"}).json()["id"]


def _ingredient(client) -> int:
    return client.post("/api/ingredients", json={"name": "Roof", "kind": "scene"}).json()["id"]


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


@pytest.fixture
def video_calls(monkeypatch):
    from app.providers import mock_video

    calls: list[dict] = []
    real = mock_video.MockVideoProvider.submit

    def spy(self, **kw):
        calls.append(kw)
        return real(self, **kw)

    monkeypatch.setattr(mock_video.MockVideoProvider, "submit", spy)
    return calls


@pytest.fixture
def custom_defaults(auth_client):
    """Distinctive Settings defaults so a test can tell where they landed."""
    from app.services import prompting

    prompting.set_negative_defaults(IMAGE_TOKEN, VIDEO_TOKEN)
    return prompting


# ---------- variations ----------


def test_variations_create_n_jobs_and_return_batch_ids(auth_client, image_calls):
    aid = _avatar(auth_client)
    r = auth_client.post("/api/studio/generate-image", json={
        "owner_kind": "avatar", "owner_id": aid, "prompt": "N on a rooftop",
        "variations": 3, "aspect_ratio": "1:1", "negative_prompt": "hats",
    })
    assert r.status_code == 202, r.text
    out = r.json()
    ids = out["batch_ids"]
    assert len(ids) == 3 and len(set(ids)) == 3
    assert ids[0] == out["id"]
    # Each is its own job with the same request, each run by the worker.
    assert len(image_calls) == 3
    for jid in ids:
        j = auth_client.get(f"/api/studio/jobs/{jid}").json()
        assert j["status"] == "completed"
        assert j["prompt"] == "N on a rooftop" and j["aspect_ratio"] == "1:1"
        assert j["negative_prompt"] == "hats"
        assert j["batch_ids"] == []  # only the create response carries the batch
    listed = {j["id"] for j in auth_client.get("/api/studio/jobs").json()}
    assert set(ids) <= listed


def test_single_run_still_answers_with_batch_ids(auth_client, image_calls):
    aid = _avatar(auth_client)
    r = auth_client.post("/api/studio/generate-image",
                         json={"owner_kind": "avatar", "owner_id": aid, "prompt": "p"})
    assert r.status_code == 202
    assert r.json()["batch_ids"] == [r.json()["id"]]
    assert len(image_calls) == 1


@pytest.mark.parametrize("n", [0, 5])
def test_variations_out_of_range_is_422(auth_client, n):
    aid = _avatar(auth_client)
    r = auth_client.post("/api/studio/generate-image",
                         json={"owner_kind": "avatar", "owner_id": aid, "prompt": "p", "variations": n})
    assert r.status_code == 422


def test_clip_variations_are_refused_not_billed(auth_client, video_calls):
    iid = _ingredient(auth_client)
    r = auth_client.post("/api/studio/generate-clip",
                         json={"owner_kind": "ingredient", "owner_id": iid, "prompt": "p", "variations": 2})
    assert r.status_code == 422
    assert video_calls == []
    assert auth_client.get("/api/studio/jobs").json() == []


def test_bad_aspect_is_422(auth_client):
    aid = _avatar(auth_client)
    r = auth_client.post("/api/studio/generate-image",
                         json={"owner_kind": "avatar", "owner_id": aid, "prompt": "p", "aspect_ratio": "4:3"})
    assert r.status_code == 422


def test_variations_unknown_owner_creates_nothing(auth_client):
    r = auth_client.post("/api/studio/generate-image",
                         json={"owner_kind": "avatar", "owner_id": 999, "prompt": "p", "variations": 4})
    assert r.status_code == 404
    assert auth_client.get("/api/studio/jobs").json() == []


# ---------- aspect ratio reaches the provider ----------


@pytest.mark.parametrize("kind,aspect,expected", [
    ("avatar", None, "9:16"),       # default: portrait for characters
    ("ingredient", None, "16:9"),   # default: landscape for scenes / objects
    ("avatar", "16:9", "16:9"),     # explicit wins
    ("ingredient", "1:1", "1:1"),
])
def test_image_aspect_reaches_the_provider(auth_client, image_calls, kind, aspect, expected):
    owner = _avatar(auth_client) if kind == "avatar" else _ingredient(auth_client)
    body = {"owner_kind": kind, "owner_id": owner, "prompt": "p"}
    if aspect is not None:
        body["aspect_ratio"] = aspect
    r = auth_client.post("/api/studio/generate-image", json=body)
    assert r.status_code == 202, r.text
    assert r.json()["aspect_ratio"] == (aspect or "")
    assert image_calls[-1]["settings"]["aspect_ratio"] == expected


def test_clip_aspect_reaches_the_provider_only_when_set(auth_client, video_calls):
    iid = _ingredient(auth_client)
    auth_client.post("/api/studio/generate-clip",
                     json={"owner_kind": "ingredient", "owner_id": iid, "prompt": "p", "aspect_ratio": "9:16"})
    assert video_calls[-1]["settings"]["aspect_ratio"] == "9:16"
    auth_client.post("/api/studio/generate-clip",
                     json={"owner_kind": "ingredient", "owner_id": iid, "prompt": "p"})
    # Unset: the model's own default, exactly as before.
    assert "aspect_ratio" not in (video_calls[-1]["settings"] or {})


# ---------- Remix fields ----------


def test_job_out_carries_remix_fields(auth_client):
    aid = _avatar(auth_client)
    asset = auth_client.post(
        f"/api/avatars/{aid}/assets",
        files={"file": ("h.png", make_png_bytes(), "image/png")},
        data={"asset_type": "hero", "rights_confirmed": "true"},
    ).json()
    r = auth_client.post("/api/studio/generate-image", json={
        "owner_kind": "avatar", "owner_id": aid, "prompt": "remix me", "negative_prompt": "blur",
        "reference_asset_ids": [asset["id"]], "aspect_ratio": "16:9", "model": "mock/image-default",
    })
    assert r.status_code == 202, r.text
    for j in (r.json(), auth_client.get(f"/api/studio/jobs/{r.json()['id']}").json(),
              auth_client.get("/api/studio/jobs").json()[0]):
        assert j["negative_prompt"] == "blur"
        assert j["reference_asset_ids"] == [asset["id"]]
        assert j["aspect_ratio"] == "16:9"
        assert j["provider_model"] == "mock/image-default"
        assert "reference_asset_ids_json" not in j


def test_old_jobs_read_with_empty_defaults(auth_client):
    """Rows from before 0003 (aspect "") and with a NULL ref list still serialize."""
    from app import models
    from app.db import SessionLocal

    aid = _avatar(auth_client)
    with SessionLocal() as db:
        job = models.AssetGenerationJob(owner_kind="avatar", owner_id=aid, output_kind="image",
                                        prompt="old", status="completed", reference_asset_ids_json=None)
        db.add(job)
        db.commit()
        jid = job.id
    j = auth_client.get(f"/api/studio/jobs/{jid}").json()
    assert j["aspect_ratio"] == "" and j["reference_asset_ids"] == [] and j["negative_prompt"] == ""


# ---------- migration 0003 ----------


def test_migration_adds_aspect_ratio_column(tmp_path, monkeypatch):
    """A real Alembic upgrade from the baseline adds the column (server
    default "" for existing rows), and the head is 0003 or later."""
    import sqlite3

    from alembic import command
    from sqlalchemy import inspect

    from app import config
    from app import db as dbmod

    url = f"sqlite:///{tmp_path / 'm.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    config.get_settings.cache_clear()
    dbmod.reset_engine(url)
    command.upgrade(dbmod._alembic_config(), "0002")
    con = sqlite3.connect(tmp_path / "m.db")
    con.execute("insert into asset_generation_jobs (owner_kind, owner_id, output_kind, duration_seconds,"
                " prompt, negative_prompt, reference_asset_ids_json, provider, provider_model,"
                " provider_job_id, status, result_path, error, created_at, updated_at) values"
                " ('avatar',1,'image',5,'old','','[]','mock','m','','completed','','',"
                " datetime('now'),datetime('now'))")
    con.commit()
    con.close()
    command.upgrade(dbmod._alembic_config(), "0003")
    cols = {c["name"]: c for c in inspect(dbmod.engine).get_columns("asset_generation_jobs")}
    assert "aspect_ratio" in cols and cols["aspect_ratio"]["nullable"] is False
    con = sqlite3.connect(tmp_path / "m.db")
    assert con.execute("select aspect_ratio from asset_generation_jobs").fetchall() == [("",)]
    con.close()
    command.downgrade(dbmod._alembic_config(), "0002")
    assert "aspect_ratio" not in {c["name"] for c in inspect(dbmod.engine).get_columns("asset_generation_jobs")}


def test_migration_0003_tolerates_create_all_databases(tmp_path, monkeypatch):
    """A pre-revision create_all SQLite file is stamped at the baseline and
    upgraded; 0003 must not fail on a column create_all already made."""
    import sqlite3

    from app import config
    from app import db as dbmod

    path = tmp_path / "old.db"
    url = f"sqlite:///{path}"
    monkeypatch.setenv("DATABASE_URL", url)
    config.get_settings.cache_clear()
    dbmod.reset_engine(url)
    dbmod.init_db()
    con = sqlite3.connect(path)
    con.execute("drop table alembic_version")
    con.execute("alter table asset_generation_jobs drop column duration_seconds")
    con.commit()
    con.close()
    dbmod.init_db()
    con = sqlite3.connect(path)
    cols = [r[1] for r in con.execute("pragma table_info(asset_generation_jobs)")]
    assert "aspect_ratio" in cols and "duration_seconds" in cols
    assert con.execute("select version_num from alembic_version").fetchone()[0] == dbmod._alembic_head()
    con.close()


# ---------- negative defaults: where they apply ----------


def test_defaults_reach_the_studio_image_provider(auth_client, image_calls, custom_defaults):
    aid = _avatar(auth_client)
    auth_client.post("/api/studio/generate-image", json={
        "owner_kind": "avatar", "owner_id": aid, "prompt": "p", "negative_prompt": "hats"})
    neg = image_calls[-1]["negative_prompt"]
    assert neg.startswith("hats") and IMAGE_TOKEN in neg and VIDEO_TOKEN not in neg
    # The job keeps only the user's own text (Remix re-applies defaults).
    assert auth_client.get("/api/studio/jobs").json()[0]["negative_prompt"] == "hats"


def test_builtin_defaults_reach_the_studio_image_provider(auth_client, image_calls):
    aid = _avatar(auth_client)
    auth_client.post("/api/studio/generate-image",
                     json={"owner_kind": "avatar", "owner_id": aid, "prompt": "p"})
    assert "extra fingers" in image_calls[-1]["negative_prompt"]


def test_defaults_reach_the_studio_clip_provider(auth_client, video_calls, custom_defaults):
    iid = _ingredient(auth_client)
    auth_client.post("/api/studio/generate-clip",
                     json={"owner_kind": "ingredient", "owner_id": iid, "prompt": "p", "negative_prompt": "rain"})
    neg = video_calls[-1]["negative_prompt"]
    assert neg.startswith("rain") and VIDEO_TOKEN in neg and IMAGE_TOKEN not in neg


def test_defaults_reach_a_storyboard_shot_clip(auth_client, video_calls, custom_defaults):
    aid = _avatar(auth_client)
    auth_client.post(f"/api/avatars/{aid}/assets",
                     files={"file": ("h.png", make_png_bytes(), "image/png")},
                     data={"asset_type": "hero", "rights_confirmed": "true"})
    pid = auth_client.post("/api/projects", json={
        "title": "T", "original_script": "one two three four five", "cta_text": "Go",
        "cast": [{"member_kind": "avatar", "avatar_id": aid, "role": "host"}],
    }).json()["id"]
    auth_client.post(f"/api/projects/{pid}/generate-plan")
    auth_client.post(f"/api/projects/{pid}/generate-video")
    assert video_calls, "storyboard shots should submit clips"
    for call in video_calls:
        assert VIDEO_TOKEN in call["negative_prompt"]


def test_shot_clip_submit_applies_defaults_but_not_for_ambient_segments(auth_client, video_calls,
                                                                         custom_defaults):
    """Directly at submit_shot_clip: a storyboard shot gets the defaults; an
    ambient chain segment (and any shot of an ambient project) does not."""
    from app import models
    from app.db import SessionLocal
    from app.services.pipeline import submit_shot_clip

    with SessionLocal() as db:
        for mode, purpose in (("reel_montage", "shot"), ("ambient_loop", "ambient_segment"),
                              ("ambient_loop", "shot")):
            p = models.VideoProject(title="t", mode=mode, aspect_ratio="16:9")
            db.add(p)
            db.commit()
            s = models.VideoShot(project_id=p.id, shot_order=0, prompt="falls",
                                 negative_prompt="people", duration_seconds=5)
            db.add(s)
            db.commit()
            submit_shot_clip(db, p, s, purpose=purpose, after="none", references=[])
    story, segment, ambient_shot = (c["negative_prompt"] for c in video_calls[-3:])
    assert story.startswith("people") and VIDEO_TOKEN in story
    assert segment == "people" and ambient_shot == "people"


def test_ambient_generation_never_gets_the_defaults(auth_client, video_calls, image_calls,
                                                    custom_defaults):
    from .test_ambient import SHORT

    r = auth_client.post("/api/ambient", json={"config": SHORT, "generate": True})
    assert r.status_code == 201, r.text
    assert video_calls, "the ambient loop should submit a clip"
    for call in video_calls + image_calls:
        neg = call.get("negative_prompt") or ""
        assert VIDEO_TOKEN not in neg and IMAGE_TOKEN not in neg
        assert "slo-mo" not in neg
