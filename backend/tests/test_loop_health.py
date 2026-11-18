"""Loop health (services/loop_health.py): a free, local measurement of an
ambient loop clip: how well it joins, flicker, frozen stretches and colour
drift, plus a "watch the seam" preview. Synthetic clips made with ffmpeg."""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from .test_ambient import SHORT, _duration

ENC = ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast", "-r", "24"]
BG = "color=c=0x336699:s=320x180:d={d}:r=24"
BOX = "color=c=0xEEDD22:s=40x40:d={d}:r=24"


def _make(path: Path, graph: str, *, seconds: float = 4.0, bg: str = BG) -> Path:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", bg.format(d=seconds),
                    "-f", "lavfi", "-i", BOX.format(d=seconds), "-filter_complex", graph,
                    "-t", str(seconds), *ENC, str(path)], check=True)
    return path


def seamless(path: Path, seconds: float = 4.0) -> Path:
    """A box circling once per clip: the last frame leads straight into the first."""
    return _make(path, f"[0][1]overlay=x='140+80*sin(2*PI*t/{seconds})':y='70+40*cos(2*PI*t/{seconds})'",
                 seconds=seconds)


def hard_cut(path: Path) -> Path:
    """Moving box, then the whole picture turns red for the last half second."""
    return _make(path, "[0][1]overlay=x='20+70*t':y=70,"
                       "drawbox=x=0:y=0:w=iw:h=ih:color=0xCC3311:t=fill:enable='gte(t,3.5)'")


def frozen_middle(path: Path) -> Path:
    """Moves for 1 s, holds still from 1 s to 3.5 s, moves again."""
    return _make(path, "[0][1]overlay=x='if(lt(t,1),20+60*t,if(lt(t,3.5),80,80+60*(t-3.5)))':y=70",
                 seconds=4.5)


def static(path: Path) -> Path:
    return _make(path, "[0][1]overlay=x=100:y=70")


def flicker(path: Path) -> Path:
    """Brightness pumping ±8 % at 1.5 Hz on a moving box."""
    return _make(path, "[0][1]overlay=x='140+80*sin(2*PI*t/4)':y=70,"
                       "eq=brightness='0.08*sin(2*PI*1.5*t)':eval=frame",
                 bg="color=c=0x808080:s=320x180:d={d}:r=24")


def drifting(path: Path) -> Path:
    """Seamless motion while the whole picture slowly darkens (exposure drift)."""
    return _make(path, "[0][1]overlay=x='140+80*sin(2*PI*t/4)':y=70,"
                       "eq=brightness='-0.06*t/8':eval=frame", seconds=8.0)


# ---------- the readings ----------


def test_a_clean_loop_reads_seamless_and_healthy(tmp_path):
    from app.services import loop_health as lh

    r = lh.measure(seamless(tmp_path / "s.mp4"))
    assert r["seam"]["verdict"] == "seamless" and r["seam"]["level"] == "good"
    assert r["seam"]["text"] == "The join is seamless" and r["seam"]["ratio"] <= lh.SEAM_SEAMLESS_RATIO
    assert r["flicker"]["verdict"] == "none" and r["flicker"]["text"] == "No flicker"
    assert r["frozen"]["verdict"] == "none" and r["frozen"]["text"] == "No frozen moments"
    assert r["drift"]["verdict"] == "none"
    assert r["clip_seconds"] == pytest.approx(4.0, abs=0.1) and r["sample_fps"] == lh.SAMPLE_FPS


def test_a_hard_cut_at_the_join_reads_visible_jump(tmp_path):
    from app.services import loop_health as lh

    r = lh.measure(hard_cut(tmp_path / "c.mp4"))
    assert r["seam"]["verdict"] == "visible_jump" and r["seam"]["level"] == "bad"
    assert r["seam"]["ratio"] > lh.SEAM_SLIGHT_RATIO and r["seam"]["difference"] > 0.1
    assert r["seam"]["text"] == "There's a visible jump at the join"
    assert r["drift"]["verdict"] == "strong"


def test_a_still_stretch_reads_frozen_with_where_and_how_long(tmp_path):
    from app.services import loop_health as lh

    r = lh.measure(frozen_middle(tmp_path / "f.mp4"))
    fz = r["frozen"]
    assert fz["verdict"] == "some" and fz["level"] == "warn"
    assert 2.0 <= fz["longest_seconds"] <= 2.8 and 0.9 <= fz["at_seconds"] <= 1.3
    assert fz["text"].startswith("Frozen for 2.") and fz["text"].endswith("around 0:01")
    whole = lh.measure(static(tmp_path / "st.mp4"))["frozen"]
    assert whole["verdict"] == "long" and whole["level"] == "bad" and whole["longest_seconds"] >= 3.5
    # An image-drift loop is a still picture by design: not judged on freezing.
    assert lh.measure(static(tmp_path / "st2.mp4"), still=True)["frozen"]["verdict"] == "not_checked"


