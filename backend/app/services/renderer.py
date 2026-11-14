from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Callable, Optional

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
    # Per clip, the most it may play (its shot's length): providers round a
    # clip's length up to what the model supports, and a longer clip would
    # push every later shot out of step with the voice and captions. None
    # (or a missing entry) = play the whole clip. Shorter clips are kept.
    clip_seconds: list[Optional[float]] = field(default_factory=list)
    # Per clip, True = play it for exactly its clip_seconds: a shorter clip
    # holds its last frame to that length (a hook-lab replacement clip must
    # fill its slot, or every later clip slides against the voiceover).
    clip_exact: list[bool] = field(default_factory=list)


@dataclass
class RenderOutputs:
    final_video_path: Path
    thumbnail_path: Path
    log: str



def textfile_arg(path: Path) -> str:
    """drawtext options that read text verbatim from `path` (no %{} expansion)."""
    p = str(path).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    return f"textfile='{p}':expansion=none"


def _run(cmd: list[str], timeout: Optional[float] = None,
         should_stop: Optional[Callable[[], bool]] = None) -> tuple[int, str]:
    """(exit code, output). With `timeout` (seconds) a run that takes longer
    is killed and answers (-1, "timed out ..."). With `should_stop`, it is
    asked about once a second while the command runs; when it says yes the
    command is killed and the answer is (-2, "stopped")."""
    if should_stop is None:
        rc, out, err = _run_parts(cmd, timeout)
        return rc, out + err
    deadline = None if timeout is None else time.monotonic() + timeout
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as proc:
        while True:
            try:
                out, err = proc.communicate(timeout=1.0)
                return proc.returncode, (out or "") + (err or "")
            except subprocess.TimeoutExpired:
                pass
            stop = False
            try:
                stop = bool(should_stop())
            except Exception:  # a failed check never stops the work
                stop = False
            if stop or (deadline is not None and time.monotonic() > deadline):
                proc.kill()
                proc.communicate()
                return (-2, "stopped") if stop else (-1, f"timed out after {timeout:.0f}s")


