"""Hook lab: other openings of a finished video without paying for the whole
video again. Window finding, suggestions, fitting and joining the voice,
captions, every shot-1 option (with its own VideoJob purposes, never
touching the VideoShot row), variants kept away from the project's
current voiceover and from maybe_compose, takes protected from prune,
pricing, guarded and atomic make, events, and Results (hook grouping and
hook tests)."""
from __future__ import annotations

import shutil
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from .test_render_fixes import _clip, _db, _duration, _rendered, _renders

# ---------- helpers ----------


def _tone(path: Path, seconds: float) -> Path:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    f"sine=frequency=440:sample_rate=44100:duration={seconds}", "-ac", "2", "-c:a", "aac",
                    str(path)], check=True)
    return path


def _voice(path: Path, parts: list[tuple[str, float]]) -> Path:
    """A synthetic voice: ("tone", s) is speech, ("gap", s) a pause."""
    inputs: list[str] = []
    for kind, s in parts:
        src = f"sine=frequency=300:sample_rate=44100:duration={s}" if kind == "tone" \
            else f"anullsrc=r=44100:cl=mono:d={s}"
        inputs += ["-f", "lavfi", "-i", src]
    graph = "".join(f"[{i}:a]" for i in range(len(parts))) + f"concat=n={len(parts)}:v=0:a=1[out]"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *inputs, "-filter_complex", graph, "-map", "[out]",
                    "-ac", "2", "-c:a", "aac", str(path)], check=True)
    return path


def _base(auth_client) -> tuple[int, int]:
    pid = _rendered(auth_client)  # "Stop scrolling now! Here is the rest of it. And one more line." (5.2 s voice)
    return pid, _renders(pid)[-1].id


def _create(auth_client, pid, base_id, variants, window=2.5):
    r = auth_client.post(f"/api/projects/{pid}/hook-lab/variants",
                         json={"base_render_id": base_id, "window_seconds": window, "variants": variants})
    assert r.status_code == 201, r.text
    return r.json()


def _make(auth_client, pid, ids):
    return auth_client.post(f"/api/projects/{pid}/hook-lab/make", json={"variant_ids": ids})


def _get(model, id_):
    db = _db()
    try:
        return db.get(model, id_)
    finally:
        db.close()


def _shot_state(pid):
    from app import models

    db = _db()
    try:
        return [(s.id, s.clip_path, s.status, s.provider, s.clip_fingerprint, len(s.takes))
                for s in db.query(models.VideoShot).filter_by(project_id=pid).order_by(models.VideoShot.id)]
    finally:
        db.close()


def _jobs(pid, purpose):
    from app import models

    db = _db()
    try:
        return db.query(models.VideoJob).filter_by(project_id=pid, purpose=purpose).order_by(models.VideoJob.id).all()
    finally:
        db.close()


def _lipsync_on(client):
    r = client.put("/api/models/active", json={"kind": "lipsync", "model": "heygen/avatar-iv", "allow_unlisted": True})
    assert r.status_code == 200, r.text


# ---------- the hook in the plan ----------


def test_the_lab_starts_from_the_plans_hook(auth_client):
    from app.schemas.storyboard import StoryboardPlan

    old = {"title": "t", "cleaned_voice_script": "Did you know? Cats sleep.", "estimated_duration_seconds": 5}
    assert StoryboardPlan.model_validate(old).hook == "Did you know?"
    pid, base_id = _base(auth_client)
    lab = auth_client.get(f"/api/projects/{pid}/hook-lab").json()
    (b,) = lab["bases"]
    assert b["render_id"] == base_id and b["hook_text"] == "Stop scrolling now!" and b["reusable"]
    assert b["version_label"] == "Version 1" and b["variants"] == []
    assert 0.3 < b["window_seconds"] < 3 and b["opening_audio_url"]
    assert auth_client.get(b["opening_audio_url"]).status_code == 200
    assert b["shot1"]["takes"] and not b["shot1"]["can_redo_talking_head"]


# ---------- where the opening ends ----------


def test_window_snaps_to_the_nearest_pause(tmp_path):
    from app import models
    from app.services import hook_lab

    voice = _voice(tmp_path / "v.m4a", [("tone", 1.2), ("gap", 0.4), ("tone", 3.0), ("gap", 0.3), ("tone", 1.0)])
    script = "Short hook here. " + "and then the rest of the script goes on for a good while longer."
    est = hook_lab.estimate_window(script, _duration(voice))
    assert 1.0 < est < 1.6  # the even-pace guess
    w = hook_lab.find_hook_window(models.Render(audio_path=str(voice)), script)
    assert w == pytest.approx(1.4, abs=0.08)  # the middle of the first pause

    # No pause near the guess: the guess itself.
    flat = _tone(tmp_path / "flat.m4a", 6.0)
    w2 = hook_lab.find_hook_window(models.Render(audio_path=str(flat)), script)
    assert w2 == pytest.approx(hook_lab.estimate_window(script, _duration(flat)), abs=0.01)
    # The mock voice (all silence): the guess too.
    assert hook_lab.detect_silences(voice)[0][0] == pytest.approx(1.2, abs=0.05)


# ---------- suggestions ----------


def test_suggestions_from_the_mock_planner(auth_client, monkeypatch):
    pid, base_id = _base(auth_client)
    r = auth_client.post(f"/api/projects/{pid}/hook-lab/suggest", json={"base_render_id": base_id, "n": 3})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["original"]["text"] == "Stop scrolling now!"
    ideas = body["suggestions"]
    assert len(ideas) == 3 and len({i["style"] for i in ideas}) == 3
    for i in ideas:
        assert i["text"] != "Stop scrolling now!" and abs(len(i["text"].split()) - 3) <= 1
        assert {"estimated_seconds", "fits", "overflow_seconds", "style_label"} <= set(i)
    # Deterministic.
    again = auth_client.post(f"/api/projects/{pid}/hook-lab/suggest", json={"base_render_id": base_id}).json()
    assert [i["text"] for i in again["suggestions"]] == [i["text"] for i in ideas]

    # A refused or unusable answer is a plain error.
    from app.providers import mock_chat

    monkeypatch.setattr(mock_chat.MockChatProvider, "hook_ideas", lambda self, **kw: "Sorry, I can't help.")
    r = auth_client.post(f"/api/projects/{pid}/hook-lab/suggest", json={"base_render_id": base_id})
    assert r.status_code == 422 and "declined" in r.json()["detail"] and "Write your own" in r.json()["detail"]
    monkeypatch.setattr(mock_chat.MockChatProvider, "hook_ideas", lambda self, **kw: "not json at all")
    r = auth_client.post(f"/api/projects/{pid}/hook-lab/suggest", json={"base_render_id": base_id})
    assert r.status_code == 502 and "write your own" in r.json()["detail"]
    too_long = '{"hooks": [{"style": "question", "text": "' + "word " * 30 + '"}]}'
    monkeypatch.setattr(mock_chat.MockChatProvider, "hook_ideas", lambda self, **kw: too_long)
    assert auth_client.post(f"/api/projects/{pid}/hook-lab/suggest",
                            json={"base_render_id": base_id}).status_code == 502


def test_suggestions_record_spend_with_the_hook_ideas_note():
    import inspect

    from app.providers import openrouter_chat

    src = inspect.getsource(openrouter_chat.OpenRouterChatProvider.hook_ideas)
    assert 'note="hook ideas"' in src


# ---------- fitting and joining the voice ----------


def test_fit_pads_speeds_up_or_refuses(tmp_path):
    from app.services import hook_lab

    raw = _tone(tmp_path / "raw.m4a", 2.0)
    padded = hook_lab.fit_hook(raw, tmp_path / "pad.wav", 2.5)
    assert padded["tempo"] == 1.0 and _duration(tmp_path / "pad.wav") == pytest.approx(2.5, abs=0.03)
    fast = hook_lab.fit_hook(raw, tmp_path / "fast.wav", 1.8)
    assert fast["tempo"] == pytest.approx(2.0 / 1.8, abs=0.01)
    assert _duration(tmp_path / "fast.wav") == pytest.approx(1.8, abs=0.03)
    with pytest.raises(hook_lab.HookTooLong) as e:
        hook_lab.fit_hook(raw, tmp_path / "no.wav", 1.5)
    assert str(e.value) == "This opening is about 0.5 s longer than the original; shorten it."
    assert e.value.overflow == pytest.approx(0.5, abs=0.05)


def test_joined_audio_keeps_the_base_length(tmp_path):
    from app.services import hook_lab

    base = _voice(tmp_path / "base.m4a", [("tone", 1.5), ("gap", 0.3), ("tone", 3.2)])
    head = tmp_path / "head.wav"
    hook_lab.fit_hook(_tone(tmp_path / "raw.m4a", 1.2), head, 1.65)
    out = hook_lab.join_audio(head, base, 1.65, tmp_path / "joined.m4a")
    assert _duration(out) == pytest.approx(_duration(base), abs=0.06)


def test_listen_makes_the_voice_once_and_says_when_it_is_too_long(auth_client):
    from app import models

    pid, base_id = _base(auth_client)
    base_audio = _renders(pid)[-1].audio_path
    made = _create(auth_client, pid, base_id, [
        {"hook_text": "Ever wondered why, friend?", "style": "question"},  # 4 words: 2.0 s in mock TTS
        {"hook_text": "This is a far longer opening line that will never fit in the window", "style": "custom"},
    ])
    ok_id, long_id = (v["id"] for v in made["variants"])
    r = auth_client.post(f"/api/hook-variants/{ok_id}/preview-audio")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["fits"] and out["overflow_seconds"] == 0 and out["cost"] > 0 and not out["reused"]
    assert out["duration_seconds"] == pytest.approx(_duration(base_audio), abs=0.06)  # joined = base length
    assert auth_client.get(out["url"]).status_code == 200
    again = auth_client.post(f"/api/hook-variants/{ok_id}/preview-audio").json()
    assert again["reused"] and again["cost"] == 0  # not paid twice
    v = _get(models.HookVariant, ok_id)
    assert v.status == "draft" and Path(v.audio_path).name == f"voice_variant_{ok_id}.m4a"

    r = auth_client.post(f"/api/hook-variants/{long_id}/preview-audio")
    assert r.status_code == 200
    out = r.json()
    assert not out["fits"] and out["overflow_seconds"] > 1 and "longer than the original" in out["message"]
    assert auth_client.get(out["url"]).status_code == 200  # the opening alone can still be heard
    # Known to be too long: the plan says so and Make refuses it before anything is paid.
    plan = auth_client.post(f"/api/projects/{pid}/hook-lab/plan", json={"variant_ids": [long_id]}).json()
    assert plan["variants"][0]["too_long_seconds"] > 1
    r = _make(auth_client, pid, [long_id])
    assert r.status_code == 422 and "longer than the original" in r.json()["detail"]
    assert _get(models.HookVariant, long_id).status == "draft" and not [
        x for x in _renders(pid) if x.hook_variant_id == long_id]


# ---------- captions ----------


def test_hook_captions_fill_the_window_then_the_rest():
    from app.services import hook_lab

    timed = hook_lab.hook_captions("Why stop scrolling now?", "Here is the rest of it. And one more line.", 2.0, 6.0)
    hook = [t for t in timed if t.start < 2.0]
    rest = [t for t in timed if t.start >= 2.0]
    assert " ".join(t.text for t in hook) == "Why stop scrolling now?"
    assert " ".join(t.text for t in rest) == "Here is the rest of it. And one more line."
    assert rest[0].start == pytest.approx(2.0) and rest[-1].start < 6.0


# ---------- make: keep, take, new ----------


def test_keep_take_and_new_shot_1_without_touching_the_shots(auth_client):
    from app import models

    pid, base_id = _base(auth_client)
    base = _get(models.Render, base_id)
    comp = base.composition_json
    shot1 = comp["shots"][0]
    # Another take of shot 1 (not the current clip).
    db = _db()
    try:
        alt = _clip(Path(shot1["clip_path"]).with_name("alt_take.mp4"), 4.0)
        take = models.ShotTake(shot_id=shot1["shot_id"], clip_path=str(alt), provider="mock",
                               provider_model="mock/alt", aspect_ratio="9:16")
        db.add(take)
        db.commit()
        take_id = take.id
    finally:
        db.close()
    before = _shot_state(pid)
    made = _create(auth_client, pid, base_id, [
        {"hook_text": "Why stop now?", "style": "question"},
        {"hook_text": "Honestly, stop scrolling.", "style": "bold_claim", "shot1_source": "take",
         "shot1_take_id": take_id},
        {"hook_text": "Fact: stop scrolling.", "style": "stat", "shot1_source": "new", "shot1_prompt": "a red car"},
    ])
    plan = made["plan"]
    keep_row, take_row, new_row = plan["variants"]
    assert keep_row["shot1"]["cost"] == 0 and take_row["shot1"]["cost"] == 0
    assert new_row["shot1"]["cost"] == pytest.approx(3.0 * 0.15) and new_row["needed"] == [0]
    assert keep_row["tts_cost"] == pytest.approx(len("Why stop now?") / 1000 * 0.20, abs=1e-4)
    assert plan["summary"].startswith("3 openings · about $") and "the rest of the video is reused" in plan["summary"]
    ids = [v["id"] for v in made["variants"]]

    r = _make(auth_client, pid, ids)
    assert r.status_code == 202, r.text
    lab = auth_client.get(f"/api/projects/{pid}/hook-lab").json()
    variants = {v["id"]: v for v in lab["bases"][0]["variants"]}
    assert all(variants[i]["status"] == "completed" for i in ids), variants
    assert [variants[i]["version_label"] for i in ids] == [
        "Version 1 · opening B", "Version 1 · opening C", "Version 1 · opening D"]
    assert auth_client.get(f"/api/projects/{pid}/status").json()["project_status"] == "completed"

    renders = {i: _get(models.Render, variants[i]["render_id"]) for i in ids}
    for i in ids:
        r = renders[i]
        assert r.hook_variant_id == i and r.status == "completed" and Path(r.final_video_path).is_file()
        c = r.composition_json
        assert c["base_render_id"] == base_id and c["hook_window_seconds"] == 2.5
        assert c["shots"][1]["clip_path"] == comp["shots"][1]["clip_path"]  # the rest is reused
        assert c["audio_path"].endswith(f"voice_variant_{i}.m4a")
        assert _duration(r.final_video_path) == pytest.approx(6.0, abs=0.15)
    keep_c, take_c, new_c = (renders[i].composition_json for i in ids)
    assert keep_c["shots"][0]["clip_path"] == shot1["clip_path"]
    assert take_c["shots"][0]["clip_path"] == str(alt) and take_c["shots"][0]["take_id"] == take_id
    assert keep_c["hook"] == "Why stop now?" and keep_c["script"].startswith("Why stop now? Here is the rest")
    assert keep_c["captions"][0]["text"].startswith("Why stop now?")
    # The new shot 1: a hook_shot job for the variant, attributed to its render.
    (job,) = _jobs(pid, "hook_shot")
    assert job.ref_id == ids[2] and job.render_id == renders[ids[2]].id and job.status == "completed"
    assert new_c["shots"][0]["clip_path"] == job.result_path
    assert Path(_get(models.HookVariant, ids[2]).shot1_clip_path) == Path(job.result_path)
    # Not one VideoShot row changed, and no shot/avatar job was made.
    assert _shot_state(pid) == before
    assert _jobs(pid, "shot")[-1].id < job.id and not _jobs(pid, "avatar")


