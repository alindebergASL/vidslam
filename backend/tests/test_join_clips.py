"""Join clips: finished Studio clips played back to back as one new Studio
clip (free, made here by ffmpeg), with a cut or a short dissolve, each
clip's own sound and silence for a clip without any."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from .test_image_ops import _avatar, _picture


def _video(path: Path, *, size=(96, 64), seconds: float = 1.5, audio: bool = False, rate: int = 10,
           pix_fmt: str = "yuv420p") -> Path:
    """A small real clip: a test pattern, with a tone when `audio`."""
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
           f"testsrc=size={size[0]}x{size[1]}:rate={rate}:duration={seconds}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-c:a", "aac", "-shortest"]
    cmd += ["-pix_fmt", pix_fmt, str(path)]
    subprocess.run(cmd, check=True)
    return path


def _streams(path: Path) -> dict[str, float]:
    out = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_entries",
                          "stream=codec_type,duration", str(path)], capture_output=True, text=True, check=True)
    return {s["codec_type"]: float(s.get("duration") or 0) for s in json.loads(out.stdout)["streams"]}


def _mean_volume(path: Path, start: float, seconds: float) -> float:
    out = subprocess.run(["ffmpeg", "-v", "info", "-ss", f"{start}", "-t", f"{seconds}", "-i", str(path),
                          "-vn", "-af", "volumedetect", "-f", "null", "-"], capture_output=True, text=True)
    for line in out.stderr.splitlines():
        if "mean_volume:" in line:
            return float(line.split("mean_volume:")[1].split("dB")[0])
    raise AssertionError(out.stderr[-500:])


# ---------- the renderer ----------


def test_cut_plays_the_clips_back_to_back_with_sound_and_silence(tmp_path):
    from app.services.renderer import _probe_size, join_clips_with_audio

    a = _video(tmp_path / "a.mp4", seconds=2.0, audio=True)
    b = _video(tmp_path / "b.mp4", seconds=1.5, audio=False, size=(64, 96), rate=24)
    out, seconds, has_audio = join_clips_with_audio([a, b, a], tmp_path / "out.mp4")
    assert has_audio is True
    assert seconds == pytest.approx(5.5, abs=0.1)
    assert _probe_size(out) == (96, 64)  # the first clip's size
    streams = _streams(out)
    assert streams["video"] == pytest.approx(5.5, abs=0.1)
    assert streams["audio"] == pytest.approx(5.5, abs=0.1)
    # The tone, then silence where the clip without sound plays, then the tone.
    assert _mean_volume(out, 0.3, 1.2) > -40
    assert _mean_volume(out, 2.3, 0.9) < -80
    assert _mean_volume(out, 3.8, 1.2) > -40


def test_dissolve_overlaps_each_join_and_is_kept_to_a_third_of_the_shortest_clip(tmp_path):
    from app.services.renderer import join_clips_with_audio, join_crossfade

    a = _video(tmp_path / "a.mp4", seconds=2.0, audio=True)
    b = _video(tmp_path / "b.mp4", seconds=2.0, audio=True)
    _, seconds, _ = join_clips_with_audio([a, b, a], tmp_path / "d.mp4", transition="dissolve", crossfade=0.5)
    assert seconds == pytest.approx(6.0 - 2 * 0.5, abs=0.1)
    streams = _streams(tmp_path / "d.mp4")
    assert streams["audio"] == pytest.approx(streams["video"], abs=0.1)

    short = _video(tmp_path / "s.mp4", seconds=0.9)
    assert join_crossfade([2.0, 0.9], "dissolve", 1.5) == pytest.approx(0.3)
    _, seconds, _ = join_clips_with_audio([a, short], tmp_path / "e.mp4", transition="dissolve", crossfade=1.5)
    assert seconds == pytest.approx(2.0 + 0.9 - 0.3, abs=0.1)
    # A dissolve too short to see is a cut; "cut" ignores the crossfade.
    assert join_crossfade([2.0, 0.2], "dissolve", 1.0) == 0.0
    assert join_crossfade([2.0, 2.0], "cut", 1.0) == 0.0


def test_sound_starting_after_the_picture_stays_in_sync(tmp_path):
    """A clip whose sound starts late (audio start_time > 0) keeps that
    lead-in as silence, so its sound neither runs short nor drifts early."""
    from app.services.renderer import join_clips_with_audio

    late = tmp_path / "late.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=96x64:rate=10:duration=2",
                    "-itsoffset", "0.6", "-f", "lavfi", "-i", "sine=frequency=440:duration=1.4",
                    "-map", "0", "-map", "1", "-pix_fmt", "yuv420p", "-c:a", "aac", str(late)], check=True)
    for transition, expected in (("cut", 8.0), ("dissolve", 8.0 - 3 * 0.5)):
        out, seconds, _ = join_clips_with_audio([late] * 4, tmp_path / f"{transition}.mp4", transition=transition)
        streams = _streams(out)
        assert streams["video"] == pytest.approx(expected, abs=0.1)
        assert streams["audio"] == pytest.approx(streams["video"], abs=0.1)
    # Cut: the 4th clip's lead-in (6.0-6.5 s) is silent, its tone after it.
    out = tmp_path / "cut.mp4"
    assert _mean_volume(out, 6.05, 0.4) < -60
    assert _mean_volume(out, 6.8, 1.0) > -40


def _flash_clip(path: Path, tmp: Path, *, video_offset: float = 0.0) -> Path:
    """A 2 s black clip with a white flash 1.0 s into its picture and a
    beep at that same moment in the file; the picture starts
    `video_offset` late in the file (video start_time > 0)."""
    v = tmp / f"{path.stem}-v.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                    "color=black:size=96x64:rate=30:duration=2,"
                    "drawbox=c=white:t=fill:enable='between(t,1.0,1.09)'", "-pix_fmt", "yuv420p", str(v)],
                   check=True)
    beep = 1.0 + video_offset  # the flash's time in the file
    a = tmp / f"{path.stem}-a.wav"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                    f"aevalsrc='if(between(t,{beep},{beep + 0.1}),sin(2*PI*1000*t),0)'"
                    f":s=48000:d={2.0 + video_offset}", str(a)], check=True)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-itsoffset", f"{video_offset}", "-i", str(v),
                    "-i", str(a), "-map", "0", "-map", "1",
                    "-c:v", "copy", "-c:a", "aac", str(path)], check=True)
    return path


def _start_times(path: Path) -> dict[str, float]:
    out = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_entries",
                          "stream=codec_type,start_time", str(path)], capture_output=True, text=True, check=True)
    return {s["codec_type"]: float(s["start_time"]) for s in json.loads(out.stdout)["streams"]}


def _flashes(path: Path) -> list[float]:
    """When each white flash starts (the first bright frame of each)."""
    out = subprocess.run(["ffprobe", "-v", "quiet", "-f", "lavfi", "-i", f"movie={path},signalstats",
                          "-show_entries", "frame=pts_time:frame_tags=lavfi.signalstats.YAVG", "-of", "json"],
                         capture_output=True, text=True, check=True)
    times, bright = [], False
    for f in json.loads(out.stdout)["frames"]:
        now = float(f["tags"]["lavfi.signalstats.YAVG"]) > 128
        if now and not bright:
            times.append(float(f["pts_time"]))
        bright = now
    return times


def _beeps(path: Path) -> list[float]:
    """When each beep starts (where silencedetect's silences end, but for
    the one the end of the file closes)."""
    out = subprocess.run(["ffmpeg", "-v", "info", "-i", str(path), "-vn", "-af",
                          "silencedetect=noise=-30dB:d=0.05", "-f", "null", "-"],
                         capture_output=True, text=True)
    ends = [float(line.split("silence_end:")[1].split("|")[0]) for line in out.stderr.splitlines()
            if "silence_end:" in line]
    return [t for t in ends if t < _streams(path)["audio"] - 0.1]


def test_picture_starting_after_its_sound_keeps_the_beep_on_the_flash(tmp_path):
    """A clip whose picture starts after its sound in the file (video
    start_time > 0, audio 0): the sound is laid on the picture's own
    timeline, so a beep made at the flash still plays on it once joined,
    and the sound isn't cut short. A sound starting slightly before 0
    (AAC priming) is fine too."""
    from app.services.renderer import FPS, _probe_clip, join_clips_with_audio

    late = _flash_clip(tmp_path / "late.mp4", tmp_path, video_offset=0.2)
    starts = _start_times(late)
    assert starts["video"] == pytest.approx(0.2, abs=0.01) and starts["audio"] == pytest.approx(0.0, abs=0.01)
    assert _probe_clip(late)[0] == pytest.approx(2.0, abs=0.05)
    primed = _flash_clip(tmp_path / "primed.mkv", tmp_path)  # Matroska keeps AAC's priming
    assert -0.05 < _start_times(primed)["audio"] < 0
    for clips in ([late, late, late], [primed, late, primed]):
        out, _, _ = join_clips_with_audio(clips, tmp_path / "out.mp4")
        flashes, beeps = _flashes(out), _beeps(out)
        assert flashes == pytest.approx([1.0, 3.0, 5.0], abs=1 / FPS + 0.01), flashes
        assert len(beeps) == 3, beeps
        for f, b in zip(flashes, beeps, strict=True):
            assert abs(b - f) <= 1 / FPS, (flashes, beeps)
        streams = _streams(out)
        assert streams["audio"] == pytest.approx(streams["video"], abs=0.1)


def test_sound_ending_before_the_picture_starts_gives_silence_not_an_error(tmp_path):
    """A clip whose short sound ends before its picture starts in the file:
    that clip contributes silence of its full length; the join neither
    fails nor comes out short."""
    from app.services.renderer import join_clips_with_audio

    v = tmp_path / "pic.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=gray:size=96x64:rate=30:duration=2",
                    "-pix_fmt", "yuv420p", str(v)], check=True)
    a = tmp_path / "short.wav"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=500:duration=0.3",
                    str(a)], check=True)
    early = tmp_path / "early.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-itsoffset", "0.6", "-i", str(v), "-i", str(a),
                    "-map", "0", "-map", "1", "-c:v", "copy", "-c:a", "aac", str(early)], check=True)
    other = _flash_clip(tmp_path / "other.mp4", tmp_path, video_offset=0.2)
    for clips in ([early, other], [other, early, other], [other, early]):
        out, length, has_audio = join_clips_with_audio(clips, tmp_path / "out.mp4")
        streams = _streams(out)
        assert has_audio and streams["video"] == pytest.approx(2.0 * len(clips), abs=0.05)
        assert streams["audio"] == pytest.approx(streams["video"], abs=0.05)


def test_matroska_picture_shorter_than_its_sound_is_measured_by_the_picture(tmp_path):
    """Matroska has no per-stream duration: the picture's length comes from
    its own packets, not the container (which the longer sound stretches),
    so joined clips stay in sync."""
    from app.services.renderer import FPS, _probe_clip, join_clips_with_audio

    v = tmp_path / "f-v.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                    "color=black:size=96x64:rate=30:duration=2,"
                    "drawbox=c=white:t=fill:enable='between(t,1.0,1.09)'", "-pix_fmt", "yuv420p", str(v)],
                   check=True)
    a = tmp_path / "f-a.wav"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                    "aevalsrc='if(between(t,1.2,1.3),sin(2*PI*1000*t),0)':s=48000:d=2.7", str(a)], check=True)
    mkv = tmp_path / "long_sound.mkv"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-itsoffset", "0.2", "-i", str(v), "-i", str(a),
                    "-map", "0", "-map", "1", "-c:v", "copy", "-c:a", "aac", str(mkv)], check=True)
    assert _probe_clip(mkv)[0] == pytest.approx(2.0, abs=1 / FPS + 0.01)
    out, _, _ = join_clips_with_audio([mkv, mkv, mkv], tmp_path / "out.mp4")
    flashes, beeps = _flashes(out), _beeps(out)
    assert flashes == pytest.approx([1.0, 3.0, 5.0], abs=1 / FPS + 0.01), flashes
    assert len(beeps) == 3 and all(abs(b - f) <= 1 / FPS + 0.01 for f, b in zip(flashes, beeps, strict=True)), beeps
    streams = _streams(out)
    assert streams["audio"] == pytest.approx(streams["video"], abs=0.05)


def test_mkv_with_aac_priming_keeps_every_frame_over_twelve_clips(tmp_path):
    """An ordinary .mkv with AAC sound starts its picture a fraction of a
    frame after the file's start; 24 fps clips of 27 frames used to lose a
    frame each at the 30 fps conversion, so the picture fell behind the
    sound clip by clip."""
    from app.services.renderer import FPS, join_clips_with_audio

    clip = tmp_path / "clip24.mkv"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=gray:s=96x64:r=24:d=1.125",
                    "-f", "lavfi", "-i", "sine=f=1000:d=1.125", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", str(clip)], check=True)
    for transition, expected in (("cut", 12 * 34 / FPS), ("dissolve", 12 * 34 / FPS - 11 * 0.3)):
        out, length, _ = join_clips_with_audio([clip] * 12, tmp_path / f"{transition}.mp4",
                                               transition=transition, crossfade=0.3)
        streams = _streams(out)
        assert streams["video"] == pytest.approx(expected, abs=1 / FPS + 0.01), (transition, streams)
        assert streams["audio"] == pytest.approx(streams["video"], abs=1 / FPS + 0.01), (transition, streams)


def test_clips_off_the_30fps_grid_keep_sound_and_picture_together(tmp_path):
    """24 fps clips whose lengths aren't whole 30 fps frames: each clip is
    laid on the output frame grid once, for picture and sound alike, so
    the two don't drift apart over many joined clips."""
    from app.services.renderer import FPS, _probe_clip, join_clips_with_audio

    frames = [121, 50, 121, 79, 121, 50, 121, 79, 121, 50]  # at 24 fps
    clips = {n: _video(tmp_path / f"c{n}.mp4", seconds=round(n / 24, 4), rate=24, audio=True)
             for n in set(frames)}
    lengths = [round(_probe_clip(clips[n])[0] * FPS) / FPS for n in frames]
    assert all(abs(L * 24 - round(L * 24)) < 1e-3 and abs(L * FPS - round(L * FPS)) > 0.1
               for L in (_probe_clip(c)[0] for c in clips.values()))  # not whole 30 fps frames
    for transition, cf in (("cut", 0.0), ("dissolve", 0.5)):
        out, seconds, _ = join_clips_with_audio([clips[n] for n in frames], tmp_path / f"{transition}.mp4",
                                                transition=transition, crossfade=cf)
        streams = _streams(out)
        expected = sum(lengths) - (len(frames) - 1) * cf
        assert streams["video"] == pytest.approx(expected, abs=1 / FPS + 0.01)
        assert abs(streams["audio"] - streams["video"]) < 1 / FPS + 0.01, (transition, streams)