def _run_parts(cmd: list[str], timeout: Optional[float] = None) -> tuple[int, str, str]:
    """(exit code, stdout, stderr) of a tool, as _run (past `timeout`:
    (-1, "", "timed out ...")). Output that is parsed (ffprobe's JSON) is
    read from stdout alone: the error lines a damaged or cut-off file logs
    ("partial file", "non-existing PPS") go to stderr and would make it
    unreadable."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return -1, "", f"timed out after {timeout:.0f}s"
    return proc.returncode, proc.stdout or "", proc.stderr or ""


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


# A clip whose shape is within this much of the frame's is cropped to fill
# it instead of padded: video models return near-16:9 sizes (Sogni's LTX-2.5
# 1280x704, MiniMax H3 1344x768), and padding drew thin black bars along the
# edges of every loop. A clearly different shape (9:16 in a 16:9 frame) is
# still padded, so nothing important is cut off.
FILL_TOLERANCE = 0.06


def _probe_size(path: Path) -> Optional[tuple[int, int]]:
    """(width, height) of a file's first video stream as ffmpeg shows it,
    i.e. after rotation metadata (a phone's portrait clip), or None."""
    rc, log = _run(["ffprobe", "-v", "quiet", "-print_format", "json", "-select_streams", "v:0",
                    "-show_entries", "stream=width,height:stream_side_data=rotation:stream_tags=rotate", str(path)])
    if rc != 0:
        return None
    try:
        st = json.loads(log)["streams"][0]
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    return stream_size(st)


def stream_size(st: dict) -> Optional[tuple[int, int]]:
    """(width, height) of one ffprobe video stream entry (width, height and
    its rotation side data or tag), rotation applied, or None."""
    try:
        w, h = int(st["width"]), int(st["height"])
    except (KeyError, TypeError, ValueError):
        return None
    rotation = 0.0
    try:
        for sd in st.get("side_data_list") or []:
            if "rotation" in sd:
                rotation = float(sd["rotation"])
        if not rotation and (st.get("tags") or {}).get("rotate"):
            rotation = float(st["tags"]["rotate"])
    except (TypeError, ValueError):
        rotation = 0.0
    if round(abs(rotation)) % 180 == 90:
        w, h = h, w
    return (w, h) if w > 0 and h > 0 else None


def fit_vf(width: int, height: int, size: Optional[tuple[int, int]] = None, *, flags: str = "") -> str:
    """Scale into width x height: crop to fill when `size`'s shape is within
    FILL_TOLERANCE of the frame's, else scale to fit and pad (unknown size: pad)."""
    fl = f":flags={flags}" if flags else ""
    if size and abs((size[0] / size[1]) / (width / height) - 1.0) <= FILL_TOLERANCE:
        return f"scale={width}:{height}:force_original_aspect_ratio=increase{fl},crop={width}:{height}"
    return (f"scale={width}:{height}:force_original_aspect_ratio=decrease{fl},"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black")


def _normalize_clip(src: Path, dst: Path, width: int, height: int,
                    max_seconds: Optional[float] = None, *, exact: bool = False) -> str:
    """Re-encode a clip to a uniform size/fps/codec so they can be
    concatenated, cut to `max_seconds` when it is longer (`exact`: and held
    on its last frame up to `max_seconds` when it is shorter)."""
    vf = f"{fit_vf(width, height, _probe_size(src))},setsar=1,fps={FPS}"
    if exact and max_seconds and max_seconds > 0:
        vf += f",tpad=stop_mode=clone:stop_duration={max_seconds:.3f}"
    limit = ["-t", f"{max_seconds:.3f}"] if max_seconds and max_seconds > 0 else []
    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-vf", vf,
        *limit,
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
    label = dst.parent / f".{dst.stem}_text.txt"
    label.write_text(text, encoding="utf-8")
    txt = textfile_arg(label)
    base = f"color=c={bg_color}:s={width}x{height}:d={duration}"

    if logo_path is not None and logo_path.exists():
        # Logo centered in the upper third, text below center.
        draw = (
            "drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:"
            f"{txt}:fontcolor={text_color}:fontsize=64:"
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
            f"{txt}:fontcolor={text_color}:fontsize=72:"
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
    label.unlink(missing_ok=True)
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
    disclosure_file = None
    if disclosure_text:
        disclosure_file = silent_in.parent / f".{silent_in.stem}_disclosure.txt"
        disclosure_file.write_text(disclosure_text, encoding="utf-8")
        vf_chain.append(
            "drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf:"
            f"{textfile_arg(disclosure_file)}:fontcolor=white:fontsize=30:"
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
    # The padded voice (apad) never ends, so -shortest alone decides where
    # the file stops, and newer FFmpeg releases overshoot it by up to a
    # second or two of audio buffered past the last frame. Cap the output at
    # the picture's own length so every version ends with the video.
    video_seconds = _probe_duration(silent_in)
    if video_seconds > 0:
        cmd += ["-t", f"{video_seconds:.3f}"]
    cmd += [str(out)]

    rc, log = _run(cmd)
    if disclosure_file is not None:
        disclosure_file.unlink(missing_ok=True)
    if rc != 0:
        raise RuntimeError(f"ffmpeg mux failed: {log[-2000:]}")
    return log


# ---------- one frame of a video, as a picture (services/video_ops.py) ----------
#
# The frame saved for a time t is the one a paused player shows at t: the
# last frame whose timestamp is at or before t (a player shows each frame
# until the next one starts), never the next one. ffmpeg's own -ss gives
# the first frame at or after t, so the frame is found in two steps:
#   1. ffprobe decodes the frames around t (from the keyframe before
#      t - FRAME_LOOKBACK) and lists their exact timestamps; the last one at
#      or before t is the frame (the first frame when t comes before it).
#   2. ffmpeg decodes up to that exact timestamp and keeps only that frame,
#      as an 8-bit PNG (HDR tone-mapped first).
# A seek that lands in the wrong place (a file without an index, MPEG-TS)
# is noticed (no frame at or before t, or not the exact frame) and that
# step is decoded from the start instead. When ffprobe can't list the
# frames at all (a damaged or cut-off file), ffmpeg decodes from the start
# up to t (to the end for the last frame) and the last frame it gets to is
# the one; only a file ffmpeg can't decode at all is refused. ffprobe's
# listings are read from its stdout alone (its error lines go to stderr).
# Times count from the start of the file. The frame paths read videos
# inside an API request, so all the ffprobe/ffmpeg runs for one frame share
# one time limit (FRAME_TIMEOUT, or the caller's): past it the tool is
# killed and FFmpegTimeout is raised.

FRAME_TIMEOUT = 60.0
# Step 1 decodes from this far before t (a lower frame rate or a longer gap
# between frames than this falls back to decoding from the start), and
# FRAME_LOOKAHEAD past it, or at least 16 frames' worth: frames are stored
# out of order (B-frames), so a frame at or before t can be stored after
# one past it.
FRAME_LOOKBACK = 1.0
FRAME_LOOKAHEAD = 1.0
# How close step 2's frame must be to the timestamp step 1 found (frames
# are always further apart than this).
FRAME_MATCH = 0.0005
# PQ (HDR10, Dolby Vision's base layer) and HLG (phones): tone-mapped to
# standard range, so the picture isn't washed out.
HDR_TRANSFERS = ("smpte2084", "arib-std-b67")
TONEMAP = ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
           "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=pc")


class FFmpegTimeout(TimeoutError, RuntimeError):
    """ffprobe/ffmpeg ran past its time limit and was stopped. A
    TimeoutError (what a caller that sets a time limit catches) and a
    RuntimeError (a caller that only knows "couldn't read it" still
    refuses, never a crash)."""


class _Deadline:
    """The time left for every tool run of one frame grab."""

    def __init__(self, seconds: float) -> None:
        self.end = time.monotonic() + seconds

    def left(self) -> float:
        left = self.end - time.monotonic()
        if left <= 0:
            raise FFmpegTimeout("reading the video took too long")
        return left

    def run(self, cmd: list[str]) -> tuple[int, str]:
        """_run within the time left; FFmpegTimeout when it runs out."""
        rc, log = _run(cmd, timeout=self.left())
        if rc == -1:
            raise FFmpegTimeout(f"{Path(cmd[0]).name}: {log}")
        return rc, log

    def run_parts(self, cmd: list[str]) -> tuple[int, str, str]:
        """_run_parts within the time left; FFmpegTimeout when it runs out."""
        rc, out, err = _run_parts(cmd, timeout=self.left())
        if rc == -1:
            raise FFmpegTimeout(f"{Path(cmd[0]).name}: {err}")
        return rc, out, err


@dataclass
class _VideoInfo:
    # The video stream's length (the file's when it doesn't say); 0 = unknown.
    duration: float
    # Where 0 s is, in the file's own timestamps (its start, never below 0).
    start: Fraction
    # What ffmpeg's -ss counts from (the file's start, may be below 0).
    file_start: Fraction
    # Timestamp of the end of the picture, when the length is known.
    end: Optional[Fraction]
    fps: float  # 0 = unknown
    pix_fmt: str
    transfer: str


def _fraction(value: object) -> Optional[Fraction]:
    """"1/15360", "0.040000", 2 → a Fraction; None for "N/A", "0/0" and the like."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return Fraction(str(value).strip())
    except (ValueError, ZeroDivisionError):
        return None


def _probe_video(path: Path, deadline: Optional[_Deadline] = None) -> Optional[_VideoInfo]:
    """What a frame grab needs to know about `path`'s first video stream;
    None when ffprobe can't read it or it has no video."""
    cmd = ["ffprobe", "-v", "error", "-print_format", "json", "-select_streams", "v:0",
           "-show_entries", "stream=duration,start_time,avg_frame_rate,r_frame_rate,pix_fmt,"
           "color_transfer:stream_tags=DURATION:format=start_time,duration", str(path)]
    rc, out, _err = deadline.run_parts(cmd) if deadline else _run_parts(cmd)
    if rc != 0:
        return None
    try:
        data = json.loads(out)
    except ValueError:
        return None
    streams = data.get("streams") or []
    if not streams:
        return None
    stream, fmt = streams[0], data.get("format") or {}
    duration = 0.0
    # Audio can outlast the picture, so the file's length alone can point
    # past the final frame: the stream's own length first.
    raw = _fraction(stream.get("duration"))
    if raw is not None and raw > 0:
        duration = float(raw)
    tag = (stream.get("tags") or {}).get("DURATION")  # Matroska keeps it here
    if not duration and isinstance(tag, str):
        try:
            h, m, sec = tag.split(":")
            duration = max(int(h) * 3600 + int(m) * 60 + float(sec), 0.0)
        except ValueError:
            pass
    raw = _fraction(fmt.get("duration"))
    if not duration and raw is not None and raw > 0:
        duration = float(raw)
    file_start = _fraction(fmt.get("start_time")) or Fraction(0)
    start = max(file_start, Fraction(0))
    stream_start = _fraction(stream.get("start_time"))
    end = None
    if duration > 0:
        end = (stream_start if stream_start is not None else start) + Fraction(duration)
    rate = _fraction(stream.get("avg_frame_rate")) or _fraction(stream.get("r_frame_rate"))
    return _VideoInfo(duration=duration, start=start, file_start=file_start, end=end,
                      fps=float(rate) if rate and rate > 0 else 0.0,
                      pix_fmt=str(stream.get("pix_fmt") or ""), transfer=str(stream.get("color_transfer") or ""))


def _frame_times(src: Path, deadline: _Deadline, start: Optional[Fraction],
                 end: Optional[Fraction]) -> list[Fraction]:
    """Timestamps (in the file's own time) of the video frames decoded
    from the keyframe before `start` (None: from the beginning) to about
    `end` (None: to the end). Empty when none could be decoded there."""
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
           "stream=time_base:frame=best_effort_timestamp", "-of", "json"]
    if start is not None or end is not None:
        # Always with an end: an open "START%" can list nothing at all.
        cmd += ["-read_intervals", ("" if start is None else f"{math.floor(float(start) * 1e6) / 1e6:.6f}")
                + ("%+1000000" if end is None else f"%{math.ceil(float(end) * 1e6) / 1e6:.6f}")]
    # The frames listed before an error (a cut-off file) still count.
    _rc, out, _err = deadline.run_parts(cmd + [str(src)])
    try:
        data = json.loads(out)
    except ValueError:
        return []
    tb = _fraction(((data.get("streams") or [{}])[0]).get("time_base"))
    if not tb:
        return []
    out = []
    for frame in data.get("frames") or []:
        ts = frame.get("best_effort_timestamp")
        if isinstance(ts, int) and not isinstance(ts, bool):
            out.append(ts * tb)
    return out


def _has_alpha(pix_fmt: str) -> bool:
    return pix_fmt.startswith(("yuva", "gbrap", "ya", "rgba", "bgra", "argb", "abgr"))


def _write_frame(src: Path, dst: Path, info: _VideoInfo, at: Fraction, deadline: _Deadline) -> bool:
    """Write the frame with timestamp `at` (as _frame_times lists it) to
    `dst` as an 8-bit PNG (RGBA when the video has transparency). False
    when it couldn't be written."""
    out_fmt = "rgba" if _has_alpha(info.pix_fmt) else "rgb24"
    pick = f"select=between(t\\,{float(at) - FRAME_MATCH:.6f}\\,{float(at) + FRAME_MATCH:.6f})"
    chains = [f"{pick},{TONEMAP},format={out_fmt}"] if info.transfer in HDR_TRANSFERS else []
    chains.append(f"{pick},format={out_fmt}")  # also when the tone-mapping filters are missing
    # -ss counts from the file's start; seek a little before the frame
    # (a keyframe before it), else decode from the beginning.
    seek = float(at - info.file_start) - 0.5
    seeks = ([seek] if seek > 0 else []) + [None]
    for chain in chains:
        for ss in seeks:
            dst.unlink(missing_ok=True)
            cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error", "-copyts"]
            if ss is not None:
                cmd += ["-ss", f"{ss:.6f}", "-t", "1.5"]
            else:
                cmd += ["-t", f"{max(float(at - info.file_start), 0.0) + 1.0:.6f}"]
            rc, _log = deadline.run(cmd + ["-i", str(src), "-map", "0:v:0", "-an", "-sn", "-dn", "-vf", chain,
                                           "-frames:v", "1", "-update", "1", str(dst)])
            if rc == 0 and dst.exists() and dst.stat().st_size > 0:
                return True
            if rc != 0:
                break  # ffmpeg itself failed (e.g. no zscale): the next way of converting
    dst.unlink(missing_ok=True)
    return False


def _target(info: _VideoInfo, t: float) -> Fraction:
    """`t` seconds in, in the file's own time: to the microsecond, rounded
    down (never past the frame on screen)."""
    return info.start + Fraction(math.floor(t * 1_000_000 + 1e-6), 1_000_000)


_SHOWINFO_TB = re.compile(r"config in time_base:\s*(\d+)/(\d+)")
_SHOWINFO_PTS = re.compile(r"\bn:\s*\d+\s+pts:\s*(-?\d+)")


def _last_decoded(src: Path, info: _VideoInfo, until: Optional[Fraction], deadline: _Deadline) -> Optional[Fraction]:
    """Timestamp (in the file's own time, as _frame_times) of the last
    video frame ffmpeg itself decodes, from the start, at or before
    `until` (None: to the end). The last resort when ffprobe couldn't list
    the frames (a damaged or cut-off file: an interrupted recording or
    copy stops before its stated length, and ffmpeg still decodes it up to
    the cut). Decoded to ffmpeg's null output, so no picture is made per
    frame; _write_frame then writes the one found. None when no frame came
    out."""
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-v", "info", "-copyts"]
    vf = "showinfo"
    if until is not None:
        cmd += ["-t", f"{max(float(until - info.file_start), 0.0) + 1.0:.6f}"]
        vf = f"select=lte(t\\,{float(until):.6f}),showinfo"
    _rc, _out, err = deadline.run_parts(cmd + ["-i", str(src), "-map", "0:v:0", "-an", "-sn", "-dn", "-vf", vf,
                                               "-f", "null", "-"])
    tb: Optional[Fraction] = None
    last: Optional[Fraction] = None
    for line in err.splitlines():
        if m := _SHOWINFO_TB.search(line):
            tb = Fraction(int(m[1]), int(m[2])) if int(m[2]) else None
        elif tb and (m := _SHOWINFO_PTS.search(line)):
            ts = int(m[1]) * tb
            last = ts if last is None else max(last, ts)
    return last


def _find_frame(src: Path, info: _VideoInfo, t: float, deadline: _Deadline) -> Optional[Fraction]:
    """Timestamp of the frame shown at `t` seconds (math.inf: the last
    frame): the last one at or before it, the first one when `t` is before
    it. None when no frame could be found."""
    if not math.isfinite(t):
        tries = ([info.end - Fraction(FRAME_LOOKBACK)] if info.end is not None else []) + [None]
        for start in tries:
            times = _frame_times(src, deadline, start if start is not None and start > info.start else None, None)
            if times:
                return max(times)
        return None
    target = _target(info, t)
    if info.end is not None:
        target = min(target, info.end)
    ahead = max(FRAME_LOOKAHEAD, 16 / info.fps) if info.fps else 2 * FRAME_LOOKAHEAD
    end = target + Fraction(ahead)
    start = target - Fraction(FRAME_LOOKBACK)
    for begin in ([start] if start > info.start else []) + [None]:
        times = _frame_times(src, deadline, begin, end)
        if not times:
            continue
        before = [x for x in times if x <= target]
        if before:
            return max(before)
        if begin is None:
            return min(times)  # before the first frame: the first frame
    return None


def extract_frame_at(src: Path, dst: Path, t: float, *, timeout: Optional[float] = None) -> float:
    """Write the frame of the video `src` that a paused player shows `t`
    seconds in (math.inf, or past the end: the last frame) to `dst`, an
    8-bit PNG at the video's own size, rotation applied, HDR tone-mapped.
    Returns that frame's time. Raises RuntimeError when the file isn't a
    video ffmpeg can decode, FFmpegTimeout (a TimeoutError and a
    RuntimeError) past `timeout` seconds for all its ffprobe/ffmpeg runs
    together (FRAME_TIMEOUT when not given)."""
    deadline = _Deadline(FRAME_TIMEOUT if timeout is None else timeout)
    info = _probe_video(src, deadline)
    if info is None:
        raise RuntimeError("couldn't read that video")
    t = max(float(t), 0.0) if not math.isnan(t) else 0.0
    at = _find_frame(src, info, t, deadline)
    if at is not None and _write_frame(src, dst, info, at, deadline):
        return max(round(float(at - info.start), 3), 0.0)
    # ffprobe couldn't list the frames there: ffmpeg decodes from the start
    # up to the time (to the end for the last frame, or for a time past
    # where a cut-off file stops) and the last frame that came out is it.
    found = _last_decoded(src, info, _target(info, t) if math.isfinite(t) else None, deadline)
    if found is not None and found != at and _write_frame(src, dst, info, found, deadline):
        return max(round(float(found - info.start), 3), 0.0)
    # No frame at or before the time (it comes before the first one) or no
    # timestamps to go by: ffmpeg's own seek (the frame at or after the
    # time), as close to the end as it gets for the last frame.
    fmt = "rgba" if _has_alpha(info.pix_fmt) else "rgb24"
    log = ""
    if math.isfinite(t) or info.duration > 0:
        near = min(t, info.duration - 0.05) if info.duration > 0 else t
        for ss in dict.fromkeys(x for x in (max(near, 0.0), near - 0.25) if x >= 0.0):
            dst.unlink(missing_ok=True)
            rc, log = deadline.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-ss", f"{ss:.3f}", "-i", str(src),
                                    "-map", "0:v:0", "-an", "-vf", f"format={fmt}", "-frames:v", "1", "-update", "1",
                                    str(dst)])
            if rc == 0 and dst.exists() and dst.stat().st_size > 0:
                return round(ss, 3)
    dst.unlink(missing_ok=True)
    raise RuntimeError(f"ffmpeg frame grab failed: {log[-2000:]}")


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
    # Nature-sound bed (rain, waves…), looped and mixed with the music.
    sound_path: Optional[Path] = None
    sound_volume: float = 0.6
    # When the clip is already periodic (image_drift), skip the video
    # crossfade: it would only soften a seam that doesn't exist.
    clip_is_periodic: bool = False
    resolution: str = "1080p"
    # Less repetition: more loop clips of the same scene, played after the
    # first one. "takes" cycles A, B, C, A, B, C…; "day_night" plays each
    # for an equal share of the total (dawn → … → night). Each change is a
    # dissolve of `transition_seconds`.
    extra_clips: list[Path] = field(default_factory=list)
    variety: str = "none"
    transition_seconds: float = 2.0
    # Overlays (see AmbientConfig). Title and watermark ride on pieces that
    # are encoded anyway; a timer re-encodes the final video once.
    overlay_title: str = ""
    title_seconds: float = 8.0
    watermark: str = ""
    watermark_position: str = "bottom_right"
    watermark_opacity: float = 0.6
    timer: str = "none"
    timer_position: str = "top_right"


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


_FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def _corner_xy(position: str, margin: int) -> str:
    x = f"w-tw-{margin}" if position.endswith("right") else str(margin)
    y = f"h-th-{margin}" if position.startswith("bottom") else str(margin)
    return f"x={x}:y={y}"


def _text_file(out: Path, name: str, text: str) -> Path:
    path = out / name
    path.write_text(text, encoding="utf-8")
    return path


def _text_width(text: str, font: str, size: int) -> float:
    from PIL import ImageFont

    return ImageFont.truetype(font, size).getlength(text)


def fit_lines(text: str, *, font: str, size: int, max_width: float, max_lines: int,
              min_size: int = 12) -> tuple[list[str], int]:
    """Wrap `text` on words into at most `max_lines` lines no wider than
    `max_width`, shrinking the font from `size` until it fits (drawtext
    neither wraps nor fits on its own, so long text would run off the
    frame). A single word too long for a line is kept whole at min_size."""
    words = text.split()
    while True:
        lines: list[str] = []
        for word in words:
            if lines and _text_width(f"{lines[-1]} {word}", font, size) <= max_width:
                lines[-1] = f"{lines[-1]} {word}"
            else:
                lines.append(word)
        fits = len(lines) <= max_lines and all(_text_width(ln, font, size) <= max_width for ln in lines)
        if fits or size <= min_size:
            return lines[:max_lines] if fits else [" ".join(words)], size
        size = max(min_size, int(size * 0.9))


