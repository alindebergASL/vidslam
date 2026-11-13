"""Render correctness fixes the hook lab builds on: clips cut to their
shot's length, caption timing hints, one voiceover file per render, a
scratch dir per compose (and no recompose while one runs), what a render
was composed from, one version numbering, publish metadata and Results
facts per render, and the budget cap counting the estimate."""
from __future__ import annotations

import subprocess
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

from app.schemas.storyboard import CaptionChunk

# ---------- helpers ----------


def _db():
    from app.db import SessionLocal

    return SessionLocal()


def _planned(auth_client, *, script="Stop scrolling now! Here is the rest of it. And one more line.",
             target=6, cta="") -> int:
    pid = auth_client.post("/api/projects", json={
        "title": "Fix test", "original_script": script, "target_duration_seconds": target, "cta_text": cta,
    }).json()["id"]
    r = auth_client.post(f"/api/projects/{pid}/generate-plan")
    assert r.status_code == 202, r.text
    return pid


def _rendered(auth_client, **kw) -> int:
    pid = _planned(auth_client, **kw)
    r = auth_client.post(f"/api/projects/{pid}/generate-video", params={"force": "true"})
    assert r.status_code == 202, r.text
    assert auth_client.get(f"/api/projects/{pid}/status").json()["project_status"] == "completed"
    return pid


def _clip(path: Path, seconds: float) -> Path:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    f"color=c=blue:s=360x640:d={seconds}", "-t", str(seconds), "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-r", "30", str(path)], check=True)
    return path


def _duration(path) -> float:
    from app.services.renderer import _probe_duration

    return _probe_duration(Path(path))


def _renders(pid: int) -> list:
    from app import models

    db = _db()
    try:
        return (db.query(models.Render).filter(models.Render.project_id == pid)
                .order_by(models.Render.id).all())
    finally:
        db.close()


# ---------- 1. clips longer than their shot ----------


def test_long_clips_are_cut_to_their_shot_and_short_ones_kept(auth_client, tmp_path):
    from app import models
    from app.services import pipeline

    pid = _rendered(auth_client)
    db = _db()
    try:
        p = db.get(models.VideoProject, pid)
        body = pipeline._body_shots(p)
        assert len(body) == 2
        for s in body:  # a 3 s shot on a 5/10 s model plays 5 s
            s.duration_seconds = 3.0
            s.clip_path = str(_clip(tmp_path / f"long_{s.id}.mp4", 5.0))
        db.commit()
        r = pipeline.recompose_project(db, pid)
        assert r.status == "completed"
        # Body = sum of the shot lengths (no end card without a CTA).
        assert _duration(r.final_video_path) == pytest.approx(6.0, abs=0.15)
        comp = r.composition_json
        assert [sh["seconds"] for sh in comp["shots"]] == [3.0, 3.0]
        assert comp["body_seconds"] == pytest.approx(6.0)
        assert comp["captions"][-1]["end"] == pytest.approx(6.0, abs=0.01)

        # A clip shorter than its shot plays as it is (still long enough
        # for the mock voiceover, which a shorter video would play on for).
        p = db.get(models.VideoProject, pid)
        body = pipeline._body_shots(p)
        body[1].clip_path = str(_clip(tmp_path / "short.mp4", 2.5))
        db.commit()
        r2 = pipeline.recompose_project(db, pid)
        assert _duration(r2.final_video_path) == pytest.approx(5.5, abs=0.15)
        assert [sh["seconds"] for sh in r2.composition_json["shots"]] == [3.0, pytest.approx(2.5, abs=0.05)]
    finally:
        db.close()


def test_a_voiceover_longer_than_the_clips_is_never_cut_off(auth_client, tmp_path, monkeypatch):
    """Two 5 s shots and a 9.5 s voiceover: the last clip plays on (held on
    its last frame) until the voice has finished, instead of the video
    ending at 10 s - 2 s of trimming and losing the last words."""
    from app import models
    from app.services import pipeline

    pid = _rendered(auth_client)
    voice = tmp_path / "long_voice.m4a"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=f=440:d=9.5",
                    "-c:a", "aac", str(voice)], check=True)
    monkeypatch.setattr(pipeline, "_recompose_voiceover", lambda db, p: (voice, ""))
    db = _db()
    try:
        p = db.get(models.VideoProject, pid)
        for s in pipeline._body_shots(p):  # 4 s shots, 5 s clips: 8 s of picture
            s.duration_seconds = 4.0
            s.clip_path = str(_clip(tmp_path / f"c_{s.id}.mp4", 5.0))
        db.commit()
        r = pipeline.recompose_project(db, pid)
        assert r.status == "completed"
        assert _duration(r.final_video_path) >= 9.45
        assert r.composition_json["body_seconds"] >= 9.45
        # The held clip records its shot's own length apart from the hold.
        last = r.composition_json["shots"][-1]
        assert last["planned_seconds"] == pytest.approx(4.0)
        assert last["held_seconds"] == pytest.approx(last["seconds"], abs=0.01) and last["seconds"] > 5.0
        # ...and how long it played before the hold (the hold's cap counts from it).
        assert last["own_seconds"] == pytest.approx(4.0, abs=0.01)
        assert r.composition_json["shots"][0]["held_seconds"] is None
        assert r.composition_json["shots"][0]["own_seconds"] is None
    finally:
        db.close()


def test_the_hold_for_a_long_voiceover_is_capped(auth_client, tmp_path, monkeypatch):
    """A voiceover far longer than the clips (8 s of picture, 20 s of voice)
    holds the last clip for VOICE_HOLD_MAX_SECONDS at most, not 12 s of a
    frozen frame: the rest of the voice is cut at the end, as the fit says."""
    from app import models
    from app.services import pipeline

    pid = _rendered(auth_client)
    voice = tmp_path / "very_long_voice.m4a"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=f=440:d=20",
                    "-c:a", "aac", str(voice)], check=True)
    monkeypatch.setattr(pipeline, "_recompose_voiceover", lambda db, p: (voice, ""))
    db = _db()
    try:
        p = db.get(models.VideoProject, pid)
        for s in pipeline._body_shots(p):  # 4 s shots, 5 s clips: 8 s of picture
            s.duration_seconds = 4.0
            s.clip_path = str(_clip(tmp_path / f"c_{s.id}.mp4", 5.0))
        db.commit()
        r = pipeline.recompose_project(db, pid)
        assert r.status == "completed"
        cap = pipeline.VOICE_HOLD_MAX_SECONDS
        assert r.composition_json["body_seconds"] == pytest.approx(8.0 + cap, abs=0.05)
        assert _duration(r.final_video_path) == pytest.approx(8.0 + cap, abs=0.2)
        assert r.composition_json["shots"][-1]["held_seconds"] == pytest.approx(4.0 + cap, abs=0.05)
        # The render records how much of the voice was cut (20 s of voice, 12 s of video).
        assert r.composition_json["voice_cut_seconds"] == pytest.approx(20.0 - (8.0 + cap), abs=0.15)
    finally:
        db.close()


