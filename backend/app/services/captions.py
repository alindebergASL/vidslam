from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..schemas.storyboard import CaptionChunk

# ASS style presets per `caption_style` value used in the project model.
# Colors are ASS &HBBGGRR& (alpha=00 opaque).
_STYLES: dict[str, dict] = {
    "clean_white": {
        "Fontname": "DejaVu Sans",
        "Fontsize": "56",
        "PrimaryColour": "&H00FFFFFF",
        "OutlineColour": "&H00202020",
        "BorderStyle": "1",
        "Outline": "3",
        "Shadow": "0",
        "Alignment": "2",  # bottom center
        "MarginV": "220",
        "Bold": "0",
    },
    "influencer_bold": {
        "Fontname": "DejaVu Sans",
        "Fontsize": "72",
        "PrimaryColour": "&H0000FFFF",  # bright yellow-ish (BGR yellow = 00FFFF)
        "OutlineColour": "&H00000000",
        "BorderStyle": "1",
        "Outline": "6",
        "Shadow": "2",
        "Alignment": "2",
        "MarginV": "260",
        "Bold": "1",
    },
    "minimal_lower_third": {
        "Fontname": "DejaVu Sans",
        "Fontsize": "44",
        "PrimaryColour": "&H00FFFFFF",
        "OutlineColour": "&H80000000",
        "BorderStyle": "3",  # opaque box
        "Outline": "0",
        "Shadow": "0",
        "Alignment": "1",  # bottom left
        "MarginL": "60",
        "MarginV": "180",
        "Bold": "0",
    },
}


@dataclass
class CaptionTiming:
    start: float
    end: float
    text: str


def chunks_to_timed(
    chunks: list[CaptionChunk], total_duration: float
) -> list[CaptionTiming]:
    """Distribute caption chunks across the timeline using start_hint when set,
    otherwise spread evenly across `total_duration`."""
    if not chunks:
        return []
    n = len(chunks)
    timed: list[CaptionTiming] = []
    hinted = [c for c in chunks if c.start_hint > 0]
    if hinted and len(hinted) == n:
        starts = [c.start_hint for c in chunks]
    else:
        slice_len = total_duration / n
        starts = [i * slice_len for i in range(n)]
    for i, ch in enumerate(chunks):
        start = starts[i]
        end = starts[i + 1] if i + 1 < n else total_duration
        timed.append(CaptionTiming(start=start, end=max(end, start + 0.5), text=ch.text))
    return timed


def _fmt_ts(t: float) -> str:
    if t < 0:
        t = 0
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t - (h * 3600 + m * 60)
    return f"{h:01d}:{m:02d}:{s:05.2f}"


def hex_to_ass_color(hex_color: str) -> str:
    """Convert a #RRGGBB (or #RGB) hex string to an ASS &HAABBGGRR (opaque) color."""
    h = hex_color.strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        return "&H00FFFFFF"
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H00{b}{g}{r}".upper()


def write_ass(
    out_path: Path,
    *,
    timed: list[CaptionTiming],
    style_name: str = "clean_white",
    width: int = 1080,
    height: int = 1920,
    primary_color: str | None = None,
) -> Path:
    style = dict(_STYLES.get(style_name, _STYLES["clean_white"]))
    if primary_color:
        # Brand-driven caption color overrides the preset's PrimaryColour so the
        # whole video carries the palette, not just the end card.
        style["PrimaryColour"] = hex_to_ass_color(primary_color)
    style_line = ",".join(
        [
            "AVS",
            style["Fontname"],
            style["Fontsize"],
            style["PrimaryColour"],
            "&H000000FF",
            style["OutlineColour"],
            "&H00000000",
            style["Bold"],
            "0",
            "0",
            "0",
            "100",
            "100",
            "0",
            "0",
            style["BorderStyle"],
            style["Outline"],
            style["Shadow"],
            style["Alignment"],
            style.get("MarginL", "60"),
            style.get("MarginL", "60"),
            style["MarginV"],
            "1",
        ]
    )
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {width}\n"
        f"PlayResY: {height}\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: {style_line}\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    events = []
    for t in timed:
        # Escape commas and newlines minimally; ASS uses \N for newline.
        text = t.text.replace("\n", "\\N").replace(",", "‚")
        events.append(
            f"Dialogue: 0,{_fmt_ts(t.start)},{_fmt_ts(t.end)},AVS,,0,0,0,,{text}"
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return out_path


def available_styles() -> list[str]:
    return list(_STYLES.keys())
