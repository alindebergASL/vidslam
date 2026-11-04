"""Ambient mode: seamless-loop long-form scenes against mock providers.

Durations are kept tiny (5s loop → 20s output) so the suite stays fast; the
compositor's math is identical at 30s → 1h.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from PIL import Image, ImageStat


def _probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    return json.loads(out)


def _duration(path: Path) -> float:
    return float(_probe(path)["format"]["duration"])


def _has_audio(path: Path) -> bool:
    return any(s["codec_type"] == "audio" for s in _probe(path)["streams"])


def _frame_mean_red(video: Path, *, first: bool, tmp: Path) -> float:
    """Decode the first or last frame and return its mean red channel."""
    nb = None
    for s in _probe(video)["streams"]:
        if s["codec_type"] == "video":
            nb = int(s.get("nb_frames") or 0)
    assert nb and nb > 1, "need frame count"
    idx = 0 if first else nb - 1
    png = tmp / f"frame_{idx}.png"
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(video), "-vf", f"select='eq(n\\,{idx})'", "-fps_mode", "passthrough",
         "-frames:v", "1", str(png)],
        capture_output=True, text=True, check=True,
    )
    with Image.open(png) as im:
        return ImageStat.Stat(im.convert("RGB")).mean[0]


SHORT = {
    "scene_preset": "waterfall",
    "loop_seconds": 5,
    "total_seconds": 20,
    "crossfade_seconds": 1.0,
    "fade_seconds": 1.0,
    "music_mood": "spa",
}


# ---------- presets + cost ----------


def test_presets_lists_core_scenes_and_moods(auth_client):
    r = auth_client.get("/api/ambient/presets")
    assert r.status_code == 200, r.text
    body = r.json()
    ids = {s["id"] for s in body["scenes"]}
    assert {"waterfall", "fireplace", "ocean", "custom"} <= ids
    assert {"spa", "zen", "none"} <= {m["id"] for m in body["music_moods"]}
    assert body["loop_options"] == [10, 25, 30, 60]
    assert 3600 in body["total_options"]
    for s in body["scenes"]:
        assert len(s["palette"]) == 2


def test_estimate_is_independent_of_total_duration(auth_client):
    one_hour = auth_client.post(
        "/api/ambient/estimate",
        json={"config": {"scene_preset": "ocean", "loop_seconds": 30, "total_seconds": 3600}},
    ).json()
    three_hours = auth_client.post(
        "/api/ambient/estimate",
        json={"config": {"scene_preset": "ocean", "loop_seconds": 30, "total_seconds": 10800}},
    ).json()
    assert one_hour["total"] == three_hours["total"] > 0
    # The tiling line is explicit and free.
    tiling = [ln for ln in one_hour["lines"] if ln["unit"] == "repeat"]
    assert tiling and tiling[0]["cost"] == 0.0
    # Mock model caps clips at 10s with first+last frame support:
    # 3 × 10s chained (−2 × 0.5s joins) = 29s, closed loop → 0.5s seam
    # → 28.5s unit → ceil(3600/28.5) = 127 repeats.
    assert tiling[0]["qty"] == 127
    plan = one_hour["plan"]
    assert plan["segments"] == [10, 10, 10]
    assert plan["strategy"] == "chain" and plan["closed"] and plan["anchored"]
    assert plan["provider_calls"] == 4  # anchor still + 3 clips
    assert any("Anchor still" in ln["item"] for ln in one_hour["lines"])
    assert any("3 × 10s = 30s" in ln["item"] for ln in one_hour["lines"])


def test_estimate_image_drift_cheaper_and_no_music_drops_line(auth_client):
    video = auth_client.post(
        "/api/ambient/estimate", json={"config": {"loop_seconds": 30, "source": "video"}}
    ).json()
    still = auth_client.post(
        "/api/ambient/estimate",
        json={"config": {"loop_seconds": 30, "source": "image_drift", "music_mood": "none"}},
    ).json()
    assert still["total"] < video["total"]
    assert not any("Music" in ln["item"] for ln in still["lines"])
    assert any("Music" in ln["item"] for ln in video["lines"])


# ---------- lifecycle ----------


def test_create_draft_without_generating(auth_client):
    r = auth_client.post(
        "/api/ambient",
        json={"title": "", "aspect_ratio": "16:9", "config": SHORT, "generate": False},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["title"].startswith("Waterfall")
    assert body["status"] == "draft"
    assert body["loop_status"] == "pending"
    assert body["music_ready"] is False
    assert body["total_seconds"] == 20
    # 5s loop − 1s crossfade = 4s unit → 5 repeats.
    assert body["repeats"] == 5
    assert body["loop_shot"]["duration_seconds"] == 5.0
    assert "seamlessly" in body["loop_shot"]["prompt"]

    # Listed in the ambient index, and visible as a project with the right mode.
    ids = [s["id"] for s in auth_client.get("/api/ambient").json()]
    assert body["id"] in ids
    p = auth_client.get(f"/api/projects/{body['id']}").json()
    assert p["mode"] == "ambient_loop"
    assert p["ambient_config_json"]["scene_preset"] == "waterfall"
    assert p["voiceover_source"] == "silent"
    assert p["include_disclosure"] is False


def test_render_before_loop_is_rejected(auth_client):
    sid = auth_client.post("/api/ambient", json={"config": SHORT, "generate": False}).json()["id"]
    r = auth_client.post(f"/api/ambient/{sid}/render")
    assert r.status_code == 409


def test_generate_then_render_full_length_with_music(auth_client):
    r = auth_client.post("/api/ambient", json={"config": SHORT, "generate": True})
    assert r.status_code == 201, r.text
    scene = r.json()
    sid = scene["id"]
    # Inline queue → generation already finished by the time create returns.
    assert scene["status"] == "planned", scene
    assert scene["loop_status"] == "completed"
    assert scene["music_ready"] is True
    clip = Path(scene["loop_shot"]["clip_path"])
    assert clip.exists()
    assert 4.5 <= _duration(clip) <= 5.6

    # Loop clip and music bed stream through the generic project endpoints.
    assert auth_client.get(f"/api/projects/{sid}/shots/{scene['loop_shot']['id']}/clip").status_code == 200
    assert auth_client.get(f"/api/projects/{sid}/music").status_code == 200

    r = auth_client.post(f"/api/ambient/{sid}/render")
    assert r.status_code == 202, r.text
    scene = auth_client.get(f"/api/ambient/{sid}").json()
    assert scene["status"] == "completed", scene
    rend = scene["latest_render"]
    assert rend["status"] == "completed", rend
    final = Path(rend["final_video_path"])
    assert final.exists()
    # Tiled to the requested total (stream-copy cut lands within a frame).
    assert 19.0 <= _duration(final) <= 21.0
    assert _has_audio(final)
    assert Path(rend["thumbnail_path"]).exists()
    assert "repeats=5" in rend["render_log"]
    assert rend["actual_cost"] == 0.0  # mock mode bills nothing
    # Landscape output by default.
    v = [s for s in _probe(final)["streams"] if s["codec_type"] == "video"][0]
    assert (v["width"], v["height"]) == (1920, 1080)
    assert scene["render_count"] == 1


def test_image_drift_source_without_music(auth_client):
    cfg = {**SHORT, "source": "image_drift", "music_mood": "none", "aspect": None}
    cfg.pop("aspect")
    r = auth_client.post(
        "/api/ambient", json={"aspect_ratio": "9:16", "config": cfg, "generate": True}
    )
    assert r.status_code == 201, r.text
    scene = r.json()
    assert scene["loop_status"] == "completed", scene
    assert scene["music_ready"] is False
    clip = Path(scene["loop_shot"]["clip_path"])
    assert 4.5 <= _duration(clip) <= 5.5
    assert (Path(clip).parent / "ambient_still.png").exists()
    # Portrait dims honoured by the drift renderer.
    v = [s for s in _probe(clip)["streams"] if s["codec_type"] == "video"][0]
    assert (v["width"], v["height"]) == (1080, 1920)

    assert auth_client.post(f"/api/ambient/{scene['id']}/render").status_code == 202
    scene = auth_client.get(f"/api/ambient/{scene['id']}").json()
    final = Path(scene["latest_render"]["final_video_path"])
    assert scene["latest_render"]["status"] == "completed", scene["latest_render"]
    assert 19.0 <= _duration(final) <= 21.0
    assert not _has_audio(final)
    # Periodic clip → no crossfade → 5s unit → 4 repeats.
    assert "repeats=4" in scene["latest_render"]["render_log"]


def test_update_scene_prompt_invalidates_clip_but_fade_does_not(auth_client):
    scene = auth_client.post("/api/ambient", json={"config": SHORT, "generate": True}).json()
    sid = scene["id"]
    assert scene["loop_status"] == "completed"

    # Compositing-only change keeps the clip.
    r = auth_client.patch(
        f"/api/ambient/{sid}", json={"config": {**SHORT, "fade_seconds": 2.0, "total_seconds": 40}}
    )
    assert r.status_code == 200, r.text
    assert r.json()["loop_status"] == "completed"
    assert r.json()["total_seconds"] == 40
    # 5s closed loop → 4.5s unit → ceil(40/4.5) = 9.
    assert r.json()["repeats"] == 9

    # Scene change → clip must be regenerated.
    r = auth_client.patch(
        f"/api/ambient/{sid}", json={"title": "Night", "config": {**SHORT, "scene_preset": "night_sky"}}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["title"] == "Night"
    assert body["loop_status"] == "pending"
    assert body["loop_shot"]["clip_path"] == ""
    assert "starry" in body["loop_shot"]["prompt"]
    assert auth_client.post(f"/api/ambient/{sid}/render").status_code == 409


def test_validation_errors(auth_client):
    bad_loop = auth_client.post("/api/ambient", json={"config": {**SHORT, "loop_seconds": 3}})
    assert bad_loop.status_code == 422
    bad_total = auth_client.post("/api/ambient", json={"config": {**SHORT, "total_seconds": 5}})
    assert bad_total.status_code == 422
    empty_custom = auth_client.post(
        "/api/ambient", json={"config": {**SHORT, "scene_preset": "custom", "scene_prompt": ""}}
    )
    assert empty_custom.status_code == 422
    assert auth_client.get("/api/ambient/999").status_code == 404
    # A storyboard project is not an ambient scene.
    pid = auth_client.post("/api/projects", json={"title": "x", "original_script": "hi"}).json()["id"]
    assert auth_client.get(f"/api/ambient/{pid}").status_code == 404


def test_requires_auth(client):
    assert client.get("/api/ambient").status_code == 401
    assert client.get("/api/ambient/presets").status_code == 401


def test_seed_creates_demo_ambient_scene(auth_client):
    assert auth_client.post("/api/system/seed").status_code == 200
    scenes = auth_client.get("/api/ambient").json()
    assert len(scenes) == 1
    assert scenes[0]["config"]["scene_preset"] == "fireplace"
    assert scenes[0]["status"] == "draft"
    assert scenes[0]["loop_shot"] is not None


def test_delete_scene_via_projects_endpoint(auth_client):
    sid = auth_client.post("/api/ambient", json={"config": SHORT, "generate": False}).json()["id"]
    assert auth_client.delete(f"/api/projects/{sid}").status_code == 204
    assert auth_client.get(f"/api/ambient/{sid}").status_code == 404


# ---------- compositor seam check (renderer, no HTTP) ----------


def test_seamless_unit_first_and_last_frames_match(tmp_path: Path):
    """A clip whose red channel ramps 0→255 over 4s is maximally non-loopable.
    After the tail→head crossfade + trim, the unit's last frame must land on
    (nearly) the same colour as its first — that's the whole trick."""
    from app.services.renderer import AmbientRenderInputs, compose_ambient

    ramp = tmp_path / "ramp.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi",
         "-i", "color=c=black:s=320x180:d=4:r=30,format=rgb24,geq=r='255*T/4':g=0:b=0",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast", str(ramp)],
        capture_output=True, text=True, check=True,
    )
    raw_first = _frame_mean_red(ramp, first=True, tmp=tmp_path)
    raw_last = _frame_mean_red(ramp, first=False, tmp=tmp_path)
    assert raw_last - raw_first > 150, "control: raw clip must have an obvious seam"

    out = compose_ambient(AmbientRenderInputs(
        project_id=0, out_dir=tmp_path / "out", clip_path=ramp,
        total_seconds=10, aspect_ratio="16:9",
        crossfade_seconds=1.0, fade_seconds=0.0, music_path=None,
    ))
    assert 2.8 <= out.unit_seconds <= 3.2  # 4s − 1s crossfade
    assert out.repeats == 4
    unit_first = _frame_mean_red(out.unit_path, first=True, tmp=tmp_path)
    unit_last = _frame_mean_red(out.unit_path, first=False, tmp=tmp_path)
    assert abs(unit_first - unit_last) < 25, (unit_first, unit_last)
    assert 9.0 <= _duration(out.final_video_path) <= 11.0
    assert not _has_audio(out.final_video_path)