def test_a_long_uploaded_voiceover_warns_and_the_render_records_the_cut(auth_client, tmp_path):
    """An uploaded voiceover far longer than the storyboard: the editor can
    read its fit (how much is cut) before rendering, and the finished render
    records the cut seconds so the render page can say so."""
    from app import models
    from app.services import pipeline

    pid = _planned(auth_client)
    voice = tmp_path / "upload.m4a"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=f=440:d=30",
                    "-c:a", "aac", str(voice)], check=True)
    r = auth_client.post(f"/api/projects/{pid}/voiceover",
                         files={"file": ("v.m4a", voice.read_bytes(), "audio/mp4")})
    assert r.status_code == 200, r.text
    db = _db()
    try:
        video = pipeline._body_seconds(db.get(models.VideoProject, pid))
    finally:
        db.close()
    cap = pipeline.VOICE_HOLD_MAX_SECONDS
    fit = auth_client.get(f"/api/projects/{pid}/voiceover/fit")
    assert fit.status_code == 200, fit.text
    body = fit.json()
    assert body["fit"] == "too_long" and "of the voice are cut" in body["fit_text"]
    assert body["cut_seconds"] == pytest.approx(30.0 - (video + cap), abs=0.15)
    r = auth_client.post(f"/api/projects/{pid}/generate-video", params={"force": "true"})
    assert r.status_code == 202, r.text
    assert auth_client.get(f"/api/projects/{pid}/status").json()["project_status"] == "completed"
    newest = auth_client.get(f"/api/projects/{pid}/renders").json()[0]
    comp = newest["composition_json"]
    assert comp["voice_cut_seconds"] > 10.0
    assert comp["voice_cut_seconds"] == pytest.approx(30.0 - comp["body_seconds"], abs=0.2)
    # No upload: nothing to read.
    other = _planned(auth_client)
    assert auth_client.get(f"/api/projects/{other}/voiceover/fit").status_code == 404


def test_a_voiceover_that_fits_records_no_cut(auth_client):
    pid = _rendered(auth_client)
    newest = auth_client.get(f"/api/projects/{pid}/renders").json()[0]
    assert newest["composition_json"]["voice_cut_seconds"] == 0.0


def test_the_fit_counts_the_real_clip_lengths(tmp_path):
    """Clips shorter than their shots make the video shorter: the fit says
    what the render does with those clips (held, then cut), not what the
    planned lengths would give. A shot without a clip yet counts its
    planned length."""
    from types import SimpleNamespace

    from app.services import pipeline

    voice = tmp_path / "voice.m4a"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=f=440:d=16",
                    "-c:a", "aac", str(voice)], check=True)
    paths = [_clip(tmp_path / f"c{i}.mp4", 3.0) for i in range(2)]
    project = SimpleNamespace(cta_text="", shots=[
        SimpleNamespace(shot_type="b_roll", duration_seconds=8.0, clip_path=str(p)) for p in paths])
    fit = pipeline.voiceover_fit(project, 16.0)
    clips = pipeline.cover_voice([pipeline.ComposeClip(path=p, seconds=8.0) for p in paths], voice, 0.0)
    rendered = sum(pipeline.clip_seconds(c) for c in clips)
    assert rendered == pytest.approx(10.0, abs=0.1)
    assert fit["fit"] == "too_long" and "of the voice are cut" in fit["fit_text"]
    assert fit["video_seconds"] == pytest.approx(6.0, abs=0.1)
    assert fit["cut_seconds"] == pytest.approx(pipeline.voice_cut_seconds(clips, voice, 0.0), abs=0.15)
    assert fit["cut_seconds"] == pytest.approx(6.0, abs=0.15)
    # Before the second clip is made, it counts its planned 8 s.
    project.shots[1].clip_path = None
    assert pipeline.voiceover_fit(project, 16.0)["video_seconds"] == pytest.approx(11.0, abs=0.1)


def test_the_fit_counts_a_stale_clip_at_its_planned_length(auth_client):
    """A clip that is out of date (its shot was made longer after it was
    made) is made again at the shot's length by the next render: the fit
    counts the shot's planned length, not the old clip's."""
    from app import models
    from app.services import fingerprints, pipeline

    pid = _rendered(auth_client)
    db = _db()
    try:
        project = db.get(models.VideoProject, pid)
        body = pipeline._body_shots(project)
        last = body[-1]
        assert last.status == "completed" and Path(last.clip_path).is_file()
        last.duration_seconds = float(last.duration_seconds) + 6.0
        db.commit()
        assert fingerprints.shot_is_stale(last)
        others = sum(min(_duration(s.clip_path), float(s.duration_seconds)) for s in body[:-1])
        planned = others + float(last.duration_seconds)
        fit = pipeline.voiceover_fit(project, planned)
        assert fit["video_seconds"] == pytest.approx(planned, abs=0.1)
        assert fit["fit"] == "fits" and fit["cut_seconds"] == 0.0
    finally:
        db.close()


