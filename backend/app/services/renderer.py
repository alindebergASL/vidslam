from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .captions import CaptionTiming, write_ass

# Defaults for 9:16 short-form output.
W, H = 1080, 1920
FPS = 30


@dataclass
class RenderInputs:
    project_id: int
    out_dir: Path
    clip_paths: list[Path]
    audio_path: Optional[Path]
    caption_timings: list[CaptionTiming]
    caption_style: str
    disclosure_text: Optional[str]
    cta_text: Optional[str]
    aspect_ratio: str = "9:16"
    music_path: Optional[Path] = None
    music_volume: float = 0.25
    end_card_bg_color: str = "#0E0E12"
    end_card_text_color: str = "#FFFFFF"
    logo_path: Optional[Path] = None
    caption_color: Optional[str] = None


@dataclass
class RenderOutputs:
    final_video_path: Path
    thumbnail_path: Path
    log: str


def _run(cmd: list[str]) -> tuple[int, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _dims_for_aspect(aspect: str, resolution: str = "1080p") -> tuple[int, int]:
    if aspect == "16:9":
        w, h = 1920, 1080
    elif aspect == "1:1":
        w, h = 1080, 1080
    else:
        w, h = W, H
    if resolution == "4k":
        return w * 2, h * 2
    return w, h


def _normalize_clip(src: Path, dst: Path, width: int, height: int) -> str:
    """Re-encode a clip to a uniform size/fps/codec so they can be concatenated."""
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"setsar=1,fps={FPS}"
    )
    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-vf", vf,
        "-an",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast",
        "-r", str(FPS),
        str(dst),
    ]
    rc, log = _run(cmd)
    if rc != 0:
        raise RuntimeError(f"ffmpeg normalize failed: {log[-2000:]}")
    return log


def _make_end_card(
    dst: Path,
    *,
    text: str,
    width: int,
    height: int,
    duration: float = 2.5,
    bg_color: str = "#0E0E12",
    text_color: str = "#FFFFFF",
    logo_path: Optional[Path] = None,
) -> str:
    """Generate a branded end card: solid bg, optional centered logo, centered text."""
    safe = text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "’")
    base = f"color=c={bg_color}:s={width}x{height}:d={duration}"

    if logo_path is not None and logo_path.exists():
        # Logo centered in the upper third, text below center.
        draw = (
            "drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:"
            f"text='{safe}':fontcolor={text_color}:fontsize=64:"
            f"x=(w-text_w)/2:y=h*0.60:line_spacing=18:box=0"
        )
        filter_complex = (
            f"[1:v]scale={int(width * 0.42)}:-1[lg];"
            f"[0:v][lg]overlay=(W-w)/2:H*0.22[bg];"
            f"[bg]{draw}[out]"
        )
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", base,
            "-i", str(logo_path),
            "-filter_complex", filter_complex,
            "-map", "[out]",
            "-t", str(duration),
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast",
            "-r", str(FPS),
            str(dst),
        ]
    else:
        vf = (
            f"{base},"
            f"drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:"
            f"text='{safe}':fontcolor={text_color}:fontsize=72:"
            f"x=(w-text_w)/2:y=(h-text_h)/2:line_spacing=18:box=0"
        )
        cmd = [
            "ffmpeg", "-y", "-f", "lavfi", "-i", vf,
            "-t", str(duration),
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast",
            "-r", str(FPS),
            str(dst),
        ]
    rc, log = _run(cmd)
    if rc != 0:
        raise RuntimeError(f"ffmpeg end-card failed: {log[-2000:]}")
    return log


def _concat(clips: list[Path], out: Path) -> str:
    listfile = out.parent / "concat.txt"
    listfile.write_text(
        "\n".join(f"file '{c.as_posix()}'" for c in clips) + "\n", encoding="utf-8"
    )
    cmd = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listfile),
        "-c", "copy",
        str(out),
    ]
    rc, log = _run(cmd)
    if rc != 0:
        # Fallback: re-encode (clips already normalized, but copy can fail across containers).
        cmd_re = [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listfile),
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast",
            "-r", str(FPS),
            str(out),
        ]
        rc2, log2 = _run(cmd_re)
        if rc2 != 0:
            raise RuntimeError(f"ffmpeg concat failed: {(log + log2)[-2000:]}")
        log += log2
    return log