def test_compose_ambient_rejects_bad_total(tmp_path: Path):
    from app.services.renderer import AmbientRenderInputs, compose_ambient

    with pytest.raises(ValueError):
        compose_ambient(AmbientRenderInputs(
            project_id=0, out_dir=tmp_path, clip_path=tmp_path / "x.mp4", total_seconds=0,
        ))


# ---------- loop planning against model limits ----------


def _caps(durations, first=True, last=True):
    from app.providers.base import VideoCapabilities

    return VideoCapabilities(durations=list(durations), first_frame=first, last_frame=last)


@pytest.mark.parametrize(
    "durations,target,expected",
    [
        ([5, 10], 30, [10, 10, 10]),
        ([5, 10], 25, [10, 10, 5]),
        ([5, 10], 10, [10]),
        ([4, 6, 8], 30, [8, 8, 8, 6]),  # Veo 3.1
        ([4, 6, 8], 60, [8] * 7 + [4]),
        (range(2, 31), 30, [30]),  # Wan 3.0 / Seedance 2.5
        (range(2, 31), 60, [30, 30]),
        (range(3, 16), 25, [15, 10]),  # Kling 3.0
    ],
)
def test_segments_fit_model_caps(durations, target, expected):
    from app.services.ambient import _segments_for

    assert _segments_for(target, _caps(durations)) == expected