def test_the_fit_says_what_a_recompose_with_a_stale_clip_does(auth_client, tmp_path):
    """Generate video makes an out-of-date clip again at its shot's length,
    but the free Re-compose reuses the old clip: when the two differ, the
    fit says what each does, and the Re-compose numbers match its render."""
    from app import models
    from app.services import fingerprints, pipeline

    pid = _rendered(auth_client)
    db = _db()
    try:
        project = db.get(models.VideoProject, pid)
        body = pipeline._body_shots(project)
        last = body[-1]
        old = _duration(last.clip_path)
        last.duration_seconds = float(last.duration_seconds) + 6.0
        db.commit()
        assert fingerprints.shot_is_stale(last)
        others = sum(min(_duration(s.clip_path), float(s.duration_seconds)) for s in body[:-1])
        planned = others + float(last.duration_seconds)
        voice = tmp_path / "voice.m4a"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"sine=f=440:d={planned}",
                        "-c:a", "aac", str(voice)], check=True)
        r = auth_client.post(f"/api/projects/{pid}/voiceover",
                             files={"file": ("v.m4a", voice.read_bytes(), "audio/mp4")})
        assert r.status_code == 200, r.text
        fit = auth_client.get(f"/api/projects/{pid}/voiceover/fit").json()
        assert fit["fit"] == "fits" and fit["cut_seconds"] == 0.0
        cap = pipeline.VOICE_HOLD_MAX_SECONDS
        again = others + min(old, planned)
        assert fit["recompose_fit"] == "too_long"
        assert fit["recompose_video_seconds"] == pytest.approx(again, abs=0.1)
        expected_cut = planned - (again + cap)
        assert expected_cut > 1.0
        assert fit["recompose_cut_seconds"] == pytest.approx(expected_cut, abs=0.15)
        assert fit["recompose_note"].startswith("Fits after Generate video; a Re-compose with the current clips cuts")
        # When a Re-compose would play a different recording (an older
        # voiceover the lip-synced clips still follow), nothing is predicted.
        real = pipeline._recompose_voiceover
        pipeline._recompose_voiceover = lambda _db, _p: (voice, "an-older-voiceover")
        try:
            other = auth_client.get(f"/api/projects/{pid}/voiceover/fit").json()
        finally:
            pipeline._recompose_voiceover = real
        assert other["fit"] == "fits" and other["recompose_fit"] is None and other["recompose_note"] == ""
        # The Re-compose itself cuts what the fit said it would.
        db.expire_all()
        render = pipeline.recompose_project(db, pid)
        assert render.status == "completed"
        assert render.composition_json["voice_cut_seconds"] == pytest.approx(
            fit["recompose_cut_seconds"], abs=0.2)
        # Once the clip is up to date again, the two agree and nothing extra is said.
        last = db.get(models.VideoShot, last.id)
        last.duration_seconds = float(last.duration_seconds) - 6.0
        db.commit()
        assert not fingerprints.shot_is_stale(last)
        fresh = pipeline.voiceover_fit(db.get(models.VideoProject, pid), again)
        assert fresh["recompose_fit"] is None and fresh["recompose_note"] == ""
    finally:
        db.close()


def test_no_storyboard_gives_no_fit(auth_client, tmp_path):
    """An uploaded voiceover on a project with no storyboard yet: there are
    no shots to fit, so the fit says so instead of warning that the whole
    voice is cut."""
    from types import SimpleNamespace

    from app.services import pipeline

    pid = auth_client.post("/api/projects", json={
        "title": "No plan", "original_script": "Hello there. One more line.", "target_duration_seconds": 6,
    }).json()["id"]
    voice = tmp_path / "upload.m4a"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=f=440:d=26",
                    "-c:a", "aac", str(voice)], check=True)
    r = auth_client.post(f"/api/projects/{pid}/voiceover",
                         files={"file": ("v.m4a", voice.read_bytes(), "audio/mp4")})
    assert r.status_code == 200, r.text
    fit = auth_client.get(f"/api/projects/{pid}/voiceover/fit")
    assert fit.status_code == 200, fit.text
    body = fit.json()
    assert body["fit"] == "no_plan" and body["cut_seconds"] == 0.0
    assert "cut" not in body["fit_text"] and "storyboard" in body["fit_text"]
    assert body["duration_seconds"] == pytest.approx(26.0, abs=0.2)
    # The spoken-voice preview's fit says the same with no shots.
    same = pipeline.voiceover_fit(SimpleNamespace(cta_text="Go", shots=[]), 26.0)
    assert same["fit"] == "no_plan" and same["cut_seconds"] == 0.0


def test_a_held_clip_is_not_held_again(tmp_path):
    """cover_voice on clips that already hold the last one (a hook-lab
    variant is composed from its base's held clips) holds it from the
    shot's planned length again: never more than VOICE_HOLD_MAX_SECONDS."""
    from app.services import pipeline

    voice = tmp_path / "long_voice.m4a"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=f=440:d=20",
                    "-c:a", "aac", str(voice)], check=True)
    clips = [pipeline.ComposeClip(path=_clip(tmp_path / f"c{i}.mp4", 4.0), seconds=4.0) for i in range(2)]
    cap = pipeline.VOICE_HOLD_MAX_SECONDS
    once = pipeline.cover_voice(clips, voice, 0.0)
    assert once[-1].seconds == pytest.approx(4.0 + cap) and once[-1].planned_seconds == 4.0
    twice = pipeline.cover_voice(once, voice, 0.0)
    assert twice[-1].seconds == pytest.approx(4.0 + cap) and twice[-1].planned_seconds == 4.0
    # A held clip that does not reach the cap yet plays on up to it, no further.
    part = [*clips[:-1], pipeline.ComposeClip(path=clips[-1].path, seconds=5.0, exact=True, planned_seconds=4.0)]
    assert pipeline.cover_voice(part, voice, 0.0)[-1].seconds == pytest.approx(4.0 + cap)
    # A shorter voice: the clip keeps the hold it had (the base's timing).
    short = tmp_path / "short_voice.m4a"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=f=440:d=9.5",
                    "-c:a", "aac", str(short)], check=True)
    assert pipeline.cover_voice(once, short, 0.0)[-1].seconds == pytest.approx(4.0 + cap)