def test_no_sound_anywhere_means_no_sound_track(tmp_path):
    from app.services.renderer import _probe_size, join_clips_with_audio

    # Odd sides (kept to even ones) and another pixel format; the second
    # clip, another shape, is fitted into the first's frame.
    a = _video(tmp_path / "a.mp4", size=(65, 97), pix_fmt="yuv444p")
    b = _video(tmp_path / "b.mp4", size=(96, 64))
    out, seconds, has_audio = join_clips_with_audio([a, b], tmp_path / "n.mp4", transition="dissolve")
    assert has_audio is False
    assert set(_streams(out)) == {"video"}
    assert _probe_size(out) == (64, 96)
    assert seconds == pytest.approx(3.0 - 0.5, abs=0.1)


def test_unreadable_clip_is_an_error(tmp_path):
    from app.services.renderer import join_clips_with_audio

    a = _video(tmp_path / "a.mp4")
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not a video")
    with pytest.raises(RuntimeError):
        join_clips_with_audio([a, bad], tmp_path / "x.mp4")
    with pytest.raises(ValueError):
        join_clips_with_audio([a], tmp_path / "x.mp4")


# ---------- the API ----------


def _clip_job(path: Path, owner_id: int, *, owner_kind: str = "avatar", status: str = "completed",
              seconds: float = 1.5, output_kind: str = "video_clip", aspect: str = "") -> int:
    """A Studio clip job pointing at a real file."""
    from app import models
    from app.db import SessionLocal

    with SessionLocal() as db:
        job = models.AssetGenerationJob(
            owner_kind=owner_kind, owner_id=owner_id, output_kind=output_kind, prompt="a clip",
            negative_prompt="", reference_asset_ids_json=[], provider="mock", provider_model="mock/video",
            status=status, result_path=str(path), result_duration_seconds=seconds, duration_seconds=seconds,
            aspect_ratio=aspect)
        db.add(job)
        db.commit()
        return job.id