def _probe_duration(path: Path) -> float:
    cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(path),
    ]
    rc, log = _run(cmd)
    if rc != 0:
        return 0.0
    try:
        return float(json.loads(log)["format"]["duration"])
    except (KeyError, ValueError):
        return 0.0


def _mux_audio_captions_overlays(
    silent_in: Path,
    out: Path,
    *,
    audio_path: Optional[Path],
    music_path: Optional[Path],
    music_volume: float,
    ass_path: Optional[Path],
    disclosure_text: Optional[str],
) -> str:
    """Burn captions + disclosure overlay + mix voiceover and optional music in one pass.

    Audio mix rules:
      - voiceover only         → voiceover (apad to video duration, AAC)
      - voiceover + music      → amix; music attenuated by `music_volume` (default 0.25)
      - music only             → music, looped to video duration
      - neither                → -an
    """
    vf_chain: list[str] = []
    if ass_path is not None:
        ass_str = str(ass_path).replace(":", r"\:").replace("'", r"\'")
        vf_chain.append(f"subtitles='{ass_str}'")
    if disclosure_text:
        safe = (
            disclosure_text.replace("\\", "\\\\")
            .replace(":", "\\:")
            .replace("'", "’")
        )
        vf_chain.append(
            "drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf:"
            f"text='{safe}':fontcolor=white:fontsize=30:"
            "x=40:y=h-th-40:box=1:boxcolor=black@0.55:boxborderw=14"
        )

    has_voice = audio_path is not None and audio_path.exists()
    has_music = music_path is not None and music_path.exists()

    cmd: list[str] = ["ffmpeg", "-y", "-i", str(silent_in)]
    if has_voice:
        cmd += ["-i", str(audio_path)]
    if has_music:
        # -stream_loop -1 loops the music file so it covers the full video duration.
        cmd += ["-stream_loop", "-1", "-i", str(music_path)]

    if vf_chain:
        cmd += ["-vf", ",".join(vf_chain)]

    cmd += [
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast",
        "-r", str(FPS),
    ]

    if has_voice and has_music:
        vol = max(0.0, min(1.0, music_volume))
        # 1:voice padded to video length; 2:music attenuated then mixed under voice.
        # amix duration=longest then -shortest on the video map gives us the right end.
        flt = (
            f"[1:a]apad[v];"
            f"[2:a]volume={vol:.3f}[m];"
            f"[v][m]amix=inputs=2:duration=longest:dropout_transition=2[a]"
        )
        cmd += [
            "-filter_complex", flt,
            "-map", "0:v:0", "-map", "[a]",
            "-c:a", "aac", "-b:a", "192k",
            "-shortest",
        ]
    elif has_voice:
        cmd += [
            "-c:a", "aac", "-b:a", "160k",
            "-af", "apad",
            "-map", "0:v:0", "-map", "1:a:0",
            "-shortest",
        ]
    elif has_music:
        cmd += [
            "-c:a", "aac", "-b:a", "192k",
            "-af", f"volume={max(0.0, min(1.0, music_volume)):.3f}",
            "-map", "0:v:0", "-map", "1:a:0",
            "-shortest",
        ]
    else:
        cmd += ["-an"]
    cmd += [str(out)]

    rc, log = _run(cmd)
    if rc != 0:
        raise RuntimeError(f"ffmpeg mux failed: {log[-2000:]}")
    return log


def _make_thumbnail(src: Path, dst: Path) -> str:
    cmd = [
        "ffmpeg", "-y", "-i", str(src), "-ss", "1.0", "-frames:v", "1",
        "-q:v", "3", str(dst),
    ]
    rc, log = _run(cmd)
    if rc != 0:
        raise RuntimeError(f"ffmpeg thumbnail failed: {log[-2000:]}")
    return log


