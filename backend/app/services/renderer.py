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


@dataclass
class RenderOutputs:
    final_video_path: Path
    thumbnail_path: Path
    log: str


def _run(cmd: list[str]) -> tuple[int, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _dims_for_aspect(aspect: str) -> tuple[int, int]:
    if aspect == "16:9":
        return 1920, 1080
    if aspect == "1:1":
        return 1080, 1080
    return W, H


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


def _make_end_card(dst: Path, *, text: str, width: int, height: int, duration: float = 2.5) -> str:
    """Generate a solid-color end card with centered text."""
    # Escape characters for drawtext.
    safe = text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "’")
    vf = (
        f"color=c=#0E0E12:s={width}x{height}:d={duration},"
        f"drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:"
        f"text='{safe}':fontcolor=white:fontsize=72:"
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
            _make_end_card(ec, text=inputs.cta_text, width=width, height=height)
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