def _ingredient(client) -> int:
    r = client.post("/api/ingredients", json={"name": "Cafe", "kind": "scene"})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _spend_rows() -> int:
    from app import models
    from app.db import SessionLocal

    with SessionLocal() as db:
        return db.query(models.SpendEntry).count()


def test_join_makes_one_new_clip_owned_by_the_first_clips_owner(auth_client, tmp_path, monkeypatch):
    from app.api import studio

    aid = _avatar(auth_client)
    iid = _ingredient(auth_client)
    a = _clip_job(_video(tmp_path / "a.mp4", seconds=2.0, audio=True), aid, seconds=2.0, aspect="16:9")
    b = _clip_job(_video(tmp_path / "b.mp4", seconds=1.5, size=(64, 96)), iid, owner_kind="ingredient")
    spent = _spend_rows()

    def guard():
        raise AssertionError("budget_guard called")

    monkeypatch.setattr(studio, "budget_guard", guard)  # free: the monthly cap doesn't apply
    r = auth_client.post("/api/studio/join-clips", json={"job_ids": [a, b, a], "transition": "dissolve"})
    assert r.status_code == 202, r.text
    assert r.json()["batch_ids"] == [r.json()["id"]]
    # Tests run the queue inline: the worker joined them.
    job = auth_client.get(f"/api/studio/jobs/{r.json()['id']}").json()
    assert job["status"] == "completed", job
    assert (job["owner_kind"], job["owner_id"]) == ("avatar", aid)
    assert (job["output_kind"], job["operation"], job["provider_model"]) == ("video_clip", "join_clips", "local/join")
    assert job["source_job_id"] is None and job["aspect_ratio"] == "16:9"
    assert job["label"] == "Joined 3 clips"
    assert job["options"] == {"job_ids": [a, b, a], "transition": "dissolve", "crossfade": 0.5, "has_audio": True}
    assert (job["result_width"], job["result_height"]) == (96, 64)
    assert job["result_duration_seconds"] == pytest.approx(5.5 - 1.0, abs=0.1)
    assert Path(job["result_path"]).is_file()
    assert _spend_rows() == spent
    # Its file is served like any other clip's.
    assert auth_client.get(f"/api/studio/jobs/{job['id']}/preview").headers["content-type"] == "video/mp4"


