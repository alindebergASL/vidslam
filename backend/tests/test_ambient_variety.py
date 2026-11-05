"""Ambient "less repetition": a scene made of several loop clips.

- "takes": several clips of the same scene, played in turn (A, B, C, A…),
  so the picture repeats every K loops instead of every loop.
- "day_night": the same place at several times of day, each for an equal
  share of the video, with a slow dissolve between them.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from PIL import Image

from .test_ambient import SHORT, _duration

COLORS = {"red": (230, 30, 30), "green": (30, 200, 40), "blue": (30, 40, 230)}


def _solid_clip(path: Path, color: str, seconds: float = 4.0) -> Path:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", f"color=c={color}:s=320x180:r=30:d={seconds}",
                    "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


def _color_at(video: Path, t: float, tmp: Path) -> str:
    """Name of the solid color closest to the frame at time t."""
    png = tmp / f"f_{t:.2f}.png"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.3f}", "-i", str(video),
                    "-frames:v", "1", str(png)], check=True)
    px = Image.open(png).convert("RGB").resize((1, 1)).getpixel((0, 0))
    return min(COLORS, key=lambda c: sum((a - b) ** 2 for a, b in zip(COLORS[c], px, strict=True)))


def _compose(tmp: Path, variety: str, total: float):
    from app.services.renderer import AmbientRenderInputs, compose_ambient

    clips = [_solid_clip(tmp / f"{c}.mp4", c) for c in ("red", "green", "blue")]
    out = compose_ambient(AmbientRenderInputs(
        project_id=1, out_dir=tmp / "out", clip_path=clips[0], extra_clips=clips[1:],
        total_seconds=total, crossfade_seconds=1.0, fade_seconds=0.5, variety=variety,
        transition_seconds=1.0,
    ))
    return out, out.unit_seconds


# ---------- slot planning ----------


def test_variety_slots():
    from app.services.renderer import variety_slots

    assert variety_slots("takes", [3, 3, 3], 20) == [0, 1, 2, 0, 1, 2, 0]
    # Day to night: an equal share each, the last one runs to the end.
    assert variety_slots("day_night", [3, 3, 3], 36) == [0] * 4 + [1] * 4 + [2] * 4
    assert variety_slots("day_night", [3, 3, 3], 40) == [0] * 4 + [1] * 4 + [2] * 6
    # Shorter than one share: every variation still gets at least one slot.
    assert variety_slots("day_night", [3, 3, 3], 8) == [0, 1, 2]


# ---------- renderer ----------


def test_takes_cycle_through_every_clip(tmp_path):
    out, unit = _compose(tmp_path, "takes", 30)
    assert abs(unit - 3.0) < 0.1
    final = out.final_video_path
    assert abs(_duration(final) - 30) < 0.2
    # Sample each slot after its 1 s dissolve: R G B R G B …
    seen = [_color_at(final, i * unit + 2.0, tmp_path) for i in range(9)]
    assert seen == ["red", "green", "blue"] * 3


def test_day_night_plays_each_clip_for_its_share(tmp_path):
    out, unit = _compose(tmp_path, "day_night", 36)
    final = out.final_video_path
    assert abs(_duration(final) - 36) < 0.2
    assert [_color_at(final, t, tmp_path) for t in (2, 8, 14, 20, 26, 32)] == [
        "red", "red", "green", "green", "blue", "blue"]


def test_transition_dissolves_instead_of_cutting(tmp_path):
    out, unit = _compose(tmp_path, "takes", 12)
    final = out.final_video_path
    # Halfway through the red → green dissolve the frame is a mix of both.
    png = tmp_path / "mid.png"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{unit + 0.5:.3f}", "-i", str(final),
                    "-frames:v", "1", str(png)], check=True)
    r, g, _ = Image.open(png).convert("RGB").resize((1, 1)).getpixel((0, 0))
    # Neither pure red (230, 30) nor pure green (30, 200): a mix (blended in
    # YUV, so not the plain RGB average).
    assert 70 < r < 190 and 45 < g < 170, (r, g)


# ---------- plan + estimate ----------


def _cfg(**kw):
    from app.schemas.ambient import AmbientConfig

    return AmbientConfig(**{**SHORT, **kw})


def test_plan_and_cost_scale_with_variations():
    from app.services import ambient

    one = ambient.plan_loop(_cfg())
    takes = ambient.plan_loop(_cfg(variety="takes", variations=3))
    assert one.variants == 1 and takes.variants == 3
    assert takes.variant_labels == ["Take 1", "Take 2", "Take 3"]
    assert takes.repeat_every_seconds == pytest.approx(one.unit_seconds * 3)
    assert takes.provider_calls == len(takes.segments) * 3 + 1  # one shared anchor
    dn = ambient.plan_loop(_cfg(variety="day_night", variations=3))
    assert dn.variant_labels == ["Golden hour", "Dusk", "Night"]
    assert dn.provider_calls == len(dn.segments) * 3 + 3  # an anchor per time of day

    video = lambda est: next(ln for ln in est["lines"] if ln["unit"] == "sec")  # noqa: E731
    base = ambient.estimate_ambient_cost(_cfg())
    est = ambient.estimate_ambient_cost(_cfg(variety="takes", variations=3))
    assert video(est)["cost"] == pytest.approx(video(base)["cost"] * 3)
    assert "× 3 variations" in video(est)["item"]
    est_dn = ambient.estimate_ambient_cost(_cfg(variety="day_night", variations=3))
    assert any(ln["item"].startswith("Anchor stills for Dusk, Night") and ln["qty"] == 2
               for ln in est_dn["lines"])


def test_takes_need_a_video_scene():
    from app.services import ambient

    plan = ambient.plan_loop(_cfg(source="image_drift", variety="takes"))
    assert plan.variants == 1 and any("Mix of takes needs a video scene" in n for n in plan.notes)
    dn = ambient.plan_loop(_cfg(source="image_drift", variety="day_night", variations=2))
    assert dn.variants == 2 and dn.provider_calls == 2


def test_variation_prompts():
    from app.services import ambient

    cfg = _cfg(variety="day_night", variations=4)
    prompts = [ambient.variant_prompt(cfg, i) for i in range(4)]
    assert "midday" in prompts[0] and "golden hour" in prompts[1] and "at night" in prompts[3]
    takes = _cfg(variety="takes", variations=3)
    assert len({ambient.variant_prompt(takes, i) for i in range(3)}) == 3  # each take differs


# ---------- end to end (mock providers) ----------


def _image_spy(monkeypatch):
    from app.providers import mock_image

    calls: list[dict] = []
    real = mock_image.MockImageProvider.generate

    def spy(self, **kw):
        calls.append(kw)
        return real(self, **kw)

    monkeypatch.setattr(mock_image.MockImageProvider, "generate", spy)
    return calls


def _make(client, **cfg):
    r = client.post("/api/ambient", json={"config": {**SHORT, **cfg}, "generate": True})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_takes_scene_generates_and_renders(auth_client, monkeypatch):
    images = _image_spy(monkeypatch)
    sid = _make(auth_client, variety="takes", variations=3)
    scene = auth_client.get(f"/api/ambient/{sid}").json()
    assert scene["loop_status"] == "completed", scene
    assert len(scene["variant_shots"]) == 2
    assert all(v["status"] == "completed" and v["clip_path"] for v in scene["variant_shots"])
    clips = {scene["loop_shot"]["clip_path"], *(v["clip_path"] for v in scene["variant_shots"])}
    assert len(clips) == 3
    assert len(images) == 1  # the takes share one anchor still

    auth_client.post(f"/api/ambient/{sid}/render")
    rend = auth_client.get(f"/api/ambient/{sid}").json()["latest_render"]
    assert rend["status"] == "completed", rend
    assert "variations=3" in rend["render_log"]
    assert 19.0 <= _duration(Path(rend["final_video_path"])) <= 21.0


def test_day_night_scene_makes_a_still_per_time_of_day(auth_client, monkeypatch):
    images = _image_spy(monkeypatch)
    sid = _make(auth_client, variety="day_night", variations=3)
    scene = auth_client.get(f"/api/ambient/{sid}").json()
    assert scene["loop_status"] == "completed", scene
    assert len(images) == 3
    assert not images[0]["references"]
    # Dusk and night are made from the first still: same place, new light.
    for call, when in zip(images[1:], ("dusk", "at night"), strict=True):
        assert len(call["references"]) == 1 and when in call["prompt"]
        assert "only the time of day and the light change" in call["prompt"]
    assert "at night" in scene["variant_shots"][1]["prompt"]
    auth_client.post(f"/api/ambient/{sid}/render")
    assert auth_client.get(f"/api/ambient/{sid}").json()["latest_render"]["status"] == "completed"


def test_drift_day_night_starts_from_the_picture(auth_client, monkeypatch):
    from .conftest import make_png_bytes

    sid = auth_client.post("/api/ambient", json={
        "config": {**SHORT, "source": "image_drift", "music_mood": "none", "variety": "day_night",
                   "variations": 2}, "generate": False}).json()["id"]
    auth_client.post(f"/api/ambient/{sid}/picture",
                     files={"file": ("p.png", make_png_bytes((1920, 1080), (200, 40, 40)), "image/png")})
    images = _image_spy(monkeypatch)
    auth_client.post(f"/api/ambient/{sid}/generate-loop")
    scene = auth_client.get(f"/api/ambient/{sid}").json()
    assert scene["loop_status"] == "completed", scene
    assert scene["loop_shot"]["provider_model"] == "your picture"
    assert len(images) == 1 and len(images[0]["references"]) == 1  # night, from the picture
    auth_client.post(f"/api/ambient/{sid}/render")
    assert auth_client.get(f"/api/ambient/{sid}").json()["latest_render"]["status"] == "completed"


def test_changing_variety_invalidates_and_resizes(auth_client):
    sid = _make(auth_client, variety="takes", variations=3)
    r = auth_client.patch(f"/api/ambient/{sid}", json={"config": {**SHORT, "variety": "takes", "variations": 2}})
    out = r.json()
    assert out["loop_status"] == "pending" and len(out["variant_shots"]) == 1
    assert not out["variant_shots"][0]["clip_path"]
    r = auth_client.patch(f"/api/ambient/{sid}", json={"config": {**SHORT}})
    assert r.json()["variant_shots"] == []
    # Back to one loop: renders as before.
    auth_client.post(f"/api/ambient/{sid}/generate-loop")
    auth_client.post(f"/api/ambient/{sid}/render")
    rend = auth_client.get(f"/api/ambient/{sid}").json()["latest_render"]
    assert rend["status"] == "completed" and "variations=1" in rend["render_log"]