# ---------------------------------------------------------------------------
# Ambient loop compositing
#
# Goal: an hour of "barely moving" scenery from one short clip, with no
# visible seam and no hour-long encode. Three ideas make that work:
#
#   1. Seamless loop unit. Crossfade the clip's tail into its own head
#      (xfade of the clip against a copy of itself at offset L-cf), then trim
#      [cf, L). The unit now *ends* on the same blended frame it *starts* on,
#      so unit→unit joins are invisible. Cost: one short encode.
#   2. Stream-copy tiling. Repeat the unit N = ceil(T / U) times with the
#      concat demuxer and `-c:v copy`. Identical encoder params on every
#      piece make this a byte-level splice — an hour tiles in seconds.
#   3. Fades live in the first/last piece only. Head = unit with a fade-in,
#      tail = unit with a fade-out; the middle pieces are untouched copies.
#      No full-length re-encode is ever needed.
#
# Audio gets the same treatment: the music bed is crossfaded against itself
# into a seamless audio unit, `-stream_loop -1`'d under the video, then faded
# at the ends and cut at T. AAC for an hour is ~15s of CPU.
# ---------------------------------------------------------------------------


@dataclass
class AmbientRenderInputs:
    project_id: int
    out_dir: Path
    clip_path: Path
    total_seconds: float
    aspect_ratio: str = "16:9"
    crossfade_seconds: float = 2.0
    slow_factor: float = 1.0
    fade_seconds: float = 4.0
    music_path: Optional[Path] = None
    music_volume: float = 0.7
    # When the clip is already periodic (image_drift), skip the video
    # crossfade: it would only soften a seam that doesn't exist.
    clip_is_periodic: bool = False
    resolution: str = "1080p"


@dataclass
class AmbientRenderOutputs:
    final_video_path: Path
    thumbnail_path: Path
    unit_path: Path
    unit_seconds: float
    repeats: int
    log: str


# Shared x264 settings so every tiled piece is bit-compatible for `-c copy`.
_X264 = ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast", "-crf", "20"]


def _encode_normalized(
    src: Path, dst: Path, *, width: int, height: int, slow_factor: float
) -> str:
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease:flags=lanczos,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
    )
    if slow_factor and abs(slow_factor - 1.0) > 1e-3:
        # Slow the clip down; fps= re-samples so the output stays constant-rate.
        vf += f",setpts={slow_factor:.4f}*PTS"
    vf += f",fps={FPS}"
    cmd = ["ffmpeg", "-y", "-i", str(src), "-vf", vf, "-an", *_X264, "-r", str(FPS), str(dst)]
    rc, log = _run(cmd)
    if rc != 0:
        raise RuntimeError(f"ffmpeg ambient normalize failed: {log[-2000:]}")
    return log


def _dissolve_expr(cf: float) -> str:
    """`blend` expression for a linear A→B dissolve over `cf` seconds.

    - Weight hits exactly 1 on the *last* dissolve frame (divide by
      cf − 1 frame), so what follows the dissolve continues B with no
      residue of A. Dividing by cf left ~2% of A on the final frame — a
      visible tick at the loop wrap.
    - +0.5 rounds: blend stores float results by truncation, which made
      every mixed frame ~0.5 luma darker than the pure frames around it.
    """
    span = max(1.0 / FPS, cf - 1.0 / FPS)
    w = f"min(T/{span:.5f}\\,1)"
    return f"A*(1-{w})+B*{w}+0.5"


def seamless_unit_graph(*, length: float, crossfade: float, src: str = "0:v", out: str = "v") -> str:
    """filter_complex text for the seamless loop unit (see _make_seamless_unit).
    Separate from the encode so tests can verify the dissolve on decoded
    frames, without encoder keyframes muddying the measurement."""
    cf = crossfade
    return (
        f"[{src}]split=3[a][b][c];"
        f"[a]trim=start={cf:.4f}:end={length - cf:.4f},setpts=PTS-STARTPTS[mid];"
        f"[b]trim=start={length - cf:.4f}:end={length:.4f},setpts=PTS-STARTPTS[tl];"
        f"[c]trim=start=0:end={cf:.4f},setpts=PTS-STARTPTS[hd];"
        f"[tl][hd]blend=all_expr='{_dissolve_expr(cf)}':shortest=1[x];"
        f"[mid][x]concat=n=2:v=1:a=0,format=yuv420p[{out}]"
    )


