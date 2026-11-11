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