def test_brightness_pumping_reads_flicker(tmp_path):
    from app.services import loop_health as lh

    r = lh.measure(flicker(tmp_path / "fl.mp4"))
    assert r["flicker"]["verdict"] == "strong" and r["flicker"]["percent"] > lh.FLICKER_STRONG
    assert r["flicker"]["text"].startswith("Noticeable flicker (")


def test_slow_exposure_drift_reads_drift_not_flicker(tmp_path):
    from app.services import loop_health as lh

    r = lh.measure(drifting(tmp_path / "d.mp4"))
    assert r["drift"]["verdict"] in ("slight", "strong") and r["drift"]["distance"] >= lh.DRIFT_SLIGHT
    assert r["flicker"]["verdict"] == "none"  # slow drift is removed before flicker is judged


def test_thresholds_in_order():
    from app.services import loop_health as lh

    assert lh.SEAM_SEAMLESS_RATIO < lh.SEAM_SLIGHT_RATIO
    assert lh.FLICKER_SLIGHT < lh.FLICKER_STRONG
    assert lh.FROZEN_MIN < lh.FROZEN_LONG and lh.DRIFT_SLIGHT < lh.DRIFT_STRONG


def test_a_long_clip_is_sampled_sparsely_and_fast(tmp_path):
    from app.services import loop_health as lh

    clip = tmp_path / "long.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=1280x720:d=90:r=24",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast", str(clip)], check=True)
    t = time.monotonic()
    r = lh.measure(clip)
    assert time.monotonic() - t < 15
    assert r["sample_fps"] == pytest.approx(lh.MAX_SAMPLES / 90, abs=0.01)


def colour_cut(path: Path) -> Path:
    """A blue/teal scene with red held at 10 (a night sea): a box moves,
    then the picture turns another teal for the last half second. Only green
    and blue change at the join."""
    return _make(path, "[0][1]overlay=x='20+70*t':y=70,"
                       "drawbox=x=0:y=0:w=iw:h=ih:color=0x0A9060:t=fill:enable='gte(t,3.5)'",
                 bg="color=c=0x0A4080:s=320x180:d={d}:r=24")


def slow_pan(path: Path, seconds: float = 10.0) -> Path:
    """A calm scene that never stops: a soft 20-level gradient drifting
    sideways (one cycle every 2 minutes), like fog or still water."""
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"nullsrc=s=320x180:r=24:d={seconds},"
                    "geq=lum='118+20*sin(2*PI*(X/W+T/120))':cb=128:cr=128",
                    *ENC, str(path)], check=True)
    return path


def test_a_colour_only_cut_at_the_join_reads_visible_jump(tmp_path):
    """Seam and freezing compare every channel, not red only: a cut where
    only green and blue change is a visible jump."""
    from PIL import Image

    from app.services import loop_health as lh

    a = Image.new("RGB", (8, 8), (10, 64, 128))
    assert lh._diff(a, Image.new("RGB", (8, 8), (10, 144, 96))) > 0.05
    assert lh._diff(a, Image.new("RGB", (8, 8), (10, 64, 200))) > 0.05
    r = lh.measure(colour_cut(tmp_path / "bc.mp4"))
    assert r["seam"]["verdict"] == "visible_jump" and r["seam"]["level"] == "bad"
    assert r["seam"]["ratio"] > lh.SEAM_SLIGHT_RATIO


