"""Colour correctness: sources are converted to BT.709 limited range and
outputs are tagged that way, so players don't shift the colours."""
import json
import subprocess
from pathlib import Path

import pytest

from app.services import renderer as r

RGB = (192, 64, 32)  # a saturated orange: matrix mistakes move it visibly


def _src(path: Path, *, tags: list[str], pix_fmt: str = "yuv420p", matrix: str = "bt601",
         rng: str = "tv") -> Path:
    """A 1 s solid-colour clip converted from RGB with `matrix`/`rng`,
    tagged with `tags` (empty: untagged, as video models return them)."""
    colour = "0x%02X%02X%02X" % RGB
    # Newer FFmpeg carries the scale's matrix into the stream tags, so an
    # untagged clip has them cleared explicitly (yuvj keeps full range by its pixel format).
    clear = "" if tags else ",setparams=range=unknown:color_primaries=unknown:color_trc=unknown:colorspace=unknown"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c={colour}:s=320x180:r=30:d=1",
                    "-vf", f"scale=out_color_matrix={matrix}:out_range={rng},format={pix_fmt}{clear}",
                    "-c:v", "libx264", "-crf", "1", *tags, str(path)], check=True)
    return path


def _rgb_as_709(path: Path) -> tuple[int, int, int]:
    """Centre pixel decoded the way a player shows a BT.709 limited clip."""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-frames:v", "1", "-vf",
                          "scale=in_color_matrix=bt709:in_range=tv,format=rgb24", "-f", "rawvideo", "-"],
                         capture_output=True, check=True).stdout
    w, h = r._probe_size(path)
    i = ((h // 2) * w + w // 2) * 3
    return raw[i], raw[i + 1], raw[i + 2]


def _rgb_as(path: Path, matrix: str) -> tuple[int, int, int]:
    """Centre pixel decoded with `matrix` (the source's true reading)."""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-frames:v", "1", "-vf",
                          f"scale=in_color_matrix={matrix}:in_range=tv,format=rgb24", "-f", "rawvideo", "-"],
                         capture_output=True, check=True).stdout
    w, h = r._probe_size(path)
    i = ((h // 2) * w + w // 2) * 3
    return raw[i], raw[i + 1], raw[i + 2]


def _tags(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json", "-select_streams", "v:0",
                          "-show_entries", "stream=color_space,color_primaries,color_transfer,color_range",
                          str(path)], capture_output=True, check=True).stdout
    return json.loads(out)["streams"][0]


def _off(a, b) -> int:
    return max(abs(x - y) for x, y in zip(a, b, strict=True))


def test_colour_vf_mapping():
    assert r.colour_vf("bt709", "tv") == ""
    assert r.colour_vf("bt709", "") == ""
    assert "ispace=bt470bg" in r.colour_vf("", "")
    assert "ispace=smpte170m" in r.colour_vf("smpte170m", "tv")
    assert "ispace=bt470bg" in r.colour_vf("bt470bg", "tv")
    # BT.709 primaries/transfer without a matrix tag: BT.709, not BT.601.
    assert r.colour_vf("", "", "bt709", "bt709") == ""
    assert "ispace=bt709" in r.colour_vf("", "pc", "bt709", "")
    assert "p=bt709" in r.colour_vf("bt2020nc", "tv")  # gamut, not just matrix
    hdr = r.colour_vf("bt2020nc", "tv", "smpte2084", "bt2020")
    assert "tin=smpte2084" in hdr and "tonemap=" in hdr and hdr.endswith("format=yuv420p")
    full = r.colour_vf("bt709", "pc")
    assert "ispace=bt709" in full and "irange=pc" in full and "range=tv" in full


@pytest.mark.parametrize("encode", ["ambient", "normalize"])
def test_untagged_bt601_source_converted_and_tagged(tmp_path, encode):
    src = _src(tmp_path / "src.mp4", tags=[])
    assert r.probe_colour(src) == ("", "", "", "")
    # Precondition: shown as BT.709 untouched, the source is visibly off.
    assert _off(_rgb_as_709(src), RGB) > 8
    dst = tmp_path / "out.mp4"
    if encode == "ambient":
        r._encode_normalized(src, dst, width=320, height=180, slow_factor=1.0)
    else:
        r._normalize_clip(src, dst, 320, 180)
    assert _off(_rgb_as_709(dst), RGB) <= 3
    t = _tags(dst)
    assert (t["color_space"], t["color_primaries"], t["color_transfer"], t["color_range"]) == \
        ("bt709", "bt709", "bt709", "tv")


def test_tagged_bt709_source_passes_through(tmp_path):
    src = _src(tmp_path / "src.mp4", matrix="bt709",
               tags=["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
                     "-color_range", "tv"])
    assert r.colour_vf(*r.probe_colour(src)) == ""
    dst = tmp_path / "out.mp4"
    r._encode_normalized(src, dst, width=320, height=180, slow_factor=1.0)
    assert _off(_rgb_as_709(dst), RGB) <= 3


def test_full_range_jpeg_style_source(tmp_path):
    src = _src(tmp_path / "src.mp4", tags=[], pix_fmt="yuvj420p", rng="pc")
    assert r.probe_colour(src)[1] == "pc"
    dst = tmp_path / "out.mp4"
    assert _off(_rgb_as_709(src), RGB) > 8  # shown as limited BT.709, it's visibly off
    r._encode_normalized(src, dst, width=320, height=180, slow_factor=1.0)
    assert _off(_rgb_as_709(dst), RGB) <= 5  # range squeeze adds a level or two of rounding
    assert _tags(dst)["color_range"] == "tv"


def test_join_converts_each_segment(tmp_path):
    a = _src(tmp_path / "a.mp4", tags=[])
    b = _src(tmp_path / "b.mp4", matrix="bt709", tags=["-colorspace", "bt709", "-color_range", "tv"])
    dst, _ = r.join_segments([a, b], tmp_path / "j.mp4", aspect_ratio="16:9", crossfade=0.2,
                             resolution="720p")
    assert _off(_rgb_as_709(dst), RGB) <= 3
    assert _tags(dst)["color_space"] == "bt709"


def test_still_image_drift_is_bt709(tmp_path):
    img = tmp_path / "still.png"
    colour = "0x%02X%02X%02X" % RGB
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c={colour}:s=640x360",
                    "-frames:v", "1", str(img)], check=True)
    dst = tmp_path / "drift.mp4"
    r.render_image_drift_clip(img, dst, seconds=1.0, width=320, height=180)
    assert _off(_rgb_as_709(dst), RGB) <= 3
    t = _tags(dst)
    assert (t["color_space"], t.get("color_primaries"), t.get("color_transfer")) == ("bt709", "bt709", "bt709")


