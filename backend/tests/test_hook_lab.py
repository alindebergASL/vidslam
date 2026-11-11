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
    r = auth_client.post(f"/api/hook-variants/{vid}/preview-audio")
    assert r.status_code == 200 and r.json()["fits"], r.text
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
