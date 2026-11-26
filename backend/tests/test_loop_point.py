"""Loop-point finder (services/loop_point.py): the stretch of a loop clip
whose end matches its start best, looped instead of the whole clip, with
the video opening on the clip's own first frames. Synthetic ffmpeg clips
and mock providers."""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from .test_ambient import SHORT, _assert_linear_dissolve, _duration, _raw_yavg, _slow_drift_clip
from .test_loop_health import _replace

ENC = ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast", "-crf", "18"]


def repeating(path: Path, *, seconds: float = 30.0, period: float = 10.0, size: str = "320x180",
              rate: int = 24) -> Path:
    """A box circling once every `period` seconds; the last 3 s are a
    different (red) picture, so the whole clip's own join jumps."""
    w, h = (int(x) for x in size.split("x"))
    box = max(8, w // 8)
    subprocess.run([
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", f"color=c=0x336699:s={size}:d={seconds}:r={rate}",
        "-f", "lavfi", "-i", f"color=c=0xEEDD22:s={box}x{box}:d={seconds}:r={rate}",
        "-filter_complex",
        f"[0][1]overlay=x='{w / 2 - box / 2}+{w / 4}*sin(2*PI*t/{period})':"
        f"y='{h / 2 - box / 2}+{h / 4}*cos(2*PI*t/{period})',"
        f"drawbox=x=0:y=0:w=iw:h=ih:color=0xCC3311:t=fill:enable='gte(t,{seconds - 3})'",
        "-t", str(seconds), *ENC, "-r", str(rate), str(path)], check=True)
    return path


def drifting_gradient(path: Path, seconds: float = 30.0) -> Path:
    """A picture that keeps brightening and never comes back: no stretch
    of it loops."""
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"nullsrc=s=320x180:r=24:d={seconds},"
                    f"geq=lum='60+150*T/{seconds}+20*sin(2*PI*X/W)':cb=128:cr=128",
                    *ENC, str(path)], check=True)
    return path


def stripes(path: Path, rate: int, seconds: float = 4.3) -> Path:
    """Stripes drifting sideways at a steady speed; the clip stops part-way
    through a cycle, so its join jumps by the same amount at any rate."""
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"nullsrc=s=320x180:r={rate}:d={seconds},"
                    "geq=lum='128+60*sin(2*PI*(4*X/W-T/2))':cb=128:cr=128",
                    *ENC, "-r", str(rate), str(path)], check=True)
    return path


# ---------- the finder ----------


def test_a_repeating_clip_loops_on_a_whole_number_of_periods(tmp_path):
    from app.services import loop_health as lh
    from app.services import loop_point as lp

    clip = repeating(tmp_path / "rep.mp4")
    assert lh.measure(clip)["seam"]["verdict"] == "visible_jump"  # control: the whole clip jumps
    r = lp.find_loop_point(clip)
    span = r["out"] - r["in"]
    assert span >= lp.default_min_loop(30.0) - 1e-6
    assert abs(span - 10 * round(span / 10)) <= 1 / 24 + 1e-3, r
    assert r["out"] <= 27.0  # never into the mismatched end
    assert r["ratio"] < lh.SEAM_SEAMLESS_RATIO and r["verdict"] == "seamless" and r["found"] is True
    assert r["raw_ratio"] > lh.SEAM_SLIGHT_RATIO
    assert r["mismatch_ok"] is True and r["mismatch"] is not None and r["mismatch"] <= lp.MISMATCH_MAX
    assert r["snap"] is False and r["usable_end"] is None
    assert r["crossfade"] in (0.5, 1.5)  # seamless, +1 s when the join is calm
    assert r["crossfade"] <= 2 * min(r["in"], 30.0 - r["out"]) + 1e-6


def test_a_clip_that_never_repeats_finds_nothing(tmp_path):
    from app.services import loop_point as lp

    r = lp.find_loop_point(drifting_gradient(tmp_path / "drift.mp4"))
    assert r["ratio"] > 6 and r["verdict"] == "visible_jump" and r["found"] is False


def test_the_crossfade_rule():
    from app.services import loop_point as lp

    assert lp.crossfade_for(2.0, False) == 0.5 and lp.crossfade_for(2.0, True) == 1.5
    assert lp.crossfade_for(3.5, False) == 1.0 and lp.crossfade_for(5.9, False) == 2.0
    assert lp.crossfade_for(5.9, True) == 3.0 and lp.crossfade_for(6.1, False) is None


def two_pictures(path: Path, seconds: float = 12.0) -> Path:
    """Stripes for the first half, the same stripes shifted half a period
    for the second: a dissolve from one half into the other overlays two
    different pictures (a ghost)."""
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"nullsrc=s=320x180:r=24:d={seconds},"
                    f"geq=lum='128+90*sin(2*PI*(X/16+if(gte(T,{seconds / 2}),0.5,0)))':cb=128:cr=128",
                    *ENC, str(path)], check=True)
    return path


def test_the_blend_mismatch_reads_two_different_pictures_as_a_ghost(tmp_path):
    """The dissolve's two sources, frame by frame (the host's reading: clean
    loop blends 20-27, ghost ones 34-50 at 320 px grey): the same picture
    reads ~0, stripes in each other's gaps read far above MISMATCH_MAX."""
    from app.services import loop_point as lp

    clip = two_pictures(tmp_path / "two.mp4")
    size = (320, 180)
    same = lp.blend_mismatch(clip, 24, 96, 6, 24.0, size)  # 1 s and 4 s: both in the first half
    ghost = lp.blend_mismatch(clip, 24, 240, 6, 24.0, size)  # 1 s against 10 s: the shifted half
    assert same is not None and same < 5
    assert ghost is not None and ghost > lp.MISMATCH_MAX