def test_join_errors(auth_client, tmp_path):
    aid = _avatar(auth_client)
    a = _clip_job(_video(tmp_path / "a.mp4"), aid)
    url = "/api/studio/join-clips"
    assert auth_client.post(url, json={"job_ids": [a]}).status_code == 422
    assert auth_client.post(url, json={"job_ids": [a] * 13}).status_code == 422
    assert auth_client.post(url, json={"job_ids": [a, a], "transition": "wipe"}).status_code == 422
    assert auth_client.post(url, json={"job_ids": [a, a], "transition": "dissolve",
                                       "crossfade": 2}).status_code == 422
    assert auth_client.post(url, json={"job_ids": [a, 999999]}).status_code == 404
    running = _clip_job(tmp_path / "a.mp4", aid, status="polling")
    assert auth_client.post(url, json={"job_ids": [a, running]}).status_code == 409
    gone = _clip_job(tmp_path / "missing.mp4", aid)
    assert auth_client.post(url, json={"job_ids": [a, gone]}).status_code == 409
    pic = _picture(auth_client, aid)
    r = auth_client.post(url, json={"job_ids": [a, pic["id"]]})
    assert r.status_code == 409 and "picture" in r.json()["detail"]
    long = _clip_job(tmp_path / "a.mp4", aid, seconds=400)
    assert auth_client.post(url, json={"job_ids": [long, long]}).status_code == 422
    # A repeated clip is fine (2 to 12 counts each time it plays).
    assert auth_client.post(url, json={"job_ids": [a, a]}).status_code == 202
    deleted = _clip_job(_video(tmp_path / "d.mp4"), aid)
    auth_client.delete(f"/api/studio/jobs/{deleted}")
    assert auth_client.post(url, json={"job_ids": [a, deleted]}).status_code == 404