def test_plan_open_loop_when_model_has_no_last_frame():
    from app.schemas.ambient import AmbientConfig
    from app.services.ambient import plan_loop

    cfg = AmbientConfig(loop_seconds=30, crossfade_seconds=2.0, total_seconds=3600)
    plan = plan_loop(cfg, _caps([2, 5, 10], first=True, last=False))  # Runway Gen-4.5
    assert plan.segments == [10, 10, 10]
    assert plan.anchored is False and plan.closed is False
    assert plan.seam_crossfade == 2.0
    assert plan.provider_calls == 3


def test_plan_single_clip_when_model_cannot_chain():
    from app.schemas.ambient import AmbientConfig
    from app.services.ambient import plan_loop

    cfg = AmbientConfig(loop_seconds=30)
    plan = plan_loop(cfg, _caps([5, 10], first=False, last=False))
    assert plan.segments == [10] and plan.strategy == "single"
    assert any("can't start from a given frame" in n for n in plan.notes)


def test_plan_single_clip_when_chaining_disabled():
    from app.schemas.ambient import AmbientConfig
    from app.services.ambient import plan_loop

    cfg = AmbientConfig(loop_seconds=30, chain_segments=False)
    plan = plan_loop(cfg, _caps([5, 10]))
    assert plan.segments == [10]
    assert plan.closed  # still anchored: one clip whose first == last frame