def test_a_pair_whose_dissolve_mixes_two_pictures_is_not_used(tmp_path, monkeypatch):
    """A candidate whose dissolve would overlay two different pictures is
    rejected (found False, mismatch_ok False), whatever its join ratio."""
    from app.services import loop_point as lp

    clip = repeating(tmp_path / "rep.mp4")
    monkeypatch.setattr(lp, "blend_mismatch", lambda *a, **k: 45.0)
    r = lp.find_loop_point(clip)
    assert r["mismatch"] == 45.0 and r["mismatch_ok"] is False and r["found"] is False


def snapping(path: Path, *, seconds: float = 12.0, period: float = 4.0, rate: int = 24) -> Path:
    """A full-frame texture swaying once every `period` seconds whose last
    two frames jump to where frame 0 shows it (a take that snaps to its
    last-frame target): the whole clip's join lands, its tail jumps."""
    from .test_loop_health import TEXTURE

    n = int(seconds * rate)
    t = f"if(gte(n,{n - 2}),0,t)"  # inside the quoted crop option: commas need no escape
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", TEXTURE.format(d=seconds),
                    "-vf", f"crop=320:180:x='160+120*sin(2*PI*{t}/{period}+0.9)':y=0",
                    "-t", str(seconds), *ENC, "-r", str(rate), str(path)], check=True)
    return path


def test_a_snapping_take_loops_clear_of_its_snapping_tail(tmp_path):
    """On a take that snaps, the search never ends the loop in, or blends
    over, the last SNAP_BACK + 1 frames."""
    from app.services import loop_health as lh
    from app.services import loop_point as lp

    clip = snapping(tmp_path / "snap.mp4")
    assert lh.measure(clip)["seam"]["verdict"] == "snap"  # control: the whole clip snaps
    r = lp.find_loop_point(clip, snap=True)
    n = 12 * 24
    assert r["snap"] is True and r["usable_end"] == pytest.approx((n - lh.SNAP_BACK - 1) / 24, abs=1e-3)
    assert r["found"] is True, r
    assert r["out"] + r["crossfade"] / 2 <= r["usable_end"] + 1e-6
    # The render's cut respects it too: the unit and its dissolve end before the tail.
    from app.services.renderer import loop_frames

    frames = loop_frames(12.0, r["in"], r["out"], r["crossfade"], fps=24, usable_end=r["usable_end"])
    assert frames is not None
    _a, b, h = frames
    assert b + h <= n - lh.SNAP_BACK - 1


def test_loop_frames_keeps_the_dissolve_out_of_a_snapping_tail():
    from app.services.renderer import loop_frames

    # A 10 s clip at 24 fps, stretch 2-9.9 s with a 2 s dissolve: the
    # dissolve's far side would reach frame 262; a usable end at 9.8 s
    # (frame 235) leaves room for a quarter-second half at most.
    a, b, h = loop_frames(10.0, 2.0, 9.6, 2.0, fps=24)
    assert b + h > 235
    a, b, h = loop_frames(10.0, 2.0, 9.6, 2.0, fps=24, usable_end=9.8)
    assert b + h <= 235


def test_ratios_do_not_depend_on_the_frame_rate(tmp_path):
    from app.services import loop_health as lh
    from app.services import loop_point as lp

    # The finder's ratios are scaled to REFERENCE_FPS (loop_health's seam
    # verdict isn't: its thresholds were calibrated on takes as measured).
    stripes(tmp_path / "s25.mp4", 25)
    stripes(tmp_path / "s50.mp4", 50)
    assert lh.REFERENCE_FPS == 24.0
    f25 = lp.find_loop_point(tmp_path / "s25.mp4")
    f50 = lp.find_loop_point(tmp_path / "s50.mp4")
    assert f50["raw_ratio"] == pytest.approx(f25["raw_ratio"], rel=0.15), (f25, f50)


def test_the_search_is_quick(tmp_path):
    from app.services import loop_point as lp

    clip = repeating(tmp_path / "hd.mp4", size="1280x720", rate=30)
    t = time.monotonic()
    r = lp.find_loop_point(clip)
    assert time.monotonic() - t < 30, r
    assert r["coarse_seconds"] < 15, r


# ---------- the render ----------


def test_the_loop_unit_dissolves_centred_on_the_join_and_unbiased(tmp_path):
    from app.services.renderer import loop_unit_graph

    clip = tmp_path / "drift.mp4"
    _slow_drift_clip(clip, seconds=6, start=0)
    src = _raw_yavg([clip], "[0:v]null[o]", "o")
    a, b, h = 45, 135, 15
    unit = _raw_yavg([clip], loop_unit_graph(start_frame=a, end_frame=b, half=h, out="o"), "o")
    assert len(unit) == b - a
    mid = b - a - 2 * h
    # unit = src[a+h : b-h] ++ dissolve(src[b-h : b+h] → src[a-h : a+h])
    assert all(abs(unit[i] - src[a + h + i]) < 0.05 for i in range(mid))
    _assert_linear_dissolve(unit[mid:], src[b - h:b + h], src[a - h:a + h], "loop-point unit")