def test_cut_without_sound_and_saved_clips_count_as_finished(auth_client, tmp_path):
    aid = _avatar(auth_client)
    a = _clip_job(_video(tmp_path / "a.mp4"), aid, status="saved")
    b = _clip_job(_video(tmp_path / "b.mp4"), aid)
    r = auth_client.post("/api/studio/join-clips", json={"job_ids": [b, a]})
    assert r.status_code == 202, r.text
    job = auth_client.get(f"/api/studio/jobs/{r.json()['id']}").json()
    assert job["status"] == "completed"
    assert job["options"] == {"job_ids": [b, a], "transition": "cut", "crossfade": 0.0, "has_audio": False}
    assert job["result_duration_seconds"] == pytest.approx(3.0, abs=0.1)
    assert set(_streams(Path(job["result_path"]))) == {"video"}


def test_retry_joins_the_same_clips_again_and_409_once_one_is_gone(auth_client, tmp_path):
    aid = _avatar(auth_client)
    a = _clip_job(_video(tmp_path / "a.mp4", audio=True), aid)
    b = _clip_job(_video(tmp_path / "b.mp4"), aid)
    first = auth_client.post("/api/studio/join-clips",
                             json={"job_ids": [a, b], "transition": "dissolve", "crossfade": 0.3}).json()
    r = auth_client.post(f"/api/studio/jobs/{first['id']}/retry", json={"model": "mock/anything", "prompt": "x"})
    assert r.status_code == 202, r.text
    again = auth_client.get(f"/api/studio/jobs/{r.json()['id']}").json()
    assert again["id"] != first["id"] and again["status"] == "completed"
    assert again["operation"] == "join_clips" and again["provider_model"] == "local/join"
    assert again["options"]["job_ids"] == [a, b] and again["options"]["crossfade"] == 0.3
    assert again["prompt"] == first["prompt"] == "Joined 2 clips"
    auth_client.delete(f"/api/studio/jobs/{b}")
    r = auth_client.post(f"/api/studio/jobs/{first['id']}/retry")
    assert r.status_code == 409 and "deleted" in r.json()["detail"]