def test_the_original_opening_as_a_control_costs_nothing(auth_client):
    from app import models

    pid, base_id = _base(auth_client)
    made = _create(auth_client, pid, base_id, [{"hook_text": "Stop scrolling now!", "style": "original"}])
    row = made["plan"]["variants"][0]
    assert row["total"] == 0 and row["tts_cost"] == 0 and row["warnings"] == []
    assert _make(auth_client, pid, [made["variants"][0]["id"]]).status_code == 202
    v = _get(models.HookVariant, made["variants"][0]["id"])
    base = _get(models.Render, base_id)
    comp = _get(models.Render, v.render_id).composition_json
    assert v.status == "completed" and comp["audio_path"] == base.audio_path  # the base voice, not bought again
    # The hook-lab caption builder: the opening over the window, then the rest.
    assert comp["captions"][0]["text"] == "Stop scrolling now!" and comp["captions"][1]["start"] == 2.5
    # Deleting the control never deletes the base voiceover.
    assert auth_client.delete(f"/api/hook-variants/{v.id}", params={"delete_render": "true"}).status_code == 200
    assert Path(base.audio_path).is_file()


def test_variants_never_become_the_projects_voiceover_or_get_composed_as_renders(auth_client):
    from app import models
    from app.services import pipeline

    pid, base_id = _base(auth_client)
    made = _create(auth_client, pid, base_id, [{"hook_text": "Why stop now?", "style": "question"}])
    assert _make(auth_client, pid, [made["variants"][0]["id"]]).status_code == 202
    vr = _renders(pid)[-1]
    assert vr.hook_variant_id and vr.status == "completed"
    db = _db()
    try:
        project = db.get(models.VideoProject, pid)
        assert pipeline._latest_audio_render(db, pid).id == base_id
        cur = pipeline.current_voiceover(db, project)
        assert cur["path"] == _get(models.Render, base_id).audio_path and cur["current"]
        plan = pipeline.render_plan(db, project)
        assert plan["voiceover"] == "reuse" and plan["generate"] == 0 and not plan["voiceover_changed"]
        assert pipeline.busy_reason(db, project) is None
        vr = db.get(models.Render, vr.id)
        vr.status = "generating_shots"
        db.commit()
        pipeline.maybe_compose(db, vr.id)
        db.refresh(vr)
        assert vr.status == "generating_shots"  # never composed from the project's shots
        assert pipeline.busy_reason(db, project) is not None  # a variant in progress
    finally:
        db.close()
    # The editor's status shows the base render, never the variant.
    assert auth_client.get(f"/api/projects/{pid}/status").json()["latest_render"]["id"] == base_id


# ---------- lip-sync ----------


def _talking_base(auth_client, *, extra_shot: bool) -> tuple[int, int]:
    from .test_review_fixes import _talking_project

    _lipsync_on(auth_client)
    pid = _talking_project(auth_client, extra_shot=extra_shot)
    assert auth_client.post(f"/api/projects/{pid}/generate-video?force=true").status_code == 202
    base = _renders(pid)[-1]
    assert base.status == "completed", base.error
    return pid, base.id


def test_redo_talking_head_and_the_lip_synced_shots_the_opening_overlaps(auth_client):
    from app import models

    pid, base_id = _talking_base(auth_client, extra_shot=True)  # shot 1: 3 s, shot 2: 4 s, both lip-synced
    lab = auth_client.get(f"/api/projects/{pid}/hook-lab").json()
    assert lab["bases"][0]["shot1"]["can_redo_talking_head"] and lab["bases"][0]["shot1"]["lipsynced"]
    before = _shot_state(pid)
    made = _create(auth_client, pid, base_id, [
        {"hook_text": "Hi, quick test here.", "style": "custom", "shot1_source": "lipsync"},
        {"hook_text": "Hello, short test here.", "style": "custom"},  # keep: a warning, and shot 2 still redone
    ], window=3.5)
    lip, keep = made["plan"]["variants"]
    assert lip["shot1"]["lipsync"] and lip["shot1"]["cost"] == pytest.approx(3 * 0.05)
    assert [e["shot_order"] for e in lip["extra_lipsync"]] == [2]  # starts at 3 s, inside the 3.5 s opening
    assert lip["extra_lipsync"][0]["cost"] == pytest.approx(4 * 0.05)
    assert lip["total"] == pytest.approx(7 * 0.05 + lip["tts_cost"], abs=1e-4)
    assert keep["warnings"] and "Redo talking head" in keep["warnings"][0]
    ids = [v["id"] for v in made["variants"]]
    assert _make(auth_client, pid, ids).status_code == 202
    jobs = _jobs(pid, "hook_avatar")
    assert sorted((j.ref_id, j.context_json["index"]) for j in jobs) == [(ids[0], 0), (ids[0], 1), (ids[1], 1)]
    assert all(j.status == "completed" for j in jobs)
    assert _shot_state(pid) == before  # the VideoShot rows are untouched
    v = _get(models.HookVariant, ids[0])
    assert v.status == "completed" and set(v.clips_json["clips"]) == {"0", "1"}
    comp = _get(models.Render, v.render_id).composition_json
    assert [s["clip_path"] for s in comp["shots"]] == [v.clips_json["clips"]["0"], v.clips_json["clips"]["1"]]


def test_a_refused_lip_sync_is_caught_before_any_clip_is_paid(auth_client, monkeypatch):
    """Make's up-front check covers the words of every shot lip-synced
    again: a refusal there comes before the new shot 1 is bought."""
    from app import models
    from app.services import minor_check
    from app.services.safety import UnsafePromptError

    pid, base_id = _talking_base(auth_client, extra_shot=True)  # shot 2 starts inside a 3.5 s opening
    made = _create(auth_client, pid, base_id, [
        {"hook_text": "Hi, quick test here.", "style": "custom", "shot1_source": "new",
         "shot1_prompt": "a green kite"}], window=3.5)
    (vid,) = (v["id"] for v in made["variants"])
    assert [e["shot_order"] for e in made["plan"]["variants"][0]["extra_lipsync"]] == [2]
    real = minor_check.check_generation

    def spy(prompt, negative="", *, kind):
        if kind == "lipsync":
            raise UnsafePromptError("lip-sync refused")
        return real(prompt, negative, kind=kind)

    monkeypatch.setattr(minor_check, "check_generation", spy)
    assert _make(auth_client, pid, [vid]).status_code == 202
    v = _get(models.HookVariant, vid)
    assert v.status == "failed" and "lip-sync refused" in v.error
    assert _jobs(pid, "hook_shot") == [] and _jobs(pid, "hook_avatar") == []


def test_a_bases_lip_synced_shots_are_known_after_their_shot_is_deleted(auth_client):
    """The base's own record says which shots were lip-synced: a re-plan or
    a shot delete that removes the row doesn't hide them."""
    from app import models

    pid, base_id = _talking_base(auth_client, extra_shot=True)
    comp = _get(models.Render, base_id).composition_json
    assert [s["lipsynced"] for s in comp["shots"]] == [True, True]
    db = _db()
    try:
        db.delete(db.get(models.VideoShot, comp["shots"][1]["shot_id"]))
        db.commit()
    finally:
        db.close()
    made = _create(auth_client, pid, base_id, [
        {"hook_text": "Hi, quick test here.", "style": "custom", "shot1_source": "lipsync"}], window=3.5)
    assert [e["shot_order"] for e in made["plan"]["variants"][0]["extra_lipsync"]] == [2]


def test_a_new_shot_1_uses_the_pictures_the_base_was_made_with(auth_client, monkeypatch, tmp_path):
    """A new shot 1 is made with the references the base's shot 1 had,
    even when its shot row is gone since."""
    from app import models
    from app.services import pipeline

    pid, _ = _base(auth_client)
    db = _db()
    try:
        pic = tmp_path / "ref.png"
        pic.write_bytes(b"png")
        asset = models.Asset(owner_kind="ingredient", file_path=str(pic), asset_type="hero")
        db.add(asset)
        db.flush()
        first = pipeline._body_shots(db.get(models.VideoProject, pid))[0]
        first.reference_asset_ids_json = [asset.id]
        db.commit()
        asset_id = asset.id
        base = pipeline.recompose_project(db, pid)
        base_id = base.id
        assert base.composition_json["shots"][0]["reference_asset_ids"] == [asset_id]
        db.delete(db.get(models.VideoShot, base.composition_json["shots"][0]["shot_id"]))
        db.commit()
    finally:
        db.close()
    asked: list[list[int]] = []
    real = pipeline._asset_refs
    monkeypatch.setattr(pipeline, "_asset_refs", lambda db, ids: asked.append(list(ids)) or real(db, ids))
    made = _create(auth_client, pid, base_id, [
        {"hook_text": "Fact: stop the scroll.", "style": "stat", "shot1_source": "new", "shot1_prompt": "a blue kite"}])
    assert _make(auth_client, pid, [made["variants"][0]["id"]]).status_code == 202
    assert [asset_id] in asked
    assert len(_jobs(pid, "hook_shot")) == 1


def test_a_single_talking_head_video_is_lip_synced_again_whole(auth_client):
    pid, base_id = _talking_base(auth_client, extra_shot=False)
    made = _create(auth_client, pid, base_id, [
        {"hook_text": "Hi, quick test here.", "style": "custom", "shot1_source": "lipsync"}], window=3.0)
    row = made["plan"]["variants"][0]
    assert row["notes"] and "all of it" in row["notes"][0] and row["extra_lipsync"] == []


def test_lipsync_option_refused_on_a_shot_that_is_not_a_talking_head(auth_client):
    pid, base_id = _base(auth_client)
    r = auth_client.post(f"/api/projects/{pid}/hook-lab/variants", json={
        "base_render_id": base_id, "window_seconds": 2.5,
        "variants": [{"hook_text": "Why stop now?", "shot1_source": "lipsync"}]})
    assert r.status_code == 422 and "not a talking head" in r.json()["detail"]
    r = auth_client.post(f"/api/projects/{pid}/hook-lab/variants", json={
        "base_render_id": base_id, "variants": [{"hook_text": "Why stop now?", "shot1_source": "new"}]})
    assert r.status_code == 422 and "Describe the new shot 1" in r.json()["detail"]


# ---------- takes ----------


def test_takes_used_by_renders_or_variants_survive_prune(auth_client, tmp_path):
    from app import models
    from app.services import takes

    pid, base_id = _base(auth_client)
    comp = _get(models.Render, base_id).composition_json
    shot_id, base_take = comp["shots"][0]["shot_id"], comp["shots"][0]["take_id"]
    db = _db()
    try:
        shot = db.get(models.VideoShot, shot_id)
        extra = []
        for i in range(takes.KEEP + 3):
            p = tmp_path / f"t{i}.mp4"
            shutil.copyfile(shot.clip_path, p)
            t = models.ShotTake(shot_id=shot.id, clip_path=str(p), provider="mock")
            shot.takes.append(t)
            db.commit()
            extra.append(t.id)
        picked = extra[0]  # an old take a variant chose for its shot 1
        db.add(models.HookVariant(project_id=pid, base_render_id=base_id, label="B", style="custom",
                                  hook_text="x", shot1_source="take", shot1_take_id=picked, status="draft"))
        # The shot's current clip is now the newest take, so the base take is old.
        shot.clip_path = shot.takes[-1].clip_path
        db.commit()
        takes.prune(db, shot)
        left = {t.id for t in db.get(models.VideoShot, shot_id).takes}
    finally:
        db.close()
    assert base_take in left and picked in left  # protected
    assert extra[1] not in left  # an old take nothing needs is pruned


# ---------- make is guarded and claimed ----------