def test_slow_continuous_motion_is_not_a_freeze(tmp_path):
    """Tiny steps that add up end a "frozen" stretch: each sample is compared
    with the first of the stretch, not its neighbour."""
    from PIL import Image

    from app.services import loop_health as lh

    # Each step is under FROZEN_DIFF, but they add up quickly.
    frames = [Image.new("RGB", (16, 16), (100 + i // 4, 100 + i // 4, 100 + i // 4)) for i in range(80)]
    assert lh._diff(frames[0], frames[1]) < lh.FROZEN_DIFF
    assert lh._frozen(frames, 8.0, still=False)["verdict"] == "none"
    # Truly identical frames still read frozen.
    held = frames[:8] + [frames[8]] * 40 + frames[8:]
    fz = lh._frozen(held, 8.0, still=False)
    assert fz["verdict"] == "long" and fz["longest_seconds"] >= 4.5
    r = lh.measure(slow_pan(tmp_path / "calm.mp4"))
    assert r["frozen"]["verdict"] == "none" and r["frozen"]["text"] == "No frozen moments"


# ---------- the scene ----------


def _scene(client, **cfg) -> dict:
    r = client.post("/api/ambient", json={"config": {**SHORT, **cfg}, "generate": True})
    assert r.status_code == 201, r.text
    scene = r.json()
    assert scene["loop_status"] == "completed", scene
    return scene


def _replace(clip: Path, maker) -> None:
    tmp = clip.with_name("new_" + clip.name)
    maker(tmp)
    st = clip.stat()
    tmp.replace(clip)
    os.utime(clip, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))  # a visibly newer file


def test_a_new_loop_is_measured_and_exposed(auth_client):
    scene = _scene(auth_client)
    h = scene["loop_health"]
    assert h is not None and h["shot_id"] == scene["loop_shot"]["id"]
    for key in ("seam", "flicker", "frozen", "drift"):
        assert h[key]["verdict"] and h[key]["level"] in ("good", "warn", "bad") and h[key]["text"]
    assert h["measured_at"] and h["clip_seconds"] > 4
    assert h["seam_preview_url"] == f"/api/ambient/{scene['id']}/seam-preview?shot_id={h['shot_id']}"
    assert h["join_at_seconds"] == pytest.approx(min(3.0, h["clip_seconds"] / 2), abs=0.01)
    assert scene["variant_loop_health"] == []
    assert auth_client.get(f"/api/ambient/{scene['id']}").json()["loop_health"] == h
    assert "clip_path" not in h and "clip_mtime_ns" not in h


def test_measure_again_and_staleness(auth_client):
    scene = _scene(auth_client)
    sid = scene["id"]
    clip = Path(scene["loop_shot"]["clip_path"])
    _replace(clip, hard_cut)
    # The stored reading was of the old clip: not shown any more.
    assert auth_client.get(f"/api/ambient/{sid}").json()["loop_health"] is None
    r = auth_client.post(f"/api/ambient/{sid}/loop-health")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["loop_health"]["seam"]["verdict"] == "visible_jump" and body["variant_loop_health"] == []
    assert auth_client.get(f"/api/ambient/{sid}").json()["loop_health"]["seam"]["verdict"] == "visible_jump"
    _replace(clip, seamless)
    again = auth_client.post(f"/api/ambient/{sid}/loop-health").json()["loop_health"]
    assert again["seam"]["verdict"] == "seamless"


def test_loop_health_errors(auth_client):
    r = auth_client.post("/api/ambient", json={"config": SHORT, "generate": False})
    sid = r.json()["id"]
    assert r.json()["loop_health"] is None
    assert auth_client.post(f"/api/ambient/{sid}/loop-health").status_code == 409
    assert auth_client.get(f"/api/ambient/{sid}/seam-preview").status_code == 404
    assert auth_client.post("/api/ambient/99999/loop-health").status_code == 404
    scene = _scene(auth_client)
    assert auth_client.get(f"/api/ambient/{scene['id']}/seam-preview?shot_id=99999").status_code == 404


def test_variations_are_measured_each(auth_client):
    scene = _scene(auth_client, variety="takes", variations=2)
    assert len(scene["variant_shots"]) == 1
    assert scene["loop_health"] is not None
    assert len(scene["variant_loop_health"]) == 1
    v = scene["variant_loop_health"][0]
    assert v is not None and v["shot_id"] == scene["variant_shots"][0]["id"]
    r = auth_client.get(f"/api/ambient/{scene['id']}/seam-preview?shot_id={v['shot_id']}")
    assert r.status_code == 200 and r.headers["content-type"] == "video/mp4"


def test_seam_preview_is_made_once_and_remade_when_the_clip_changes(auth_client):
    scene = _scene(auth_client)
    sid = scene["id"]
    clip = Path(scene["loop_shot"]["clip_path"])
    r = auth_client.get(f"/api/ambient/{sid}/seam-preview")
    assert r.status_code == 200 and r.headers["content-type"] == "video/mp4"
    assert float(r.headers["x-join-at"]) == pytest.approx(2.5, abs=0.05)  # 5 s loop: half each side
    made = list(clip.parent.glob(f"seam_{clip.stem}_*.mp4"))
    assert len(made) == 1
    first, mtime = made[0], made[0].stat().st_mtime_ns
    assert 4.5 <= _duration(first) <= 5.5
    assert auth_client.get(f"/api/ambient/{sid}/seam-preview").content == r.content
    assert first.stat().st_mtime_ns == mtime  # served from the cache
    _replace(clip, hard_cut)
    r2 = auth_client.get(f"/api/ambient/{sid}/seam-preview")
    assert r2.status_code == 200 and r2.content != r.content
    made = list(clip.parent.glob(f"seam_{clip.stem}_*.mp4"))
    assert len(made) == 1 and made[0] != first and not first.exists()
    assert float(r2.headers["x-join-at"]) == pytest.approx(2.0, abs=0.05)  # 4 s clip


def test_a_failed_measurement_never_fails_the_loop(auth_client, monkeypatch):
    from app.services import loop_health

    def boom(*a, **kw):
        raise RuntimeError("ffmpeg exploded")

    monkeypatch.setattr(loop_health, "measure", boom)
    scene = _scene(auth_client)
    assert scene["status"] == "planned" and scene["loop_health"] is None


def test_the_state_keeps_only_live_clips(auth_client):
    from app import models
    from app.db import SessionLocal

    scene = _scene(auth_client)
    db = SessionLocal()
    try:
        p = db.get(models.VideoProject, scene["id"])
        state = (p.ambient_config_json or {})["_loop_health"]
        assert list(state) == [str(scene["loop_shot"]["id"])]
        entry = state[str(scene["loop_shot"]["id"])]
        assert entry["clip_path"] == scene["loop_shot"]["clip_path"] and entry["clip_mtime_ns"]
        json.dumps(entry)  # plain JSON in the scene's state
    finally:
        db.close()


def test_overlapping_seam_previews_never_break_the_cached_file(tmp_path):
    """Requests at once (a second tab, the player asking again) all get the
    same playable file; a broken cached file is remade; another clip whose
    name starts the same keeps its preview."""
    import threading

    from app.services import loop_health as lh
    from app.services.renderer import _probe_duration

    clip = seamless(tmp_path / "loop.mp4", seconds=6.0)
    other = seamless(tmp_path / "loop_2.mp4")
    other_dst, _ = lh.seam_preview(other)
    results, errors = [], []

    def go():
        try:
            results.append(lh.seam_preview(clip))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=go) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and len(results) == 4
    dst = results[0][0]
    assert all(r[0] == dst for r in results) and _probe_duration(dst) > 5.0
    assert not list(tmp_path.glob("*.part.mp4"))
    assert other_dst.is_file()  # "seam_loop_" is not "seam_loop_2_"'s prefix
    # A cached file that doesn't play (an earlier crash) is remade, not served.
    dst.write_bytes(b"not a video")
    again, _ = lh.seam_preview(clip)
    assert again == dst and _probe_duration(dst) > 5.0


def test_a_seam_preview_that_takes_too_long_is_an_error_not_a_crash(tmp_path, monkeypatch):
    from app.services import loop_health as lh

    clip = seamless(tmp_path / "slow.mp4")
    real = subprocess.run

    def slow(cmd, *a, **k):
        if "-filter_complex" in cmd:
            raise subprocess.TimeoutExpired(cmd, 120)
        return real(cmd, *a, **k)

    monkeypatch.setattr(lh.subprocess, "run", slow)
    with pytest.raises(RuntimeError, match="took too long"):
        lh.seam_preview(clip)
    assert not list(tmp_path.glob("seam_*"))


def test_a_jump_the_render_blends_says_so(monkeypatch):
    """The reading is of the raw clip; when the finished video's blend covers
    the join, the text says so and the level is a warning. A jump the blend
    doesn't cover keeps its level (and says so); the raw level is kept."""
    from types import SimpleNamespace

    from app.services import ambient, loop_health

    def treat(blend, covers):
        return lambda p: {"blend_seconds": blend, "verdict": "slight_jump", "ratio": 4.0,
                          "basis": "measured", "covers": covers}

    monkeypatch.setattr(ambient, "seam_treatment", treat(2.0, True))
    slight = {"seam": {"verdict": "slight_jump", "level": "warn", "text": "There's a slight jump at the join",
                       "difference": 0.05, "typical": 0.01, "ratio": 4.0}, "clip_seconds": 10}
    view = loop_health.out(SimpleNamespace(id=1), SimpleNamespace(id=2), slight)
    assert view["seam"]["level"] == "warn" and view["seam"]["blend_seconds"] == 2.0
    assert view["seam"]["raw_level"] == "warn" and view["seam"]["treatment"]["covers"] is True
    assert "blends it over 2 s" in view["seam"]["text"]
    # A variation's slight jump is covered even when another clip of the
    # scene jumps visibly (the scene's treatment is the worst join).
    monkeypatch.setattr(ambient, "seam_treatment", treat(2.0, False))
    view = loop_health.out(SimpleNamespace(id=1), SimpleNamespace(id=2), slight)
    assert view["seam"]["level"] == "warn" and "blends it over 2 s" in view["seam"]["text"]
    entry = {"seam": {"verdict": "visible_jump", "level": "bad", "text": "There's a visible jump at the join",
                      "difference": 0.3, "typical": 0.01, "ratio": 30.0}, "clip_seconds": 10}
    view = loop_health.out(SimpleNamespace(id=1), SimpleNamespace(id=2), entry)
    assert view["seam"]["level"] == "bad" and view["seam"]["raw_level"] == "bad"
    assert "won't hide it" in view["seam"]["text"]
    # Even when the scene's treatment says it covers (no reading of the loop
    # clip yet), a visible jump is never softened.
    monkeypatch.setattr(ambient, "seam_treatment", treat(2.0, True))
    view = loop_health.out(SimpleNamespace(id=1), SimpleNamespace(id=2), entry)
    assert view["seam"]["level"] == "bad" and "won't hide it" in view["seam"]["text"]
    monkeypatch.setattr(ambient, "seam_treatment", treat(0.0, True))
    view = loop_health.out(SimpleNamespace(id=1), SimpleNamespace(id=2), entry)
    assert view["seam"]["level"] == "bad" and "visible jump" in view["seam"]["text"]


# ---------- snap: the last frame lands only by jumping to it ----------

# A still, soft pattern wider than the frame: only the crop moves.
TEXTURE = "nullsrc=s=640x180:d={d}:r=24,geq=lum='128+60*sin(X/9)*cos(Y/7)':cb=128:cr=128,format=yuv420p"


def _scroll(path: Path, x: str, seconds: float = 4.0) -> Path:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", TEXTURE.format(d=seconds),
                    "-vf", f"crop=320:180:x='{x}':y=0", "-t", str(seconds), *ENC, str(path)], check=True)
    return path