def test_a_held_clip_is_held_from_the_length_it_played(tmp_path):
    """A clip shorter than its shot is held from the clip's own length, so
    a hook-lab variant composed from its base's held clip holds it the same
    way: counted from what the base recorded (or, for an older record,
    from the file cut to the shot's length), never from the shot's planned
    length, which would give the variant a longer ending than its base."""
    from app.services import pipeline

    voice = tmp_path / "long_voice.m4a"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=f=440:d=20",
                    "-c:a", "aac", str(voice)], check=True)
    cap = pipeline.VOICE_HOLD_MAX_SECONDS
    first = pipeline.ComposeClip(path=_clip(tmp_path / "a.mp4", 4.0), seconds=4.0)
    short = _clip(tmp_path / "short.mp4", 2.0)  # a 2 s clip for a 4 s shot
    base = pipeline.cover_voice([first, pipeline.ComposeClip(path=short, seconds=4.0)], voice, 0.0)
    assert base[-1].seconds == pytest.approx(2.0 + cap, abs=0.05)
    assert base[-1].planned_seconds == 4.0 and base[-1].own_seconds == pytest.approx(2.0, abs=0.05)
    # The same clip again (a variant of that base): the same length.
    assert pipeline.cover_voice(base, voice, 0.0)[-1].seconds == pytest.approx(base[-1].seconds, abs=1 / 30)
    # An older record without its own length: counted from the file.
    old = pipeline.ComposeClip(path=short, seconds=2.0 + cap, exact=True, planned_seconds=4.0)
    assert pipeline.cover_voice([first, old], voice, 0.0)[-1].seconds == pytest.approx(2.0 + cap, abs=0.05)
    # The recorded length wins over the file (a variant may play a take there).
    swapped = pipeline.ComposeClip(path=_clip(tmp_path / "take.mp4", 4.0), seconds=2.0 + cap, exact=True,
                                   planned_seconds=4.0, own_seconds=2.0)
    assert pipeline.cover_voice([first, swapped], voice, 0.0)[-1].seconds == pytest.approx(2.0 + cap)


def test_renderer_cuts_only_clips_with_a_limit(tmp_path):
    from app.services.renderer import RenderInputs, compose

    a, b = _clip(tmp_path / "a.mp4", 5.0), _clip(tmp_path / "b.mp4", 4.0)
    out = compose(RenderInputs(project_id=1, out_dir=tmp_path / "o", clip_paths=[a, b], audio_path=None,
                               caption_timings=[], caption_style="clean_white", disclosure_text=None,
                               cta_text=None, clip_seconds=[3.0, None]))
    assert _duration(out.final_video_path) == pytest.approx(7.0, abs=0.15)


# ---------- 2. caption timing hints ----------


def test_caption_hints_are_used_when_they_make_a_timeline():
    from app.services.captions import chunks_to_timed

    chunks = [CaptionChunk(start_hint=0.0, text="a"), CaptionChunk(start_hint=1.0, text="b"),
              CaptionChunk(start_hint=2.5, text="c")]
    t = chunks_to_timed(chunks, 6.0)
    assert [(x.start, x.end) for x in t] == [(0.0, 1.0), (1.0, 2.5), (2.5, 6.0)]
    # Equal hints would show two captions at once: they are spread evenly.
    same = [CaptionChunk(start_hint=0.0, text="a"), CaptionChunk(start_hint=2.0, text="b"),
            CaptionChunk(start_hint=2.0, text="c")]
    timed = chunks_to_timed(same, 6.0)
    assert [x.start for x in timed] == [0.0, 2.0, 4.0]
    # Hints closer than half a second: each caption ends when the next starts.
    close = [CaptionChunk(start_hint=0.0, text="a"), CaptionChunk(start_hint=2.0, text="b"),
             CaptionChunk(start_hint=2.2, text="c")]
    timed = chunks_to_timed(close, 6.0)
    assert all(a.end <= b.start for a, b in zip(timed, timed[1:], strict=False))


@pytest.mark.parametrize("hints", [
    [0.0, 0.0, 0.0],      # no hints at all
    [0.0, 3.0, 1.0],      # going back
    [0.5, 1.0, 2.0],      # not starting at 0
    [0.0, 2.0, 9.0],      # the last one past the end of the body
    [0.0, 2.0, 2.0],      # two at once
])
def test_unusable_caption_hints_spread_evenly(hints):
    from app.services.captions import chunks_to_timed

    chunks = [CaptionChunk(start_hint=h, text=str(i)) for i, h in enumerate(hints)]
    assert [x.start for x in chunks_to_timed(chunks, 6.0)] == [0.0, 2.0, 4.0]


# ---------- 3. one voiceover file per render ----------


def test_each_render_keeps_its_own_voiceover_and_reuse_still_works(auth_client):
    from app import models
    from app.services import fingerprints, pipeline

    pid = _rendered(auth_client)
    first = _renders(pid)[-1]
    assert Path(first.audio_path).name == f"voice_{first.id}.m4a"

    auth_client.patch(f"/api/projects/{pid}/plan",
                      json={"cleaned_voice_script": "A different line. Said in a new way."})
    assert auth_client.post(f"/api/projects/{pid}/generate-video", params={"force": "true"}).status_code == 202
    second = _renders(pid)[-1]
    assert second.id != first.id and Path(second.audio_path).name == f"voice_{second.id}.m4a"
    assert Path(first.audio_path).is_file()  # the older render still points at its own audio

    # Back to the first script: its voiceover is reused, not made again.
    auth_client.patch(f"/api/projects/{pid}/plan",
                      json={"cleaned_voice_script": "Stop scrolling now! Here is the rest of it. And one more line."})
    plan = auth_client.get(f"/api/projects/{pid}/render-plan").json()
    assert plan["voiceover"] == "reuse" and plan["voiceover_cost"] == 0
    db = _db()
    try:
        p = db.get(models.VideoProject, pid)
        fp = fingerprints.voice_fingerprint(db, p)
        assert pipeline._reusable_voiceover(db, p, fp) == first.audio_path
        # "Make voiceover" reuses it too, and a recompose puts it under the clips.
        assert pipeline.make_voiceover_preview(db, p)["reused"] is True
        r = pipeline.recompose_project(db, pid)
        assert r.audio_path == first.audio_path and r.composition_json["audio_path"] == first.audio_path
    finally:
        db.close()