def test_chained_30s_loop_pins_first_and_last_frames(auth_client, monkeypatch):
    """End-to-end on the mock (10s cap): 30s loop → anchor still + 3 clips,
    each starting on the previous clip's last frame, the last one ending on
    the anchor, all frame images fetchable from the public frame route."""
    from app.providers import mock_video

    calls = []
    real_submit = mock_video.MockVideoProvider.submit

    def spy(self, **kw):
        calls.append(kw)
        return real_submit(self, **kw)

    monkeypatch.setattr(mock_video.MockVideoProvider, "submit", spy)

    cfg = {**SHORT, "loop_seconds": 30, "total_seconds": 120}
    scene = auth_client.post("/api/ambient", json={"config": cfg, "generate": True}).json()
    assert scene["loop_status"] == "completed", scene
    assert scene["plan"]["segments"] == [10, 10, 10]
    assert len(calls) == 3
    assert all(c["duration_seconds"] == 10 for c in calls)
    assert all(c["reference_strategy"] == "frame_images" for c in calls)
    assert all(c["settings"]["aspect_ratio"] == "16:9" for c in calls)
    assert all(c["settings"]["generate_audio"] is False for c in calls)

    first_urls = [c["references"][0].url for c in calls]
    assert [r.frame_type for r in calls[0]["references"]] == ["first_frame"]
    assert [r.frame_type for r in calls[1]["references"]] == ["first_frame"]
    assert [r.frame_type for r in calls[2]["references"]] == ["first_frame", "last_frame"]
    anchor = first_urls[0]
    assert calls[2]["references"][1].url == anchor  # closes on the anchor
    assert len(set(first_urls)) == 3  # each segment starts from a new frame

    # Frame images are public (providers fetch them) and real PNGs.
    for url in first_urls:
        path = url.split("http://test.local", 1)[1]
        r = auth_client.get(path)
        assert r.status_code == 200 and r.content[:4] == b"\x89PNG"
    assert auth_client.get("/api/public-ambient-frames/" + "x" * 40).status_code == 404

    # 3 × 10s joined with 0.5s crossfades ≈ 29s loop clip.
    clip = Path(scene["loop_shot"]["clip_path"])
    assert 28.5 <= _duration(clip) <= 29.6
    p = auth_client.get(f"/api/projects/{scene['id']}").json()
    segs = [s for s in p["shots"] if s["shot_type"] == "ambient_segment"]
    assert len(segs) == 3 and all(s["status"] == "completed" for s in segs)
    assert p["ambient_config_json"]["_generated"]["closed"] is True

    assert auth_client.post(f"/api/ambient/{scene['id']}/render").status_code == 202
    scene = auth_client.get(f"/api/ambient/{scene['id']}").json()
    log = scene["latest_render"]["render_log"]
    assert scene["latest_render"]["status"] == "completed", log[:400]
    assert "clips=3" in log and "seam=closed" in log
    assert 119.0 <= _duration(Path(scene["latest_render"]["final_video_path"])) <= 121.0

    # Regenerating with fewer segments leaves no stale segment shots.
    r = auth_client.patch(f"/api/ambient/{scene['id']}", json={"config": {**cfg, "loop_seconds": 10}})
    assert r.json()["loop_status"] == "pending"
    auth_client.post(f"/api/ambient/{scene['id']}/generate-loop")
    p = auth_client.get(f"/api/projects/{scene['id']}").json()
    assert len([s for s in p["shots"] if s["shot_type"] == "ambient_segment"]) == 1


