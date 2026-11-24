"""Known-colour probe: every RGB->YUV path must code RGB (200,120,40) as
BT.709 limited Y/U/V 129/85/166 (+-1) under a BT.709 tag. 132/81/169 is the
same colour coded with BT.601 (the fireplace bug); 127/90/162 is BT.601
limited data read as full range (washed out: black 30, white 218).
Real ffmpeg; no conversion when reading the coded planes back."""
import json
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from app.services import previews
from app.services import renderer as r

KNOWN = (200, 120, 40)
HEX = "#C87828"
BT709 = (129, 85, 166)


def _coded(path: Path, box: tuple[int, int, int, int], frame: int = 0) -> tuple[float, float, float]:
    """Mean coded Y, U, V over `box` (x, y, w, h; even x/y) of frame `frame`,
    read as the stream's own yuv420p (no matrix or range conversion)."""
    st = json.loads(subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,pix_fmt", str(path)], capture_output=True, check=True).stdout)["streams"][0]
    assert st["pix_fmt"] == "yuv420p"
    w, h = st["width"], st["height"]
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vf", f"select=eq(n\\,{frame})",
                          "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "yuv420p", "-"],
                         capture_output=True, check=True).stdout
    x, y, bw, bh = box
    Y, U, V = raw[:w * h], raw[w * h:w * h * 5 // 4], raw[w * h * 5 // 4:]
    ys = [Y[(y + j) * w + x + i] for j in range(bh) for i in range(bw)]
    cs = [((y + j) // 2) * (w // 2) + (x + i) // 2 for j in range(0, bh, 2) for i in range(0, bw, 2)]
    return (sum(ys) / len(ys), sum(U[k] for k in cs) / len(cs), sum(V[k] for k in cs) / len(cs))


def _space(path: Path) -> str:
    return json.loads(subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-select_streams", "v:0",
         "-show_entries", "stream=color_space", str(path)], capture_output=True, check=True).stdout)["streams"][0].get(
        "color_space", "")


def _assert_709(path: Path, box, frame: int = 0) -> None:
    got = _coded(path, box, frame)
    assert _space(path) == "bt709"
    assert max(abs(a - b) for a, b in zip(got, BT709, strict=True)) <= 1.0, \
        f"coded {tuple(round(v, 1) for v in got)}, want {BT709} (132/81/169 = BT.601 data under a 709 tag)"


@pytest.mark.parametrize("mode", ["RGB", "RGBA", "P", "JPEG"])
def test_still_drift_codes_known_colour_as_bt709(tmp_path, mode):
    img = tmp_path / "still.png"  # generated stills are written under .png whatever their format
    im = Image.new("RGB", (640, 360), KNOWN)
    if mode == "P":
        im = im.quantize(colors=4)  # an indexed PNG: swscale's palette path is always BT.601
    elif mode == "RGBA":
        im = im.convert("RGBA")
    im.save(img, format="JPEG" if mode == "JPEG" else "PNG", **({"quality": 100, "subsampling": 0}
                                                                 if mode == "JPEG" else {}))
    dst = tmp_path / "drift.mp4"
    r.render_image_drift_clip(img, dst, seconds=0.5, width=320, height=180)
    _assert_709(dst, (128, 60, 64, 60))  # frame 0: zoom 1, no drift


@pytest.mark.parametrize("logo", [None, "RGBA", "P"])
def test_end_card_codes_brand_colours_as_bt709(tmp_path, logo):
    logo_path = None
    if logo:
        logo_path = tmp_path / "logo.png"
        im = Image.new("RGB", (200, 100), KNOWN)
        (im.quantize(colors=4) if logo == "P" else im.convert("RGBA")).save(logo_path)
    ec = tmp_path / "endcard.mp4"
    r._make_end_card(ec, text="Go", width=320, height=180, duration=0.5,
                     bg_color="#0E0E12" if logo else HEX, text_color="#FFFFFF", logo_path=logo_path)
    # the corner (background), or the logo's centre (scaled to 0.42 W, centred, top at 0.22 H)
    box = (100, 52, 24, 12) if logo else (8, 8, 32, 32)
    _assert_709(ec, box)


def test_end_card_stays_bt709_through_concat_and_mux(tmp_path):
    png = tmp_path / "s.png"
    Image.new("RGB", (320, 180), KNOWN).save(png)
    shot, norm, ec = tmp_path / "shot.mp4", tmp_path / "norm.mp4", tmp_path / "ec.mp4"
    r.render_image_drift_clip(png, shot, seconds=0.5, width=320, height=180)
    r._normalize_clip(shot, norm, 320, 180)
    r._make_end_card(ec, text="Go", width=320, height=180, duration=0.5, bg_color=HEX)
    silent, final = tmp_path / "silent.mp4", tmp_path / "final.mp4"
    r._concat([norm, ec], silent)
    r._mux_audio_captions_overlays(silent, final, audio_path=None, music_path=None, music_volume=0.25,
                                   ass_path=None, disclosure_text=None)
    _assert_709(final, (8, 8, 32, 32), frame=2)    # the shot
    _assert_709(final, (8, 8, 32, 32), frame=20)   # the end card (shot is 15 frames)


@pytest.mark.parametrize("codec,pix,ext", [("ffv1", "gbrp", "mkv"), ("png", "rgb24", "mov"),
                                           ("qtrle", "rgb24", "mov")])
@pytest.mark.parametrize("path", ["normalize", "ambient", "preview"])
def test_rgb_video_source_codes_known_colour_as_bt709(tmp_path, codec, pix, ext, path):
    src = tmp_path / f"src.{ext}"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c={HEX}:s=320x180:r=30:d=0.5",
                    "-vf", f"format={pix}", "-c:v", codec, str(src)], check=True)
    dst = tmp_path / "out.mp4"
    if path == "normalize":
        r._normalize_clip(src, dst, 320, 180)
    elif path == "ambient":
        r._encode_normalized(src, dst, width=320, height=180, slow_factor=1.0)
    else:
        assert previews._make(src, dst)[0] == dst  # (path, ffmpeg rc) since #62
    _assert_709(dst, (128, 60, 64, 60))