def watermark_filter(out: Path, text: str, *, height: int, position: str, opacity: float,
                     width: Optional[int] = None) -> str:
    """drawtext for a small corner watermark ("" when there's none), shrunk
    to fit one line within the frame."""
    if not text:
        return ""
    width = width or height * 16 // 9
    m = int(height * 0.035)
    lines, size = fit_lines(text, font=_FONT, size=max(12, int(height * 0.032)),
                            max_width=width - 2 * m, max_lines=1, min_size=10)
    tf = _text_file(out, "overlay_watermark.txt", lines[0])
    return (f"drawtext=fontfile={_FONT}:{textfile_arg(tf)}:fontsize={size}:"
            f"fontcolor=white@{opacity:.2f}:shadowcolor=black@{opacity * 0.6:.2f}:shadowx=2:shadowy=2:"
            f"{_corner_xy(position, m)}")


def title_filter(out: Path, text: str, *, height: int, seconds: float,
                 width: Optional[int] = None) -> str:
    """drawtext for a centered title over the first `seconds`, fading in and
    out: wrapped onto up to 3 lines (each centered) and shrunk to fit."""
    if not text or seconds <= 0:
        return ""
    width = width or height * 16 // 9
    lines, size = fit_lines(text, font=_FONT_BOLD, size=max(18, int(height * 0.075)),
                            max_width=width * 0.88, max_lines=3)
    f = min(1.0, seconds / 4)
    alpha = f"if(lt(t,{f:.2f}),t/{f:.2f},if(lt(t,{seconds - f:.2f}),1,max(0,({seconds:.2f}-t)/{f:.2f})))"
    gap = int(size * 1.25)
    top = f"(h-{gap * len(lines)})/2"
    parts = []
    for i, line in enumerate(lines):
        tf = _text_file(out, f"overlay_title_{i}.txt", line)
        parts.append(f"drawtext=fontfile={_FONT_BOLD}:{textfile_arg(tf)}:fontsize={size}:fontcolor=white:"
                     f"shadowcolor=black@0.6:shadowx=3:shadowy=3:x=(w-tw)/2:y={top}+{i * gap}:"
                     f"alpha='{alpha}':enable='lt(t,{seconds:.2f})'")
    return ",".join(parts)


def timer_filter(out: Path, mode: str, *, total: float, height: int, position: str) -> str:
    """drawtext for a countdown / elapsed clock, mm:ss (h:mm:ss from an hour)."""
    if mode not in ("countdown", "elapsed"):
        return ""
    r = f"max(0,{total:.3f}-t)" if mode == "countdown" else "t"
    if total >= 3600:
        expr = (f"%{{eif:trunc({r}/3600):d:1}}:%{{eif:mod(trunc({r}/60),60):d:2}}:"
                f"%{{eif:mod(trunc({r}),60):d:2}}")
    else:
        expr = f"%{{eif:trunc({r}/60):d:2}}:%{{eif:mod(trunc({r}),60):d:2}}"
    tf = _text_file(out, "overlay_timer.txt", expr)
    size, m = max(16, int(height * 0.06)), int(height * 0.04)
    arg = textfile_arg(tf).replace("expansion=none", "expansion=normal")
    return (f"drawtext=fontfile={_FONT_BOLD}:{arg}:fontsize={size}:fontcolor=white@0.9:"
            f"shadowcolor=black@0.6:shadowx=2:shadowy=2:{_corner_xy(position, m)}")


def _encode_normalized(
    src: Path, dst: Path, *, width: int, height: int, slow_factor: float, extra_vf: str = ""
) -> str:
    vf = f"{fit_vf(width, height, _probe_size(src), flags='lanczos')},setsar=1"
    if slow_factor and abs(slow_factor - 1.0) > 1e-3:
        # Slow the clip down; fps= re-samples so the output stays constant-rate.
        vf += f",setpts={slow_factor:.4f}*PTS"
    vf += f",fps={FPS}"
    if extra_vf:
        vf += "," + extra_vf
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


def _faded_copy(unit: Path, dst: Path, *, unit_len: float, fade_in: float, fade_out: float,
                extra_vf: str = "") -> str:
    parts: list[str] = [extra_vf] if extra_vf else []
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


def _end_piece(unit: Path, dst: Path, *, unit_len: float, length: float,
               fade_in: float, fade_out: float, extra_vf: str = "") -> str:
    """The last `length` seconds of the tiled video, i.e. the unit (twice
    over if needed) cut to `length`, with the fade-out ending exactly at its
    end."""
    loops = 1 if length <= unit_len + 1e-3 else 2
    flt = ("[0:v][1:v]concat=n=2:v=1:a=0," if loops == 2 else "[0:v]")
    flt += f"trim=duration={length:.3f},setpts=PTS-STARTPTS"
    if extra_vf:
        flt += "," + extra_vf
    if fade_in > 0:
        flt += f",fade=t=in:st=0:d={fade_in:.3f}"
    if fade_out > 0:
        flt += f",fade=t=out:st={max(0.0, length - fade_out):.3f}:d={fade_out:.3f}"
    cmd = ["ffmpeg", "-y", "-i", str(unit)] + (["-i", str(unit)] if loops == 2 else [])
    cmd += ["-filter_complex", flt + "[v]", "-map", "[v]", "-an", *_X264, "-r", str(FPS), str(dst)]
    rc, log = _run(cmd)
    if rc != 0:
        raise RuntimeError(f"ffmpeg end piece failed: {log[-2000:]}")
    return log


# Equal-power (quarter-sine) curves for every audio crossfade. The two sides
# of a seam are different stretches of sound (the bed's tail vs its head, or
# two separately generated parts), i.e. uncorrelated, so a linear ("tri")
# fade sums to half power mid-overlap: a ~3 dB dip you can hear on steady
# beds like rain or a waterfall, once per loop for the whole video.
AUDIO_XFADE_CURVES = "c1=qsin:c2=qsin"

# Loop units are written as uncompressed PCM (.wav), not AAC: an AAC file
# carries encoder padding at its edges, and `-stream_loop` replays it on
# every pass, so an AAC unit drops out for ~10–30 ms at each loop point.
# PCM loops sample-exactly. Float, because an equal-power seam can peak up
# to ~3 dB above the source on a brickwalled master and 16-bit would clip it
# (volume is applied later, in _tile_and_mux). Render scratch, deleted after.
AUDIO_UNIT_CODEC = ("-c:a", "pcm_f32le")


