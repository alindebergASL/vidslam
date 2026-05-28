from __future__ import annotations

import re

from ..schemas.storyboard import CaptionChunk, ShotPlan, StoryboardPlan
from .base import CastContext, ChatProvider


def _chunk_caption(text: str, max_words: int = 5) -> list[str]:
    words = re.split(r"\s+", text.strip())
    chunks: list[str] = []
    buf: list[str] = []
    for w in words:
        if not w:
            continue
        buf.append(w)
        if len(buf) >= max_words:
            chunks.append(" ".join(buf))
            buf = []
    if buf:
        chunks.append(" ".join(buf))
    return chunks


class MockChatProvider(ChatProvider):
    """Deterministic storyboard generator used in MOCK_PROVIDERS mode and tests."""

    def generate_storyboard(self, *, project: dict, cast: CastContext) -> StoryboardPlan:
        script = (project.get("original_script") or "").strip() or "Sample script."
        title = (project.get("title") or "Untitled").strip() or "Untitled"
        cta = (project.get("cta_text") or "").strip()
        target_seconds = float(project.get("target_duration_seconds") or 25)
        mode = project.get("mode") or "reel_montage"
        include_disclosure = bool(project.get("include_disclosure", True))

        # Build shot plan based on mode.
        avatars = cast.avatars or [{"name": "Avatar", "role": "host"}]
        ingredients = cast.ingredients or []
        ing_scene = next((i for i in ingredients if i.get("kind") == "scene"), None)
        ing_style = next((i for i in ingredients if i.get("kind") == "style"), None)

        def _scene_str() -> str:
            return f" in {ing_scene['name']}" if ing_scene else ""

        def _style_str() -> str:
            return f", {ing_style['name']} style" if ing_style else ""

        body_duration = max(target_seconds - (3.0 if cta else 0.0), 6.0)
        shots: list[ShotPlan] = []

        if mode == "talking_head_beta":
            shots.append(
                ShotPlan(
                    order=1,
                    shot_type="talking_head",
                    duration_seconds=body_duration,
                    visual_prompt=(
                        f"{avatars[0]['name']} (AI-generated virtual creator) speaking to camera"
                        f"{_scene_str()}{_style_str()}. Direct eye contact, natural expression."
                    ),
                    negative_prompt="blurry, distorted, low quality",
                    reference_strategy="frame_images",
                    recommended_asset_types=["hero"],
                    recommended_cast_roles=[avatars[0].get("role", "host")],
                    caption_text=script,
                    camera_direction="medium close-up, slight handheld motion",
                )
            )
        elif mode == "static_motion":
            shots.append(
                ShotPlan(
                    order=1,
                    shot_type="hero",
                    duration_seconds=body_duration,
                    visual_prompt=(
                        f"{avatars[0]['name']} (AI-generated virtual creator) hero portrait"
                        f"{_scene_str()}{_style_str()}, subtle parallax zoom-in."
                    ),
                    negative_prompt="warping, jittery motion",
                    reference_strategy="static_motion",
                    recommended_asset_types=["hero"],
                    recommended_cast_roles=[avatars[0].get("role", "host")],
                    caption_text=script,
                    camera_direction="slow push-in",
                )
            )
        else:  # reel_montage
            n = min(max(2, len(avatars) + 1), 4)
            per = body_duration / n
            roles = [a.get("role", "host") for a in avatars]
            for i in range(n):
                a = avatars[i % len(avatars)]
                shot_type = "hero" if i == 0 else "b_roll"
                shots.append(
                    ShotPlan(
                        order=i + 1,
                        shot_type=shot_type,
                        duration_seconds=round(per, 2),
                        visual_prompt=(
                            f"{a['name']} (AI-generated virtual creator)"
                            f"{_scene_str()}{_style_str()}, "
                            f"{'wide establishing shot' if i == 0 else 'mid b-roll moment'}, "
                            f"shot {i + 1} of {n}."
                        ),
                        negative_prompt="logos, watermark, text overlay",
                        reference_strategy="input_references",
                        recommended_asset_types=["hero", "lifestyle", "scene", "style"],
                        recommended_cast_roles=[roles[i % len(roles)]],
                        caption_text="",
                        camera_direction="varied",
                    )
                )

        if cta:
            shots.append(
                ShotPlan(
                    order=len(shots) + 1,
                    shot_type="end_card",
                    duration_seconds=2.5,
                    visual_prompt=f"Clean branded end card with text: '{cta}'.",
                    negative_prompt="",
                    reference_strategy="static_motion",
                    recommended_asset_types=["logo"],
                    recommended_cast_roles=[],
                    caption_text=cta,
                    camera_direction="hold",
                )
            )

        # Caption chunks across the body script.
        chunk_strs = _chunk_caption(script)
        per_chunk = (body_duration / len(chunk_strs)) if chunk_strs else 0.0
        caption_chunks = [
            CaptionChunk(start_hint=round(i * per_chunk, 2), text=t)
            for i, t in enumerate(chunk_strs)
        ]

        return StoryboardPlan(
            title=title,
            cleaned_voice_script=script,
            estimated_duration_seconds=sum(s.duration_seconds for s in shots),
            content_warning_notes="",
            disclosure_text=(
                "AI-generated virtual creator" if include_disclosure else ""
            ),
            end_card_text=cta,
            caption_chunks=caption_chunks,
            shots=shots,
        )