def test_compose_with_a_loop_point_opens_on_the_clip_and_lasts_exactly(tmp_path):
    from app.services.renderer import FPS, AmbientRenderInputs, compose_ambient

    clip = tmp_path / "drift.mp4"
    _slow_drift_clip(clip, seconds=10, start=0)
    total = 13.0
    out = compose_ambient(AmbientRenderInputs(
        project_id=0, out_dir=tmp_path / "out", clip_path=clip, total_seconds=total,
        crossfade_seconds=1.0, fade_seconds=0.0, loop_in=2.0, loop_out=7.0, intro=True))
    assert abs(_duration(out.final_video_path) - total) <= 1.5 / FPS
    assert out.unit_seconds == pytest.approx(5.0, abs=1.5 / FPS)
    scale = "scale=320:180,setsar=1"
    final = _raw_yavg([out.final_video_path], f"[0:v]{scale}[o]", "o")
    src = _raw_yavg([clip], f"[0:v]{scale}[o]", "o")
    assert len(final) == pytest.approx(total * FPS, abs=1)
    # The video opens on the clip's own first frame (the photo) ...
    assert abs(final[0] - src[0]) < 1.0, (final[0], src[0])
    # ... plays the clip's opening up to the unit's start (2 s + half the
    # blend), then the unit carries on from exactly there.
    k = int(round((2.0 + 0.5) * FPS))
    assert abs(final[k - 1] - src[k - 1]) < 1.0 and abs(final[k] - src[k]) < 1.0
    # One unit later the loop has wrapped back onto the same frame.
    assert abs(final[k + 5 * FPS] - src[k]) < 1.5


def test_compose_ignores_a_loop_point_with_variations(tmp_path):
    from app.services.renderer import AmbientRenderInputs, compose_ambient

    a, b = tmp_path / "a.mp4", tmp_path / "b.mp4"
    _slow_drift_clip(a, seconds=4, start=0)
    _slow_drift_clip(b, seconds=4, start=4)
    out = compose_ambient(AmbientRenderInputs(
        project_id=0, out_dir=tmp_path / "out", clip_path=a, extra_clips=[b], variety="takes",
        total_seconds=10, crossfade_seconds=1.0, fade_seconds=0.0, loop_in=1.0, loop_out=2.5, intro=True))
    assert out.unit_seconds == pytest.approx(3.0, abs=0.1)  # the whole clip less the blend
    assert not (tmp_path / "out" / "ambient_intro.mp4").exists()


# ---------- the scene ----------


def _scene(client, **cfg) -> dict:
    r = client.post("/api/ambient", json={"config": {**SHORT, "music_mood": "none", **cfg}, "generate": True})
    assert r.status_code == 201, r.text
    scene = r.json()
    assert scene["loop_status"] == "completed", scene
    return scene


def test_find_a_better_loop_point_then_render_it_then_use_the_whole_clip(auth_client):
    scene = _scene(auth_client)
    sid = scene["id"]
    _replace(Path(scene["loop_shot"]["clip_path"]), repeating)
    assert auth_client.post(f"/api/ambient/{sid}/loop-health").status_code == 200
    before = auth_client.get(f"/api/ambient/{sid}").json()
    assert before["loop_point"] is None and before["seam_treatment"]["verdict"] == "visible_jump"
    r = auth_client.post(f"/api/ambient/{sid}/find-loop-point")
    assert r.status_code == 202, r.text
    out = auth_client.get(f"/api/ambient/{sid}").json()
    assert out["status"] == before["status"]  # a search never claims the scene
    assert out["loop_point_state"]["status"] == "done" and out["loop_point_state"]["found"] is True
    lp = out["loop_point"]
    assert lp["verdict"] == "seamless" and lp["off"] is False and lp["intro"] is True and lp["applies"]
    assert abs(lp["loop_seconds"] - 20.0) <= 0.05
    st = out["seam_treatment"]
    assert st["loop_point"] is True and st["verdict"] == "seamless" and st["covers"] is True
    assert out["plan"]["unit_seconds"] == pytest.approx(lp["loop_seconds"], abs=0.01)
    # The seam card describes the stretch, and its preview plays it.
    health = out["loop_health"]
    assert health["seam"]["level"] == "good" and "The render loops" in health["seam"]["text"]
    assert health["seam"]["raw_level"] == "bad"
    p = auth_client.get(health["seam_preview_url"])
    assert p.status_code == 200 and float(p.headers["x-join-at"]) == pytest.approx(3.0, abs=0.05)

    # The render loops the stretch, with no question about the whole clip's jump.
    r = auth_client.post(f"/api/ambient/{sid}/render")
    assert r.status_code == 202, r.text
    log = auth_client.get(f"/api/ambient/{sid}").json()["latest_render"]["render_log"]
    assert "seam=seamless" in log and f"loop={lp['in_seconds']:g}-{lp['out_seconds']:g}s" in log

    # Use the whole clip: back to the whole clip's (jumping) join.
    r = auth_client.delete(f"/api/ambient/{sid}/find-loop-point")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["loop_point"]["off"] is True
    assert out["seam_treatment"]["loop_point"] is False and out["seam_treatment"]["verdict"] == "visible_jump"
    assert auth_client.post(f"/api/ambient/{sid}/render").status_code == 409
    # Finding again switches it back on.
    assert auth_client.post(f"/api/ambient/{sid}/find-loop-point").status_code == 202
    assert auth_client.get(f"/api/ambient/{sid}").json()["loop_point"]["off"] is False