def test_variant_renders_never_become_the_current_voiceover(auth_client, tmp_path):
    from app import models
    from app.services import pipeline

    pid = _rendered(auth_client)
    base = _renders(pid)[-1]
    db = _db()
    try:
        v = models.HookVariant(project_id=pid, base_render_id=base.id, label="B", style="question",
                               hook_text="What if?", status="completed")
        db.add(v)
        db.flush()
        audio = _clip(tmp_path / "x.mp4", 1.0)  # any file on disk
        vr = models.Render(project_id=pid, status="generating_shots", audio_path=str(audio),
                           voice_fingerprint="other", hook_variant_id=v.id)
        db.add(vr)
        db.commit()
        assert pipeline._latest_audio_render(db, pid).id == base.id
        assert pipeline._latest_completed_audio(db, pid) == Path(base.audio_path)
        pipeline.maybe_compose(db, vr.id)  # never composed from the project's clips
        db.refresh(vr)
        assert vr.status == "generating_shots"
    finally:
        db.close()


# ---------- 4. scratch dir per compose + recompose busy check ----------


def test_each_compose_has_its_own_scratch_dir_removed_afterwards(auth_client, monkeypatch):
    from app.services import pipeline, storage

    pid = _rendered(auth_client)
    seen: list[Path] = []
    real = pipeline.compose

    def spy(inputs):
        seen.append(inputs.out_dir)
        return real(inputs)

    monkeypatch.setattr(pipeline, "compose", spy)
    db = _db()
    try:
        r1 = pipeline.recompose_project(db, pid)
        r2 = pipeline.recompose_project(db, pid)
        assert Path(r1.final_video_path).is_file() and Path(r2.final_video_path).is_file()
        ids = (r1.id, r2.id)
    finally:
        db.close()
    project_dir = storage.render_subdir(pid)
    assert seen == [project_dir / f"tmp_{ids[0]}", project_dir / f"tmp_{ids[1]}"]
    assert not any(p.name.startswith("tmp_") for p in project_dir.iterdir())
    for name in ("norm_00.mp4", "silent.mp4", "final.mp4", "thumb.jpg", "captions.ass"):
        assert not (project_dir / name).exists()
    assert (project_dir / f"captions_{ids[1]}.ass").is_file()


def test_failed_compose_removes_its_scratch_dir(auth_client, monkeypatch):
    from app.services import pipeline, storage

    pid = _rendered(auth_client)

    def boom(inputs):
        (inputs.out_dir / "norm_00.mp4").write_bytes(b"x")
        raise RuntimeError("ffmpeg broke")

    monkeypatch.setattr(pipeline, "compose", boom)
    db = _db()
    try:
        with pytest.raises(RuntimeError):
            pipeline.recompose_project(db, pid)
    finally:
        db.close()
    assert not any(p.name.startswith("tmp_") for p in storage.render_subdir(pid).iterdir())


@pytest.mark.parametrize("status", ["rendering", "generating_shots"])
def test_recompose_refused_while_a_render_runs(auth_client, status):
    from app import models

    pid = _rendered(auth_client)
    db = _db()
    try:
        db.add(models.Render(project_id=pid, status=status))
        db.commit()
    finally:
        db.close()
    r = auth_client.post(f"/api/projects/{pid}/recompose")
    assert r.status_code == 409, r.text
    assert len(_renders(pid)) == 2  # nothing composed


def test_recompose_refused_while_a_variant_composes_next_to_a_newer_render(auth_client):
    from app import models

    pid = _rendered(auth_client)
    base = _renders(pid)[-1]
    db = _db()
    try:
        v = models.HookVariant(project_id=pid, base_render_id=base.id, label="B", status="composing")
        db.add(v)
        db.flush()
        db.add(models.Render(project_id=pid, status="rendering", hook_variant_id=v.id))
        db.add(models.Render(project_id=pid, status="failed"))  # the newest one is done
        db.commit()
    finally:
        db.close()
    assert auth_client.post(f"/api/projects/{pid}/recompose").status_code == 409


def test_recompose_works_when_idle(auth_client):
    pid = _rendered(auth_client)
    r = auth_client.post(f"/api/projects/{pid}/recompose")
    assert r.status_code == 202, r.text
    rs = _renders(pid)
    assert len(rs) == 2 and rs[-1].status == "completed"


# ---------- 5. what a render was composed from ----------


def test_composition_json_records_clips_takes_audio_and_captions(auth_client):
    from app import models
    from app.services import pipeline, takes

    pid = _rendered(auth_client)
    r = _renders(pid)[-1]
    comp = r.composition_json
    db = _db()
    try:
        p = db.get(models.VideoProject, pid)
        body = pipeline._body_shots(p)
        assert [sh["shot_id"] for sh in comp["shots"]] == [s.id for s in body]
        assert [sh["clip_path"] for sh in comp["shots"]] == [s.clip_path for s in body]
        assert [sh["take_id"] for sh in comp["shots"]] == [takes.current_take(s).id for s in body]
        assert [sh["fingerprint"] for sh in comp["shots"]] == [s.clip_fingerprint for s in body]
    finally:
        db.close()
    assert comp["audio_path"] == r.audio_path and comp["voice_fingerprint"] == r.voice_fingerprint
    assert comp["captions"] and comp["captions"][0]["start"] == 0.0
    assert comp["hook"] == "Stop scrolling now!"
    assert comp["script"].startswith("Stop scrolling now!")
    assert comp["hook_window_seconds"] is None and comp["hook_variant_id"] is None
    assert comp["duration_seconds"] == pytest.approx(_duration(r.final_video_path), abs=0.01)
    assert comp["aspect_ratio"] == "9:16"
    # The API shows it too.
    out = auth_client.get(f"/api/renders/{r.id}").json()
    assert out["composition_json"]["shots"] == comp["shots"] and out["hook_variant_id"] is None


# ---------- 6. version numbers ----------