def test_make_is_refused_over_budget_with_the_estimate(auth_client, monkeypatch):
    from app import models
    from app.services import spend

    from .test_spend import _add

    pid, base_id = _base(auth_client)
    made = _create(auth_client, pid, base_id, [
        {"hook_text": "Fact: stop scrolling.", "style": "stat", "shot1_source": "new", "shot1_prompt": "a red car"}])
    vid = made["variants"][0]["id"]
    monkeypatch.setattr(spend, "_all_mocked", lambda: False)
    auth_client.put("/api/spend/budget", json={"monthly_usd": 1, "hard_stop": True, "alert_pcts": []})
    _add(0.8)  # $0.20 left; this costs ~$0.45
    r = _make(auth_client, pid, [vid])
    assert r.status_code == 402 and "would cost" in r.json()["detail"], r.text
    assert _get(models.HookVariant, vid).status == "draft"
    assert auth_client.get(f"/api/projects/{pid}").json()["status"] == "completed"  # no claim left behind


def test_make_refused_when_a_provider_is_off(auth_client, monkeypatch):
    from app.api import hook_lab as api
    from app.services.provider_keys import ProviderUnavailable

    pid, base_id = _base(auth_client)
    made = _create(auth_client, pid, base_id, [{"hook_text": "Why is it off, friend?", "style": "question"}])

    def off(kind, *a, **kw):
        raise ProviderUnavailable("elevenlabs", kind=kind)

    monkeypatch.setattr(api.provider_gate, "require_model", off)
    r = _make(auth_client, pid, [made["variants"][0]["id"]])
    assert r.status_code == 409 and r.json()["code"] == "provider_unavailable"


def test_make_suggest_and_listen_are_rate_limited(auth_client, monkeypatch):
    from .test_rate_limit import _tighten_limits

    pid, base_id = _base(auth_client)
    made = _create(auth_client, pid, base_id, [{"hook_text": "Why limit me, friend?", "style": "question"}])
    vid = made["variants"][0]["id"]
    _tighten_limits(monkeypatch, burst=1)
    url = f"/api/projects/{pid}/hook-lab/suggest"
    assert auth_client.post(url, json={"base_render_id": base_id}).status_code == 200
    assert auth_client.post(url, json={"base_render_id": base_id}).status_code == 429
    assert auth_client.post(f"/api/hook-variants/{vid}/preview-audio").status_code == 429
    assert _make(auth_client, pid, [vid]).status_code == 429


def test_make_claims_atomically_so_nothing_runs_twice(auth_client, monkeypatch):
    from app import models
    from app.api import hook_lab as api

    pid, base_id = _base(auth_client)
    made = _create(auth_client, pid, base_id, [{"hook_text": "Why stop now?", "style": "question"},
                                               {"hook_text": "So stop scrolling.", "style": "story"}])
    a, b = (v["id"] for v in made["variants"])
    queued: list = []
    monkeypatch.setattr(api, "enqueue", lambda fn, *args, **kw: queued.append((args, kw)) or "rq-1")
    assert _make(auth_client, pid, [a]).status_code == 202
    assert _get(models.HookVariant, a).status == "making"
    # A second click, or another opening while the first job holds the project: refused.
    assert _make(auth_client, pid, [a]).status_code == 409
    r = _make(auth_client, pid, [b])
    assert r.status_code == 409 and _get(models.HookVariant, b).status == "draft"
    assert len(queued) == 1 and queued[0][1]["claim"] == "generating" and queued[0][1]["claim_token"]

    # The queued job runs once; a stale copy of it does nothing.
    from app.workers.jobs import make_hook_variants_job

    make_hook_variants_job(*queued[0][0], **queued[0][1])
    make_hook_variants_job(*queued[0][0], **queued[0][1])
    assert _get(models.HookVariant, a).status == "completed"
    assert len([r for r in _renders(pid) if r.hook_variant_id == a]) == 1
    assert auth_client.get(f"/api/projects/{pid}").json()["status"] == "completed"
    # Made: it can't be made again, edited or plainly deleted.
    assert _make(auth_client, pid, [a]).status_code == 409
    assert auth_client.patch(f"/api/hook-variants/{a}", json={"hook_text": "x"}).status_code == 409


# ---------- events ----------


def test_events_per_variant(auth_client):
    from app import models

    pid, base_id = _base(auth_client)
    made = _create(auth_client, pid, base_id, [
        {"hook_text": "Why stop now?", "style": "question"},
        # Not heard with Listen first: only the make finds it too long.
        {"hook_text": "This is a much longer opening line that will never fit in that window", "style": "custom"},
    ])
    ok, bad = (v["id"] for v in made["variants"])
    assert _make(auth_client, pid, [ok, bad]).status_code == 202
    db = _db()
    try:
        evs = db.query(models.Event).filter_by(kind="hook_lab").order_by(models.Event.id).all()
    finally:
        db.close()
    by = {}
    for e in evs:
        by.setdefault(e.entity_id, []).append(e.status)
        assert e.entity_type == "hook_variant" and e.project_id == pid and e.url == f"/projects/{pid}/hooks"
    assert by[ok][0] == "started" and by[ok][-1] == "completed"
    assert by[bad] == ["started", "failed"]
    v = _get(models.HookVariant, bad)
    assert v.status == "failed" and "longer than the original" in v.error
    failed = [e for e in evs if e.entity_id == bad and e.status == "failed"][0]
    assert failed.title.startswith("Opening C failed: This opening is about")
    # A failed opening can be edited and made again.
    r = auth_client.patch(f"/api/hook-variants/{bad}", json={"hook_text": "So stop scrolling."})
    assert r.status_code == 200 and r.json()["status"] == "draft"
    assert _make(auth_client, pid, [bad]).status_code == 202
    assert _get(models.HookVariant, bad).status == "completed"


# ---------- edit and delete ----------


def test_deleting_an_opening_takes_its_earlier_failed_renders_with_it(auth_client):
    """An opening's renders from earlier failed or cancelled tries must not
    be left behind as ordinary storyboard renders."""
    from app import models

    pid, base_id = _base(auth_client)
    made = _create(auth_client, pid, base_id, [{"hook_text": "Why stop now?", "style": "question"}])
    vid = made["variants"][0]["id"]
    db = _db()
    try:
        old = [models.Render(project_id=pid, status=st, hook_variant_id=vid) for st in ("failed", "cancelled")]
        db.add_all(old)
        db.flush()
        last = models.Render(project_id=pid, status="failed", hook_variant_id=vid)
        db.add(last)
        db.flush()
        v = db.get(models.HookVariant, vid)
        v.status, v.render_id = "failed", last.id
        db.commit()
        ids = [r.id for r in old] + [last.id]
    finally:
        db.close()
    r = auth_client.delete(f"/api/hook-variants/{vid}")
    assert r.status_code == 200 and not r.json()["render_kept"]
    assert all(_get(models.Render, i) is None for i in ids)
    assert _get(models.HookVariant, vid) is None
    assert [x.id for x in _renders(pid)] == [base_id]


def test_patch_and_delete(auth_client):
    from app import models

    pid, base_id = _base(auth_client)
    made = _create(auth_client, pid, base_id, [{"hook_text": "Why stop now?", "style": "question"},
                                               {"hook_text": "So stop scrolling.", "style": "story"}])
    a, b = (v["id"] for v in made["variants"])
    r = auth_client.patch(f"/api/hook-variants/{a}", json={"window_seconds": 2.0, "shot1_source": "new",
                                                           "shot1_prompt": "a cat"})
    assert r.status_code == 200 and r.json()["plan"]["shot1"]["cost"] > 0 and r.json()["window_seconds"] == 2.0
    assert auth_client.patch(f"/api/hook-variants/{a}", json={"window_seconds": 99}).status_code == 422
    assert auth_client.patch(f"/api/hook-variants/{a}", json={"style": "nope"}).status_code == 422
    assert auth_client.delete(f"/api/hook-variants/{a}").json()["deleted"]
    assert _get(models.HookVariant, a) is None

    assert _make(auth_client, pid, [b]).status_code == 202
    rid = _get(models.HookVariant, b).render_id
    db = _db()
    try:
        db.add(models.PublishJob(render_id=rid, title="t", status="published", privacy="unlisted",
                                 external_id="yt1", external_url="https://youtu.be/yt1"))
        db.commit()
    finally:
        db.close()
    r = auth_client.delete(f"/api/hook-variants/{b}", params={"delete_render": "true"})
    assert r.status_code == 409 and "YouTube" in r.json()["detail"]
    r = auth_client.delete(f"/api/hook-variants/{b}")
    assert r.status_code == 200 and r.json()["render_kept"]
    assert _get(models.Render, rid).hook_variant_id == b  # still a variant in history and Results
    assert auth_client.get(f"/api/projects/{pid}/hook-lab").json()["bases"][0]["variants"] == []
    assert auth_client.get(f"/api/hook-variants/{b}").status_code == 404


def test_a_render_without_its_clips_must_be_recomposed_first(auth_client):
    from app import models

    pid, base_id = _base(auth_client)
    db = _db()
    try:
        r = db.get(models.Render, base_id)
        comp = dict(r.composition_json)
        comp["shots"] = [{**s, "clip_path": "/gone.mp4"} for s in comp["shots"]]
        r.composition_json = comp
        db.commit()
    finally:
        db.close()
    b = auth_client.get(f"/api/projects/{pid}/hook-lab").json()["bases"][0]
    assert not b["reusable"] and "Re-compose this version first" in b["reuse_error"]
    r = auth_client.post(f"/api/projects/{pid}/hook-lab/variants", json={
        "base_render_id": base_id, "window_seconds": 2.5, "variants": [{"hook_text": "Why stop now?"}]})
    assert r.status_code == 409 and "Re-compose this version first" in r.json()["detail"]


# ---------- Results ----------


def _published(db, render, ext, days_ago, views_by_day, likes=0):
    from app import models

    now = datetime.utcnow()
    up = now - timedelta(days=days_ago)
    job = models.PublishJob(render_id=render.id, title=f"t{ext}", status="published", privacy="public",
                            external_id=ext, external_url=f"https://youtu.be/{ext}", uploaded_at=up)
    db.add(job)
    db.flush()
    for d, views in enumerate(views_by_day, start=1):
        at = up + timedelta(days=d)
        if at > now:
            break
        db.add(models.VideoStat(publish_job_id=job.id, fetched_at=at, day=date(at.year, at.month, at.day),
                                views=views, likes=likes))
    return job