def test_worker_fails_plainly_when_a_clip_went_missing_before_it_ran(auth_client, tmp_path, monkeypatch):
    from app.api import studio

    aid = _avatar(auth_client)
    pa = _video(tmp_path / "a.mp4")
    a = _clip_job(pa, aid)
    b = _clip_job(_video(tmp_path / "b.mp4"), aid)
    queued: list = []
    monkeypatch.setattr(studio, "enqueue", lambda fn, *args: queued.append((fn, args)) or "q1")
    job = auth_client.post("/api/studio/join-clips", json={"job_ids": [a, b]}).json()
    assert job["status"] == "pending"
    pa.unlink()
    fn, args = queued[0]
    with pytest.raises(ValueError):
        fn(*args)  # the worker records the failure, then raises (RQ logs it)
    job = auth_client.get(f"/api/studio/jobs/{job['id']}").json()
    assert job["status"] == "failed" and "missing" in job["error"]


def test_a_joined_clip_is_never_cast_exempt(auth_client, tmp_path):
    """Even when the clips belong to a Cast character marked "adult content
    allowed": the joined clip, and a frame of it, are new Studio results."""
    from app import models
    from app.db import SessionLocal
    from app.services import pipeline

    aid = _avatar(auth_client)
    a = _clip_job(_video(tmp_path / "a.mp4"), aid)
    joined = auth_client.post("/api/studio/join-clips", json={"job_ids": [a, a]}).json()
    frame = auth_client.post(f"/api/studio/jobs/{joined['id']}/frame-to-picture", json={"at": "first"})
    assert frame.status_code == 201, frame.text
    with SessionLocal() as db:
        for jid in (joined["id"], frame.json()["id"]):
            job = db.get(models.AssetGenerationJob, jid)
            assert pipeline.studio_cast_source(db, job) == (None, None)
            avatar_id, source = pipeline.studio_cast_source(db, job)
            assert pipeline.avatar_adult_ok(avatar_id, source, {aid: frozenset({1, 2, 3})}) is False


def test_a_generated_clip_can_be_joined(auth_client):
    aid = _avatar(auth_client)
    r = auth_client.post("/api/studio/generate-clip", json={"owner_kind": "avatar", "owner_id": aid,
                                                            "prompt": "slow push-in"})
    clip = auth_client.get(f"/api/studio/jobs/{r.json()['id']}").json()
    assert clip["status"] == "completed", clip
    r = auth_client.post("/api/studio/join-clips", json={"job_ids": [clip["id"], clip["id"]]})
    assert r.status_code == 202, r.text
    job = auth_client.get(f"/api/studio/jobs/{r.json()['id']}").json()
    assert job["status"] == "completed", job
    assert (job["result_width"], job["result_height"]) == (clip["result_width"], clip["result_height"])


def test_at_most_two_joins_run_at_once(auth_client, tmp_path):
    """Joins are free but heavy: a third while two are queued or running is
    refused (429), on retry too; once one finishes another may start."""
    from app import models
    from app.db import SessionLocal

    aid = _avatar(auth_client)
    a = _clip_job(_video(tmp_path / "a.mp4"), aid)
    url = "/api/studio/join-clips"
    done = auth_client.post(url, json={"job_ids": [a, a]})
    assert done.status_code == 202
    with SessionLocal() as db:
        db.query(models.AssetGenerationJob).filter(models.AssetGenerationJob.id == done.json()["id"]).update(
            {"status": "pending"})
        busy = models.AssetGenerationJob(
            owner_kind="avatar", owner_id=aid, output_kind="video_clip", prompt="Joined 2 clips",
            negative_prompt="", reference_asset_ids_json=[], provider="local", provider_model="local/join",
            operation="join_clips", options_json={"job_ids": [a, a]}, status="polling")
        db.add(busy)
        db.commit()
        busy_id = busy.id
    r = auth_client.post(url, json={"job_ids": [a, a]})
    assert r.status_code == 429, r.text
    assert "already running" in r.json()["detail"]
    assert auth_client.post(f"/api/studio/jobs/{done.json()['id']}/retry").status_code == 429
    with SessionLocal() as db:
        db.query(models.AssetGenerationJob).filter(models.AssetGenerationJob.id == busy_id).update(
            {"status": "failed"})
        db.commit()
    assert auth_client.post(url, json={"job_ids": [a, a]}).status_code == 202


def test_joined_clip_is_at_most_1080p_and_ffmpeg_is_time_limited(tmp_path, monkeypatch):
    from app.services import renderer

    assert renderer.join_size((3840, 2160)) == (1920, 1080)
    assert renderer.join_size((2160, 3840)) == (1080, 1920)
    assert renderer.join_size((4096, 4096)) == (1080, 1080)
    assert renderer.join_size((65, 97)) == (64, 96)
    assert renderer.join_timeout(600) == 3600
    assert renderer.join_timeout(3) == 120

    big = _video(tmp_path / "big.mp4", size=(2560, 1440), seconds=0.5)
    out, _, _ = renderer.join_clips_with_audio([big, big], tmp_path / "o.mp4")
    assert renderer._probe_size(out) == (1920, 1080)

    seen = {}
    real = renderer._run

    def run(cmd, timeout=None):
        if cmd[0] == "ffmpeg":
            seen["timeout"] = timeout
            return -1, "timed out after 120s"
        return real(cmd, timeout)

    monkeypatch.setattr(renderer, "_run", run)
    with pytest.raises(RuntimeError, match="timed out"):
        renderer.join_clips_with_audio([big, big], tmp_path / "t.mp4")
    assert seen["timeout"] == 120