def _history(pid: int, tmp_path: Path) -> dict:
    """completed, failed, completed, cancelled, a variant of the 2nd
    completed one, and a running render."""
    from app import models

    db = _db()
    try:
        made = {}
        for key, status in (("c1", "completed"), ("f", "failed"), ("c2", "completed"), ("x", "cancelled")):
            f = tmp_path / f"{key}.mp4"
            f.write_bytes(b"mp4")
            r = models.Render(project_id=pid, status=status, final_video_path=str(f))
            db.add(r)
            db.flush()
            made[key] = r.id
        v = models.HookVariant(project_id=pid, base_render_id=made["c2"], label="B", status="completed")
        db.add(v)
        db.flush()
        f = tmp_path / "v.mp4"
        f.write_bytes(b"mp4")
        r = models.Render(project_id=pid, status="completed", final_video_path=str(f), hook_variant_id=v.id)
        db.add(r)
        db.flush()
        made["v"] = r.id
        r = models.Render(project_id=pid, status="rendering")
        db.add(r)
        db.commit()
        made["run"] = r.id
        return made
    finally:
        db.close()


def test_version_numbers_agree_everywhere(auth_client, tmp_path):
    from app.services import publishing

    pid = auth_client.post("/api/projects", json={"title": "Versions", "original_script": "x"}).json()["id"]
    ids = _history(pid, tmp_path)
    rows = {r["id"]: r for r in auth_client.get(f"/api/projects/{pid}/renders").json()}
    assert (rows[ids["c1"]]["version"], rows[ids["c1"]]["version_label"]) == (1, "Version 1")
    assert (rows[ids["c2"]]["version"], rows[ids["c2"]]["version_label"]) == (2, "Version 2")
    assert (rows[ids["f"]]["version"], rows[ids["f"]]["version_label"]) == (None, "Failed attempt")
    assert (rows[ids["x"]]["version"], rows[ids["x"]]["version_label"]) == (None, "Cancelled attempt")
    assert (rows[ids["v"]]["version"], rows[ids["v"]]["version_label"]) == (2, "Version 2 · opening B")
    assert rows[ids["run"]]["version"] is None
    assert auth_client.get(f"/api/renders/{ids['v']}").json()["version_label"] == "Version 2 · opening B"

    db = _db()
    try:
        assert publishing.render_versions(db, [pid]) == {ids["c1"]: 1, ids["c2"]: 2, ids["v"]: 2}
    finally:
        db.close()

    z = auth_client.get(f"/api/projects/{pid}/renders/export")
    assert z.status_code == 200
    names = sorted(zipfile.ZipFile(BytesIO(z.content)).namelist())
    assert names == ["versions_v1.mp4", "versions_v2.mp4", "versions_v2_opening_b.mp4"]


def test_expired_renders_keep_their_number(auth_client, tmp_path):
    from app import models
    from app.services import publishing

    pid = auth_client.post("/api/projects", json={"title": "Old", "original_script": "x"}).json()["id"]
    ids = _history(pid, tmp_path)
    db = _db()
    try:
        db.get(models.Render, ids["c1"]).status = "expired"
        db.commit()
        assert publishing.render_versions(db, [pid])[ids["c2"]] == 2
    finally:
        db.close()


# ---------- 7. publish metadata per render ----------


def test_publish_meta_per_render_uses_its_hook_and_never_repeats_it(auth_client):
    from app import models

    pid = _planned(auth_client)
    meta = auth_client.get(f"/api/projects/{pid}/publish-meta").json()
    desc = meta["description"]
    assert desc.startswith("Stop scrolling now!")
    assert desc.count("Stop scrolling now!") == 1
    assert "Here is the rest of it." in desc

    db = _db()
    try:
        base = models.Render(project_id=pid, status="completed")
        db.add(base)
        db.flush()
        v = models.HookVariant(project_id=pid, base_render_id=base.id, label="B", status="completed",
                               hook_text="What if you never scrolled again?")
        db.add(v)
        db.flush()
        vr = models.Render(project_id=pid, status="completed", hook_variant_id=v.id, composition_json={
            "script": "What if you never scrolled again? Here is the rest of it. And one more line.",
            "hook": "What if you never scrolled again?"})
        db.add(vr)
        other = models.VideoProject(title="Other")
        db.add(other)
        db.flush()
        stray = models.Render(project_id=other.id, status="completed")
        db.add(stray)
        db.commit()
        vr_id, base_id, stray_id = vr.id, base.id, stray.id
    finally:
        db.close()

    vdesc = auth_client.get(f"/api/projects/{pid}/publish-meta", params={"render_id": vr_id}).json()["description"]
    assert vdesc.startswith("What if you never scrolled again?")
    assert vdesc.count("What if you never scrolled again?") == 1
    assert "Stop scrolling now!" not in vdesc and "Here is the rest of it." in vdesc
    # A render without a recorded composition: the project's current plan.
    bdesc = auth_client.get(f"/api/projects/{pid}/publish-meta", params={"render_id": base_id}).json()["description"]
    assert bdesc == desc
    assert auth_client.get(f"/api/projects/{pid}/publish-meta",
                           params={"render_id": stray_id}).status_code == 404


def test_publish_meta_of_an_opening_typed_without_end_punctuation(auth_client):
    """An opening typed as "What if you never scrolled again" is voiced
    with a period; the description's body must not start with "."."""
    from app import models

    pid = _rendered(auth_client)
    db = _db()
    try:
        vr = models.Render(project_id=pid, status="completed", composition_json={
            "script": "What if you never scrolled again. Here is the rest of it.",
            "hook": "What if you never scrolled again"})
        db.add(vr)
        db.commit()
        vr_id = vr.id
    finally:
        db.close()
    desc = auth_client.get(f"/api/projects/{pid}/publish-meta", params={"render_id": vr_id}).json()["description"]
    lines = desc.split("\n")
    assert lines[0] == "What if you never scrolled again."
    assert not any(ln.startswith(".") for ln in lines)
    assert "Here is the rest of it." in desc and desc.count("never scrolled") == 1


def test_publish_meta_with_a_hook_that_is_not_the_first_sentence():
    from app import models
    from app.services import publish_meta

    p = models.VideoProject(title="T", mode="reel_montage", aspect_ratio="9:16", cta_text="",
                            generated_plan_json={"cleaned_voice_script": "no punctuation at all",
                                                 "hook": ""})
    d = publish_meta.draft(p)["description"]
    assert d.count("no punctuation at all") == 1


# ---------- 8. Results facts per render ----------


def _uploaded(db, render, ext):
    from app import models

    db.add(models.PublishJob(render_id=render.id, title="t", status="published", privacy="public",
                             external_id=ext, external_url=f"https://www.youtube.com/watch?v={ext}"))