def _y_values(path: Path) -> tuple[int, int]:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-frames:v", "1", "-vf", "extractplanes=y",
                          "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True, check=True).stdout
    return min(raw), max(raw)


def test_full_range_bt709_source_is_squeezed(tmp_path):
    """Tagged BT.709 but full range: the matrix stays, the range must still
    go 0-255 → 16-235 (not just be relabelled)."""
    src = tmp_path / "pc709.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=black:s=320x180:r=30:d=1[a];color=c=white:s=320x180:r=30:d=1[b];[a][b]hstack",
                    "-vf", "scale=out_color_matrix=bt709:out_range=pc,format=yuvj420p",
                    "-c:v", "libx264", "-qp", "0", "-colorspace", "bt709", "-color_range", "pc", str(src)],
                   check=True)
    assert r.probe_colour(src)[:2] == ("bt709", "pc")
    lo, hi = _y_values(src)
    assert lo <= 2 and hi >= 253
    for i, enc in enumerate((lambda d: r._normalize_clip(src, d, 640, 180),
                             lambda d: r._encode_normalized(src, d, width=640, height=180, slow_factor=1.0))):
        dst = tmp_path / f"out{i}.mp4"
        enc(dst)
        lo, hi = _y_values(dst)
        assert 12 <= lo <= 20 and 231 <= hi <= 239, (lo, hi)


def test_trim_converts_before_tagging(tmp_path):
    src = _src(tmp_path / "src.mp4", tags=[])
    dst = tmp_path / "trim.mp4"
    r.trim_clip(src, dst, duration=0.5)
    assert _off(_rgb_as_709(dst), RGB) <= 3
    assert _tags(dst)["color_space"] == "bt709"


def _wide(path: Path, transfer: str) -> Path:
    """RGB converted (by zimg) to BT.2020 primaries/matrix with `transfer`,
    10-bit, fully tagged: a wide-gamut SDR or an HDR (PQ) clip."""
    colour = "0x%02X%02X%02X" % RGB
    tag = {"709": "bt709", "smpte2084": "smpte2084"}[transfer]
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c={colour}:s=320x180:r=30:d=1",
                    "-vf", f"format=gbrpf32le,zscale=tin=709:pin=709:min=gbr:rin=full:"
                           f"p=2020:t={transfer}:m=2020_ncl:r=limited:npl=100,format=yuv420p10le",
                    "-c:v", "libx264", "-crf", "1", "-colorspace", "bt2020nc", "-color_primaries", "bt2020",
                    "-color_trc", tag, "-color_range", "tv", str(path)], check=True)
    return path


def test_wide_gamut_sdr_converted_to_709(tmp_path):
    src = _wide(tmp_path / "w.mkv", "709")
    assert r.probe_colour(src)[3] == "bt2020"
    assert _off(_rgb_as_709(src), RGB) > 8  # shown as BT.709 untouched: wrong
    dst = tmp_path / "out.mp4"
    r._encode_normalized(src, dst, width=320, height=180, slow_factor=1.0)
    assert _off(_rgb_as_709(dst), RGB) <= 6
    t = _tags(dst)
    assert (t["color_space"], t["color_primaries"], t["color_transfer"]) == ("bt709", "bt709", "bt709")


