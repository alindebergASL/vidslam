"""Near-16:9 clips fill the frame (no thin black bars); clearly different
shapes are still padded. Sogni returns LTX-2.5 at 1280x704 and MiniMax H3 at
1344x768, and padding drew bars along the edges of every ambient loop."""
from __future__ import annotations

import subprocess
from pathlib import Path

from PIL import Image, ImageStat


def _clip(path: Path, w: int, h: int, seconds: float = 1.0) -> Path:
    # A flat mid-grey picture: any black at the edges comes from padding.
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c=0x808080:s={w}x{h}:r=24",
                    "-t", str(seconds), "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


def _edges(path: Path, tmp: Path) -> dict[str, float]:
    png = tmp / f"{path.stem}.png"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(path), "-frames:v", "1", str(png)], check=True)
    im = Image.open(png).convert("L")
    w, h = im.size
    boxes = {"top": (0, 0, w, 3), "bottom": (0, h - 3, w, h), "left": (0, 0, 3, h), "right": (w - 3, 0, w, h)}
    return {k: ImageStat.Stat(im.crop(b)).mean[0] for k, b in boxes.items()}


def test_fit_filter_fills_near_shapes_and_pads_others():
    from app.services.renderer import fit_vf

    def fills(size):
        return "crop=1920:1080" in fit_vf(1920, 1080, size)

    assert fills((1280, 704)) and fills((1344, 768)) and fills((2688, 1536))  # Sogni's sizes
    assert "pad=" not in fit_vf(1920, 1080, (1280, 704))
    assert fills((1883, 1000)) and not fills((1886, 1000))  # the 6% boundary, both sides
    assert not fills((1440, 1080)) and not fills((1680, 1050))  # 4:3 and 16:10 are padded, not cut
    assert not fills((1080, 1920)) and "pad=1920:1080" in fit_vf(1920, 1080, (1080, 1920))  # 9:16
    assert "pad=1920:1080" in fit_vf(1920, 1080, None)  # unknown size: the safe choice
    assert ":flags=lanczos" in fit_vf(1920, 1080, (1280, 704), flags="lanczos")


def test_the_crop_is_centred(tmp_path):
    from app.services.renderer import _encode_normalized

    # Grey 1344x768 with a red square in the middle: after filling 320x180,
    # the middle is still red and the edges grey (no bars, nothing shifted).
    src = tmp_path / "marked.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x808080:s=1344x768:r=24",
                    "-vf", "drawbox=x=622:y=334:w=100:h=100:color=red:t=fill", "-t", "1", "-pix_fmt", "yuv420p",
                    str(src)], check=True)
    out = tmp_path / "marked_out.mp4"
    _encode_normalized(src, out, width=320, height=180, slow_factor=1.0)
    png = tmp_path / "marked.png"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(out), "-frames:v", "1", str(png)], check=True)
    im = Image.open(png).convert("RGB")
    r, g, b = im.getpixel((160, 90))
    assert r > 150 and g < 100 and b < 100
    for xy in ((2, 90), (317, 90), (160, 2), (160, 177)):
        assert abs(im.getpixel(xy)[0] - 128) < 25 and abs(im.getpixel(xy)[1] - 128) < 25


def test_a_rotated_phone_clip_is_padded_not_cropped(tmp_path):
    from app.services.renderer import _encode_normalized, _probe_size

    flat = _clip(tmp_path / "flat.mp4", 640, 360)
    rotated = tmp_path / "rotated.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-display_rotation", "90", "-i", str(flat), "-c", "copy",
                    str(rotated)], check=True)
    assert _probe_size(rotated) == (360, 640)  # as ffmpeg shows it: portrait
    out = tmp_path / "rotated_out.mp4"
    _encode_normalized(rotated, out, width=320, height=180, slow_factor=1.0)
    e = _edges(out, tmp_path)
    assert e["left"] < 20 and e["right"] < 20


def test_sogni_sized_clips_have_no_black_bars(tmp_path):
    from app.services.renderer import _encode_normalized, _normalize_clip, join_segments

    for w, h in ((1280, 704), (1344, 768)):
        src = _clip(tmp_path / f"src_{w}.mp4", w, h)
        for fn in ("encode", "normalize"):
            out = tmp_path / f"{fn}_{w}.mp4"
            if fn == "encode":
                _encode_normalized(src, out, width=320, height=180, slow_factor=1.0)
            else:
                _normalize_clip(src, out, 320, 180)
            assert min(_edges(out, tmp_path).values()) > 100, (w, fn)
    # Chained segments joined in one graph fill too.
    segs = [_clip(tmp_path / f"seg{i}.mp4", 1344, 768, 1.5) for i in range(2)]
    joined, _ = join_segments(segs, tmp_path / "joined.mp4", aspect_ratio="16:9", crossfade=0.3)
    assert min(_edges(joined, tmp_path).values()) > 100


def test_a_portrait_clip_is_still_padded(tmp_path):
    from app.services.renderer import _encode_normalized

    src = _clip(tmp_path / "tall.mp4", 360, 640)
    out = tmp_path / "tall_out.mp4"
    _encode_normalized(src, out, width=320, height=180, slow_factor=1.0)
    e = _edges(out, tmp_path)
    assert e["left"] < 20 and e["right"] < 20  # bars, nothing cut off


def test_sogni_loop_models_are_offered_only_with_a_sogni_key(client, monkeypatch):
    from app import config
    from app.services import ambient, provider_keys

    ids = lambda: [c.id for c in ambient.presets().video_models]  # noqa: E731
    monkeypatch.setenv("SOGNI_API_KEY", "")  # whatever the environment has
    config.get_settings.cache_clear()
    provider_keys.invalidate()
    assert "sogni/minimax-h3-fasth3-flf2v-turbo-2stage" not in ids()
    monkeypatch.setenv("SOGNI_API_KEY", "sk-sogni-test")
    config.get_settings.cache_clear()
    provider_keys.invalidate()
    offered = ids()
    assert offered[0] == "alibaba/wan-3.0"  # still the default suggestion
    assert {"sogni/minimax-h3-fasth3-flf2v-turbo-2stage", "sogni/minimax-h3-fasth3-flf2v-turbo"} <= set(offered)
    assert "sogni/ltx25" not in offered  # poor loops in the blind test
    config.get_settings.cache_clear()


def test_a_4k_scene_joins_its_loop_at_4k(tmp_path):
    from app.services.renderer import _probe_size, join_segments

    seg = _clip(tmp_path / "sharp.mp4", 2688, 1536)
    joined, _ = join_segments([seg], tmp_path / "joined4k.mp4", aspect_ratio="16:9", resolution="4k")
    assert _probe_size(joined) == (3840, 2160)