def test_no_loop_point_in_a_clip_that_never_repeats(auth_client):
    scene = _scene(auth_client)
    sid = scene["id"]
    _replace(Path(scene["loop_shot"]["clip_path"]), drifting_gradient)
    status = auth_client.get(f"/api/ambient/{sid}").json()["status"]
    assert auth_client.post(f"/api/ambient/{sid}/find-loop-point").status_code == 202
    out = auth_client.get(f"/api/ambient/{sid}").json()
    assert out["status"] == status
    assert out["loop_point"] is None
    assert out["loop_point_state"]["found"] is False and "No better loop point" in out["loop_point_state"]["message"]
    assert auth_client.post(f"/api/ambient/{sid}/loop-health").status_code == 200
    assert auth_client.get(f"/api/ambient/{sid}").json()["seam_treatment"]["verdict"] == "visible_jump"


def test_find_loop_point_refusals(auth_client):
    from app import models
    from app.db import SessionLocal

    drift = _scene(auth_client, source="image_drift")
    assert auth_client.post(f"/api/ambient/{drift['id']}/find-loop-point").status_code == 409
    takes = _scene(auth_client, variety="takes", variations=2)
    r = auth_client.post(f"/api/ambient/{takes['id']}/find-loop-point")
    assert r.status_code == 409 and "single-clip" in r.json()["detail"]
    assert any("single-clip" in n for n in takes["plan"]["notes"])
    scene = _scene(auth_client)
    db = SessionLocal()
    try:
        db.get(models.VideoProject, scene["id"]).status = "generating"
        db.commit()
    finally:
        db.close()
    assert auth_client.post(f"/api/ambient/{scene['id']}/find-loop-point").status_code == 409


def test_a_new_loop_that_jumps_gets_a_loop_point_by_itself(auth_client, monkeypatch, tmp_path):
    """After generating, a join that isn't seamless starts a search; a
    stretch that beats the whole clip is kept and the render uses it."""
    from app.services import loop_health, loop_point

    rep = repeating(tmp_path / "rep.mp4")
    real_measure = loop_health.measure
    monkeypatch.setattr(loop_health, "measure", lambda clip, still=False, **kw: real_measure(rep, still=still, **kw))
    seen = []
    real_find = loop_point.find_loop_point

    def spy(clip, min_loop=None, *, snap=False):
        seen.append(clip)
        return real_find(rep, min_loop, snap=snap)

    monkeypatch.setattr(loop_point, "find_loop_point", spy)
    scene = _scene(auth_client)
    assert len(seen) == 1
    lp = scene["loop_point"]
    assert lp is not None and lp["verdict"] == "seamless" and lp["intro"] is True
    assert scene["seam_treatment"]["loop_point"] is True and scene["seam_treatment"]["verdict"] == "seamless"
    # A clean loop isn't searched.
    seen.clear()
    monkeypatch.setattr(loop_health, "measure", real_measure)
    assert _scene(auth_client)["loop_point"] is None and seen == []


def test_the_scene_says_whether_a_loop_point_applies(auth_client):
    """The page hides "Find a better loop point" where the API would refuse
    it (variations), before any stretch has been found."""
    assert _scene(auth_client)["loop_point_applies"] is True
    assert _scene(auth_client, variety="takes", variations=2)["loop_point_applies"] is False
    assert _scene(auth_client, source="image_drift")["loop_point_applies"] is False


def test_a_search_whose_worker_died_reads_failed_and_can_run_again(auth_client):
    from datetime import datetime, timedelta, timezone

    from app import models
    from app.db import SessionLocal
    from app.services import ambient

    scene = _scene(auth_client)
    sid = scene["id"]
    old = (datetime.now(timezone.utc) - timedelta(seconds=ambient.LOOP_POINT_STALE_SECONDS + 5)).isoformat()
    db = SessionLocal()
    try:
        p = db.get(models.VideoProject, sid)
        ambient._set_state(p, ambient._LOOP_POINT_KEY, {"status": "running", "started_at": old})
        db.commit()
    finally:
        db.close()
    st = auth_client.get(f"/api/ambient/{sid}").json()["loop_point_state"]
    assert st["status"] == "failed" and st["message"]  # the page stops showing "Looking…"
    assert auth_client.post(f"/api/ambient/{sid}/find-loop-point").status_code == 202
    assert auth_client.get(f"/api/ambient/{sid}").json()["loop_point_state"]["status"] == "done"


def test_a_running_search_turns_away_a_second_one(auth_client):
    from datetime import datetime, timezone

    from app import models
    from app.db import SessionLocal
    from app.services import ambient

    scene = _scene(auth_client)
    sid = scene["id"]
    db = SessionLocal()
    try:
        p = db.get(models.VideoProject, sid)
        ambient._set_state(p, ambient._LOOP_POINT_KEY,
                           {"status": "running", "started_at": datetime.now(timezone.utc).isoformat()})
        db.commit()
    finally:
        db.close()
    out = auth_client.get(f"/api/ambient/{sid}").json()
    assert out["loop_point_state"]["status"] == "running"
    r = auth_client.post(f"/api/ambient/{sid}/find-loop-point")
    assert r.status_code == 409 and "Already" in r.json()["detail"]
    assert auth_client.get(f"/api/ambient/{sid}").json()["status"] == out["status"]