def _make_seamless_unit(norm: Path, unit: Path, *, length: float, crossfade: float) -> str:
    """Make `norm` loop without a visible cut. Output length = length - crossfade.

    unit = clip[cf, L-cf)  ++  blend(clip[L-cf, L) → clip[0, cf))
    The tail is cross-dissolved into the head with an explicit per-frame
    weight, so the unit ends on (almost exactly) clip[cf) — the frame it
    starts on — and wraps continuously. Uses trims + `blend` with explicit
    rounding rather than `xfade`, whose fade truncates mixed pixels and left
    a one-frame brightness step at the start of every dissolve (measured:
    0.85 vs ~0.03 typical frame-to-frame luma delta)."""
    flt = seamless_unit_graph(length=length, crossfade=crossfade)
    cmd = [
        "ffmpeg", "-y", "-i", str(norm),
        "-filter_complex", flt, "-map", "[v]", "-an",
        *_X264, "-r", str(FPS), str(unit),
    ]
    rc, log = _run(cmd)
    if rc != 0:
        raise RuntimeError(f"ffmpeg loop-unit failed: {log[-2000:]}")
    return log


def _faded_copy(unit: Path, dst: Path, *, unit_len: float, fade_in: float, fade_out: float) -> str:
    parts: list[str] = []
    if fade_in > 0:
        parts.append(f"fade=t=in:st=0:d={fade_in:.3f}")
    if fade_out > 0:
        parts.append(f"fade=t=out:st={max(0.0, unit_len - fade_out):.3f}:d={fade_out:.3f}")
    cmd = ["ffmpeg", "-y", "-i", str(unit)]
    if parts:
        cmd += ["-vf", ",".join(parts)]
    cmd += ["-an", *_X264, "-r", str(FPS), str(dst)]
    rc, log = _run(cmd)
    if rc != 0:
        raise RuntimeError(f"ffmpeg fade piece failed: {log[-2000:]}")
    return log


def _make_seamless_audio_unit(music: Path, dst: Path, *, crossfade: float) -> tuple[str, float]:
    """Same trick for audio: acrossfade the bed against itself, trim the
    overlap so the file loops cleanly. Returns (log, unit_seconds)."""
    length = _probe_duration(music)
    if length <= 0:
        raise RuntimeError("music bed has no readable duration")
    cf = min(crossfade, max(0.0, length / 3.0))
    if cf < 0.05:
        # Too short to crossfade meaningfully — just transcode as-is.
        cmd = ["ffmpeg", "-y", "-i", str(music), "-vn", "-c:a", "aac", "-b:a", "192k", str(dst)]
        rc, log = _run(cmd)
        if rc != 0:
            raise RuntimeError(f"ffmpeg audio unit failed: {log[-2000:]}")
        return log, _probe_duration(dst) or length
    flt = (
        f"[0:a][1:a]acrossfade=d={cf:.3f}:c1=tri:c2=tri,"
        f"atrim=start={cf:.3f}:end={length:.3f},asetpts=PTS-STARTPTS[a]"
    )
    cmd = [
        "ffmpeg", "-y", "-i", str(music), "-i", str(music),
        "-filter_complex", flt, "-map", "[a]",
        "-c:a", "aac", "-b:a", "192k", str(dst),
    ]
    rc, log = _run(cmd)
    if rc != 0:
        raise RuntimeError(f"ffmpeg audio unit failed: {log[-2000:]}")
    return log, _probe_duration(dst) or (length - cf)