def _join_row(owner_id: int, clip: int, *, status: str = "pending", age=None) -> int:
    """A join job queued or running, last touched `age` ago (now if None)."""
    from datetime import datetime

    from app import models
    from app.db import SessionLocal

    with SessionLocal() as db:
        job = models.AssetGenerationJob(
            owner_kind="avatar", owner_id=owner_id, output_kind="video_clip", prompt="Joined 2 clips",
            negative_prompt="", reference_asset_ids_json=[], provider="local", provider_model="local/join",
            operation="join_clips", options_json={"job_ids": [clip, clip]}, status=status)
        if age is not None:
            job.created_at = job.updated_at = datetime.utcnow() - age
        db.add(job)
        db.commit()
        return job.id


def _join_rows() -> int:
    from app import models
    from app.db import SessionLocal

    with SessionLocal() as db:
        return db.query(models.AssetGenerationJob).filter(
            models.AssetGenerationJob.operation == "join_clips").count()


def test_two_requests_passing_the_check_together_queue_only_up_to_two(auth_client, tmp_path, monkeypatch):
    """The busy check and the insert aren't one step: a request whose check
    passed while another was being stored is counted again once stored, and
    removed (429) when it is past the cap; nothing of it is queued."""
    from app.api import studio
    from app.services import video_ops

    aid = _avatar(auth_client)
    a = _clip_job(_video(tmp_path / "a.mp4"), aid)
    first = auth_client.post("/api/studio/join-clips", json={"job_ids": [a, a]}).json()
    _join_row(aid, a)
    _join_row(aid, a)  # two others were stored after this request's check
    monkeypatch.setattr(video_ops, "join_busy", lambda db: False)
    queued: list = []
    monkeypatch.setattr(studio, "enqueue", lambda fn, *args: queued.append(args) or "q1")
    rows = _join_rows()
    r = auth_client.post("/api/studio/join-clips", json={"job_ids": [a, a]})
    assert r.status_code == 429, r.text
    assert auth_client.post(f"/api/studio/jobs/{first['id']}/retry").status_code == 429
    assert _join_rows() == rows and queued == []


def test_racing_requests_count_every_join_not_only_lower_ids(auth_client, tmp_path, monkeypatch):
    """On Postgres ids aren't handed out in commit order: a request with a
    lower id may count again after one with a higher id was stored. Every
    join holding a place is counted, whatever its id, so the third is
    refused (both may be, which is safe) and never queued."""
    from app.api import studio
    from app.db import SessionLocal
    from app.services import video_ops

    aid = _avatar(auth_client)
    a = _clip_job(_video(tmp_path / "a.mp4"), aid)
    _join_row(aid, a)  # one join already queued
    monkeypatch.setattr(video_ops, "join_busy", lambda db: False)  # both passed the first check
    queued: list = []
    monkeypatch.setattr(studio, "enqueue", lambda fn, *args: queued.append(args) or "q1")
    real = video_ops.joins_running
    later: list[int] = []

    def recount(db, **kw):
        # The other request, stored after this one (a higher id), commits
        # before this one counts again.
        if not later:
            later.append(_join_row(aid, a))
        return real(db, **kw)

    monkeypatch.setattr(video_ops, "joins_running", recount)
    rows = _join_rows()
    r = auth_client.post("/api/studio/join-clips", json={"job_ids": [a, a]})
    assert r.status_code == 429, r.text
    assert len(later) == 1
    assert _join_rows() == rows + 1 and queued == []  # only the other request's row is left
    with SessionLocal() as db:
        assert real(db) == video_ops.JOIN_MAX_RUNNING  # the other one, counting again, may stay


def test_a_join_orphaned_by_a_killed_worker_stops_holding_a_place(auth_client, tmp_path):
    """Only a join a worker claimed ('submitted') stops holding a place
    after join_stale_after(); one still queued, however long it waits
    behind other work, keeps it."""
    from datetime import timedelta

    from app.db import SessionLocal
    from app.services.video_ops import join_stale_after, joins_running

    aid = _avatar(auth_client)
    a = _clip_job(_video(tmp_path / "a.mp4"), aid)
    assert join_stale_after() == timedelta(minutes=65)
    _join_row(aid, a, status="submitted", age=timedelta(hours=2))  # orphaned
    _join_row(aid, a, status="pending", age=timedelta(hours=3))  # waiting in the queue
    with SessionLocal() as db:
        assert joins_running(db) == 1
    assert auth_client.post("/api/studio/join-clips", json={"job_ids": [a, a]}).status_code == 202  # (runs inline)
    _join_row(aid, a, status="submitted", age=timedelta(minutes=50))  # still running
    with SessionLocal() as db:
        assert joins_running(db) == 2
    assert auth_client.post("/api/studio/join-clips", json={"job_ids": [a, a]}).status_code == 429