def _make_seamless_audio_unit(music: Path, dst: Path, *, crossfade: float) -> tuple[str, float]:
    """Same trick for audio: crossfade the bed's tail into its own head so the
    file loops cleanly. `dst` should be a .wav (see AUDIO_UNIT_CODEC).
    Returns (log, unit_seconds).

    The unit is source[cf:] with its last `cf` seconds crossfaded into
    source[:cf], so it ends on the sample just before the one it starts on.
    Both cuts are at the same time `cf` in the decoded audio. The container's
    reported duration is never used as a cut point: for MP3/AAC it counts
    encoder delay and padding the decoder drops (ElevenLabs' 30 s MP3 probes
    as 30.04 s), and cutting there replayed or skipped 10–50 ms at every loop
    point — a click on tonal music."""
    length = _probe_duration(music)
    if length <= 0:
        raise RuntimeError("music bed has no readable duration")
    cf = min(crossfade, max(0.0, length / 3.0))
    if cf < 0.05:
        # Too short to crossfade meaningfully — just transcode as-is.
        cmd = ["ffmpeg", "-y", "-i", str(music), "-vn", *AUDIO_UNIT_CODEC, str(dst)]
        rc, log = _run(cmd)
        if rc != 0:
            raise RuntimeError(f"ffmpeg audio unit failed: {log[-2000:]}")
        return log, _probe_duration(dst) or length
    flt = (
        f"[0:a]asetpts=PTS-STARTPTS,atrim=start={cf:.3f},asetpts=PTS-STARTPTS[body];"
        f"[1:a]asetpts=PTS-STARTPTS,atrim=end={cf:.3f},asetpts=PTS-STARTPTS[head];"
        f"[body][head]acrossfade=d={cf:.3f}:{AUDIO_XFADE_CURVES}[a]"
    )
    cmd = [
        "ffmpeg", "-y", "-i", str(music), "-i", str(music),
        "-filter_complex", flt, "-map", "[a]",
        *AUDIO_UNIT_CODEC, str(dst),
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
    sound_unit: Optional[Path] = None,
    sound_volume: float = 0.6,
    video_vf: str = "",
) -> str:
    # A timer changes every frame, so the tiled video is re-encoded once;
    # otherwise it is stream-copied.
    vcodec = ["-vf", video_vf, *_X264, "-r", str(FPS)] if video_vf else ["-c:v", "copy"]
    listfile = out.parent / "ambient_concat.txt"
    listfile.write_text(
        "\n".join(f"file '{p.as_posix()}'" for p in pieces) + "\n", encoding="utf-8"
    )
    cmd: list[str] = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listfile)]
    # Each audio bed (music, nature sound) loops forever at its own volume;
    # with both, they're mixed. Fades apply to the mix.
    beds = [(p, v) for p, v in ((music_unit, music_volume), (sound_unit, sound_volume)) if p is not None]
    if beds:
        chains: list[str] = []
        for i, (bed, vol) in enumerate(beds, start=1):
            cmd += ["-stream_loop", "-1", "-i", str(bed)]
            chains.append(f"[{i}:a]volume={max(0.0, min(1.0, vol)):.3f}[b{i}]")
        fades = []
        if fade > 0:
            fades = [f"afade=t=in:st=0:d={fade:.3f}",
                     f"afade=t=out:st={max(0.0, total - fade):.3f}:d={fade:.3f}"]
        post = ",".join(fades) or "anull"
        if len(beds) == 1:
            graph = ";".join(chains) + f";[b1]{post}[a]"
        else:
            # normalize=0: keep each bed at the volume the user set.
            graph = ";".join(chains) + f";[b1][b2]amix=inputs=2:duration=longest:normalize=0,{post}[a]"
        cmd += [
            "-filter_complex", graph,
            "-map", "0:v:0", "-map", "[a]",
            *vcodec, "-c:a", "aac", "-b:a", "192k",
        ]
    else:
        cmd += ["-map", "0:v:0", *vcodec, "-an"]
    cmd += ["-t", f"{total:.3f}", "-movflags", "+faststart", str(out)]
    rc, log = _run(cmd)
    if rc != 0:
        raise RuntimeError(f"ffmpeg ambient mux failed: {log[-2000:]}")
    return log


def _transition_piece(a: Path, b: Path, dst: Path, *, b_len: float, dissolve: float,
                      a_offset: float = 0.0) -> str:
    """`b` played from its start for `b_len` seconds, starting as `a` (from
    `a_offset`, where the previous piece left a) and dissolving into `b`
    over `dissolve` seconds.

    It starts exactly where the previous piece left off and, when `b_len` is
    b's full length, ends on b's last frame, where the next play of b
    starts, so it drops into the stream-copied tiling with no cut on either
    side."""
    flt = (
        f"[0:v]trim=start={a_offset:.4f}:duration={b_len:.4f},setpts=PTS-STARTPTS[a];"
        f"[1:v]trim=duration={b_len:.4f},setpts=PTS-STARTPTS[b];"
        f"[a][b]blend=all_expr='{_dissolve_expr(dissolve)}':shortest=1,format=yuv420p[v]"
    )
    cmd = ["ffmpeg", "-y", "-stream_loop", "-1", "-i", str(a), "-i", str(b),
           "-filter_complex", flt, "-map", "[v]", "-an", *_X264, "-r", str(FPS), str(dst)]
    rc, log = _run(cmd)
    if rc != 0:
        raise RuntimeError(f"ffmpeg ambient transition failed: {log[-2000:]}")
    return log


def _end_piece_from(sources: list[Path], dst: Path, *, length: float,
                    fade_in: float, fade_out: float, extra_vf: str = "") -> str:
    """`sources` played back to back, cut to `length`, with the fades."""
    n = len(sources)
    flt = "".join(f"[{i}:v]" for i in range(n)) + (f"concat=n={n}:v=1:a=0," if n > 1 else "")
    flt += f"trim=duration={length:.3f},setpts=PTS-STARTPTS"
    if extra_vf:
        flt += "," + extra_vf
    if fade_in > 0:
        flt += f",fade=t=in:st=0:d={fade_in:.3f}"
    if fade_out > 0:
        flt += f",fade=t=out:st={max(0.0, length - fade_out):.3f}:d={fade_out:.3f}"
    cmd = ["ffmpeg", "-y"]
    for src in sources:
        cmd += ["-i", str(src)]
    cmd += ["-filter_complex", flt + "[v]", "-map", "[v]", "-an", *_X264, "-r", str(FPS), str(dst)]
    rc, log = _run(cmd)
    if rc != 0:
        raise RuntimeError(f"ffmpeg end piece failed: {log[-2000:]}")
    return log


def variety_slots(lengths: list[float], total: float) -> list[int]:
    """Mix of takes: which unit plays in each slot, A, B, C, A, B, C… until
    `total` is covered (the pattern repeats every sum(lengths))."""
    slots: list[int] = []
    covered, v = 0.0, 0
    while covered < total - 1e-3:
        slots.append(v)
        covered += lengths[v]
        v = (v + 1) % len(lengths)
    return slots


# A piece of the tiling: ("unit", v, len) a full play of unit v ·
# ("part", v, len) unit v from its start, cut short · ("trans", p, o, v, len)
# unit v from its start, dissolving in from unit p played from second o.
Piece = tuple


def variety_plan(variety: str, lengths: list[float], total: float) -> list[Piece]:
    """The pieces that cover `total` for a multi-variant scene.

    Day to night gives each variation exactly total/K: its block is whole
    plays of its unit, the last one cut at the boundary, and the next
    variation dissolves in from where that cut left off. So every time of
    day appears, even when total is shorter than all the loops together."""
    pieces: list[Piece] = []
    if variety != "day_night":
        prev: Optional[int] = None
        for v in variety_slots(lengths, total):
            pieces.append(("unit", v, lengths[v]) if prev is None else ("trans", prev, 0.0, v, lengths[v]))
            prev = v
        return pieces
    k = len(lengths)
    t, prev, phase = 0.0, None, 0.0
    for v in range(k):
        end = total if v == k - 1 else total * (v + 1) / k
        first = True
        while t < end - 1e-3:
            length = min(lengths[v], end - t)
            full = length >= lengths[v] - 1e-3
            if first and prev is not None:
                pieces.append(("trans", prev, phase, v, length))
            else:
                pieces.append(("unit", v, lengths[v]) if full else ("part", v, length))
            t += length
            phase = 0.0 if full else length
            first = False
        prev = v
    return pieces