def _tile_and_mux(
    pieces: list[Path],
    out: Path,
    *,
    total: float,
    music_unit: Optional[Path],
    music_volume: float,
    fade: float,
) -> str:
    listfile = out.parent / "ambient_concat.txt"
    listfile.write_text(
        "\n".join(f"file '{p.as_posix()}'" for p in pieces) + "\n", encoding="utf-8"
    )
    cmd: list[str] = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listfile)]
    if music_unit is not None:
        cmd += ["-stream_loop", "-1", "-i", str(music_unit)]
        vol = max(0.0, min(1.0, music_volume))
        af = [f"volume={vol:.3f}"]
        if fade > 0:
            af.append(f"afade=t=in:st=0:d={fade:.3f}")
            af.append(f"afade=t=out:st={max(0.0, total - fade):.3f}:d={fade:.3f}")
        cmd += [
            "-filter_complex", f"[1:a]{','.join(af)}[a]",
            "-map", "0:v:0", "-map", "[a]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        ]
    else:
        cmd += ["-map", "0:v:0", "-c:v", "copy", "-an"]
    cmd += ["-t", f"{total:.3f}", "-movflags", "+faststart", str(out)]
    rc, log = _run(cmd)
    if rc != 0:
        raise RuntimeError(f"ffmpeg ambient mux failed: {log[-2000:]}")
    return log


def compose_ambient(inputs: AmbientRenderInputs) -> AmbientRenderOutputs:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg binary not found on PATH")
    if inputs.total_seconds <= 0:
        raise ValueError("total_seconds must be positive")

    width, height = _dims_for_aspect(inputs.aspect_ratio, inputs.resolution)
    out = inputs.out_dir
    out.mkdir(parents=True, exist_ok=True)
    logs: list[str] = []

    # 1. Normalize (size/fps/codec, optional slow-down).
    norm = out / "ambient_norm.mp4"
    logs.append(_encode_normalized(
        inputs.clip_path, norm, width=width, height=height, slow_factor=inputs.slow_factor
    ))
    length = _probe_duration(norm)
    if length <= 0.5:
        raise RuntimeError("normalized loop clip is empty or unreadable")

    # 2. Seamless unit. Need room for the overlap on both sides of the trim.
    cf = 0.0 if inputs.clip_is_periodic else max(0.0, inputs.crossfade_seconds)
    unit = out / "ambient_unit.mp4"
    if cf > 0 and length > (2 * cf + 0.5):
        logs.append(_make_seamless_unit(norm, unit, length=length, crossfade=cf))
    else:
        shutil.copyfile(norm, unit)
    unit_len = _probe_duration(unit) or (length - cf)

    # 3. Tile plan. Fades are clamped so a single short unit still works.
    total = float(inputs.total_seconds)
    repeats = max(1, int(-(-total // unit_len)))  # ceil without importing math
    fade = max(0.0, min(inputs.fade_seconds, unit_len / 2.5))
    pieces: list[Path]
    if fade <= 0:
        pieces = [unit] * repeats
    elif repeats == 1:
        single = out / "ambient_single.mp4"
        logs.append(_faded_copy(unit, single, unit_len=unit_len, fade_in=fade, fade_out=fade))
        pieces = [single]
    else:
        head = out / "ambient_head.mp4"
        tail = out / "ambient_tail.mp4"
        logs.append(_faded_copy(unit, head, unit_len=unit_len, fade_in=fade, fade_out=0.0))
        logs.append(_faded_copy(unit, tail, unit_len=unit_len, fade_in=0.0, fade_out=fade))
        pieces = [head, *([unit] * (repeats - 2)), tail]

    # 4. Music unit (optional).
    music_unit: Optional[Path] = None
    if inputs.music_path is not None and inputs.music_path.exists():
        music_unit = out / "ambient_music_unit.m4a"
        mlog, _ = _make_seamless_audio_unit(
            inputs.music_path, music_unit, crossfade=max(1.0, inputs.crossfade_seconds)
        )
        logs.append(mlog)

    # 5. Tile + mux. The tail piece's fade-out lands at the end of the last
    #    repeat; -t trims any overshoot past `total` (stream-copy cuts land
    #    on packet boundaries, so expect ≤1 frame of slack).
    final = out / "final.mp4"
    logs.append(_tile_and_mux(
        pieces, final,
        total=total,
        music_unit=music_unit,
        music_volume=inputs.music_volume,
        fade=fade,
    ))

    # 6. Thumbnail — sample past the fade-in so it isn't a black frame.
    thumb = out / "thumb.jpg"
    rc, tlog = _run([
        "ffmpeg", "-y", "-ss", f"{min(fade + 1.0, max(0.0, total - 0.5)):.2f}",
        "-i", str(final), "-frames:v", "1", "-q:v", "3", str(thumb),
    ])
    if rc != 0:
        raise RuntimeError(f"ffmpeg thumbnail failed: {tlog[-2000:]}")
    logs.append(tlog)

    return AmbientRenderOutputs(
        final_video_path=final,
        thumbnail_path=thumb,
        unit_path=unit,
        unit_seconds=unit_len,
        repeats=repeats,
        log="\n".join(logs)[-8000:],
    )


def extract_frame(src: Path, dst: Path, *, last: bool) -> Path:
    """Write the first or last decoded frame of `src` as a PNG. The last frame
    is what the next chained segment starts from."""
    if last:
        dur = _probe_duration(src)
        cmd = ["ffmpeg", "-y", "-ss", f"{max(0.0, dur - 0.08):.3f}", "-i", str(src),
               "-frames:v", "1", "-update", "1", str(dst)]
    else:
        cmd = ["ffmpeg", "-y", "-i", str(src), "-frames:v", "1", "-update", "1", str(dst)]
    rc, log = _run(cmd)
    if rc != 0 or not dst.exists():
        raise RuntimeError(f"ffmpeg frame extract failed: {log[-1500:]}")
    return dst


def join_graph(lengths: list[float], *, crossfade: float, width: int, height: int) -> str:
    """filter_complex text joining len(lengths) inputs with rounded linear
    dissolves of `crossfade` seconds; output label [out]."""
    cf = crossfade
    norm = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps={FPS},format=yuv420p"
    )
    n = len(lengths)
    parts: list[str] = []
    order: list[str] = []
    for i in range(n):
        L = lengths[i]
        head_in = cf if (i > 0 and cf > 0) else 0.0  # covered by the previous dissolve
        tail_out = cf if (i < n - 1 and cf > 0) else 0.0  # dissolved into the next
        outs = ["b"] + (["h"] if head_in else []) + (["t"] if tail_out else [])
        labels = "".join(f"[{o}{i}]" for o in outs)
        parts.append(f"[{i}:v]{norm},split={len(outs)}{labels}" if len(outs) > 1 else f"[{i}:v]{norm}[b{i}]")
        parts[-1] = parts[-1].replace(f"[b{i}]", f"[bb{i}]")
        parts.append(f"[bb{i}]trim=start={head_in:.4f}:end={L - tail_out:.4f},setpts=PTS-STARTPTS[body{i}]")
        if head_in:
            parts.append(f"[h{i}]trim=start=0:end={cf:.4f},setpts=PTS-STARTPTS[head{i}]")
        if tail_out:
            parts.append(f"[t{i}]trim=start={L - cf:.4f}:end={L:.4f},setpts=PTS-STARTPTS[tail{i}]")
    for i in range(n):
        order.append(f"[body{i}]")
        if i < n - 1 and cf > 0:
            # Same rounded linear dissolve as _make_seamless_unit (see there).
            parts.append(
                f"[tail{i}][head{i + 1}]blend=all_expr='{_dissolve_expr(cf)}'"
                f":shortest=1[x{i}]"
            )
            order.append(f"[x{i}]")
    parts.append(f"{''.join(order)}concat=n={len(order)}:v=1:a=0,format=yuv420p[out]")
    return ";".join(parts)


def join_segments(
    segments: list[Path],
    dst: Path,
    *,
    aspect_ratio: str,
    crossfade: float = 0.5,
) -> tuple[Path, float]:
    """Join chained clips into one loop clip with short crossfades.

    Each segment already *starts* on the previous one's last frame, so the
    joins are near-continuous; the brief xfade only absorbs the small colour
    / motion jump image-to-video models introduce on their first frames.
    Segments are normalized inside the graph (size/fps/SAR/pix_fmt) because
    xfade requires identical streams. Returns (path, duration)."""
    if not segments:
        raise ValueError("no segments to join")
    width, height = _dims_for_aspect(aspect_ratio)
    if len(segments) == 1:
        _encode_normalized(segments[0], dst, width=width, height=height, slow_factor=1.0)
        return dst, _probe_duration(dst)
    lengths = [_probe_duration(p) for p in segments]
    cf = max(0.0, min(crossfade, min(lengths) / 3.0))
    graph = join_graph(lengths, crossfade=cf, width=width, height=height)
    prev = "out"
    cmd = ["ffmpeg", "-y"]
    for p in segments:
        cmd += ["-i", str(p)]
    cmd += ["-filter_complex", graph, "-map", f"[{prev}]", "-an",
            *_X264, "-r", str(FPS), str(dst)]
    rc, log = _run(cmd)
    if rc != 0:
        raise RuntimeError(f"ffmpeg segment join failed: {log[-2000:]}")
    return dst, _probe_duration(dst)


def join_audio(parts: list[Path], dst: Path, *, crossfade: float = 6.0) -> tuple[Path, float]:
    """Crossfade several music parts into one bed (AAC)."""
    if len(parts) == 1:
        cmd = ["ffmpeg", "-y", "-i", str(parts[0]), "-vn", "-c:a", "aac", "-b:a", "192k", str(dst)]
    else:
        flt, prev = [], "0:a"
        for i in range(1, len(parts)):
            out = f"a{i}"
            flt.append(f"[{prev}][{i}:a]acrossfade=d={crossfade:.2f}:c1=tri:c2=tri[{out}]")
            prev = out
        cmd = ["ffmpeg", "-y"]
        for p in parts:
            cmd += ["-i", str(p)]
        cmd += ["-filter_complex", ";".join(flt), "-map", f"[{prev}]", "-c:a", "aac", "-b:a", "192k", str(dst)]
    rc, log = _run(cmd)
    if rc != 0:
        raise RuntimeError(f"ffmpeg audio join failed: {log[-1500:]}")
    return dst, _probe_duration(dst)


def best_frame(src: Path, dst: Path, *, width: int, height: int, samples: int = 12) -> Path:
    """Pick the most thumbnail-worthy frame of `src`: sharp (edge energy),
    well exposed (mean luma near mid-grey) and colourful. Saved as a JPEG
    at width×height (1280×720 for 16:9 — YouTube's recommended size)."""
    import tempfile

    from PIL import Image, ImageFilter, ImageStat

    dur = max(0.5, _probe_duration(src))
    best, best_score = None, -1.0
    with tempfile.TemporaryDirectory() as td:
        for i in range(samples):
            t = dur * (i + 0.5) / samples
            f = Path(td) / f"f{i}.png"
            rc, _ = _run(["ffmpeg", "-y", "-ss", f"{t:.3f}", "-i", str(src), "-frames:v", "1",
                          "-vf", "scale=320:-2", str(f)])
            if rc != 0 or not f.exists():
                continue
            with Image.open(f) as im:
                rgb = im.convert("RGB")
                grey = rgb.convert("L")
                sharp = ImageStat.Stat(grey.filter(ImageFilter.FIND_EDGES)).var[0] ** 0.5
                mean = ImageStat.Stat(grey).mean[0]
                exposure = 1.0 - abs(mean - 118) / 118
                sat = ImageStat.Stat(rgb.convert("HSV")).mean[1] / 255
                score = sharp * (0.5 + exposure) * (0.6 + sat)
            if score > best_score:
                best, best_score = t, score
    if best is None:
        raise RuntimeError("no frame could be decoded for the thumbnail")
    vf = (f"scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,"
          f"crop={width}:{height}")
    rc, log = _run(["ffmpeg", "-y", "-ss", f"{best:.3f}", "-i", str(src), "-frames:v", "1",
                    "-vf", vf, "-q:v", "2", str(dst)])
    if rc != 0:
        raise RuntimeError(f"ffmpeg thumbnail failed: {log[-1000:]}")
    return dst


def render_image_drift_clip(
    image: Path,
    dst: Path,
    *,
    seconds: float,
    width: int,
    height: int,
    zoom_amount: float = 0.06,
) -> str:
    """Turn a still into a slow, *periodic* push-in/pull-out. The zoom follows
    1 + a·(1-cos(2πt/T))/2, which returns exactly to its start at t=T, so the
    clip loops with zero seam and needs no crossfade. A faint horizontal drift
    rides on the same period so the motion isn't a pure zoom."""
    frames = max(2, int(round(seconds * FPS)))
    # Upscale first so zoompan has sub-pixel headroom (avoids jitter).
    big_w, big_h = width * 2, height * 2
    z = f"1+{zoom_amount:.4f}*(1-cos(2*PI*on/{frames}))/2"
    x = f"iw/2-(iw/zoom/2)+(iw*0.01)*sin(2*PI*on/{frames})"
    y = "ih/2-(ih/zoom/2)"
    vf = (
        f"scale={big_w}:{big_h}:force_original_aspect_ratio=increase,"
        f"crop={big_w}:{big_h},"
        f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={width}x{height}:fps={FPS},"
        "setsar=1"
    )
    cmd = [
        "ffmpeg", "-y", "-loop", "1", "-i", str(image),
        "-vf", vf, "-frames:v", str(frames), "-an",
        *_X264, "-r", str(FPS), str(dst),
    ]
    rc, log = _run(cmd)
    if rc != 0:
        raise RuntimeError(f"ffmpeg image-drift failed: {log[-2000:]}")
    return log


def compose(inputs: RenderInputs) -> RenderOutputs:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg binary not found on PATH")

    width, height = _dims_for_aspect(inputs.aspect_ratio)
    inputs.out_dir.mkdir(parents=True, exist_ok=True)
    log_parts: list[str] = []

    # 1. Normalize each clip to a uniform size/fps/codec.
    normalized: list[Path] = []
    for i, clip in enumerate(inputs.clip_paths):
        norm = inputs.out_dir / f"norm_{i:02d}.mp4"
        log_parts.append(_normalize_clip(clip, norm, width, height))
        normalized.append(norm)

    # 2. Optional end card.
    if inputs.cta_text:
        ec = inputs.out_dir / "endcard.mp4"
        log_parts.append(
            _make_end_card(
                ec,
                text=inputs.cta_text,
                width=width,
                height=height,
                bg_color=inputs.end_card_bg_color,
                text_color=inputs.end_card_text_color,
                logo_path=inputs.logo_path,
            )
        )
        normalized.append(ec)

    # 3. Concat into a silent base video.
    silent = inputs.out_dir / "silent.mp4"
    log_parts.append(_concat(normalized, silent))

    # 4. Write ASS subtitle file (if any captions).
    ass_path: Optional[Path] = None
    if inputs.caption_timings:
        ass_path = inputs.out_dir / "captions.ass"
        write_ass(
            ass_path,
            timed=inputs.caption_timings,
            style_name=inputs.caption_style,
            width=width,
            height=height,
            primary_color=inputs.caption_color,
        )

    # 5. Burn captions + disclosure + mix audio (voiceover + optional music).
    final = inputs.out_dir / "final.mp4"
    log_parts.append(
        _mux_audio_captions_overlays(
            silent,
            final,
            audio_path=inputs.audio_path,
            music_path=inputs.music_path,
            music_volume=inputs.music_volume,
            ass_path=ass_path,
            disclosure_text=inputs.disclosure_text,
        )
    )

    # 6. Thumbnail.
    thumb = inputs.out_dir / "thumb.jpg"
    log_parts.append(_make_thumbnail(final, thumb))

    return RenderOutputs(
        final_video_path=final,
        thumbnail_path=thumb,
        log="\n".join(log_parts)[-8000:],
    )