def test_a_new_loop_that_snaps_is_searched_clear_of_its_tail(auth_client, monkeypatch, tmp_path):
    """The host's 11-16 correction: a snap take (its join alone reads
    seamless) also starts a search, told to keep clear of the snapping
    tail; the stretch found is kept even when its own join ratio is no
    better than the whole clip's, and the render's treatment then comes
    from it (so the render doesn't ask)."""
    from app.services import loop_health, loop_point

    snap = snapping(tmp_path / "snap.mp4")
    real_measure = loop_health.measure
    monkeypatch.setattr(loop_health, "measure", lambda clip, still=False, **kw: real_measure(snap, still=still, **kw))
    calls = []
    real_find = loop_point.find_loop_point

    def spy(clip, min_loop=None, *, snap=False):
        calls.append(snap)
        return real_find(tmp_path / "snap.mp4", min_loop, snap=snap)

    monkeypatch.setattr(loop_point, "find_loop_point", spy)
    scene = _scene(auth_client)
    assert calls == [True]
    lp = scene["loop_point"]
    assert lp is not None and lp["snap"] is True, scene
    st = scene["seam_treatment"]
    assert st["loop_point"] is True and st["verdict"] != "snap"


@pytest.mark.parametrize("mode, expected", [("auto", 1.0), ("blend", 2.0)])
def test_the_blend_stays_within_the_width_the_ghost_check_looked_at(auth_client, monkeypatch, mode, expected):
    """A stretch checked for ghosting over a 1 s dissolve isn't rendered with
    the scene's 2 s default: the outer second mixes frames the check never
    compared. A blend the person chose (seam_mode "blend") is theirs."""
    from app import models
    from app.db import SessionLocal
    from app.services import ambient

    from .test_ambient import SHORT

    sid = auth_client.post("/api/ambient", json={"config": {**SHORT, "crossfade_seconds": 2.0, "seam_mode": mode},
                                                 "generate": False}).json()["id"]
    lp = {"in": 2.0, "out": 9.0, "ratio": 3.0, "crossfade": 1.0, "checked_blend": 1.0,
          "mismatch": 22.0, "mismatch_ok": True}
    monkeypatch.setattr(ambient, "active_loop_point", lambda p: lp)
    monkeypatch.setattr(ambient, "_loop_room", lambda lp, cfg: 10.0)
    monkeypatch.setattr(ambient, "_loop_seconds", lambda lp, cfg: 7.0)
    db = SessionLocal()
    try:
        t = ambient.seam_treatment(db.get(models.VideoProject, sid))
    finally:
        db.close()
    assert t["loop_point"] is True and t["blend_seconds"] == expected


def test_a_usable_pair_beats_a_calm_one_past_the_crossfade_rule():
    """Calm 6.1 x 0.85 < 5.5, but 6.1 is past the rule (nothing found): the
    usable 5.5 pair must be picked."""
    from app.services import loop_point as lp

    calm_past = {"ratio": 6.1, "calm": True, "mismatch_ok": True}
    usable = {"ratio": 5.5, "calm": False, "mismatch_ok": True}
    assert lp.crossfade_for(6.1, True) is None and lp.crossfade_for(5.5, False) is not None
    assert min([calm_past, usable], key=lp._rank) is usable


def test_a_seamless_loop_point_keeps_a_zero_blend(auth_client, monkeypatch):
    """crossfade_seconds 0 means no blend: a seamless found stretch doesn't
    switch a dissolve on by itself (as for the whole clip)."""
    from app import models
    from app.db import SessionLocal
    from app.services import ambient

    from .test_ambient import SHORT

    sid = auth_client.post("/api/ambient", json={"config": {**SHORT, "crossfade_seconds": 0.0},
                                                 "generate": False}).json()["id"]
    lp = {"in": 2.0, "out": 9.0, "ratio": 1.0, "crossfade": 0.5, "checked_blend": 0.5,
          "mismatch": 12.0, "mismatch_ok": True}
    monkeypatch.setattr(ambient, "active_loop_point", lambda p: lp)
    monkeypatch.setattr(ambient, "_loop_room", lambda lp, cfg: 10.0)
    monkeypatch.setattr(ambient, "_loop_seconds", lambda lp, cfg: 7.0)
    db = SessionLocal()
    try:
        t = ambient.seam_treatment(db.get(models.VideoProject, sid))
    finally:
        db.close()
    assert t["verdict"] == "seamless" and t["blend_seconds"] == 0.0


# ---------- host review of #64: picture length, fail-closed check, guards, the queue ----------


def with_sound_past(clip: Path, extra: float = 0.08) -> Path:
    """`clip` with an AAC track `extra` seconds longer than the picture (as
    Wan's downloads: 69-80 ms), so the container outlasts the video."""
    out = clip.with_name(clip.stem + "_aac.mp4")
    seconds = float(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                    "stream=duration", "-of", "csv=p=0", str(clip)],
                                   capture_output=True, text=True, check=True).stdout.strip())
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(clip), "-f", "lavfi", "-i",
                    f"sine=frequency=220:duration={seconds + extra}", "-map", "0:v", "-map", "1:a",
                    "-c:v", "copy", "-c:a", "aac", str(out)], check=True)
    return out


def test_frames_are_counted_from_the_picture_not_the_container(tmp_path):
    """A sound track past the picture made the container longer: the finder
    counted frames that don't exist, so the snap guard was off by those
    frames (and a mismatch read past the end got nothing, which passed)."""
    from app.services import loop_health as lh
    from app.services import loop_point as lp
    from app.services.renderer import _probe_duration

    clip = with_sound_past(snapping(tmp_path / "snap.mp4"))
    assert _probe_duration(clip) > 12.05  # the container counts the sound
    assert lp.picture_seconds(clip) == pytest.approx(12.0, abs=1e-3)
    r = lp.find_loop_point(clip, snap=True)
    n = 12 * 24  # the picture's frames
    assert r["clip_seconds"] == pytest.approx(12.0, abs=1e-3)
    assert r["usable_end"] == pytest.approx((n - lh.SNAP_BACK - 1) / 24, abs=1e-3)