def _frame_deltas(video: Path) -> list[float]:
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(video), "-vf",
         "scale=320:-1,tblend=all_mode=difference,signalstats,"
         "metadata=print:key=lavfi.signalstats.YAVG:file=-", "-f", "null", "-"],
        capture_output=True, text=True, check=True,
    ).stdout
    return [float(ln.split("=")[1]) for ln in out.splitlines() if "YAVG" in ln]


def _slow_drift_clip(path: Path, *, seconds: int, start: float) -> None:
    """Smooth, slowly moving footage (like a calm scene): a soft gradient
    image with a gentle push-in. `start` offsets the motion so chained
    segments continue each other."""
    grad = path.with_suffix(".png")
    Image.radial_gradient("L").resize((640, 360)).convert("RGB").save(grad)
    frames = seconds * 30
    subprocess.run(
        ["ffmpeg", "-y", "-loop", "1", "-framerate", "30", "-i", str(grad), "-vf",
         f"scale=1280:720,zoompan=z='1.0+0.15*(on+{start * 30})/600':d={frames}:s=320x180:fps=30",
         "-frames:v", str(frames), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "16", str(path)],
        capture_output=True, text=True, check=True,
    )


def _raw_deltas(inputs: list[Path], graph: str, out: str) -> list[float]:
    """Frame-to-frame mean luma delta of a filter graph's output, measured
    on decoded frames (no re-encode), so encoder keyframes can't mask or
    fake a step."""
    cmd = ["ffmpeg", "-v", "error"]
    for p in inputs:
        cmd += ["-i", str(p)]
    graph += (f";[{out}]scale=320:-1,tblend=all_mode=difference,signalstats,"
              "metadata=print:key=lavfi.signalstats.YAVG:file=-[m]")
    res = subprocess.run(cmd + ["-filter_complex", graph, "-map", "[m]", "-f", "null", "-"],
                         capture_output=True, text=True, check=True).stdout
    return [float(ln.split("=")[1]) for ln in res.splitlines() if "YAVG" in ln]


def _raw_yavg(inputs: list[Path], graph: str, out: str) -> list[float]:
    """Mean luma of every decoded frame a filter graph produces (no encode)."""
    cmd = ["ffmpeg", "-v", "error"]
    for p in inputs:
        cmd += ["-i", str(p)]
    graph += f";[{out}]signalstats,metadata=print:key=lavfi.signalstats.YAVG:file=-[m]"
    res = subprocess.run(cmd + ["-filter_complex", graph, "-map", "[m]", "-f", "null", "-"],
                         capture_output=True, text=True, check=True).stdout
    return [float(ln.split("=")[1]) for ln in res.splitlines() if "YAVG" in ln]