def _variety_pieces(units: list[Path], lengths: list[float], out: Path, *, variety: str,
                    total: float, fade: float, dissolve: float, title_vf: str = "",
                    title_seconds: float = 0.0) -> tuple[list[Path], list[str]]:
    """Files that cover exactly `total` for a multi-variant scene (see
    variety_plan). Transitions and cut-short plays are encoded once each;
    whole plays of a unit are stream-copied."""
    logs: list[str] = []
    made: dict[tuple, Path] = {}
    pieces: list[Path] = []
    plens: list[float] = []
    for spec in variety_plan(variety, lengths, total):
        length = spec[-1]
        if spec[0] == "unit":
            path = units[spec[1]]
        else:
            key = tuple(round(x, 3) if isinstance(x, float) else x for x in spec)
            if key not in made:
                dst = out / f"ambient_{'_'.join(str(x) for x in key)}.mp4"
                if spec[0] == "part":
                    logs.append(_end_piece_from([units[spec[1]]], dst, length=length, fade_in=0.0, fade_out=0.0))
                else:
                    _, p, o, v, _ = spec
                    cf = max(0.1, min(dissolve, length / 2))
                    logs.append(_transition_piece(units[p], units[v], dst, b_len=length, dissolve=cf, a_offset=o))
                made[key] = dst
            path = made[key]
        pieces.append(path)
        plens.append(length)

    # The end piece: the last piece cut to what's left (with the fade-out
    # ending at `total`), absorbing the piece before it if too little is left.
    remainder = total - sum(plens[:-1])
    tail_from = len(pieces) - 1
    if tail_from >= 1 and remainder < fade + 0.5:
        tail_from -= 1
        remainder += plens[tail_from]
    # The title can outlast the first piece: the opening spans as many
    # pieces as it needs; if that reaches the end piece, it is all one piece.
    n_head = 1
    if title_vf:
        while n_head < len(plens) and sum(plens[:n_head]) < title_seconds - 1e-6:
            n_head += 1
        if n_head > tail_from:
            tail_from, remainder = 0, total
    head_in_tail = tail_from == 0
    end = out / "ambient_tail.mp4"
    logs.append(_end_piece_from(pieces[tail_from:], end, length=remainder,
                                fade_in=fade if head_in_tail else 0.0, fade_out=fade,
                                extra_vf=title_vf if head_in_tail else ""))
    body = pieces[:tail_from]
    if body and (fade > 0 or title_vf):
        head = out / "ambient_head.mp4"
        logs.append(_end_piece_from(body[:n_head], head, length=sum(plens[:n_head]), fade_in=fade,
                                    fade_out=0.0, extra_vf=title_vf))
        body = [head, *body[n_head:]]
    return [*body, end], logs