def test_a_mismatch_that_cannot_be_read_fails_closed(tmp_path, monkeypatch):
    """Frames the check can't read (past the picture's end) no longer count
    as a pass: the stretch isn't used."""
    from app.services import loop_point as lp

    clip = with_sound_past(repeating(tmp_path / "rep.mp4"))
    # Past the last picture frame the check reads nothing, not a stale frame.
    assert lp.blend_mismatch(clip, 24, 30 * 24 + 2, 6, 24.0, (320, 180)) is None
    monkeypatch.setattr(lp, "blend_mismatch", lambda *a, **k: None)
    r = lp.find_loop_point(clip)
    assert r["mismatch"] is None and r["mismatch_ok"] is False and r["found"] is False


def test_the_seam_preview_cuts_like_the_render(tmp_path, monkeypatch):
    """The preview of a found stretch counts the picture's frames and leaves
    a snapping tail out, as the render does."""
    from app.services import loop_health as lh
    from app.services import renderer

    clip = with_sound_past(repeating(tmp_path / "rep.mp4"))
    seen = []
    real = renderer.loop_frames

    def spy(length, a, b, cf, **kw):
        seen.append((length, kw.get("usable_end")))
        return real(length, a, b, cf, **kw)

    monkeypatch.setattr(renderer, "loop_frames", spy)
    path, join_at = lh.seam_preview(clip, loop=(2.0, 22.0), blend=1.0, usable_end=29.5)
    assert path.exists() and join_at > 0
    assert seen and seen[0][0] == pytest.approx(30.0, abs=1e-3) and seen[0][1] == 29.5


def test_the_frames_read_in_one_pass_are_the_frames_a_seek_reads(tmp_path):
    """One decode pass (select by frame number) instead of a keyframe seek
    per sample: the same pixels, frame for frame."""
    from app.services import loop_health as lh
    from app.services import loop_point as lp

    clip = tmp_path / "gop.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=4",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "48", str(clip)], check=True)
    size = (160, 90)

    def seek(i: int):
        start = max(0.0, (i - 0.5) / 30)
        return lh._read_frames(["-ss", f"{start:.4f}", "-t", f"{1.5 / 30:.4f}", "-i", str(clip)], "", size)[0]

    idx = [0, 5, 17, 47, 48, 49, 90, 119]
    got = lp._frames_at(clip, idx, size)
    assert sorted(got) == idx
    assert all(got[i].tobytes() == seek(i).tobytes() for i in idx)
    assert lp._frames_at(clip, [500], size) == {}


def test_find_measures_first_so_a_snapping_take_keeps_its_guard(auth_client, monkeypatch):
    """A Find with no current reading (a new take) measures first, as the
    render does: a snapping take is searched clear of its tail."""
    from app.services import loop_point

    scene = _scene(auth_client)
    sid = scene["id"]
    _replace(Path(scene["loop_shot"]["clip_path"]), snapping)  # the reading is stale now
    calls = []
    real_find = loop_point.find_loop_point

    def spy(clip, min_loop=None, *, snap=False):
        calls.append(snap)
        return real_find(clip, min_loop, snap=snap)

    monkeypatch.setattr(loop_point, "find_loop_point", spy)
    assert auth_client.post(f"/api/ambient/{sid}/find-loop-point").status_code == 202
    assert calls == [True]


def test_find_leaves_a_seamless_clip_whole(auth_client, monkeypatch):
    """As after generation: a clip whose own join is seamless isn't trimmed
    by a manual Find (a trim made such joins worse)."""
    from app.services import loop_point

    scene = _scene(auth_client)
    sid = scene["id"]
    assert auth_client.post(f"/api/ambient/{sid}/loop-health").status_code == 200
    assert auth_client.get(f"/api/ambient/{sid}").json()["seam_treatment"]["verdict"] == "seamless"
    calls = []
    monkeypatch.setattr(loop_point, "find_loop_point", lambda *a, **k: calls.append(1))
    assert auth_client.post(f"/api/ambient/{sid}/find-loop-point").status_code == 202
    out = auth_client.get(f"/api/ambient/{sid}").json()
    assert calls == [] and out["loop_point"] is None
    st = out["loop_point_state"]
    assert st["status"] == "done" and st["found"] is False and "joins seamlessly" in st["message"]


def _running_state(sid: int, **extra) -> None:
    from datetime import datetime, timedelta, timezone

    from app import models
    from app.db import SessionLocal
    from app.services import ambient

    started = datetime.now(timezone.utc) - timedelta(seconds=extra.pop("age", 0))
    db = SessionLocal()
    try:
        p = db.get(models.VideoProject, sid)
        ambient._set_state(p, ambient._LOOP_POINT_KEY,
                           {"status": "running", "started_at": started.isoformat(), **extra})
        db.commit()
    finally:
        db.close()