def test_results_facts_come_from_the_published_render(auth_client):
    from app import models
    from app.services import publishing

    db = _db()
    try:
        p = models.VideoProject(title="R", mode="reel_montage", aspect_ratio="9:16", cta_text="")
        db.add(p)
        db.flush()
        db.add(models.VideoShot(project_id=p.id, shot_order=1, shot_type="hero", duration_seconds=10,
                                provider_model="model/now"))
        # A planned end-card shot with no CTA: compose draws no end card.
        db.add(models.VideoShot(project_id=p.id, shot_order=2, shot_type="end_card", duration_seconds=3))
        old = models.Render(project_id=p.id, status="completed", final_video_path="/x.mp4")
        new = models.Render(project_id=p.id, status="completed", final_video_path="/y.mp4", composition_json={
            "shots": [{"provider_model": "model/then"}, {"provider_model": "model/then"},
                      {"provider_model": "model/other"}],
            "duration_seconds": 72.4, "aspect_ratio": "16:9"})
        db.add_all([old, new])
        db.flush()
        _uploaded(db, old, "o1")
        _uploaded(db, new, "n1")
        db.commit()
        rows = {r["render_id"]: r for r in publishing.result_rows(db)}
        o, n = rows[old.id], rows[new.id]
    finally:
        db.close()
    assert (n["duration_seconds"], n["length_bucket"], n["model"], n["aspect_ratio"]) == \
        (72.4, "1–10 min", "model/then", "16:9")
    # An old render: the project's state, and the planned end-card shot does not count.
    assert (o["duration_seconds"], o["model"], o["aspect_ratio"]) == (10.0, "model/now", "9:16")
    assert n["render_version_label"] == "Version 2" and o["render_version_label"] == "Version 1"


# ---------- 9. the budget cap counts the estimate ----------


def test_generate_and_retry_refused_when_the_estimate_passes_the_cap(auth_client, monkeypatch):
    from app.services import spend

    from .test_spend import _add

    pid = _planned(auth_client)
    est = auth_client.get(f"/api/projects/{pid}/render-plan").json()["total"]
    assert est > 0.1
    monkeypatch.setattr(spend, "_all_mocked", lambda: False)  # mock mode never hits the cap
    auth_client.put("/api/spend/budget", json={"monthly_usd": 1, "hard_stop": True, "alert_pcts": []})
    _add(1.0 - est / 2)  # under the cap, but not with this render on top
    r = auth_client.post(f"/api/projects/{pid}/generate-video", params={"force": "true"})
    assert r.status_code == 402 and "would cost" in r.json()["detail"], r.text
    r = auth_client.post(f"/api/projects/{pid}/retry")
    assert r.status_code == 402, r.text
    assert _renders(pid) == []
    assert auth_client.get(f"/api/projects/{pid}").json()["status"] == "planned"  # no claim left behind

    # Room for it: it runs.
    auth_client.put("/api/spend/budget", json={"monthly_usd": 100, "hard_stop": True, "alert_pcts": []})
    assert auth_client.post(f"/api/projects/{pid}/generate-video", params={"force": "true"}).status_code == 202


def test_estimate_check_helper(auth_client):
    from app.services import spend

    db = _db()
    try:
        assert spend.over_budget_with(db, 5.0) is None  # mock mode / no cap
    finally:
        db.close()


# ---------- migration 0011 ----------


def test_migration_0011_adds_composition_and_hook_variants(tmp_path, monkeypatch):
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
    command.upgrade(dbmod._alembic_config(), "0010")
    with dbmod.engine.begin() as conn:
        conn.execute(text("INSERT INTO video_projects (title, original_script, mode, aspect_ratio, "
                          "target_duration_seconds, cta_text, caption_style, include_disclosure, disclosure_text, "
                          "creative_direction, voiceover_source, voiceover_upload_path, music_upload_path, "
                          "music_volume, status, created_at, updated_at) VALUES ('p', '', 'reel_montage', '9:16', "
                          "25, '', 'clean_white', 1, '', '', 'tts', '', '', 0.25, 'completed', "
                          "'2026-01-01', '2026-01-01')"))
        conn.execute(text("INSERT INTO renders (project_id, status, audio_path, final_video_path, thumbnail_path, "
                          "render_log, estimated_cost, actual_cost, error, share_token, created_at, updated_at) "
                          "VALUES (1, 'completed', '', '', '', '', 0, 0, '', 'tok', '2026-01-01', '2026-01-01')"))
    command.upgrade(dbmod._alembic_config(), "head")
    insp = inspect(dbmod.engine)
    assert {"composition_json", "hook_variant_id"} <= {c["name"] for c in insp.get_columns("renders")}
    fk = [f for f in insp.get_foreign_keys("renders") if f["constrained_columns"] == ["hook_variant_id"]]
    assert fk and fk[0]["referred_table"] == "hook_variants" and fk[0]["options"].get("ondelete") == "SET NULL"
    cols = {c["name"] for c in insp.get_columns("hook_variants")}
    assert {"project_id", "base_render_id", "label", "style", "hook_text", "window_seconds", "shot1_source",
            "shot1_take_id", "shot1_prompt", "shot1_clip_path", "audio_path", "audio_fingerprint", "status",
            "error", "render_id", "cost_estimate", "created_at", "updated_at"} <= cols
    with dbmod.engine.connect() as conn:
        assert tuple(conn.execute(text("SELECT composition_json, hook_variant_id FROM renders")).one()) == (None, None)
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0011"
        diff = compare_metadata(MigrationContext.configure(conn), dbmod.Base.metadata)
    assert diff == [], diff  # same as `alembic check`
    command.downgrade(dbmod._alembic_config(), "0010")
    insp = inspect(dbmod.engine)
    assert "hook_variants" not in insp.get_table_names()
    assert "composition_json" not in {c["name"] for c in insp.get_columns("renders")}


# ---------- review fixes: shared legacy voice.m4a, recompose script ----------