def test_hdr_pq_is_tone_mapped_before_tagging(tmp_path):
    src = _wide(tmp_path / "pq.mkv", "smpte2084")
    assert r.probe_colour(src)[2] == "smpte2084"
    before = _rgb_as_709(src)
    dst = tmp_path / "out.mp4"
    r._encode_normalized(src, dst, width=320, height=180, slow_factor=1.0)
    after = _rgb_as_709(dst)
    t = _tags(dst)
    assert (t["color_space"], t["color_primaries"], t["color_transfer"]) == ("bt709", "bt709", "bt709")
    # Tone mapping is approximate; it must keep the hue order (orange stays
    # orange) and land far closer to the picture than the raw PQ code values.
    assert after[0] > after[1] > after[2]
    assert _off(after, RGB) < _off(before, RGB)


def _plane_y(path: Path, w: int, h: int) -> bytes:
    return subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-frames:v", "1", "-vf",
                           "extractplanes=y", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                          capture_output=True, check=True).stdout[:w * h]


def test_grey_ramp_luma_unchanged(tmp_path):
    """Greys have no chroma: a matrix conversion must leave luma alone
    (swscale's YUV->YUV path lowered it by a level, darkening real clips)."""
    src = tmp_path / "ramp.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "nullsrc=s=320x180:r=30:d=1,geq=lum='16+219*X/W':cb=128:cr=128,format=yuv420p,"
                    "setparams=range=unknown:color_primaries=unknown:color_trc=unknown:colorspace=unknown",
                    "-c:v", "libx264", "-qp", "0", str(src)], check=True)
    assert r.probe_colour(src)[0] == ""
    dst = tmp_path / "out.mp4"
    r._normalize_clip(src, dst, 320, 180)
    a, b = _plane_y(src, 320, 180), _plane_y(dst, 320, 180)
    assert abs(sum(b) - sum(a)) / len(a) <= 0.5


def test_dark_low_saturation_footage_is_accurate(tmp_path):
    """Real ambient footage is dark and muted: converted and shown as BT.709
    it must stay within 0.75 (mean over R, G, B) of its BT.601 truth."""
    global RGB
    saved, RGB = RGB, (62, 50, 42)  # Y ~ 52, slightly warm
    try:
        src = _src(tmp_path / "dark.mp4", tags=[])
        dst = tmp_path / "out.mp4"
        r._encode_normalized(src, dst, width=320, height=180, slow_factor=1.0)
        got, truth = _rgb_as_709(dst), _rgb_as(src, "bt601")
        assert sum(abs(x - y) for x, y in zip(got, truth, strict=True)) / 3 <= 0.75, (got, truth)
    finally:
        RGB = saved


def test_bt709_primaries_without_matrix_tag_are_not_read_as_bt601(tmp_path):
    src = tmp_path / "part.mp4"
    colour = "0x%02X%02X%02X" % RGB
    # Matrix tag cleared on the frames (newer FFmpeg would carry the scale's
    # matrix into the stream), primaries and transfer kept as BT.709.
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c={colour}:s=320x180:r=30:d=1",
                    "-vf", "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p,"
                           "setparams=range=tv:colorspace=unknown:color_primaries=bt709:color_trc=bt709",
                    "-c:v", "libx264", "-crf", "1", "-color_primaries", "bt709", "-color_trc", "bt709",
                    str(src)], check=True)
    space, _rng, trc, prim = r.probe_colour(src)
    assert space == "" and (trc, prim) == ("bt709", "bt709")
    dst = tmp_path / "out.mp4"
    r._normalize_clip(src, dst, 320, 180)
    assert _off(_rgb_as_709(dst), RGB) <= 3


def test_hdr_is_tone_mapped_after_scaling_to_the_output(tmp_path, monkeypatch):
    """Tone-mapping runs at the output size (it was ~3x slower at a 1080p
    source); SDR sources still convert on their own pixels first."""
    monkeypatch.setattr(r, "probe_colour", lambda p, timeout=None: ("bt2020nc", "tv", "arib-std-b67", "bt2020"))
    vf = r._with_colour("scale=320:180,setsar=1", tmp_path / "x.mp4")
    assert vf.startswith("scale=320:180,setsar=1,") and "tonemap=" in vf
    monkeypatch.setattr(r, "probe_colour", lambda p, timeout=None: ("", "", "", ""))
    vf = r._with_colour("scale=320:180,setsar=1", tmp_path / "x.mp4")
    assert vf.startswith("crop=") and vf.endswith("scale=320:180,setsar=1")