def test_results_group_by_hook_style_and_hook_tests(auth_client):
    from app import models
    from app.services import publishing

    db = _db()
    try:
        p = models.VideoProject(title="Hooks", mode="reel_montage", aspect_ratio="9:16", cta_text="",
                                generated_plan_json={"cleaned_voice_script": "Stop now! Rest."})
        db.add(p)
        db.flush()
        base = models.Render(project_id=p.id, status="completed", final_video_path="/b.mp4",
                             composition_json={"hook": "Stop now!", "duration_seconds": 20})
        db.add(base)
        db.flush()
        vb = models.HookVariant(project_id=p.id, base_render_id=base.id, label="B", style="question",
                                hook_text="Why stop?", status="completed")
        vc = models.HookVariant(project_id=p.id, base_render_id=base.id, label="C", style="stat",
                                hook_text="Fact: stop.", status="completed")
        db.add_all([vb, vc])
        db.flush()
        rb = models.Render(project_id=p.id, status="completed", hook_variant_id=vb.id, final_video_path="/v.mp4")
        rc = models.Render(project_id=p.id, status="completed", hook_variant_id=vc.id, final_video_path="/c.mp4")
        db.add_all([rb, rc])
        db.flush()
        vb.render_id, vc.render_id = rb.id, rc.id
        db.add_all([
            models.SpendEntry(provider="x", kind="video", cost_usd=2.0, project_id=p.id, render_id=base.id),
            models.SpendEntry(provider="x", kind="tts", cost_usd=0.3, project_id=p.id, render_id=rb.id),
            models.SpendEntry(provider="x", kind="tts", cost_usd=0.1, project_id=p.id, render_id=rc.id),
        ])
        # A: 100 views/day; B: 200 views/day; both 6 days up, numbers daily.
        ja = _published(db, base, "a", 6, [100 * d for d in range(1, 7)], likes=30)
        _published(db, rb, "b", 6, [200 * d for d in range(1, 7)], likes=12)
        db.commit()
        rows = {r["render_id"]: r for r in publishing.result_rows(db)}
        a_row, b_row = rows[base.id], rows[rb.id]
        assert (a_row["hook_label"], a_row["hook_style"], a_row["hook_base_render_id"]) == ("A", "Original opening",
                                                                                           base.id)
        assert (b_row["hook_label"], b_row["hook_style"], b_row["hook_variant_id"]) == ("B", "Question", vb.id)
        assert b_row["render_version_label"] == "Version 1 · opening B"
        # Cost: the variant's own; the base the project's without its openings' costs.
        assert (b_row["cost_usd"], b_row["cost_scope"]) == (0.3, "render")
        assert (a_row["cost_usd"], a_row["cost_scope"]) == (2.0, "project")
        groups = {g["key"]: g for g in publishing.group_rows(list(rows.values()), "hook")}
        assert set(groups) == {"Original opening", "Question"}
        assert publishing.group_rows(list(rows.values()), "type")[0]["total_cost_usd"] == 2.3

        (test,) = publishing.hook_tests(db, list(rows.values()))
        assert test["base_render_id"] == base.id and test["equal_age_days"] == 6 and test["ready"]
        assert test["verdict"] == "B is ahead: 2.0× the views per day." and test["leader"] == "B"
        ent = {e["hook_label"]: e for e in test["entries"]}
        assert ent["A"]["views_per_day_equal_age"] == 100 and ent["B"]["views_per_day_equal_age"] == 200
        assert ent["A"]["likes_per_1k_views"] == 50.0 and ent["B"]["likes_per_1k_views"] == 10.0
        assert ent["A"]["is_base"] and ent["B"]["cost_usd"] == 0.3

        # C goes up later: compared at C's age, and too early with 2 days.
        _published(db, rc, "c", 2, [500, 1000])
        db.commit()
        rows = publishing.result_rows(db)
        (test,) = publishing.hook_tests(db, rows)
        assert test["equal_age_days"] == 2 and not test["ready"] and test["verdict"].startswith("Too early to tell")
        ent = {e["hook_label"]: e for e in test["entries"]}
        assert ent["A"]["views_at_equal_age"] == 200 and ent["C"]["views_per_day_equal_age"] == 500
        assert ja.id in {e["publish_job_id"] for e in test["entries"]}
    finally:
        db.close()

    r = auth_client.get("/api/results", params={"group_by": "hook"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["group_label"] == "Opening (hook) style" and len(body["hook_tests"]) == 1
    assert {row["hook_label"] for row in body["rows"]} == {"A", "B", "C"}


def test_views_at_equal_age_need_a_snapshot_near_that_age():
    from types import SimpleNamespace

    from app.services import publishing

    t0 = datetime(2026, 1, 1)
    snaps = [SimpleNamespace(fetched_at=t0 + timedelta(days=d), views=10 * d) for d in (1, 2, 5)]
    assert publishing.views_at_age(snaps, t0, 2) == 20
    assert publishing.views_at_age(snaps, t0, 4) is None  # nothing taken around day 4
    assert publishing.views_at_age(snaps, t0, 5) == 50
    assert publishing.views_at_age(snaps, t0, 0) is None


# ---------- the minors check ----------


def test_every_new_prompt_meets_the_safety_check_before_anything_is_paid(auth_client, monkeypatch):
    from app import models
    from app.services import minor_check
    from app.services.safety import UnsafePromptError

    pid, base_id = _base(auth_client)
    made = _create(auth_client, pid, base_id, [
        {"hook_text": "Fact: stop the scroll.", "style": "stat", "shot1_source": "new", "shot1_prompt": "a blue kite"},
        {"hook_text": "Refused opening here.", "style": "custom", "shot1_source": "new", "shot1_prompt": "a red kite"},
    ])
    ok, bad = (v["id"] for v in made["variants"])
    seen: list[tuple[str, str]] = []
    real = minor_check.check_generation

    def spy(prompt, negative="", *, kind):
        seen.append((prompt, kind))
        if prompt == "Refused opening here.":
            raise UnsafePromptError("refused")
        return real(prompt, negative, kind=kind)

    monkeypatch.setattr(minor_check, "check_generation", spy)
    assert _make(auth_client, pid, [ok, bad]).status_code == 202
    assert ("Fact: stop the scroll.", "script") in seen and ("a blue kite", "video") in seen
    # Checked again at the guarded providers (TTS and video), not only up front.
    assert seen.count(("Fact: stop the scroll.", "script")) >= 2 and seen.count(("a blue kite", "video")) >= 2
    v = _get(models.HookVariant, bad)
    assert v.status == "failed" and "refused" in v.error
    assert {j.ref_id for j in _jobs(pid, "hook_shot")} == {ok}  # nothing was made for the refused one
    assert not Path(v.audio_path or "/nope").is_file()


# ---------- review fixes ----------


def _waiting_variant(pid, base_id, needed, tmp_path):
    """A variant waiting for its clips: making, its render in generating_shots."""
    from app import models

    db = _db()
    try:
        base = db.get(models.Render, base_id)
        v = models.HookVariant(project_id=pid, base_render_id=base_id, label="B", style="question",
                               hook_text="Why stop now?", window_seconds=2.5, status="making",
                               audio_path=base.audio_path, audio_fingerprint="fp",
                               clips_json={"needed": needed, "clips": {}})
        db.add(v)
        db.flush()
        r = models.Render(project_id=pid, status="generating_shots", hook_variant_id=v.id,
                          audio_path=base.audio_path)
        db.add(r)
        db.flush()
        v.render_id = r.id
        db.commit()
        return v.id, r.id
    finally:
        db.close()


def _done_job(pid, vid, rid, index, path, status="completed"):
    from app import models

    db = _db()
    try:
        job = models.VideoJob(purpose="hook_avatar", ref_id=vid, project_id=pid, render_id=rid, status=status,
                              result_path=str(path) if status == "completed" else "",
                              error="" if status == "completed" else "provider said no",
                              context_json={"index": index})
        db.add(job)
        db.commit()
        return job.id
    finally:
        db.close()


def test_two_clips_landing_at_once_are_both_counted(auth_client, monkeypatch, tmp_path):
    """'have' comes from the finished jobs, not a read-modify-write of
    clips_json that the second writer can overwrite."""
    from app import models
    from app.services import hook_lab
    from app.workers import queue

    pid, base_id = _base(auth_client)
    queued = []
    monkeypatch.setattr(queue, "enqueue", lambda f, *a, **k: queued.append(a) or "q")
    vid, rid = _waiting_variant(pid, base_id, [0, 1], tmp_path)
    _done_job(pid, vid, rid, 0, _clip(tmp_path / "c0.mp4", 3.0))
    _done_job(pid, vid, rid, 1, _clip(tmp_path / "c1.mp4", 3.0))
    # Both handlers read clips_json == {} and the last write kept only one key:
    # the clips are still both seen.
    db = _db()
    try:
        v = db.get(models.HookVariant, vid)
        v.clips_json = {"needed": [0, 1], "clips": {"1": str(tmp_path / "c1.mp4")}}
        db.commit()
        hook_lab.maybe_compose_variant(db, vid)
        v = db.get(models.HookVariant, vid)
        db.refresh(v)
        assert v.status == "ready" and set(v.clips_json["clips"]) == {"0", "1"}
        assert queued == [(vid,)]
    finally:
        db.close()


def test_the_sweeper_leaves_an_opening_alone_while_make_still_submits_its_clips(auth_client, monkeypatch,
                                                                               tmp_path):
    """Make holds the project while it submits the clips: an opening whose
    first clip is not submitted yet (a slow upload) is not failed then. If
    the Make job died, the opening is failed once the claim is old and its
    queue entry gone, and the render sweeper then gives the project back
    (neither sweeper waits on the other forever)."""
    from app import models
    from app.config import get_settings
    from app.services import hook_lab, sweepers
    from app.services.pipeline import claim_marker, hook_claim_token
    from app.workers import queue

    state = {"q": "alive"}
    monkeypatch.setattr(queue, "queue_entry_state", lambda qid: state["q"])
    pid, base_id = _base(auth_client)
    vid, rid = _waiting_variant(pid, base_id, [0], tmp_path)
    soon = datetime.utcnow() + timedelta(minutes=10)
    old = (datetime.utcnow() + timedelta(seconds=get_settings().queue_job_timeout_seconds)
           + sweepers.RENDER_GRACE + timedelta(minutes=5))
    db = _db()
    try:
        p = db.get(models.VideoProject, pid)
        p.status, p.claim_token, p.queue_job_id = "generating", claim_marker(hook_claim_token("completed")), "q1"
        db.commit()
        # A young claim: Make may still be submitting, whatever the queue says.
        state["q"] = "gone"
        hook_lab.sweep(db, now=soon)
        sweepers.sweep_renders(db, now=soon)
        assert db.get(models.HookVariant, vid).status == "making"
        assert db.get(models.Render, rid).status == "generating_shots"
        # An old claim whose queue entry still runs: still left alone.
        state["q"] = "alive"
        hook_lab.sweep(db, now=old)
        assert db.get(models.HookVariant, vid).status == "making"
        # The Make job is gone: the opening fails, then the project is given back.
        state["q"] = "gone"
        hook_lab.sweep(db, now=old)
        sweepers.sweep_renders(db, now=old)
        db.expire_all()
        assert db.get(models.HookVariant, vid).status == "failed"
        assert db.get(models.Render, rid).status == "failed"
        p = db.get(models.VideoProject, pid)
        assert (p.status, p.claim_token) == ("completed", "")
    finally:
        db.close()


def test_the_sweeper_moves_on_stuck_openings(auth_client, monkeypatch, tmp_path):
    from app import models
    from app.services import hook_lab, pipeline
    from app.workers import queue

    pid, base_id = _base(auth_client)
    queued = []
    monkeypatch.setattr(queue, "enqueue", lambda f, *a, **k: queued.append(a) or "q")
    later = datetime.utcnow() + timedelta(minutes=10)
    # 1. Every clip is in but nothing composed it (the handler died): composed.
    vid, rid = _waiting_variant(pid, base_id, [0], tmp_path)
    _done_job(pid, vid, rid, 0, _clip(tmp_path / "c0.mp4", 3.0))
    db = _db()
    try:
        out = hook_lab.sweep(db, now=later)
        assert out["composed"] == 1 and queued == [(vid,)]
        assert db.get(models.HookVariant, vid).status == "ready"
        # 2. Ready but its compose was never queued: queued again.
        queued.clear()
        out = hook_lab.sweep(db, now=later + timedelta(minutes=5))
        assert out["requeued"] == 1 and queued == [(vid,)]
        v = db.get(models.HookVariant, vid)
        v.status = "completed"
        r = db.get(models.Render, rid)
        r.status = "completed"
        db.commit()
    finally:
        db.close()
    # 3. A clip failed and its failure handler never ran: the opening fails,
    #    and the project is free again.
    vid, rid = _waiting_variant(pid, base_id, [0], tmp_path)
    _done_job(pid, vid, rid, 0, None, status="failed")
    db = _db()
    try:
        project = db.get(models.VideoProject, pid)
        assert pipeline.busy_reason(db, project) is not None
        out = hook_lab.sweep(db, now=later)
        assert out["failed"] >= 1
        v = db.get(models.HookVariant, vid)
        assert v.status == "failed" and "provider said no" in v.error
        assert db.get(models.Render, rid).status == "failed"
        assert pipeline.busy_reason(db, project) is None
    finally:
        db.close()


def test_an_opening_waiting_for_its_clips_can_be_stopped(auth_client, tmp_path):
    from app import models
    from app.services import pipeline

    pid, base_id = _base(auth_client)
    vid, rid = _waiting_variant(pid, base_id, [0], tmp_path)
    r = auth_client.post(f"/api/hook-variants/{vid}/cancel")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "failed" and r.json()["error"] == "Cancelled"
    assert _get(models.Render, rid).status == "cancelled"
    db = _db()
    try:
        assert pipeline.busy_reason(db, db.get(models.VideoProject, pid)) is None
    finally:
        db.close()
    assert auth_client.post(f"/api/hook-variants/{vid}/cancel").status_code == 409  # not running any more
    # A clip landing after the stop is not used.
    jid = _done_job(pid, vid, rid, 0, _clip(tmp_path / "late.mp4", 3.0))
    db = _db()
    try:
        from app.services import hook_lab

        hook_lab.on_clip_ready(db, db.get(models.VideoJob, jid))
        assert db.get(models.HookVariant, vid).status == "failed"
    finally:
        db.close()


def test_a_shorter_shot_1_take_holds_its_slot(auth_client):
    """The variant's body keeps the base timeline: a slightly short take is
    held on its last frame; a much shorter one is refused before paying."""
    from app import models

    pid, base_id = _base(auth_client)
    base = _get(models.Render, base_id)
    shot1 = base.composition_json["shots"][0]
    assert shot1["seconds"] == pytest.approx(3.0, abs=0.05)
    db = _db()
    try:
        short = models.ShotTake(shot_id=shot1["shot_id"], provider="mock", aspect_ratio="9:16",
                                clip_path=str(_clip(Path(shot1["clip_path"]).with_name("short.mp4"), 2.7)))
        tiny = models.ShotTake(shot_id=shot1["shot_id"], provider="mock", aspect_ratio="9:16",
                               clip_path=str(_clip(Path(shot1["clip_path"]).with_name("tiny.mp4"), 1.2)))
        db.add_all([short, tiny])
        db.commit()
        short_id, tiny_id = short.id, tiny.id
    finally:
        db.close()
    r = auth_client.post(f"/api/projects/{pid}/hook-lab/variants", json={
        "base_render_id": base_id, "window_seconds": 2.5,
        "variants": [{"hook_text": "Why stop now?", "shot1_source": "take", "shot1_take_id": tiny_id}]})
    assert r.status_code == 422 and "shorter than shot 1" in r.json()["detail"]
    made = _create(auth_client, pid, base_id, [
        {"hook_text": "Why stop now?", "shot1_source": "take", "shot1_take_id": short_id}])
    vid = made["variants"][0]["id"]
    assert _make(auth_client, pid, [vid]).status_code == 202
    vr = _get(models.Render, _get(models.HookVariant, vid).render_id)
    assert vr.status == "completed", vr.error
    c = vr.composition_json
    assert c["shots"][0]["seconds"] == pytest.approx(shot1["seconds"], abs=0.01)
    assert c["body_seconds"] == pytest.approx(base.composition_json["body_seconds"], abs=0.01)
    assert _duration(vr.final_video_path) == pytest.approx(_duration(base.final_video_path), abs=0.1)


def test_a_held_one_shot_video_checks_and_prices_shot_1_at_its_planned_length(auth_client):
    """A one-shot video whose clip was held on to cover the voiceover: a
    take only has to fill the shot's planned length (it is held the same
    way), a new shot 1 is asked for and priced at that length, and the
    opening still plays for the held length."""
    from app import models

    pid, base_id = _base(auth_client)
    db = _db()
    try:
        base = db.get(models.Render, base_id)
        comp = dict(base.composition_json)
        body = comp["body_seconds"]
        shot1 = dict(comp["shots"][0], planned_seconds=3.0, held_seconds=body, seconds=body)
        comp["shots"] = [shot1]  # as recorded by a compose whose only clip was held to cover the voice
        base.composition_json = comp
        take = models.ShotTake(shot_id=shot1["shot_id"], provider="mock", aspect_ratio="9:16",
                               clip_path=str(_clip(Path(shot1["clip_path"]).with_name("held_take.mp4"), 2.8)))
        db.add(take)
        db.commit()
        take_id = take.id
    finally:
        db.close()
    made = _create(auth_client, pid, base_id, [
        {"hook_text": "Why stop now?", "shot1_source": "take", "shot1_take_id": take_id},
        {"hook_text": "Fact: stop scrolling.", "shot1_source": "new", "shot1_prompt": "a red car"}])
    row = made["plan"]["variants"][1]
    assert row["shot1"]["seconds"] == pytest.approx(3.0)
    assert row["shot1"]["cost"] == pytest.approx(3.0 * 0.15, abs=1e-4)
    vid = made["variants"][0]["id"]
    assert _make(auth_client, pid, [vid]).status_code == 202
    vr = _get(models.Render, _get(models.HookVariant, vid).render_id)
    assert vr.status == "completed", vr.error
    assert vr.composition_json["body_seconds"] == pytest.approx(body, abs=0.01)
    assert _duration(vr.final_video_path) == pytest.approx(_duration(_get(models.Render, base_id).final_video_path),
                                                           abs=0.1)


def test_a_variant_of_a_capped_hold_is_not_held_again(auth_client, monkeypatch):
    """A one-shot video whose clip was held for the most it may be (the
    voice runs on past it and is cut): a variant whose shot 1 is a take is
    held from the shot's planned length again, not for another
    VOICE_HOLD_MAX_SECONDS on top of the base's hold."""
    from app import models
    from app.services import pipeline

    monkeypatch.setattr(pipeline, "VOICE_HOLD_MAX_SECONDS", 1.0)
    pid, base_id = _base(auth_client)
    db = _db()
    try:
        base = db.get(models.Render, base_id)
        comp = dict(base.composition_json)
        # As recorded by a compose that held its only clip, planned at 3 s, for 1 s at most.
        shot1 = dict(comp["shots"][0], planned_seconds=3.0, held_seconds=4.0, seconds=4.0)
        comp["shots"] = [shot1]
        comp["body_seconds"] = 4.0
        base.composition_json = comp
        take = models.ShotTake(shot_id=shot1["shot_id"], provider="mock", aspect_ratio="9:16",
                               clip_path=str(_clip(Path(shot1["clip_path"]).with_name("capped_take.mp4"), 2.8)))
        db.add(take)
        db.commit()
        take_id = take.id
    finally:
        db.close()
    made = _create(auth_client, pid, base_id, [
        {"hook_text": "Why stop now?", "shot1_source": "take", "shot1_take_id": take_id}])
    vid = made["variants"][0]["id"]
    assert _make(auth_client, pid, [vid]).status_code == 202
    vr = _get(models.Render, _get(models.HookVariant, vid).render_id)
    assert vr.status == "completed", vr.error
    assert _duration(vr.audio_path) > 5.1  # the voice runs past the cap
    assert vr.composition_json["body_seconds"] == pytest.approx(4.0, abs=0.01)
    assert vr.composition_json["shots"][0]["held_seconds"] == pytest.approx(4.0, abs=0.01)
    assert vr.composition_json["shots"][0]["planned_seconds"] == pytest.approx(3.0)


def test_a_new_shot_1_is_asked_for_at_the_length_it_is_priced_at(auth_client):
    from app import models

    pid, base_id = _base(auth_client)
    made = _create(auth_client, pid, base_id, [
        {"hook_text": "Fact: stop scrolling.", "shot1_source": "new", "shot1_prompt": "a red car"}])
    row = made["plan"]["variants"][0]
    assert _make(auth_client, pid, [made["variants"][0]["id"]]).status_code == 202
    (job,) = _jobs(pid, "hook_shot")
    assert job.context_json["duration"] == pytest.approx(row["shot1"]["seconds"])
    assert row["shot1"]["cost"] == pytest.approx(row["shot1"]["seconds"] * 0.15, abs=1e-4)
    assert _get(models.Render, job.render_id).status == "completed"


def test_a_one_sentence_script_swaps_the_whole_voiceover(auth_client, tmp_path):
    from app import models
    from app.services import hook_lab

    # Unit: the window is the whole voiceover, not a pause inside the line.
    voice = _voice(tmp_path / "one.m4a", [("tone", 1.5), ("gap", 0.3), ("tone", 2.0)])
    base = models.Render(audio_path=str(voice))
    assert hook_lab.find_hook_window(base, "Stop scrolling, because here is why.") == pytest.approx(
        _duration(voice), abs=0.02)
    # Captions: the new line only, over its spoken time; never overlapping.
    timed = hook_lab.hook_captions("A new line that is long enough to make chunks", "", 3.8, 6.0, 1.5)
    assert timed[-1].start < 1.5 and all(a.end <= b.start for a, b in zip(timed, timed[1:], strict=False))

    pid = _rendered(auth_client, script="Stop scrolling now because this one line is all you need.")
    lab = auth_client.get(f"/api/projects/{pid}/hook-lab").json()
    (b,) = lab["bases"]
    assert b["window_seconds"] == pytest.approx(b["audio_seconds"], abs=0.02)
    made = _create(auth_client, pid, b["render_id"], [{"hook_text": "Wait, watch this one.", "style": "custom"}],
                   window=b["audio_seconds"])
    vid = made["variants"][0]["id"]
    from app.services import hook_lab

    db = _db()
    try:  # not left over from another test's project with the same ids
        hook_lab.raw_hook_path(db, db.get(models.VideoProject, pid), db.get(models.HookVariant, vid)).unlink(
            missing_ok=True)
    finally:
        db.close()
    r = auth_client.post(f"/api/hook-variants/{vid}/preview-audio")
    assert r.status_code == 200 and r.json()["fits"] and r.json()["cost"] > 0, r.text
    # Only the new line: nothing of the original is joined after it.
    assert r.json()["duration_seconds"] == pytest.approx(b["audio_seconds"], abs=0.1)
    raw = hook_lab.raw_hook_path(_db(), _get(models.VideoProject, pid), _get(models.HookVariant, vid))
    assert _duration(raw) < b["audio_seconds"]


def test_an_opening_keeps_the_look_its_base_was_made_with(auth_client):
    from app import models

    pid, base_id = _base(auth_client)
    base = _get(models.Render, base_id)
    look = base.composition_json["settings"]
    assert look["caption_style"] and "cta_text" in look
    made = _create(auth_client, pid, base_id, [{"hook_text": "Why stop now?", "style": "question"}])
    # The project's look changes after the base was made.
    r = auth_client.patch(f"/api/projects/{pid}", json={"cta_text": "Follow for part two", "caption_style": "bold_yellow"})
    assert r.status_code == 200, r.text
    assert _make(auth_client, pid, [made["variants"][0]["id"]]).status_code == 202
    vr = _renders(pid)[-1]
    assert vr.hook_variant_id and vr.status == "completed", vr.error
    assert vr.composition_json["settings"] == look
    assert vr.composition_json["end_card_seconds"] == base.composition_json["end_card_seconds"]


def test_an_old_version_without_its_clips_recorded_is_not_rebuilt_from_newer_ones(auth_client):
    from app import models
    from app.services import hook_lab

    pid = _rendered(auth_client)
    assert auth_client.post(f"/api/projects/{pid}/generate-video", params={"force": "true"}).status_code == 202
    v1, v2 = [r for r in _renders(pid) if r.status == "completed"][:2]
    db = _db()
    try:
        for rid in (v1.id, v2.id):
            db.get(models.Render, rid).composition_json = None
        db.commit()
        with pytest.raises(hook_lab.HookLabError, match="Re-compose this version first"):
            hook_lab.base_clips(db, db.get(models.Render, v1.id))
        assert hook_lab.base_clips(db, db.get(models.Render, v2.id))  # the newest can still be rebuilt
    finally:
        db.close()


def test_listen_in_progress_blocks_make(auth_client):
    from app import models

    pid, base_id = _base(auth_client)
    made = _create(auth_client, pid, base_id, [{"hook_text": "Why stop now?", "style": "question"}])
    vid = made["variants"][0]["id"]
    db = _db()
    try:
        v = db.get(models.HookVariant, vid)
        v.voicing_until = datetime.utcnow() + timedelta(minutes=2)  # a Listen is making its voice
        db.commit()
    finally:
        db.close()
    r = _make(auth_client, pid, [vid])
    assert r.status_code == 409 and _get(models.HookVariant, vid).status == "draft"
    assert auth_client.post(f"/api/hook-variants/{vid}/preview-audio").status_code == 409
    assert auth_client.get(f"/api/projects/{pid}").json()["status"] == "completed"  # claim given back


def test_takes_a_version_or_opening_uses_cannot_be_deleted(auth_client):
    from app import models

    pid, base_id = _base(auth_client)
    shot1 = _get(models.Render, base_id).composition_json["shots"][0]
    db = _db()
    try:
        take = models.ShotTake(shot_id=shot1["shot_id"], provider="mock", aspect_ratio="9:16",
                               clip_path=str(_clip(Path(shot1["clip_path"]).with_name("pick.mp4"), 3.0)))
        db.add(take)
        db.commit()
        take_id = take.id
    finally:
        db.close()
    _create(auth_client, pid, base_id, [{"hook_text": "Why?", "shot1_source": "take", "shot1_take_id": take_id}])
    r = auth_client.delete(f"/api/projects/{pid}/shots/{shot1['shot_id']}/takes/{take_id}")
    assert r.status_code == 409 and "uses this take" in r.json()["detail"]
    assert Path(_get(models.ShotTake, take_id).clip_path).is_file()
    # Deleting shot 1 keeps the clips the version with openings is built from.
    base_clip = Path(shot1["clip_path"])
    from app.services import takes

    db = _db()
    try:
        assert not takes.remove_file(db, str(base_clip), project_id=pid)
    finally:
        db.close()
    assert base_clip.is_file()


def test_suggest_refused_when_the_version_has_no_room(auth_client, monkeypatch):
    from app import models
    from app.services import hook_lab

    monkeypatch.setattr(hook_lab, "MAX_PER_BASE", 1)
    pid, base_id = _base(auth_client)
    _create(auth_client, pid, base_id, [{"hook_text": "Why stop now?", "style": "question"}])
    lab = auth_client.get(f"/api/projects/{pid}/hook-lab").json()
    assert lab["bases"][0]["room"] == 0
    r = auth_client.post(f"/api/projects/{pid}/hook-lab/suggest", json={"base_render_id": base_id, "n": 1})
    assert r.status_code == 409
    db = _db()
    try:
        assert not db.query(models.ProviderCall).filter_by(endpoint="chat.hook_ideas").count() \
            if hasattr(models, "ProviderCall") else True
    finally:
        db.close()


def test_a_hook_without_end_punctuation_stays_its_own_sentence():
    from app.services import hook_lab

    script = "Stop scrolling. Here is why it matters."
    assert hook_lab.replace_first_sentence(script, "Wait") == "Wait. Here is why it matters."
    assert hook_lab.replace_first_sentence(script, "Wait!") == "Wait! Here is why it matters."
    assert hook_lab.replace_first_sentence("Only this.", "Wait") == "Wait"


def test_hook_tests_count_a_video_uploaded_twice_once(auth_client):
    from app.services import publishing

    now = datetime(2026, 11, 10)
    def row(job, render, label, views, variant=None):
        return {"hook_base_render_id": 1, "hook_variant_id": variant, "hook_label": label, "render_id": render,
                "publish_job_id": job, "published_at": now - timedelta(days=5), "views": views, "likes": 0,
                "title": "t", "external_url": "", "thumbnail_url": None, "age_days": 5, "cost_usd": 0.0,
                "cost_is_estimate": True, "project_id": 1, "project_title": "p", "project_url": "/p"}
    rows = [row(1, 10, "A", 100), row(2, 11, "B", 50, variant=7), row(3, 11, "B", 80, variant=7)]
    db = _db()
    try:
        (t,) = publishing.hook_tests(db, rows, now=now)
    finally:
        db.close()
    labels = [e["hook_label"] for e in t["entries"]]
    assert labels == ["A", "B"]
    b = t["entries"][1]
    assert b["publish_job_id"] == 3 and b["uploads"] == 2


# ---------- review fixes: music, lost Make jobs, Listen spend ----------


def _sha(path) -> str:
    import hashlib

    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_an_opening_keeps_the_music_its_base_was_made_with(auth_client, tmp_path):
    """New music after the base was made replaces music.<ext> in place: the
    base keeps its own copy, and an opening of it is made with that one."""
    from app import models
    from app.services import hook_lab

    pid = _rendered(auth_client)
    first = _tone(tmp_path / "first.m4a", 6.0)
    with first.open("rb") as f:
        r = auth_client.post(f"/api/projects/{pid}/music", files={"file": ("m.m4a", f, "audio/mp4")})
    assert r.status_code == 200, r.text
    assert auth_client.post(f"/api/projects/{pid}/generate-video", params={"force": "true"}).status_code == 202
    base = _renders(pid)[-1]
    assert base.status == "completed"
    original = _sha(first)
    look = base.composition_json["settings"]
    assert look["music_path"] and _sha(look["music_path"]) == original
    # Music made again: the same file name, other sound.
    second = _voice(tmp_path / "second.m4a", [("tone", 2.0), ("gap", 1.0), ("tone", 3.0)])
    with second.open("rb") as f:
        r = auth_client.post(f"/api/projects/{pid}/music", files={"file": ("m.m4a", f, "audio/mp4")})
    assert r.status_code == 200, r.text
    assert _sha(_get(models.VideoProject, pid).music_upload_path) != original
    made = _create(auth_client, pid, base.id, [{"hook_text": "Why stop now?", "style": "question"}])
    assert _make(auth_client, pid, [made["variants"][0]["id"]]).status_code == 202
    vr = _renders(pid)[-1]
    assert vr.hook_variant_id and vr.status == "completed", vr.error
    assert _sha(vr.composition_json["settings"]["music_path"]) == original
    # The base's own copy changed after all: refused before anything is paid.
    Path(look["music_path"]).write_bytes(second.read_bytes())
    db = _db()
    try:
        with pytest.raises(hook_lab.HookLabError, match="music was replaced"):
            hook_lab.base_settings(db, db.get(models.Render, base.id))
    finally:
        db.close()


def test_an_unrecorded_base_refuses_music_changed_after_it(auth_client, tmp_path):
    """A render from before its look was recorded is composed with the
    project's current music: refused when that file was replaced after
    the render, or is gone."""
    from app import models
    from app.services import hook_lab

    pid = _rendered(auth_client)
    first = _tone(tmp_path / "first.m4a", 6.0)
    with first.open("rb") as f:
        r = auth_client.post(f"/api/projects/{pid}/music", files={"file": ("m.m4a", f, "audio/mp4")})
    assert r.status_code == 200, r.text
    assert auth_client.post(f"/api/projects/{pid}/generate-video", params={"force": "true"}).status_code == 202
    base_id = _renders(pid)[-1].id
    db = _db()
    try:
        db.query(models.Render).filter_by(id=base_id).update(
            {"composition_json": None, "created_at": datetime.utcnow() + timedelta(seconds=5)},
            synchronize_session=False)
        db.commit()
        # The music it was made with, unchanged: its look is the project's.
        look = hook_lab.base_settings(db, db.get(models.Render, base_id))
        assert look["music_path"] == _get(models.VideoProject, pid).music_upload_path
        # Music uploaded again after the render: same file name, other sound.
        db.query(models.Render).filter_by(id=base_id).update(
            {"created_at": datetime.utcnow() - timedelta(minutes=1)}, synchronize_session=False)
        db.commit()
        second = _voice(tmp_path / "second.m4a", [("tone", 2.0), ("gap", 1.0), ("tone", 3.0)])
        with second.open("rb") as f:
            r = auth_client.post(f"/api/projects/{pid}/music", files={"file": ("m.m4a", f, "audio/mp4")})
        assert r.status_code == 200, r.text
        db.expire_all()
        with pytest.raises(hook_lab.HookLabError, match="music was replaced"):
            hook_lab.base_settings(db, db.get(models.Render, base_id))
        # The music file is gone although the project still names it.
        Path(_get(models.VideoProject, pid).music_upload_path).unlink()
        with pytest.raises(hook_lab.HookLabError, match="music this version was made with is gone"):
            hook_lab.base_settings(db, db.get(models.Render, base_id))
    finally:
        db.close()


def test_openings_a_lost_make_job_never_reached_fail(auth_client):
    """Make claims every opening before queueing its job; one the job never
    reached (lost queue entry, worker died mid-batch) has no render. Once
    the project is no longer held for that job, the sweeper fails it."""
    from app import models
    from app.services import hook_lab

    pid, base_id = _base(auth_client)
    made = _create(auth_client, pid, base_id, [{"hook_text": "Why stop now?", "style": "question"},
                                               {"hook_text": "What if you stopped?", "style": "question"}])
    a, b = (v["id"] for v in made["variants"])
    old = datetime.utcnow() - timedelta(minutes=10)
    db = _db()
    try:
        for vid in (a, b):
            db.query(models.HookVariant).filter_by(id=vid).update(
                {"status": "making", "render_id": None, "updated_at": old}, synchronize_session=False)
        project = db.get(models.VideoProject, pid)
        project.status = "generating"  # the job still holds it (queued, or running)
        db.commit()
        hook_lab.sweep(db)
        assert {db.get(models.HookVariant, v).status for v in (a, b)} == {"making"}
        project = db.get(models.VideoProject, pid)
        project.status = "failed"  # the render sweeper gave up on the lost job
        db.commit()
        db.query(models.HookVariant).filter(models.HookVariant.id.in_((a, b))).update(
            {"updated_at": old}, synchronize_session=False)
        db.commit()
        out = hook_lab.sweep(db)
        assert out["failed"] == 2
        for vid in (a, b):
            v = db.get(models.HookVariant, vid)
            db.refresh(v)
            assert v.status == "failed" and "make it again" in v.error
        assert db.query(models.Event).filter_by(entity_type="hook_variant", entity_id=a, status="failed").count() == 1
    finally:
        db.close()
    # Editable again.
    r = auth_client.patch(f"/api/hook-variants/{a}", json={"hook_text": "Why not stop?"})
    assert r.status_code == 200, r.text


def test_the_voice_listen_paid_for_is_charged_to_the_opening(auth_client, monkeypatch):
    """Listen pays for the opening's voice with only the project to charge
    it to; Make reuses it. The opening's cost includes it, the base video's
    cost in Results doesn't."""
    from app import models, providers
    from app.services import publishing, spend

    real = providers.get_tts()

    class PaidTTS:
        def __getattr__(self, name):
            return getattr(real, name)

        def synthesize(self, **kw):
            spend.record_rate_card("elevenlabs", "tts", "voice", 0.05)
            return real.synthesize(**kw)

    monkeypatch.setattr(providers, "get_tts", lambda: PaidTTS())
    pid, base_id = _base(auth_client)
    made = _create(auth_client, pid, base_id, [{"hook_text": "Why stop now?", "style": "question"}])
    vid = made["variants"][0]["id"]
    from app.services import hook_lab

    db = _db()
    try:  # not left over from another test's project with the same ids
        hook_lab.raw_hook_path(db, db.get(models.VideoProject, pid), db.get(models.HookVariant, vid)).unlink(
            missing_ok=True)
    finally:
        db.close()
    r = auth_client.post(f"/api/hook-variants/{vid}/preview-audio")
    assert r.status_code == 200 and r.json()["fits"] and r.json()["cost"] > 0, r.text
    assert _make(auth_client, pid, [vid]).status_code == 202
    vr = _renders(pid)[-1]
    assert vr.hook_variant_id == vid and vr.status == "completed", vr.error
    db = _db()
    try:
        entries = db.query(models.SpendEntry).filter_by(project_id=pid, kind="tts").all()
        assert len(entries) == 1  # paid once, by Listen
        assert entries[0].render_id == vr.id
        assert publishing.variant_render_costs(db, [pid])[vr.id][1] == 0.05
        assert db.get(models.Render, vr.id).actual_cost == 0.05
        # The base video's cost in Results: the project's spend less its openings'.
        total, _ = publishing.project_costs(db, [pid])[pid]
        assert total == 0.05  # the mock providers record nothing else
    finally:
        db.close()
    # What Results shows for the base video: 0 (the voice is the opening's).
    db = _db()
    try:
        others = sum(c for p, c, _ in publishing.variant_render_costs(db, [pid]).values() if p == pid)
        assert round(total - others, 4) == 0.0
    finally:
        db.close()


# ---------- an opening inside the script it joins ----------

_ADULT_SCRIPT = "Stop scrolling now! Watch her strip naked and have sex on camera. And one more line."
_MINOR_HOOK = "She just turned 15."


def _adult_base(auth_client) -> tuple[int, int]:
    """A base whose script is explicit (adults only): fine on its own, and
    an opening that only states an age passes alone too."""
    from app import models
    from app.services import safety

    pid, base_id = _base(auth_client)
    db = _db()
    try:
        r = db.get(models.Render, base_id)
        r.composition_json = {**(r.composition_json or {}), "script": _ADULT_SCRIPT}
        db.commit()
    finally:
        db.close()
    safety.check_script(_MINOR_HOOK)  # passes alone: the gap these tests close
    with pytest.raises(safety.UnsafeScriptError):
        safety.check_script(f"{_MINOR_HOOK} Watch her strip naked and have sex on camera. And one more line.")
    return pid, base_id


def _set_hook(vid: int, text: str) -> None:
    from app import models

    db = _db()
    try:
        db.get(models.HookVariant, vid).hook_text = text
        db.commit()
    finally:
        db.close()


def test_an_opening_is_checked_inside_the_script_it_joins(auth_client):
    from app import models

    pid, base_id = _adult_base(auth_client)
    url = f"/api/projects/{pid}/hook-lab/variants"
    r = auth_client.post(url, json={"base_render_id": base_id, "window_seconds": 2.5,
                                    "variants": [{"hook_text": _MINOR_HOOK, "style": "custom"}]})
    assert r.status_code == 422 and "minor" in r.json()["detail"]
    assert not auth_client.get(f"/api/projects/{pid}/hook-lab").json()["bases"][0]["variants"]

    made = _create(auth_client, pid, base_id, [{"hook_text": "Why stop now?", "style": "question"}])
    vid = made["variants"][0]["id"]
    # Changing it to that opening is refused as soon as it is written.
    r = auth_client.patch(f"/api/hook-variants/{vid}", json={"hook_text": _MINOR_HOOK})
    assert r.status_code == 422 and "minor" in r.json()["detail"]
    assert _get(models.HookVariant, vid).hook_text == "Why stop now?"

    # Got in anyway (written before this check): Listen makes no voice ...
    _set_hook(vid, _MINOR_HOOK)
    r = auth_client.post(f"/api/hook-variants/{vid}/preview-audio")
    assert r.status_code == 422 and "minor" in r.json()["detail"]
    v = _get(models.HookVariant, vid)
    assert not v.audio_path and v.voicing_until is None
    db = _db()
    try:
        from app.services import hook_lab

        assert not hook_lab.raw_hook_path(db, db.get(models.VideoProject, pid), v).is_file()
    finally:
        db.close()
    # ... and Make fails it before anything is paid.
    assert _make(auth_client, pid, [vid]).status_code == 202
    v = _get(models.HookVariant, vid)
    assert v.status == "failed" and "minor" in v.error and not v.audio_path
    assert not _jobs(pid, "hook_shot") and not _jobs(pid, "hook_avatar")


def test_compose_checks_the_opening_inside_the_script_again(auth_client, tmp_path):
    """The script can change after Make (an older base takes the project's
    current one): the captions and the stored script are checked as composed."""
    from app import models
    from app.services import hook_lab

    pid, base_id = _adult_base(auth_client)
    vid, rid = _waiting_variant(pid, base_id, [], tmp_path)
    _set_hook(vid, _MINOR_HOOK)
    db = _db()
    try:
        db.get(models.HookVariant, vid).status = "ready"
        db.commit()
        assert hook_lab.compose_variant(db, vid) is None
    finally:
        db.close()
    v = _get(models.HookVariant, vid)
    assert v.status == "failed" and "minor" in v.error
    assert _get(models.Render, rid).status == "failed"


def test_suggested_openings_meet_the_minors_check(auth_client, monkeypatch):
    import json

    from app.providers import mock_chat

    pid, base_id = _adult_base(auth_client)
    url = f"/api/projects/{pid}/hook-lab/suggest"

    def ideas(*texts):
        reply = json.dumps({"hooks": [{"style": s, "text": t}
                                      for s, t in zip(("question", "bold_claim", "stat"), texts, strict=False)]})
        monkeypatch.setattr(mock_chat.MockChatProvider, "hook_ideas", lambda self, **kw: reply)

    # Refused alone, and refused only inside the script: both dropped.
    ideas("Sexy 14-year-old here?", _MINOR_HOOK, "Honestly, stop scrolling.")
    r = auth_client.post(url, json={"base_render_id": base_id, "n": 3})
    assert r.status_code == 200, r.text
    assert [i["text"] for i in r.json()["suggestions"]] == ["Honestly, stop scrolling."]
    # None left: a 422 that says why, not "try again".
    ideas("Sexy 14-year-old here?", _MINOR_HOOK)
    r = auth_client.post(url, json={"base_render_id": base_id, "n": 3})
    assert r.status_code == 422 and "safety check" in r.json()["detail"] and "Sexy" not in r.text

    # The planner's sentinels are refusals, not unusable answers.
    monkeypatch.setattr(mock_chat.MockChatProvider, "hook_ideas", lambda self, **kw: "REFUSED_MINOR")
    r = auth_client.post(url, json={"base_render_id": base_id})
    assert r.status_code == 422 and "minor" in r.json()["detail"]
    monkeypatch.setattr(mock_chat.MockChatProvider, "hook_ideas", lambda self, **kw: "DECLINED")
    r = auth_client.post(url, json={"base_render_id": base_id})
    assert r.status_code == 422 and "declined" in r.json()["detail"]


def test_the_hook_ideas_instructions_keep_minors_out():
    from app.providers.openrouter_chat import HOOK_IDEAS_SYSTEM

    assert "REFUSED_MINOR" in HOOK_IDEAS_SYSTEM and "DECLINED" in HOOK_IDEAS_SYSTEM
    assert "under 18" in HOOK_IDEAS_SYSTEM


# ---------- the base's script and voice ----------


def _edit(pid: int, **fields) -> None:
    """Change the project's script or voice directly (as the editor would)."""
    from app import models

    db = _db()
    try:
        p = db.get(models.VideoProject, pid)
        if "script" in fields:
            p.generated_plan_json = {**p.generated_plan_json, "cleaned_voice_script": fields.pop("script")}
        for k, v in fields.items():
            setattr(p, k, v)
        db.commit()
    finally:
        db.close()


def test_an_unrecorded_base_needs_the_script_and_voice_it_was_made_with(auth_client):
    """A render from before its script was recorded takes its script and
    opening from the project's plan: refused once that script or the voice
    changed, as the captions and the cut would no longer match its audio."""
    from app import models
    from app.services import hook_lab

    pid, base_id = _base(auth_client)
    db = _db()
    try:
        db.get(models.Render, base_id).composition_json = None
        db.commit()
        hook_lab.check_base(db, db.get(models.Render, base_id), pid)  # unchanged: fine
    finally:
        db.close()
    script = _get(models.VideoProject, pid).generated_plan_json["cleaned_voice_script"]
    _edit(pid, script="A whole new opening line. Here is the rest of it. And one more line.")
    db = _db()
    try:
        with pytest.raises(hook_lab.HookLabError, match="Re-compose this version first"):
            hook_lab.check_base(db, db.get(models.Render, base_id), pid)
    finally:
        db.close()
    r = auth_client.post(f"/api/projects/{pid}/hook-lab/variants",
                         json={"base_render_id": base_id, "window_seconds": 2.5,
                               "variants": [{"hook_text": "Why stop now?", "style": "question"}]})
    assert r.status_code == 409 and "Re-compose" in r.text, r.text
    _edit(pid, script=script)  # back to what it said
    _edit(pid, voice_id="another-voice")
    db = _db()
    try:
        with pytest.raises(hook_lab.HookLabError, match="Re-compose this version first"):
            hook_lab.check_base(db, db.get(models.Render, base_id), pid)
    finally:
        db.close()


def test_the_opening_is_refused_when_the_voice_changed(auth_client):
    """The opening is spoken with the project's current voice and joined onto
    the base voiceover: the base records its voice, and a change refuses it
    (a script edit doesn't: the base records its script too)."""
    from app import models
    from app.services import hook_lab

    pid, base_id = _base(auth_client)
    voice = _get(models.Render, base_id).composition_json["voice"]
    assert voice["source"] == "tts" and "voice_id" in voice and "model" in voice
    _edit(pid, script="A whole new opening line. Here is the rest of it. And one more line.")
    db = _db()
    try:
        assert hook_lab.check_base(db, db.get(models.Render, base_id), pid)
    finally:
        db.close()
    _edit(pid, voice_id="another-voice")
    db = _db()
    try:
        with pytest.raises(hook_lab.HookLabError, match="voice changed since this version was made"):
            hook_lab.check_base(db, db.get(models.Render, base_id), pid)
    finally:
        db.close()
    r = auth_client.post(f"/api/projects/{pid}/hook-lab/variants",
                         json={"base_render_id": base_id, "window_seconds": 2.5,
                               "variants": [{"hook_text": "Why stop now?", "style": "question"}]})
    assert r.status_code == 409 and "voice changed" in r.text, r.text
    _edit(pid, voice_id="")  # switched back
    made = _create(auth_client, pid, base_id, [{"hook_text": "Why stop now?", "style": "question"}])
    assert _make(auth_client, pid, [made["variants"][0]["id"]]).status_code == 202
    vr = _renders(pid)[-1]
    assert vr.hook_variant_id and vr.status == "completed", vr.error
    assert vr.composition_json["voice"] == voice


def test_an_uploaded_voiceover_is_kept_per_version_and_gets_no_new_opening(auth_client, tmp_path, monkeypatch):
    """An uploaded voiceover keeps its file name when uploaded again: each
    version keeps its own copy (removed with its files), and a new opening
    can't be spoken in an uploaded recording."""
    from app import models
    from app.services import hook_lab, render_files

    pid = _rendered(auth_client)
    first = _voice(tmp_path / "first.m4a", [("tone", 2.0), ("gap", 0.5), ("tone", 3.0)])
    with first.open("rb") as f:
        r = auth_client.post(f"/api/projects/{pid}/voiceover", files={"file": ("v.m4a", f, "audio/mp4")})
    assert r.status_code == 200, r.text
    assert auth_client.post(f"/api/projects/{pid}/generate-video", params={"force": "true"}).status_code == 202
    base = _renders(pid)[-1]
    assert base.status == "completed", base.error
    upload = _get(models.VideoProject, pid).voiceover_upload_path
    assert base.audio_path != upload and Path(base.audio_path).name.startswith(f"render_{base.id}_voice")
    assert base.composition_json["voice"] == {"source": "upload"}
    original = _sha(first)
    assert _sha(base.audio_path) == original
    # Uploaded again: same file name, other sound. The version keeps its own.
    second = _tone(tmp_path / "second.m4a", 4.0)
    with second.open("rb") as f:
        r = auth_client.post(f"/api/projects/{pid}/voiceover", files={"file": ("v.m4a", f, "audio/mp4")})
    assert r.status_code == 200, r.text
    assert _sha(upload) != original and _sha(base.audio_path) == original
    db = _db()
    try:
        with pytest.raises(hook_lab.HookLabError, match="uploaded recording"):
            hook_lab.check_base(db, db.get(models.Render, base.id), pid)
        # A version from before its voice was recorded, made with the upload.
        r0 = db.get(models.Render, base.id)
        r0.composition_json, r0.audio_path = None, upload
        db.commit()
        with pytest.raises(hook_lab.HookLabError, match="uploaded recording"):
            hook_lab.check_base(db, db.get(models.Render, base.id), pid)
    finally:
        db.close()
    # A later version expires the earlier ones: their copies go with them.
    assert auth_client.post(f"/api/projects/{pid}/generate-video", params={"force": "true"}).status_code == 202
    latest = _renders(pid)[-1]
    copy = Path(latest.audio_path)
    assert copy.is_file() and copy.name.startswith(f"render_{latest.id}_voice")
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "render_retention_keep", 1)
    assert auth_client.post(f"/api/projects/{pid}/generate-video", params={"force": "true"}).status_code == 202
    db = _db()
    try:
        render_files.prune(db, pid)
    finally:
        db.close()
    assert _get(models.Render, latest.id).status == "expired"
    assert not copy.exists()
    assert Path(_renders(pid)[-1].audio_path).is_file()


def test_a_recompose_after_a_new_upload_plays_the_new_recording(auth_client, tmp_path):
    """Each version keeps its own copy of an uploaded voiceover, so a
    recompose must look at the upload itself: after a new recording is
    uploaded, the next recompose plays it, not the last version's copy."""
    from app import models
    from app.services import fingerprints, pipeline

    pid = _rendered(auth_client)
    first = _voice(tmp_path / "first.m4a", [("tone", 2.0), ("gap", 0.5), ("tone", 3.0)])
    with first.open("rb") as f:
        r = auth_client.post(f"/api/projects/{pid}/voiceover", files={"file": ("v.m4a", f, "audio/mp4")})
    assert r.status_code == 200, r.text
    assert auth_client.post(f"/api/projects/{pid}/generate-video", params={"force": "true"}).status_code == 202
    base = _renders(pid)[-1]
    assert base.status == "completed", base.error
    db = _db()
    try:
        # Nothing new uploaded: the recompose keeps the same recording.
        same = pipeline.recompose_project(db, pid)
        assert same.status == "completed", same.error
        assert _sha(same.audio_path) == _sha(first)
        assert same.voice_fingerprint == base.voice_fingerprint
    finally:
        db.close()
    second = _tone(tmp_path / "second.m4a", 4.0)
    with second.open("rb") as f:
        r = auth_client.post(f"/api/projects/{pid}/voiceover", files={"file": ("v.m4a", f, "audio/mp4")})
    assert r.status_code == 200, r.text
    db = _db()
    try:
        again = pipeline.recompose_project(db, pid)
        assert again.status == "completed", again.error
        assert Path(again.audio_path).name.startswith(f"render_{again.id}_voice")
        assert _sha(again.audio_path) == _sha(second)
        project = db.get(models.VideoProject, pid)
        assert again.voice_fingerprint == fingerprints.voice_fingerprint(db, project)
        # The older versions still play what they were made with.
        assert _sha(base.audio_path) == _sha(first)
    finally:
        db.close()


def test_make_again_reuses_the_clips_a_failed_try_finished(auth_client, monkeypatch):
    """A new shot 1 finished, then the lip-sync of shot 2 failed: Make again
    takes the finished shot 1 (same opening, same inputs, file still there)
    instead of buying it again, and leaves it out of the price."""
    from app import models
    from app.providers.mock_avatar import MockAvatarProvider

    pid, base_id = _talking_base(auth_client, extra_shot=True)  # shot 1: 3 s, shot 2: 4 s, both lip-synced
    made = _create(auth_client, pid, base_id, [
        {"hook_text": "Hi, quick test here.", "style": "custom", "shot1_source": "new",
         "shot1_prompt": "a red car"},
        {"hook_text": "Hello, short test here.", "style": "custom", "shot1_source": "new",
         "shot1_prompt": "a red car"},
    ], window=3.5)
    first = made["plan"]["variants"][0]
    assert first["shot1"]["cost"] > 0 and not first["shot1"]["reused"] and first["reused"] == {}
    vid, other = (v["id"] for v in made["variants"])

    real = MockAvatarProvider.submit_avatar

    def refuse(self, **kw):
        raise RuntimeError("lip-sync provider is down")

    monkeypatch.setattr(MockAvatarProvider, "submit_avatar", refuse)
    assert _make(auth_client, pid, [vid]).status_code == 202
    v = _get(models.HookVariant, vid)
    assert v.status == "failed" and "down" in v.error, (v.status, v.error)
    (shot_job,) = _jobs(pid, "hook_shot")
    assert shot_job.status == "completed" and shot_job.ref_id == vid and shot_job.context_json["inputs"]
    # What the provider charged for shot 1 (the mock providers record nothing): on that try's render.
    first_render = shot_job.render_id
    db = _db()
    try:
        db.add_all([models.SpendEntry(provider="x", kind="video", cost_usd=2.5, project_id=pid,
                                      render_id=first_render, ref=f"video_job:{shot_job.id}"),
                    models.SpendEntry(provider="x", kind="video", cost_usd=0.7, project_id=pid,
                                      render_id=first_render, ref="video_job:999999")])
        db.commit()
    finally:
        db.close()

    # Make again: shot 1 is reused (not priced, not submitted), shot 2 is made.
    monkeypatch.setattr(MockAvatarProvider, "submit_avatar", real)
    priced = auth_client.post(f"/api/projects/{pid}/hook-lab/plan", json={"variant_ids": [vid]}).json()
    row = priced["variants"][0]
    assert row["shot1"]["reused"] and row["shot1"]["cost"] == 0
    assert row["reused"] == {"0": shot_job.id} and row["extra_lipsync"][0]["cost"] > 0
    assert row["total"] == pytest.approx(row["extra_lipsync"][0]["cost"] + row["tts_cost"], abs=1e-4)
    assert _make(auth_client, pid, [vid]).status_code == 202
    v = _get(models.HookVariant, vid)
    assert v.status == "completed", v.error
    assert [j.id for j in _jobs(pid, "hook_shot")] == [shot_job.id]  # not bought again
    assert v.shot1_clip_path == shot_job.result_path and v.clips_json["clips"]["0"] == shot_job.result_path
    render = _get(models.Render, v.render_id)
    assert render.composition_json["shots"][0]["clip_path"] == shot_job.result_path
    assert render.estimated_cost == pytest.approx(row["total"], abs=1e-4)
    # What the opening cost includes the clip it took (and the earlier try's render no longer does).
    assert render.actual_cost == pytest.approx(2.5, abs=1e-4)
    db = _db()
    try:
        assert {e.ref: e.render_id for e in db.query(models.SpendEntry).filter_by(project_id=pid)} == {
            f"video_job:{shot_job.id}": render.id, "video_job:999999": first_render}
    finally:
        db.close()

    # Never another opening's clip, even with the same prompt.
    priced = auth_client.post(f"/api/projects/{pid}/hook-lab/plan", json={"variant_ids": [other]}).json()
    assert not priced["variants"][0]["shot1"]["reused"] and priced["variants"][0]["shot1"]["cost"] > 0

    # A changed prompt, or a clip whose file is gone, is made again.
    db = _db()
    try:
        from app.services import hook_lab

        variant = db.get(models.HookVariant, vid)
        variant.status = "failed"
        db.commit()
        assert hook_lab.parts(db, variant)["shot1"]["reused"]
        variant.shot1_prompt = "a blue car"
        db.commit()
        assert not hook_lab.parts(db, variant)["shot1"]["reused"]
        variant.shot1_prompt = "a red car"
        db.commit()
        Path(shot_job.result_path).unlink()
        assert not hook_lab.parts(db, variant)["shot1"]["reused"]
    finally:
        db.close()


def test_make_again_reuses_a_finished_lip_sync_with_the_same_voice(auth_client, monkeypatch):
    from app import models
    from app.providers.mock_avatar import MockAvatarProvider

    pid, base_id = _talking_base(auth_client, extra_shot=True)
    made = _create(auth_client, pid, base_id, [
        {"hook_text": "Hi, quick test here.", "style": "custom", "shot1_source": "lipsync"}], window=3.5)
    vid = made["variants"][0]["id"]
    real = MockAvatarProvider.submit_avatar
    calls = []

    def second_fails(self, **kw):
        calls.append(kw)
        if len(calls) == 2:
            raise RuntimeError("lip-sync provider is down")
        return real(self, **kw)

    monkeypatch.setattr(MockAvatarProvider, "submit_avatar", second_fails)
    assert _make(auth_client, pid, [vid]).status_code == 202
    assert _get(models.HookVariant, vid).status == "failed"
    done = [j for j in _jobs(pid, "hook_avatar") if j.status == "completed"]
    assert [j.context_json["index"] for j in done] == [0]
    monkeypatch.setattr(MockAvatarProvider, "submit_avatar", real)
    row = auth_client.post(f"/api/projects/{pid}/hook-lab/plan", json={"variant_ids": [vid]}).json()["variants"][0]
    assert row["shot1"]["reused"] and row["shot1"]["cost"] == 0 and row["extra_lipsync"][0]["cost"] > 0
    assert _make(auth_client, pid, [vid]).status_code == 202
    v = _get(models.HookVariant, vid)
    assert v.status == "completed", v.error
    jobs = _jobs(pid, "hook_avatar")
    assert sorted(j.context_json["index"] for j in jobs if j.status == "completed") == [0, 1]
    assert v.clips_json["clips"]["0"] == done[0].result_path
    # Another opening text means another voice: shot 1's lip-sync is made again.
    db = _db()
    try:
        from app.services import hook_lab

        variant = db.get(models.HookVariant, vid)
        variant.status = "failed"
        db.commit()
        assert hook_lab.parts(db, variant)["shot1"]["reused"]
        variant.hook_text = "Hey, a quick test now."
        db.commit()
        p = hook_lab.parts(db, variant)
        assert not p["shot1"]["reused"] and p["shot1"]["cost"] > 0 and p["reused"] == {}
    finally:
        db.close()


def test_make_again_waits_for_a_clip_an_earlier_try_is_still_making(auth_client, monkeypatch):
    """A new shot 1 is still at the provider when the lip-sync of shot 2
    fails: Make again doesn't buy shot 1 a second time. It waits for the
    clip already asked for (the sweeper doesn't fail it meanwhile) and is
    put together with it once it is back. Never another opening's clip."""
    from datetime import datetime, timedelta

    from app import models
    from app.providers.mock_avatar import MockAvatarProvider
    from app.services import hook_lab, video_jobs

    pid, base_id = _talking_base(auth_client, extra_shot=True)
    made = _create(auth_client, pid, base_id, [
        {"hook_text": "Hi, quick test here.", "style": "custom", "shot1_source": "new",
         "shot1_prompt": "a red car"},
        {"hook_text": "Hello, short test here.", "style": "custom", "shot1_source": "new",
         "shot1_prompt": "a red car"},
    ], window=3.5)
    vid, other = (v["id"] for v in made["variants"])
    real_avatar, real_check = MockAvatarProvider.submit_avatar, video_jobs.check

    def refuse(self, **kw):
        raise RuntimeError("lip-sync provider is down")

    def slow_shots(db, job_id):  # a new shot 1 stays at the provider; the rest finish at once
        job = db.get(models.VideoJob, job_id)
        return job.status if job.purpose == "hook_shot" else real_check(db, job_id)

    monkeypatch.setattr(MockAvatarProvider, "submit_avatar", refuse)
    monkeypatch.setattr(video_jobs, "check", slow_shots)
    assert _make(auth_client, pid, [vid]).status_code == 202
    v = _get(models.HookVariant, vid)
    assert v.status == "failed" and "down" in v.error, (v.status, v.error)
    (shot_job,) = _jobs(pid, "hook_shot")
    assert shot_job.status == "submitted" and shot_job.ref_id == vid

    monkeypatch.setattr(MockAvatarProvider, "submit_avatar", real_avatar)
    priced = auth_client.post(f"/api/projects/{pid}/hook-lab/plan", json={"variant_ids": [vid]}).json()
    row = priced["variants"][0]
    assert row["shot1"]["reused"] and row["shot1"]["cost"] == 0 and row["reused"] == {"0": shot_job.id}
    assert any("still being made" in n for n in row["notes"]), row["notes"]
    # Another opening never takes it, even with the same prompt.
    priced = auth_client.post(f"/api/projects/{pid}/hook-lab/plan", json={"variant_ids": [other]}).json()
    assert not priced["variants"][0]["shot1"]["reused"] and priced["variants"][0]["shot1"]["cost"] > 0

    assert _make(auth_client, pid, [vid]).status_code == 202
    assert [j.id for j in _jobs(pid, "hook_shot")] == [shot_job.id]  # not bought again
    v = _get(models.HookVariant, vid)
    assert v.status == "making" and v.clips_json["reused"] == {"0": shot_job.id}, (v.status, v.error)
    db = _db()
    try:
        # Its cost is recorded when it comes back, on the earlier try's render (the mock records nothing).
        db.add(models.SpendEntry(provider="x", kind="video", cost_usd=2.5, project_id=pid,
                                 render_id=shot_job.render_id, ref=f"video_job:{shot_job.id}"))
        db.commit()
        # Its own clips are in; the one it took is still being made: it waits.
        hook_lab.sweep(db, now=datetime.utcnow() + timedelta(hours=1))
        assert db.get(models.HookVariant, vid).status == "making"
        # The earlier try's shot 1 comes back: this round is put together with it.
        assert real_check(db, shot_job.id) == "completed"
    finally:
        db.close()
    v = _get(models.HookVariant, vid)
    done = _get(models.VideoJob, shot_job.id)
    assert v.status == "completed", v.error
    assert v.shot1_clip_path == done.result_path and v.clips_json["clips"]["0"] == done.result_path
    assert _get(models.Render, v.render_id).composition_json["shots"][0]["clip_path"] == done.result_path
    # The opening's cost includes the clip it took.
    assert _get(models.Render, v.render_id).actual_cost == pytest.approx(2.5, abs=1e-4)


def test_a_clip_an_earlier_round_took_doesnt_reach_the_next_make_before_it_starts(auth_client, monkeypatch):
    """Make claims an opening (making, no render yet) and may spend minutes
    on the openings before it: a clip the previous round took (clips_json
    "reused", not yet replaced) that fails or finishes meanwhile is nothing
    to the new round, which may not use it."""
    from app import models
    from app.providers.mock_avatar import MockAvatarProvider
    from app.services import hook_lab, video_jobs

    pid, base_id = _talking_base(auth_client, extra_shot=True)
    (vid,) = (v["id"] for v in _create(auth_client, pid, base_id, [
        {"hook_text": "Hi, quick test here.", "style": "custom", "shot1_source": "new",
         "shot1_prompt": "a red car"},
    ], window=3.5)["variants"])
    real_check = video_jobs.check

    def refuse(self, **kw):
        raise RuntimeError("lip-sync provider is down")

    def slow_shots(db, job_id):  # a new shot 1 stays at the provider; the rest finish at once
        job = db.get(models.VideoJob, job_id)
        return job.status if job.purpose == "hook_shot" else real_check(db, job_id)

    monkeypatch.setattr(MockAvatarProvider, "submit_avatar", refuse)
    monkeypatch.setattr(video_jobs, "check", slow_shots)
    assert _make(auth_client, pid, [vid]).status_code == 202
    assert _make(auth_client, pid, [vid]).status_code == 202  # takes the clip, then fails again
    v = _get(models.HookVariant, vid)
    (shot_job,) = _jobs(pid, "hook_shot")
    assert v.status == "failed" and v.clips_json["reused"] == {"0": shot_job.id}, (v.status, v.error)
    db = _db()
    try:
        # What Make's claim leaves, while the worker is still on other openings.
        db.query(models.HookVariant).filter(models.HookVariant.id == vid).update(
            {"status": "making", "error": "", "render_id": None}, synchronize_session=False)
        job = db.get(models.VideoJob, shot_job.id)
        job.status, job.error = "failed", "the provider refused it"
        db.commit()
        assert hook_lab._job_variant(db, job) is None
        hook_lab.on_clip_failed(db, job)
        v = db.get(models.HookVariant, vid)
        db.refresh(v)
        assert v.status == "making" and v.error == "", (v.status, v.error)
    finally:
        db.close()


# ---------- the project page while other openings are being made ----------


def _queued_make(auth_client, monkeypatch, text="Why stop now?"):
    from app.api import hook_lab as api

    pid, base_id = _base(auth_client)
    vid = _create(auth_client, pid, base_id, [{"hook_text": text, "style": "question"}])["variants"][0]["id"]
    queued: list = []
    monkeypatch.setattr(api, "enqueue", lambda fn, *args, **kw: queued.append((args, kw)) or "rq-hl")
    assert _make(auth_client, pid, [vid]).status_code == 202
    return pid, vid, queued


def test_the_project_page_says_other_openings_are_being_made_and_cancel_leaves_it(auth_client, monkeypatch):
    """A queued Make holds the project as "generating": the status says the
    hook lab holds it, and the editor's Cancel neither cancels the finished
    video nor gives the openings back."""
    from app import models

    pid, vid, queued = _queued_make(auth_client, monkeypatch)
    st = auth_client.get(f"/api/projects/{pid}/status").json()
    assert st["project_status"] == "generating" and st["hook_lab_making"] is True
    r = auth_client.post(f"/api/projects/{pid}/cancel")
    assert r.status_code == 409 and "hook lab" in r.json()["detail"]
    assert auth_client.get(f"/api/projects/{pid}").json()["status"] == "generating"

    # The job runs, holds the claim (still known as the hook lab's), and
    # gives the project back as it was.
    from app.services import hook_lab as svc

    seen: list = []
    real = svc._make_one

    def spy(db, variant):
        seen.append(auth_client.get(f"/api/projects/{pid}/status").json()["hook_lab_making"])
        assert auth_client.post(f"/api/projects/{pid}/cancel").status_code == 409
        return real(db, variant)

    monkeypatch.setattr(svc, "_make_one", spy)
    from app.workers.jobs import make_hook_variants_job

    make_hook_variants_job(*queued[0][0], **queued[0][1])
    assert seen == [True]
    st = auth_client.get(f"/api/projects/{pid}/status").json()
    assert st["project_status"] == "completed" and st["hook_lab_making"] is False
    assert _get(models.VideoProject, pid).claim_token == ""
    assert _get(models.HookVariant, vid).status == "completed"


def test_the_status_names_the_version_whose_openings_are_being_made(auth_client, monkeypatch):
    """The project page's "See them" opens the hook lab on the version being
    made, not on the newest one (a newer version exists here)."""
    from app import models

    pid, vid, _ = _queued_make(auth_client, monkeypatch)
    base_id = _get(models.HookVariant, vid).base_render_id
    db = _db()
    try:
        db.add(models.Render(project_id=pid, status="completed", final_video_path="/newer.mp4"))
        db.commit()
    finally:
        db.close()
    assert _renders(pid)[-1].id != base_id
    st = auth_client.get(f"/api/projects/{pid}/status").json()
    assert st["hook_lab_making"] is True and st["hook_lab_base_render_id"] == base_id


def test_a_render_claim_still_says_waiting_and_can_be_cancelled(auth_client, monkeypatch):
    """Only the hook lab's claim is flagged; Generate's waiting claim is not."""
    from app.api import generation

    pid, _ = _base(auth_client)
    monkeypatch.setattr(generation, "enqueue", lambda fn, *a, **k: "rq-gen")
    assert auth_client.post(f"/api/projects/{pid}/recompose").status_code in (200, 202)
    st = auth_client.get(f"/api/projects/{pid}/status").json()
    assert st["project_status"] == "generating" and st["hook_lab_making"] is False
    assert auth_client.post(f"/api/projects/{pid}/cancel").json()["action"] == "released"


@pytest.mark.parametrize("taken_over", [False, True])
def test_the_sweeper_gives_a_lost_make_back_to_the_finished_video(auth_client, monkeypatch, taken_over):
    """The Make job's queue entry is gone (never ran, or died before its
    first opening): the project goes back to "completed", not "failed",
    and the openings it never reached go back to drafts."""
    from sqlalchemy import update

    from app import models
    from app.config import get_settings
    from app.services import pipeline, sweepers
    from app.workers import queue

    pid, vid, queued = _queued_make(auth_client, monkeypatch)
    db = _db()
    try:
        if taken_over:
            assert pipeline.consume_claim(db, pid, "generating", queued[0][1]["claim_token"])
            db.commit()
        monkeypatch.setattr(queue, "queue_entry_state", lambda qid: "gone")
        db.execute(update(models.VideoProject).where(models.VideoProject.id == pid).values(
            updated_at=datetime.utcnow() - timedelta(seconds=get_settings().queue_job_timeout_seconds + 11 * 60)))
        db.commit()
        assert sweepers.sweep_renders(db)["projects"] == 1
    finally:
        db.close()
    p = _get(models.VideoProject, pid)
    assert p.status == "completed" and p.claim_token == ""
    assert _get(models.HookVariant, vid).status == "draft"


def test_a_stuck_opening_render_leaves_the_project_to_the_hook_lab(auth_client, monkeypatch):
    """The job died while an opening's render was stuck: that render fails,
    but the project is not set to "failed"; once the job is gone it goes
    back to "completed"."""
    from sqlalchemy import update

    from app import models
    from app.config import get_settings
    from app.services import pipeline, sweepers
    from app.workers import queue

    pid, vid, queued = _queued_make(auth_client, monkeypatch)
    old = datetime.utcnow() - timedelta(seconds=get_settings().queue_job_timeout_seconds + 11 * 60)
    db = _db()
    try:
        assert pipeline.consume_claim(db, pid, "generating", queued[0][1]["claim_token"])
        render = models.Render(project_id=pid, status="generating_audio", hook_variant_id=vid)
        db.add(render)
        db.flush()
        db.query(models.HookVariant).filter_by(id=vid).update({"render_id": render.id}, synchronize_session=False)
        db.commit()
        db.execute(update(models.Render).where(models.Render.id == render.id).values(updated_at=old))
        db.execute(update(models.VideoProject).where(models.VideoProject.id == pid).values(updated_at=old))
        db.commit()
        monkeypatch.setattr(queue, "queue_entry_state", lambda qid: "gone")
        out = sweepers.sweep_renders(db)
        assert out["failed"] == 1 and out["projects"] == 1
        db.expire_all()
        assert db.get(models.Render, render.id).status == "failed"
    finally:
        db.close()
    assert _get(models.VideoProject, pid).status == "completed"