def test_a_shared_legacy_voice_file_belongs_only_to_its_newest_render(auth_client):
    """Before each synthesis had its own file, every render wrote voice.m4a:
    an older render pointing at it no longer holds its own audio."""
    import shutil

    from app import models
    from app.services import fingerprints, hook_lab, pipeline

    pid = _rendered(auth_client)
    first = _renders(pid)[-1]
    legacy = Path(first.audio_path).with_name("voice.m4a")
    shutil.copy(first.audio_path, legacy)
    db = _db()
    try:
        r1 = db.get(models.Render, first.id)
        fp_x = r1.voice_fingerprint
        r1.audio_path = str(legacy)
        # Render 2 was made with another script / voice (Y) and overwrote voice.m4a.
        r2 = models.Render(project_id=pid, status="completed", audio_path=str(legacy), voice_fingerprint="fp-y")
        db.add(r2)
        db.commit()
        project = db.get(models.VideoProject, pid)
        assert fingerprints.voice_fingerprint(db, project) == fp_x  # the user went back to X
        assert pipeline.audio_overwritten(db, r1) and not pipeline.audio_overwritten(db, r2)
        assert pipeline._reusable_voiceover(db, project, fp_x) is None
        assert not pipeline.current_voiceover_made(db, project)
        assert pipeline._reusable_voiceover(db, project, "fp-y") == str(legacy)
        assert pipeline.render_plan(db, project)["voiceover"] != "reuse"
        with pytest.raises(hook_lab.HookLabError, match="replaced by a later version"):
            hook_lab.check_base(db, r1, pid)
        # A newer render that reused the same file with the same voice keeps it valid.
        r2.voice_fingerprint = fp_x
        db.commit()
        assert pipeline._reusable_voiceover(db, project, fp_x) == str(legacy)
    finally:
        db.close()


def test_recompose_over_the_old_voiceover_records_the_words_it_says(auth_client):
    from app import models
    from app.services import hook_lab, pipeline, publish_meta

    old = "Stop scrolling now! Here is the rest of it. And one more line."
    pid = _rendered(auth_client)
    auth_client.patch(f"/api/projects/{pid}/plan",
                      json={"cleaned_voice_script": "A brand new opening. With new words after it."})
    db = _db()
    try:
        r = pipeline.recompose_project(db, pid)  # keeps the old voice: the new script isn't voiced yet
        comp = r.composition_json
        assert comp["script"] == old and comp["hook"] == "Stop scrolling now!"
        assert hook_lab.base_hook(db, r) == "Stop scrolling now!"
        meta = publish_meta.draft(db.get(models.VideoProject, pid), r)
        assert "brand new opening" not in meta["description"] and "Stop scrolling now!" in meta["description"]
    finally:
        db.close()


def test_recompose_over_an_unrecorded_voiceover_records_no_script(auth_client):
    from app import models
    from app.services import hook_lab, pipeline

    pid = _rendered(auth_client)
    db = _db()
    try:
        first = db.get(models.Render, _renders(pid)[-1].id)
        first.composition_json = None  # made before compositions were recorded
        db.commit()
    finally:
        db.close()
    auth_client.patch(f"/api/projects/{pid}/plan",
                      json={"cleaned_voice_script": "A brand new opening. With new words after it."})
    db = _db()
    try:
        r = pipeline.recompose_project(db, pid)
        assert r.composition_json["script"] == "" and r.composition_json["spoken_unknown"]
        assert hook_lab.base_hook(db, r) == "" and hook_lab.base_script(db, r) == ""
    finally:
        db.close()


def test_hook_lab_clips_never_hold_up_a_reroll_recompose(auth_client):
    from app import models
    from app.services import video_jobs

    pid = _rendered(auth_client)
    db = _db()
    try:
        job = models.VideoJob(purpose="hook_shot", ref_id=999, project_id=pid, status="running")
        db.add(job)
        db.commit()
        assert video_jobs.pending_for(db, project_id=pid) == 0
        assert video_jobs.pending_for(db, project_id=pid, purposes=video_jobs.HOOK_PURPOSES) == 1
        shot_id = db.query(models.VideoShot.id).filter_by(project_id=pid).first()[0]
    finally:
        db.close()
    # A re-roll (which may recompose) waits until the openings are done.
    r = auth_client.post(f"/api/projects/{pid}/shots/{shot_id}/regenerate")
    assert r.status_code == 409 and "Other openings" in r.json()["detail"]
    r = auth_client.post(f"/api/projects/{pid}/shots/regenerate-bulk", json={"shot_ids": [shot_id]})
    assert r.status_code == 409


def test_retention_never_counts_or_expires_hook_lab_openings(auth_client, monkeypatch):
    from app import models
    from app.config import get_settings
    from app.services import render_files

    pid = _rendered(auth_client)
    base = _renders(pid)[-1]
    db = _db()
    try:
        for label in "BC":
            v = models.HookVariant(project_id=pid, base_render_id=base.id, label=label, status="completed")
            db.add(v)
            db.flush()
            db.add(models.Render(project_id=pid, status="completed", hook_variant_id=v.id))
        db.commit()
        monkeypatch.setattr(get_settings(), "render_retention_keep", 1)
        assert render_files.prune(db, pid) == 0
        assert db.get(models.Render, base.id).status == "completed"
    finally:
        db.close()


def test_a_render_failed_by_the_sweeper_loses_its_scratch_dir(auth_client):
    from app import models
    from app.services import render_files, storage, sweepers

    pid = _rendered(auth_client)
    db = _db()
    try:
        r = models.Render(project_id=pid, status="rendering")
        db.add(r)
        db.commit()
        work = render_files.scratch_dir(storage.render_subdir(pid), r.id)
        (work / "final.mp4").write_bytes(b"x")
        assert sweepers._fail_stuck_render(db, r)
        assert not work.exists()
    finally:
        db.close()


def test_results_length_of_an_old_render_comes_from_its_file(auth_client):
    from app import models
    from app.services import publishing

    pid = _rendered(auth_client)
    r = _renders(pid)[-1]
    real = _duration(r.final_video_path)
    db = _db()
    try:
        r = db.get(models.Render, r.id)
        r.composition_json = None
        db.commit()
        facts = publishing._render_facts(r, {"duration_seconds": 99.0, "length_bucket": "x"}, {})
        assert facts["duration_seconds"] == pytest.approx(real, abs=0.01)
    finally:
        db.close()