def _single_unit_pieces(unit: Path, unit_len: float, out: Path, *, total: float,
                        fade_seconds: float, logs: list[str], title_vf: str = "",
                        title_seconds: float = 0.0) -> tuple[list[Path], int, float]:
    """Pieces covering exactly `total` for one loop unit."""
    # Tile plan. The pieces cover exactly `total`: head (fade-in), plain
    #    units, and an end piece cut to the remaining time whose fade-out
    #    ends at `total`. (Fading the last full unit instead got cut off
    #    whenever repeats × unit overshot total.)
    fade = max(0.0, min(fade_seconds, unit_len / 2.5, total / 2.5))
    full = int(total // unit_len)
    remainder = total - full * unit_len
    if remainder < 0.05:  # lands on a unit boundary
        full, remainder = full - 1, unit_len
    if full >= 1 and remainder < fade + 0.5:
        # Too little left for a full fade: the end piece absorbs one unit.
        full, remainder = full - 1, remainder + unit_len
    repeats = full + 1
    if full == 0:
        single = out / "ambient_single.mp4"
        logs.append(_end_piece(unit, single, unit_len=unit_len, length=remainder, fade_in=fade, fade_out=fade,
                               extra_vf=title_vf))
        pieces = [single]
    else:
        end = out / "ambient_tail.mp4"
        logs.append(_end_piece(unit, end, unit_len=unit_len, length=remainder, fade_in=0.0, fade_out=fade))
        # The title can outlast one unit: the opening piece is then that
        # many units, encoded once.
        n = max(1, math.ceil(title_seconds / unit_len - 1e-6)) if title_vf else 1
        if n > full:
            # The title runs into the end piece (a short video): one piece.
            single = out / "ambient_single.mp4"
            logs.append(_end_piece_from([unit] * math.ceil(total / unit_len + 1e-6), single, length=total,
                                        fade_in=fade, fade_out=fade, extra_vf=title_vf))
            pieces = [single]
        elif fade > 0 or title_vf:
            head = out / "ambient_head.mp4"
            logs.append(_end_piece_from([unit] * n, head, length=n * unit_len, fade_in=fade,
                                        fade_out=0.0, extra_vf=title_vf))
            pieces = [head, *([unit] * (full - n)), end]
        else:
            pieces = [*([unit] * full), end]
    return pieces, repeats, fade


def compose_ambient(inputs: AmbientRenderInputs) -> AmbientRenderOutputs:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg binary not found on PATH")
    if inputs.total_seconds <= 0:
        raise ValueError("total_seconds must be positive")

    width, height = _dims_for_aspect(inputs.aspect_ratio, inputs.resolution)
    out = inputs.out_dir
    out.mkdir(parents=True, exist_ok=True)
    logs: list[str] = []

    # Overlays. The watermark is drawn on each normalized clip, so every
    # tiled copy carries it for free.
    wm_vf = watermark_filter(out, inputs.watermark.strip(), height=height, width=width,
                             position=inputs.watermark_position, opacity=inputs.watermark_opacity)

    # 1. Normalize (size/fps/codec, optional slow-down).
    norm = out / "ambient_norm.mp4"
    logs.append(_encode_normalized(
        inputs.clip_path, norm, width=width, height=height, slow_factor=inputs.slow_factor,
        extra_vf=wm_vf,
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

    total = float(inputs.total_seconds)
    pieces: list[Path]
    if inputs.extra_clips:
        # 2b. Less repetition: a seamless unit per extra clip, then a slot
        #     plan that moves between them (see _variety_pieces).
        units, lengths = [unit], [unit_len]
        for i, clip in enumerate(inputs.extra_clips, start=1):
            n_i = out / f"ambient_norm_{i}.mp4"
            logs.append(_encode_normalized(clip, n_i, width=width, height=height,
                                           slow_factor=inputs.slow_factor, extra_vf=wm_vf))
            l_i = _probe_duration(n_i)
            if l_i <= 0.5:
                raise RuntimeError(f"normalized loop clip {i + 1} is empty or unreadable")
            u_i = out / f"ambient_unit_{i}.mp4"
            if cf > 0 and l_i > (2 * cf + 0.5):
                logs.append(_make_seamless_unit(n_i, u_i, length=l_i, crossfade=cf))
            else:
                shutil.copyfile(n_i, u_i)
            units.append(u_i)
            lengths.append(_probe_duration(u_i) or (l_i - cf))
        fade = max(0.0, min(inputs.fade_seconds, min(lengths) / 2.5, total / 2.5))
        # The title is drawn on the opening piece (a unit, or the whole
        # video when it is shorter than one).
        title_secs = min(inputs.title_seconds, max(1.0, total - 0.3))
        title_vf = title_filter(out, inputs.overlay_title.strip(), height=height, width=width,
                                seconds=title_secs)
        pieces, plogs = _variety_pieces(units, lengths, out, variety=inputs.variety, total=total,
                                        fade=fade, dissolve=inputs.transition_seconds, title_vf=title_vf,
                                        title_seconds=title_secs)
        logs.extend(plogs)
        repeats = len(pieces)
    else:
        title_secs = min(inputs.title_seconds, max(1.0, total - 0.3))
        title_vf = title_filter(out, inputs.overlay_title.strip(), height=height, width=width,
                                seconds=title_secs)
        pieces, repeats, fade = _single_unit_pieces(unit, unit_len, out, total=total,
                                                    fade_seconds=inputs.fade_seconds, logs=logs,
                                                    title_vf=title_vf, title_seconds=title_secs)

    # 4. Music unit (optional).
    music_unit: Optional[Path] = None
    if inputs.music_path is not None and inputs.music_path.exists():
        music_unit = out / "ambient_music_unit.wav"
        mlog, _ = _make_seamless_audio_unit(
            inputs.music_path, music_unit, crossfade=max(1.0, inputs.crossfade_seconds)
        )
        logs.append(mlog)

    # 4b. Nature-sound unit (optional), seam-crossfaded like the music.
    sound_unit: Optional[Path] = None
    if inputs.sound_path is not None and inputs.sound_path.exists():
        sound_unit = out / "ambient_sound_unit.wav"
        slog, _ = _make_seamless_audio_unit(inputs.sound_path, sound_unit, crossfade=1.5)
        logs.append(slog)

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
        sound_unit=sound_unit,
        sound_volume=inputs.sound_volume,
        video_vf=timer_filter(out, inputs.timer, total=total, height=height, position=inputs.timer_position),
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


def _frame_at(seconds: float) -> int:
    """The first output frame (at FPS) whose time is not before `seconds`:
    trimming by frame number, so a time on the frame grid (written with a
    few decimals) never keeps or drops a frame by rounding."""
    return max(0, math.ceil(seconds * FPS - 1e-6))


def on_frame_grid(seconds: float) -> float:
    """`seconds` rounded to whole output frames (at least one)."""
    return max(1, round(seconds * FPS)) / FPS


def join_graph(lengths: list[float], *, crossfade: float, width: int, height: int,
               sizes: Optional[list[Optional[tuple[int, int]]]] = None) -> str:
    """filter_complex text joining len(lengths) inputs with rounded linear
    dissolves of `crossfade` seconds; output label [out]. `sizes` (each
    input's width x height, if known) lets near-frame shapes fill (fit_vf).
    Trims are by output frame: a length on the FPS grid gives exactly
    that many frames."""
    cf = crossfade

    def trim(start: float, end: float) -> str:
        return f"trim=start_frame={_frame_at(start)}:end_frame={_frame_at(end)}"

    n = len(lengths)
    sizes = list(sizes or []) + [None] * (n - len(sizes or []))
    parts: list[str] = []
    order: list[str] = []
    for i in range(n):
        L = lengths[i]
        head_in = cf if (i > 0 and cf > 0) else 0.0  # covered by the previous dissolve
        tail_out = cf if (i < n - 1 and cf > 0) else 0.0  # dissolved into the next
        outs = ["b"] + (["h"] if head_in else []) + (["t"] if tail_out else [])
        labels = "".join(f"[{o}{i}]" for o in outs)
        # Timestamps from 0 first, so fps's rounding starts on the grid; one
        # cloned frame at the end, so the trim below always finds every
        # frame of L (its sound is cut to exactly L).
        norm = (f"setpts=PTS-STARTPTS,{fit_vf(width, height, sizes[i])},setsar=1,fps={FPS},"
                f"tpad=stop_mode=clone:stop=1,format=yuv420p")
        parts.append(f"[{i}:v]{norm},split={len(outs)}{labels}" if len(outs) > 1 else f"[{i}:v]{norm}[b{i}]")
        parts[-1] = parts[-1].replace(f"[b{i}]", f"[bb{i}]")
        parts.append(f"[bb{i}]{trim(head_in, L - tail_out)},setpts=PTS-STARTPTS[body{i}]")
        if head_in:
            parts.append(f"[h{i}]{trim(0.0, cf)},setpts=PTS-STARTPTS[head{i}]")
        if tail_out:
            parts.append(f"[t{i}]{trim(L - cf, L)},setpts=PTS-STARTPTS[tail{i}]")
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
    resolution: str = "1080p",
) -> tuple[Path, float]:
    """Join chained clips into one loop clip with short crossfades.

    Each segment already *starts* on the previous one's last frame, so the
    joins are near-continuous; the brief xfade only absorbs the small colour
    / motion jump image-to-video models introduce on their first frames.
    Segments are normalized inside the graph (size/fps/SAR/pix_fmt) because
    xfade requires identical streams. Returns (path, duration)."""
    if not segments:
        raise ValueError("no segments to join")
    # At the scene's output size: a 4K scene keeps a 2K source's detail.
    width, height = _dims_for_aspect(aspect_ratio, resolution)
    if len(segments) == 1:
        _encode_normalized(segments[0], dst, width=width, height=height, slow_factor=1.0)
        return dst, _probe_duration(dst)
    lengths = [_probe_duration(p) for p in segments]
    cf = max(0.0, min(crossfade, min(lengths) / 3.0))
    graph = join_graph(lengths, crossfade=cf, width=width, height=height,
                       sizes=[_probe_size(p) for p in segments])
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


def _probe_clip(path: Path) -> tuple[float, bool, float]:
    """(video length in seconds, whether the file has an audio stream,
    when the picture starts in the file). The length is the video stream's
    own when ffprobe knows it (a sound track a little longer than the
    picture would otherwise stretch it), else the container's; 0.0 when
    the file can't be read. The picture's start is its stream start_time
    measured from the file's own start (the earliest stream's, which is
    where ffmpeg puts 0 when it reads the file), at least 0: a picture
    starting after its sound (start_time > 0, or a sound starting before 0
    with AAC priming) has its sound laid on the picture's own timeline
    (join_audio_graph)."""
    rc, log = _run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_entries",
                    "stream=codec_type,duration,start_time:format=duration,start_time", str(path)])
    if rc != 0:
        return 0.0, False, 0.0
    try:
        info = json.loads(log)
    except ValueError:
        return 0.0, False, 0.0
    streams = info.get("streams") or []
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    length = start = 0.0
    for s in streams:
        if s.get("codec_type") == "video":
            try:
                length = float(s.get("duration") or 0.0)
            except (TypeError, ValueError):
                length = 0.0
            try:
                start = float(s.get("start_time") or 0.0)
            except (TypeError, ValueError):
                start = 0.0
            break
    else:
        return 0.0, has_audio, 0.0  # no picture: not a clip
    try:
        start -= float((info.get("format") or {}).get("start_time") or 0.0)
    except (TypeError, ValueError):
        pass
    start = max(start, 0.0) if math.isfinite(start) else 0.0
    if length <= 0:
        # No stream duration (Matroska/WebM): measure the picture itself,
        # from its first packet to the end of its last. The container's
        # length would include a sound track that outlasts the picture.
        length = _video_packets_span(path)
    if length <= 0:
        try:
            length = float((info.get("format") or {}).get("duration") or 0.0) - start
        except (TypeError, ValueError):
            length = 0.0
    return max(length, 0.0), has_audio, start


def _video_packets_span(path: Path) -> float:
    """Seconds from the first video packet's start to the last one's end,
    or 0.0 when they can't be read."""
    rc, log = _run(["ffprobe", "-v", "quiet", "-select_streams", "v:0", "-show_entries",
                    "packet=pts_time,duration_time", "-of", "csv=p=0", str(path)])
    if rc != 0:
        return 0.0
    first = last_end = None
    for line in log.splitlines():
        bits = line.strip().split(",")
        try:
            pts = float(bits[0])
        except (IndexError, ValueError):
            continue
        try:
            dur = float(bits[1]) if len(bits) > 1 else 0.0
        except ValueError:
            dur = 0.0
        first = pts if first is None else min(first, pts)
        last_end = pts + dur if last_end is None else max(last_end, pts + dur)
    if first is None or last_end is None:
        return 0.0
    return max(last_end - first, 0.0)


# Below this a dissolve is too short to see (and leaves no whole frame to
# blend): it is a cut.
MIN_DISSOLVE = 0.1
JOIN_AUDIO_RATE = 48000


# The joined clip is at most 1080p (long side 1920, short side 1080): a 4K
# first clip is scaled down so one join can't write a 10-minute 4K file.
JOIN_MAX_LONG, JOIN_MAX_SHORT = 1920, 1080