def test_a_search_still_queued_is_not_read_as_failed_or_queued_twice(auth_client, monkeypatch):
    """The queue is asked (as the sweepers do): a search waiting behind a
    long render past the stale age still reads "running", and a second click
    doesn't queue a duplicate; one whose entry is gone reads failed at once."""
    from app.services import ambient
    from app.workers import queue

    sid = _scene(auth_client)["id"]
    _running_state(sid, age=ambient.LOOP_POINT_STALE_SECONDS + 60, job_id="rq-search")
    monkeypatch.setattr(queue, "queue_entry_state", lambda qid: "alive" if qid == "rq-search" else "gone")
    assert auth_client.get(f"/api/ambient/{sid}").json()["loop_point_state"]["status"] == "running"
    r = auth_client.post(f"/api/ambient/{sid}/find-loop-point")
    assert r.status_code == 409 and "Already" in r.json()["detail"]

    _running_state(sid, job_id="rq-dead")  # young, but its worker is gone
    st = auth_client.get(f"/api/ambient/{sid}").json()["loop_point_state"]
    assert st["status"] == "failed"

    monkeypatch.setattr(queue, "queue_entry_state", lambda qid: "unknown")  # Redis didn't answer: the age rule
    _running_state(sid, job_id="rq-x")
    assert auth_client.get(f"/api/ambient/{sid}").json()["loop_point_state"]["status"] == "running"


def test_the_search_keeps_its_queue_id(auth_client):
    from app import models
    from app.db import SessionLocal
    from app.services import ambient

    sid = _scene(auth_client)["id"]
    _running_state(sid)
    db = SessionLocal()
    try:
        p = db.get(models.VideoProject, sid)
        ambient.note_loop_point_job(db, p, "rq-123")
        db.refresh(p)
        assert (p.ambient_config_json or {})[ambient._LOOP_POINT_KEY]["job_id"] == "rq-123"
    finally:
        db.close()


def test_the_checked_blend_is_kept_with_the_stretch():
    from app.services import ambient

    class Shot:
        clip_path = __file__

    entry = ambient._loop_entry({"in": 1.0, "out": 9.0, "checked_blend": 1.0, "crossfade": 1.0}, Shot())
    assert entry["checked_blend"] == 1.0


def test_find_whose_measurement_fails_again_does_not_search_blind(auth_client, monkeypatch):
    """No current reading and the Find's own measurement fails too (a read
    timeout, say): without a reading there is no snap guard, so the search
    ends failed instead of searching a possibly snapping take blind."""
    from app.services import loop_health, loop_point

    scene = _scene(auth_client)
    sid = scene["id"]
    _replace(Path(scene["loop_shot"]["clip_path"]), snapping)  # the reading is stale now

    def broken(*a, **k):
        raise TimeoutError("the measurement ran out of time")

    monkeypatch.setattr(loop_health, "measure", broken)
    calls = []
    monkeypatch.setattr(loop_point, "find_loop_point", lambda *a, **k: calls.append(1))
    assert auth_client.post(f"/api/ambient/{sid}/find-loop-point").status_code == 202
    out = auth_client.get(f"/api/ambient/{sid}").json()
    st = out["loop_point_state"]
    assert calls == [] and out["loop_point"] is None
    assert st["status"] == "failed" and "Couldn't measure the loop clip's join" in st["message"]


def test_the_seam_preview_route_passes_the_stretchs_usable_end(auth_client, monkeypatch, tmp_path):
    """GET seam-preview plays the found stretch as the render cuts it: a
    snap take's stretch (its sound running past the picture, as Wan's) hands
    its usable_end to the preview, so the dissolve stays out of the tail."""
    from app.services import loop_health, loop_point

    snap = with_sound_past(snapping(tmp_path / "snap.mp4"))
    real_measure = loop_health.measure
    monkeypatch.setattr(loop_health, "measure", lambda clip, still=False, **kw: real_measure(snap, still=still, **kw))
    real_find = loop_point.find_loop_point
    monkeypatch.setattr(loop_point, "find_loop_point",
                        lambda clip, min_loop=None, *, snap=False: real_find(tmp_path / "snap_aac.mp4", min_loop,
                                                                             snap=snap))
    scene = _scene(auth_client)
    assert scene["loop_point"] is not None and scene["loop_point"]["snap"] is True, scene
    from app import models
    from app.db import SessionLocal
    from app.services import ambient

    db = SessionLocal()
    try:
        lp = ambient.active_loop_point(db.get(models.VideoProject, scene["id"]))
    finally:
        db.close()
    assert lp is not None and lp.get("usable_end"), lp
    seen = {}
    out = tmp_path / "preview.mp4"
    out.write_bytes(b"\0")

    def spy(clip, *, loop=None, blend=0.0, usable_end=None):
        seen.update(loop=loop, usable_end=usable_end)
        return out, 1.0

    monkeypatch.setattr(loop_health, "seam_preview", spy)
    r = auth_client.get(f"/api/ambient/{scene['id']}/seam-preview")
    assert r.status_code == 200, r.text
    assert seen["loop"] is not None and seen["usable_end"] == pytest.approx(float(lp["usable_end"]))


def test_a_stretch_render_is_exactly_its_length_in_frames(tmp_path):
    """The intro and tail pieces were cut by a duration rounded to the
    millisecond (6.4667 s → 6.467), which let one more frame through: the
    video came out a frame long, ending on a frame faded fully to black."""
    from app.services.renderer import FPS, AmbientRenderInputs, compose_ambient

    clip = tmp_path / "drift.mp4"
    _slow_drift_clip(clip, seconds=10, start=0)
    total = 31.0
    out = compose_ambient(AmbientRenderInputs(
        project_id=0, out_dir=tmp_path / "out", clip_path=clip, total_seconds=total,
        crossfade_seconds=1.0, fade_seconds=3.0, loop_in=2.13, loop_out=9.07, intro=True))
    frames = _raw_yavg([out.final_video_path], "[0:v]scale=320:180,setsar=1[o]", "o")
    assert len(frames) == round(total * FPS)