def test_sweeper_fails_an_orphaned_join_well_before_the_general_limit(auth_client, tmp_path, monkeypatch):
    from datetime import datetime, timedelta

    from app import models
    from app.db import SessionLocal
    from app.services import sweepers
    from app.workers import queue

    # The queued join's entry still waits (a busy worker, say a 70-minute
    # render): the general pending rule leaves it alone however long it
    # waits, and so does the join rule, which is only for running joins.
    monkeypatch.setattr(queue, "queue_entry_state", lambda qid: "queued")

    aid = _avatar(auth_client)
    a = _clip_job(_video(tmp_path / "a.mp4"), aid)
    orphan = _join_row(aid, a, status="submitted", age=timedelta(minutes=70))
    queued = _join_row(aid, a, status="pending", age=timedelta(minutes=70))
    running = _join_row(aid, a, status="submitted", age=timedelta(minutes=40))
    with SessionLocal() as db:
        other = models.AssetGenerationJob(  # a generated clip, not a join
            owner_kind="avatar", owner_id=aid, output_kind="video_clip", prompt="x", negative_prompt="",
            reference_asset_ids_json=[], provider="mock", provider_model="mock/video", status="submitted",
            created_at=datetime.utcnow() - timedelta(minutes=70),
            updated_at=datetime.utcnow() - timedelta(minutes=70))
        db.add(other)
        db.commit()
        other_id = other.id
    with SessionLocal() as db:
        sweepers.sweep_studio(db)
        status = {j.id: (j.status, j.error) for j in db.query(models.AssetGenerationJob).filter(
            models.AssetGenerationJob.id.in_([orphan, queued, running, other_id]))}
    assert status[orphan] == ("failed", "Stopped unexpectedly")
    assert status[queued][0] == "pending"  # only waiting behind other work: kept, and holds its place
    assert status[running][0] == "submitted"
    from app.services.video_ops import joins_running

    with SessionLocal() as db:
        assert joins_running(db) == 2  # the queued one and the one running
    assert status[other_id][0] == "submitted"  # not a join: the general limit


def test_a_running_command_is_killed_soon_after_should_stop_says_yes():
    """FFmpeg of a cancelled join is stopped within about a second, so the
    join doesn't keep re-encoding after giving up its place."""
    import time

    from app.services.renderer import _run

    asked = []
    t0 = time.monotonic()
    rc, log = _run(["sleep", "30"], should_stop=lambda: asked.append(1) or len(asked) >= 2)
    assert (rc, log) == (-2, "stopped") and time.monotonic() - t0 < 5
    rc, _ = _run(["sleep", "30"], timeout=1.5, should_stop=lambda: False)
    assert rc == -1
    # A check that fails never stops the work.
    rc, _ = _run(["true"], should_stop=lambda: 1 / 0)
    assert rc == 0


def test_cancelling_a_running_join_stops_its_ffmpeg_and_keeps_it_cancelled(auth_client, tmp_path, monkeypatch):
    from app import models
    from app.db import SessionLocal
    from app.services import renderer, video_ops

    aid = _avatar(auth_client)
    a = _clip_job(_video(tmp_path / "a.mp4", seconds=1.5), aid)
    b = _clip_job(_video(tmp_path / "b.mp4", seconds=1.5), aid)
    seen = {}

    def fake_join(clips, dst, *, transition="cut", crossfade=0.5, should_stop=None):
        with SessionLocal() as db:  # the user cancels while FFmpeg runs
            db.query(models.AssetGenerationJob).filter(
                models.AssetGenerationJob.operation == video_ops.JOIN_OP).update(
                {"status": "failed", "error": "Cancelled"}, synchronize_session=False)
            db.commit()
        seen["stop"] = should_stop()
        raise renderer.JoinStopped("stopped")

    monkeypatch.setattr(renderer, "join_clips_with_audio", fake_join)
    r = auth_client.post("/api/studio/join-clips", json={"job_ids": [a, b]})
    assert r.status_code == 202, r.text
    assert seen["stop"] is True
    job = auth_client.get(f"/api/studio/jobs/{r.json()['id']}").json()
    assert job["status"] == "failed" and job["error"] == "Cancelled"