def _assert_linear_dissolve(got: list[float], a: list[float], b: list[float], label: str) -> None:
    """Every dissolve frame's mean luma must equal the weighted mix of its
    two sources; weight runs 0 → exactly 1 across the dissolve. Truncating
    blends sit ~0.5 low on every mixed frame; a weight that stops short of 1
    leaves the last frame off B."""
    n = len(got)
    assert n == len(a) == len(b) and n > 2, (n, len(a), len(b))
    for k in range(n):
        w = k / (n - 1)
        expected = (1 - w) * a[k] + w * b[k]
        assert abs(got[k] - expected) < 0.2, (
            f"{label}: frame {k}/{n - 1} mean luma {got[k]:.2f}, expected {expected:.2f}"
        )


def test_dissolves_are_unbiased_and_complete(tmp_path: Path):
    """Regression for a one-frame brightness tick at every loop seam (≈120×
    per hour): ffmpeg's xfade, and an unrounded blend, truncate mixed pixels
    (~0.85 step at dissolve start vs ~0.03 normal motion)."""
    from app.services.renderer import _probe_duration, join_graph, seamless_unit_graph

    fps, cf = 30, 1.5
    clip = tmp_path / "drift.mp4"
    _slow_drift_clip(clip, seconds=6, start=0)
    src = _raw_yavg([clip], "[0:v]null[o]", "o")
    L = _probe_duration(clip)
    unit = _raw_yavg([clip], seamless_unit_graph(length=L, crossfade=cf, out="o"), "o")
    k_cf, k_l = int(cf * fps), len(src)
    assert len(unit) == k_l - k_cf
    # unit = src[cf : L-cf] ++ dissolve(src[L-cf : L] → src[0 : cf])
    mid = k_l - 2 * k_cf
    assert all(abs(unit[i] - src[k_cf + i]) < 0.05 for i in range(mid))
    _assert_linear_dissolve(unit[mid:], src[k_l - k_cf:], src[:k_cf], "loop unit")
    # The wrap lands on src[cf]: the frame after the unit's last (= src[cf-1]).
    assert abs(unit[-1] - src[k_cf - 1]) < 0.2 and abs(unit[0] - src[k_cf]) < 0.05

    segs, lengths, ys = [], [], []
    for k in range(3):
        p = tmp_path / f"seg{k}.mp4"
        _slow_drift_clip(p, seconds=3, start=3 * k)
        segs.append(p)
        lengths.append(_probe_duration(p))
        ys.append(_raw_yavg([p], "[0:v]scale=320:180,setsar=1[o]", "o"))
    jcf = 0.5
    joined = _raw_yavg(segs, join_graph(lengths, crossfade=jcf, width=320, height=180), "out")
    kj, n0 = int(jcf * fps), len(ys[0])
    assert len(joined) == sum(len(y) for y in ys) - 2 * kj
    _assert_linear_dissolve(joined[n0 - kj:n0], ys[0][n0 - kj:], ys[1][:kj], "join 1")


def test_truncating_dissolve_is_caught(tmp_path: Path):
    """Guard the guard: the old unrounded expression fails the same check."""
    from app.services.renderer import _probe_duration

    fps, cf = 30, 1.5
    clip = tmp_path / "drift.mp4"
    _slow_drift_clip(clip, seconds=6, start=0)
    src = _raw_yavg([clip], "[0:v]null[o]", "o")
    L, k_cf = _probe_duration(clip), int(cf * fps)
    span = cf - 1 / fps
    bad = (
        f"[0:v]split=3[a][b][c];[a]trim=start={cf}:end={L - cf},setpts=PTS-STARTPTS[mid];"
        f"[b]trim=start={L - cf}:end={L},setpts=PTS-STARTPTS[tl];[c]trim=start=0:end={cf},setpts=PTS-STARTPTS[hd];"
        f"[tl][hd]blend=all_expr='A*(1-min(T/{span}\\,1))+B*min(T/{span}\\,1)':shortest=1[x];"
        "[mid][x]concat=n=2:v=1:a=0[o]"
    )
    unit = _raw_yavg([clip], bad, "o")
    mid = len(src) - 2 * k_cf
    with pytest.raises(AssertionError):
        _assert_linear_dissolve(unit[mid:], src[len(src) - k_cf:], src[:k_cf], "truncating")