def join_size(size: tuple[int, int]) -> tuple[int, int]:
    """The joined clip's size: the first clip's, scaled down (keeping its
    shape) to fit 1080p, with even sides (x264 with 4:2:0 needs them)."""
    w, h = size
    long_max, short_max = (JOIN_MAX_LONG, JOIN_MAX_SHORT) if w >= h else (JOIN_MAX_SHORT, JOIN_MAX_LONG)
    scale = min(1.0, long_max / max(w, 1), short_max / max(h, 1))
    w, h = int(w * scale), int(h * scale)
    return max(2, w - w % 2), max(2, h - h % 2)


def join_timeout(total_seconds: float) -> float:
    """How long the join's ffmpeg may run before it is killed: a few times
    the clips' total length, and at least two minutes."""
    return max(120.0, 6.0 * total_seconds)


def join_crossfade(lengths: list[float], transition: str, crossfade: float) -> float:
    """The dissolve actually used: 0 for a cut; else `crossfade` kept to a
    third of the shortest clip, and 0 when that is too short to see."""
    if transition != "dissolve" or len(lengths) < 2:
        return 0.0
    cf = max(0.0, min(float(crossfade), min(lengths) / 3.0))
    return round(cf, 3) if cf >= MIN_DISSOLVE else 0.0


def join_audio_graph(lengths: list[float], has_audio: list[bool], *, crossfade: float,
                     video_starts: Optional[list[float]] = None) -> str:
    """filter_complex text for the joined sound (output label [aout]): each
    clip's own, or silence for a clip without any, fitted to exactly that
    clip's picture length, then joined back to back (`crossfade` > 0: each
    join crossfades over the same span as the picture's dissolve).
    `video_starts`: when each clip's picture starts in its file (>= 0);
    its sound is taken from that moment on, so it plays on the picture's
    own timeline."""
    fmt = "aformat=sample_fmts=fltp:channel_layouts=stereo"
    starts = video_starts if video_starts is not None else [0.0] * len(lengths)
    parts: list[str] = []
    for i, (L, audio, vs) in enumerate(zip(lengths, has_audio, starts, strict=True)):
        if audio:
            # On the file's timeline from 0: a sound starting after 0 gets
            # that gap filled with silence (async/first_pts=0), and one
            # starting a little before it (AAC priming) loses that part.
            # Then the part before the picture starts is cut off, so the
            # sound lines up with the picture's first frame.
            # Padded before the cut, so a sound that ends before the
            # picture starts still gives L seconds (of silence).
            vs = max(vs, 0.0)
            parts.append(f"[{i}:a]aresample={JOIN_AUDIO_RATE}:async=1:first_pts=0,{fmt},apad,"
                         f"atrim=start={vs:.4f}:end={vs + L:.4f},asetpts=PTS-STARTPTS[a{i}]")
        else:
            parts.append(f"anullsrc=r={JOIN_AUDIO_RATE}:cl=stereo,{fmt},"
                         f"atrim=end={L:.4f},asetpts=PTS-STARTPTS[a{i}]")
    n = len(lengths)
    if crossfade > 0 and n > 1:
        prev = "a0"
        for i in range(1, n):
            out = "aout" if i == n - 1 else f"ax{i}"
            parts.append(f"[{prev}][a{i}]acrossfade=d={crossfade:.4f}:{AUDIO_XFADE_CURVES}[{out}]")
            prev = out
    else:
        parts.append(f"{''.join(f'[a{i}]' for i in range(n))}concat=n={n}:v=0:a=1[aout]")
    return ";".join(parts)


class JoinStopped(RuntimeError):
    """The join's FFmpeg was killed because the job was cancelled or deleted."""


def join_clips_with_audio(
    clips: list[Path],
    dst: Path,
    *,
    transition: str = "cut",
    crossfade: float = 0.5,
    should_stop: Optional[Callable[[], bool]] = None,
) -> tuple[Path, float, bool]:
    """Play `clips` back to back as one clip (Studio's "Join clips").
    `should_stop`: asked about once a second while FFmpeg runs; yes kills it
    (JoinStopped).

    The picture is the first clip's size; the others are fitted into it
    (scaled and padded, or cropped when their shape is nearly the same:
    fit_vf), at FPS with square pixels, as join_segments does. Sound: each
    clip's own, silence for a clip without any so the track stays
    continuous, and no sound track at all when no clip has sound.
    `transition` "dissolve" crossfades picture and sound over `crossfade`
    seconds (see join_crossfade); "cut" plays them straight on. Returns
    (path, length, has sound). RuntimeError when a clip can't be read or
    ffmpeg fails."""
    if len(clips) < 2:
        raise ValueError("join needs at least two clips")
    probed = [_probe_clip(p) for p in clips]
    lengths = [length for length, _, _ in probed]
    has_audio = [audio for _, audio, _ in probed]
    video_starts = [start for _, _, start in probed]
    if any(length <= 0 for length in lengths):
        raise RuntimeError("a clip to join can't be read")
    size = _probe_size(clips[0])
    if size is None:
        raise RuntimeError("the first clip's size can't be read")
    width, height = join_size(size)
    # Each clip laid on the output frame grid once: picture, sound and the
    # dissolve all use these lengths, so the sound can't drift from the
    # picture by a part-frame per clip.
    lengths = [on_frame_grid(length) for length in lengths]
    cf = join_crossfade(lengths, transition, crossfade)
    cf = on_frame_grid(cf) if cf > 0 else 0.0
    graph = join_graph(lengths, crossfade=cf, width=width, height=height,
                       sizes=[_probe_size(p) for p in clips])
    audio = any(has_audio)
    if audio:
        graph += ";" + join_audio_graph(lengths, has_audio, crossfade=cf, video_starts=video_starts)
    cmd = ["ffmpeg", "-y"]
    for p in clips:
        cmd += ["-i", str(p)]
    cmd += ["-filter_complex", graph, "-map", "[out]"]
    cmd += ["-map", "[aout]", "-c:a", "aac", "-b:a", "192k"] if audio else ["-an"]
    cmd += [*_X264, "-r", str(FPS), "-movflags", "+faststart", str(dst)]
    rc, log = _run(cmd, timeout=join_timeout(sum(lengths)))
    if rc != 0 or not dst.exists():
        raise RuntimeError(f"ffmpeg clip join failed: {log[-2000:]}")
    return dst, _probe_duration(dst), audio


def slice_audio(src: Path, dst: Path, *, start: float, duration: float) -> Path:
    """Cut [start, start+duration) out of an audio file (AAC)."""
    rc, log = _run(["ffmpeg", "-y", "-ss", f"{max(0.0, start):.3f}", "-t", f"{duration:.3f}", "-i", str(src),
                    "-vn", "-c:a", "aac", "-b:a", "160k", str(dst)])
    if rc != 0:
        raise RuntimeError(f"ffmpeg audio slice failed: {log[-1000:]}")
    return dst


def trim_clip(src: Path, dst: Path, *, duration: float) -> Path:
    """Re-encode the first `duration` seconds of a clip (frame-accurate)."""
    rc, log = _run(["ffmpeg", "-y", "-i", str(src), "-t", f"{duration:.3f}", "-an", *_X264,
                    "-r", str(FPS), str(dst)])
    if rc != 0:
        raise RuntimeError(f"ffmpeg trim failed: {log[-1000:]}")
    return dst


def join_audio(parts: list[Path], dst: Path, *, crossfade: float = 6.0) -> tuple[Path, float]:
    """Crossfade several music parts into one bed (AAC)."""
    if len(parts) == 1:
        cmd = ["ffmpeg", "-y", "-i", str(parts[0]), "-vn", "-c:a", "aac", "-b:a", "192k", str(dst)]
    else:
        flt, prev = [], "0:a"
        for i in range(1, len(parts)):
            out = f"a{i}"
            flt.append(f"[{prev}][{i}:a]acrossfade=d={crossfade:.2f}:{AUDIO_XFADE_CURVES}[{out}]")
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
        limit = inputs.clip_seconds[i] if i < len(inputs.clip_seconds) else None
        exact = bool(inputs.clip_exact[i]) if i < len(inputs.clip_exact) else False
        log_parts.append(_normalize_clip(clip, norm, width, height, limit, exact=exact))
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