def _still_frames(n: int, *, base: int, blip: bool) -> list:
    """Near-still 100x100 grey frames: neighbouring frames differ by a 50-px
    blip (about 0.001 of the frame), far below TYPICAL_FLOOR."""
    from PIL import Image

    out = []
    for i in range(n):
        im = Image.new("RGB", (100, 100), (base, base, base))
        if blip and i % 2:
            for x in range(50):
                im.putpixel((x, 0), (base + 51, base + 51, base + 51))
        out.append(im)
    return out


def test_a_near_still_join_keeps_its_verdict_and_records_its_unfloored_ratio():
    """Rain/snow/candle: the join (2 grey levels everywhere) reads seamless
    against TYPICAL_FLOOR, exactly as #60 calibrated it; the unfloored ratio
    against the clip's own steps is kept beside it as raw_ratio."""
    from app.services import loop_health as lh

    first = _still_frames(6, base=100, blip=True)
    last = _still_frames(6, base=102, blip=True)
    seam = lh._seam(first, last)
    assert seam["verdict"] == "seamless"
    assert seam["ratio"] == pytest.approx(seam["difference"] / lh.TYPICAL_FLOOR, abs=0.02)
    assert seam["raw_ratio"] is not None and seam["raw_ratio"] > lh.SEAM_SEAMLESS_RATIO


@pytest.mark.parametrize("raw_ratio, searched", [(8.0, True), (None, False), (1.5, False)])
def test_a_hidden_jump_starts_the_automatic_search(auth_client, monkeypatch, raw_ratio, searched):
    """A seamless verdict with an unfloored ratio above SEAM_SEAMLESS_RATIO
    starts the automatic search after generation; a reading without
    raw_ratio (stored before it was kept) or a low one doesn't."""
    from app.services import loop_health, loop_point

    real_measure = loop_health.measure

    def measure(clip, still=False, **kw):
        reading = real_measure(clip, still=still, **kw)
        reading["seam"].update(verdict="seamless", level="good", ratio=1.9)
        reading["seam"].pop("raw_ratio", None)
        if raw_ratio is not None:
            reading["seam"]["raw_ratio"] = raw_ratio
        return reading

    monkeypatch.setattr(loop_health, "measure", measure)
    calls = []

    def spy(clip, min_loop=None, *, snap=False):
        calls.append(snap)
        return {"found": False}

    monkeypatch.setattr(loop_point, "find_loop_point", spy)
    _scene(auth_client)
    assert bool(calls) is searched


def _hidden(monkeypatch, raw_ratio=8.0):
    from app.services import loop_health

    real_measure = loop_health.measure

    def measure(clip, still=False, **kw):
        reading = real_measure(clip, still=still, **kw)
        reading["seam"].update(verdict="seamless", level="good", ratio=1.9, raw_ratio=raw_ratio)
        return reading

    monkeypatch.setattr(loop_health, "measure", measure)


def test_a_hidden_jump_keeps_the_stretch_the_automatic_search_found(auth_client, monkeypatch, tmp_path):
    from app.services import loop_point

    rep = repeating(tmp_path / "rep.mp4")
    _hidden(monkeypatch)
    real_find = loop_point.find_loop_point
    calls = []

    def find(clip, min_loop=None, *, snap=False):
        calls.append(snap)
        return real_find(rep, min_loop, snap=snap)

    monkeypatch.setattr(loop_point, "find_loop_point", find)
    scene = _scene(auth_client)
    assert calls == [False]
    assert scene["loop_point"] is not None, scene.get("loop_point")


def test_find_searches_a_hidden_jump(auth_client, monkeypatch):
    from app.services import loop_point

    _hidden(monkeypatch)
    monkeypatch.setattr(loop_point, "find_loop_point", lambda *a, **k: {"found": False, "ratio": 9.0})
    scene = _scene(auth_client)
    sid = scene["id"]
    calls = []
    monkeypatch.setattr(loop_point, "find_loop_point",
                        lambda *a, **k: calls.append(1) or {"found": False, "ratio": 9.0})
    assert auth_client.post(f"/api/ambient/{sid}/find-loop-point").status_code == 202
    st = auth_client.get(f"/api/ambient/{sid}").json()["loop_point_state"]
    assert calls == [1], st
    assert "joins seamlessly" not in st["message"], st


def test_find_reads_again_a_seamless_reading_stored_without_raw_ratio(auth_client, monkeypatch):
    """A seamless reading stored before raw_ratio was kept can't tell a hidden
    jump: Find reads the loop clip again and searches when the new reading
    shows one."""
    from app.services import loop_health, loop_point

    real_measure = loop_health.measure

    def old_reading(clip, still=False, **kw):
        reading = real_measure(clip, still=still, **kw)
        reading["seam"].update(verdict="seamless", level="good", ratio=1.9)
        reading["seam"].pop("raw_ratio", None)
        return reading

    monkeypatch.setattr(loop_health, "measure", old_reading)
    scene = _scene(auth_client)
    sid = scene["id"]
    seam = auth_client.get(f"/api/ambient/{sid}").json()["seam_treatment"]
    assert seam["verdict"] == "seamless", seam
    _hidden(monkeypatch)  # the clip read again now shows its hidden jump
    calls = []
    monkeypatch.setattr(loop_point, "find_loop_point",
                        lambda *a, **k: calls.append(1) or {"found": False, "ratio": 9.0})
    assert auth_client.post(f"/api/ambient/{sid}/find-loop-point").status_code == 202
    st = auth_client.get(f"/api/ambient/{sid}").json()["loop_point_state"]
    assert calls == [1], st
    assert "joins seamlessly" not in st["message"], st