def test_a_take_that_snaps_to_its_last_frame_reads_snap_not_seamless(tmp_path):
    """The host's long-Wan-take failure: the picture drifts away from the
    target for the whole take, then jumps back to it in the last two
    frames. The join alone reads seamless (the last frame lands); the
    approach does not."""
    from app.services import loop_health as lh

    n = 96  # 4 s at 24 fps
    r = lh.measure(_scroll(tmp_path / "snap.mp4", f"if(gte(n,{n - 2}),0,n)"))
    seam = r["seam"]
    assert seam["difference"] <= lh.SEAM_TINY or seam["ratio"] <= lh.SEAM_SEAMLESS_RATIO
    assert seam["verdict"] == "snap" and seam["level"] == "bad"
    assert seam["approach"]["snaps"] is True


def test_a_smooth_loop_does_not_read_snap(tmp_path):
    from app.services import loop_health as lh

    r = lh.measure(_scroll(tmp_path / "smooth.mp4", "40-40*cos(2*PI*t/4)"))
    assert r["seam"]["verdict"] == "seamless"
    assert r["seam"]["approach"]["snaps"] is False
    assert r["seam"]["approach"]["to_target"] >= lh.SNAP_SSIM


def test_the_box_loop_and_the_cut_keep_their_verdicts(tmp_path):
    from app.services import loop_health as lh

    assert lh.measure(seamless(tmp_path / "s.mp4"))["seam"]["approach"]["snaps"] is False
    assert lh.measure(hard_cut(tmp_path / "c.mp4"))["seam"]["verdict"] == "visible_jump"


def test_blend_mismatch_reads_how_different_the_blended_pictures_are(tmp_path):
    """The two pictures a loop blend mixes (the last second against the
    first, frame by frame): alike for a still loop, far apart across a cut.
    A note for calibration (D5), not a gate."""
    from app.services import loop_health as lh

    still = lh.measure(static(tmp_path / "still.mp4"))["seam"]
    snap = lh.measure(_scroll(tmp_path / "snap.mp4", "if(gte(n,94),0,n)"))["seam"]
    assert still["blend_mismatch"] < 5 and still["blend_mismatch_level"] == "good"
    assert snap["blend_mismatch"] > lh.MISMATCH_WARN and snap["blend_mismatch_level"] == "warn"


def test_ssim_is_one_for_the_same_frame_and_lower_for_a_shift():
    from PIL import Image, ImageFilter

    from app.services import loop_health as lh

    img = Image.effect_noise((160, 90), 60).filter(ImageFilter.GaussianBlur(2)).convert("RGB")
    assert lh._ssim(img, img) == pytest.approx(1.0, abs=1e-6)
    assert lh._ssim(img, img.transform(img.size, Image.AFFINE, (1, 0, 20, 0, 1, 0))) < lh.SNAP_SSIM
