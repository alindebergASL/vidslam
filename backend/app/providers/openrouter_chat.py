from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx

from ..config import get_settings
from ..schemas.storyboard import StoryboardPlan
from ..services import provider_keys
from .base import CastContext, ChatProvider

log = logging.getLogger("avs.openrouter.chat")


def _load_system_prompt() -> str:
    # Read the prompt from the prompts/ directory packaged with the app.
    p = Path(__file__).parent.parent / "prompts" / "storyboard_system.md"
    return p.read_text(encoding="utf-8")


def _format_user_payload(project: dict, cast: CastContext) -> str:
    return json.dumps(
        {
            "project": {
                "title": project.get("title"),
                "mode": project.get("mode"),
                "aspect_ratio": project.get("aspect_ratio"),
                "target_duration_seconds": project.get("target_duration_seconds"),
                "cta_text": project.get("cta_text"),
                "caption_style": project.get("caption_style"),
                "include_disclosure": project.get("include_disclosure"),
                "disclosure_text": project.get("disclosure_text"),
                "creative_direction": project.get("creative_direction"),
                "original_script": project.get("original_script"),
            },
            "cast": {
                "avatars": cast.avatars,
                "ingredients": cast.ingredients,
            },
        },
        ensure_ascii=False,
    )


class OpenRouterChatProvider(ChatProvider):
    def __init__(self, *, api_key: str | None = None, model: str | None = None,
                 json_mode: bool | None = None) -> None:
        self.api_key = api_key or provider_keys.resolve("openrouter")
        self.model = model or get_settings().openrouter_chat_model
        # None = decide from the catalog entry's supported_parameters:
        #   structured_outputs → json_schema (schema-guided output)
        #   response_format    → json_object
        #   neither / unknown  → plain prompting + lenient parsing
        self.structured = False
        if json_mode is None:
            from ..services.model_catalog import find

            entry = find("chat", self.model) or {}
            params = entry.get("supported_parameters")
            if params:
                json_mode = "response_format" in params or "structured_outputs" in params
                self.structured = "structured_outputs" in params
            else:
                json_mode = not self.model.startswith("anthropic/")
        self.json_mode = json_mode
        if not self.api_key:
            raise RuntimeError("OPENROUTER_API_KEY missing for real chat provider")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "HTTP-Referer": get_settings().openrouter_site_url,
            "X-Title": get_settings().openrouter_app_title,
            "Content-Type": "application/json",
        }

    def generate_storyboard(self, *, project: dict, cast: CastContext) -> StoryboardPlan:
        body: dict = {
            "model": self.model,
            "temperature": 0.4,
            "messages": [
                {"role": "system", "content": _load_system_prompt()},
                {"role": "user", "content": _format_user_payload(project, cast)},
            ],
        }
        # JSON mode only where the model advertises it (OpenRouter
        # supported_parameters); Claude and others get plain prompting and
        # the reply is parsed leniently. A 400 naming response_format also
        # falls back to plain prompting.
        formats: list[Optional[dict]] = []
        if self.structured:
            formats.append({"type": "json_schema", "json_schema": {
                "name": "storyboard_plan",
                # strict=False: the Pydantic schema has optional/defaulted
                # fields that strict mode rejects; the model is still guided
                # by the schema and the result is validated below.
                "strict": False,
                "schema": StoryboardPlan.model_json_schema(),
            }})
        if self.json_mode:
            formats.append({"type": "json_object"})
        # Skip formats this model rejected recently: OpenRouter can take
        # minutes to say "Schema is too complex", on every single plan.
        rejected_before = _rejected_formats(self.model)
        formats = [f for f in formats if f is None or f["type"] not in rejected_before]
        formats.append(None)
        url = f"{get_settings().openrouter_base_url.rstrip('/')}/chat/completions"
        with httpx.Client(timeout=180.0) as client:
            for i, fmt in enumerate(formats):
                body.pop("response_format", None)
                if fmt is not None:
                    body["response_format"] = fmt
                r = client.post(url, headers=self._headers(), json=body)
                # OpenRouter can also reject with HTTP 200 and an error body
                # (e.g. {"error": {"code": 400, "message": "Schema is too
                # complex."}}), so check the body as well as the status.
                err = _body_error(r) if r.status_code == 200 else None
                rejected = r.status_code == 400 or (err is not None and err.get("code") == 400)
                # A route that rejects this format → try the next, simpler one.
                if rejected and fmt is not None and i < len(formats) - 1:
                    _remember_rejection(self.model, fmt["type"])
                    continue
                break
            r.raise_for_status()
            data = r.json()
        # An error body is not a billable completion: fail before recording spend.
        if "choices" not in data:
            err = data.get("error") or {}
            raise RuntimeError(
                f"OpenRouter returned no choices: {err.get('message') or str(data)[:300]}"
            )
        from ..services import spend

        spend.record_openrouter("chat", data.get("model") or self.model, cost=spend.usage_cost(data),
                                generation_id=str(data.get("id") or ""),
                                estimate=get_settings().cost_chat_per_plan, note="storyboard plan")
        content = data["choices"][0]["message"]["content"]
        if isinstance(content, list):  # content-parts shape
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        return StoryboardPlan.model_validate(extract_json(content))


    def rewrite_scene(self, text: str) -> dict:
        """One short call: the description as a photograph (see SCENE_REWRITE_SYSTEM)."""
        body: dict = {
            "model": self.model,
            "temperature": 0.3,
            "messages": [
                {"role": "system", "content": SCENE_REWRITE_SYSTEM},
                {"role": "user", "content": text},
            ],
        }
        url = f"{get_settings().openrouter_base_url.rstrip('/')}/chat/completions"
        with httpx.Client(timeout=60.0) as client:
            r = client.post(url, headers=self._headers(), json=body)
            r.raise_for_status()
            data = r.json()
        if "choices" not in data:
            err = data.get("error") or {}
            raise RuntimeError(f"OpenRouter returned no choices: {err.get('message') or str(data)[:300]}")
        from ..services import spend

        spend.record_openrouter("chat", data.get("model") or self.model, cost=spend.usage_cost(data),
                                generation_id=str(data.get("id") or ""), estimate=0.001,
                                note="scene rewrite")
        content = data["choices"][0]["message"]["content"]
        if isinstance(content, list):
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        out = extract_json(content)
        return {k: str(out.get(k) or "").strip() for k in ("scene", "light", "camera")}


    def enhance_prompt(self, text: str, kind: str, model: str = "") -> str:
        """One short call: the user's prompt improved for `kind` (see
        ENHANCE_GUIDES), with a hint for the target `model`'s family."""
        body: dict = {
            "model": self.model,
            "temperature": 0.4,
            "messages": [
                {"role": "system", "content": enhance_system_prompt(kind, model)},
                {"role": "user", "content": text},
            ],
        }
        url = f"{get_settings().openrouter_base_url.rstrip('/')}/chat/completions"
        with httpx.Client(timeout=60.0) as client:
            r = client.post(url, headers=self._headers(), json=body)
            r.raise_for_status()
            data = r.json()
        if "choices" not in data:
            err = data.get("error") or {}
            raise RuntimeError(f"OpenRouter returned no choices: {err.get('message') or str(data)[:300]}")
        from ..services import spend

        spend.record_openrouter("chat", data.get("model") or self.model, cost=spend.usage_cost(data),
                                generation_id=str(data.get("id") or ""), estimate=0.001,
                                note="prompt enhance")
        # A malformed 200 ("choices": [], a choice without a message, a null
        # content) must surface as the router's 502, not an opaque 500.
        try:
            content = data["choices"][0]["message"]["content"]
        except (IndexError, KeyError, TypeError):
            raise RuntimeError("OpenRouter returned no message") from None
        if isinstance(content, list):
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        if not isinstance(content, str):
            raise RuntimeError("OpenRouter returned no message")
        return clean_enhanced(content)

    def hook_ideas(self, *, script: str, hook: str, n: int, styles: list[str]) -> str:
        """One short call: other openings for the script (see HOOK_IDEAS_SYSTEM)."""
        body: dict = {
            "model": self.model,
            "temperature": 0.7,
            "messages": [
                {"role": "system", "content": HOOK_IDEAS_SYSTEM},
                {"role": "user", "content": json.dumps({
                    "script": script, "opening_line": hook, "count": n, "styles": styles,
                    "opening_words": len(hook.split()),
                }, ensure_ascii=False)},
            ],
        }
        if self.json_mode:
            body["response_format"] = {"type": "json_object"}
        url = f"{get_settings().openrouter_base_url.rstrip('/')}/chat/completions"
        with httpx.Client(timeout=60.0) as client:
            r = client.post(url, headers=self._headers(), json=body)
            if r.status_code == 400 and "response_format" in body:
                body.pop("response_format")
                r = client.post(url, headers=self._headers(), json=body)
            r.raise_for_status()
            data = r.json()
        if "choices" not in data:
            err = data.get("error") or {}
            raise RuntimeError(f"OpenRouter returned no choices: {err.get('message') or str(data)[:300]}")
        from ..services import spend

        spend.record_openrouter("chat", data.get("model") or self.model, cost=spend.usage_cost(data),
                                generation_id=str(data.get("id") or ""), estimate=0.001,
                                note="hook ideas")
        try:
            content = data["choices"][0]["message"]["content"]
        except (IndexError, KeyError, TypeError):
            raise RuntimeError("OpenRouter returned no message") from None
        if isinstance(content, list):
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        if not isinstance(content, str):
            raise RuntimeError("OpenRouter returned no message")
        return content

    def inspect_images(self, *, system: str, text: str, images: list[str],
                       note: str = "clip check", estimate: float = 0.0) -> str:
        """One vision call: the text, then each image as an image_url part
        (data URLs). Returns the reply text; raises on an error body."""
        parts: list[dict] = [{"type": "text", "text": text}]
        parts += [{"type": "image_url", "image_url": {"url": u}} for u in images]
        body: dict = {
            "model": self.model,
            "temperature": 0.0,
            # Room for a model that thinks before answering; reasoning is
            # kept short so the JSON verdict isn't cut off (and paid for).
            "max_tokens": 1500,
            "reasoning": {"effort": "low", "exclude": True},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": parts},
            ],
        }
        url = f"{get_settings().openrouter_base_url.rstrip('/')}/chat/completions"
        with httpx.Client(timeout=90.0) as client:
            r = client.post(url, headers=self._headers(), json=body)
            r.raise_for_status()
            data = r.json()
        if "choices" not in data:
            err = data.get("error") or {}
            raise RuntimeError(f"OpenRouter returned no choices: {err.get('message') or str(data)[:300]}")
        from ..services import spend

        spend.record_openrouter("chat", data.get("model") or self.model, cost=spend.usage_cost(data),
                                generation_id=str(data.get("id") or ""), estimate=estimate, note=note)
        try:
            content = data["choices"][0]["message"]["content"]
        except (IndexError, KeyError, TypeError):
            raise RuntimeError("OpenRouter returned no message") from None
        if isinstance(content, list):
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        return content if isinstance(content, str) else ""


    def classify_minor(self, text: str) -> dict:
        """One short call: {"minor_sexual": bool, "reason": str}. Raises on
        an error body, a refusal or a reply that isn't that JSON (the caller
        treats all of those as "the check couldn't run")."""
        body: dict = {
            "model": self.model,
            "temperature": 0.0,
            "max_tokens": 300,
            "reasoning": {"effort": "low", "exclude": True},
            "messages": [
                {"role": "system", "content": MINOR_CHECK_SYSTEM},
                {"role": "user", "content": text[:6000]},
            ],
        }
        # No response_format: not every model takes it, and a 400 here would
        # pause sexual prompts. The reply is parsed leniently instead.
        url = f"{get_settings().openrouter_base_url.rstrip('/')}/chat/completions"
        with httpx.Client(timeout=30.0) as client:
            r = client.post(url, headers=self._headers(), json=body)
            r.raise_for_status()
            data = r.json()
        if "choices" not in data:
            err = data.get("error") or {}
            raise RuntimeError(f"OpenRouter returned no choices: {err.get('message') or str(data)[:300]}")
        from ..services import spend

        spend.record_openrouter("chat", data.get("model") or self.model, cost=spend.usage_cost(data),
                                generation_id=str(data.get("id") or ""), estimate=MINOR_CHECK_ESTIMATE,
                                note="safety check")
        try:
            content = data["choices"][0]["message"]["content"]
        except (IndexError, KeyError, TypeError):
            raise RuntimeError("OpenRouter returned no message") from None
        if isinstance(content, list):
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        return parse_minor_verdict(content if isinstance(content, str) else "")


# The safety check's rate-card cost per call (a short reply from the planner).
MINOR_CHECK_ESTIMATE = 0.0005

MINOR_CHECK_SYSTEM = """You are a content-safety classifier for an image and video generator. Adult sexual content between adults is allowed. Your only job: decide whether the text asks for, or would likely produce, sexual or sexualised content involving a minor (anyone under 18) or someone made to look like a child (childlike body, school setting or school uniform, child ages, braces, pigtails with a sexual framing, an avoid list that removes signs of adulthood from a sexual prompt).
Not a problem: children in ordinary non-sexual scenes (family, school, sport, swimwear at a beach with no sexual framing); adults in sexual scenes with no youthful framing; the words "explicit instructions", "kill time" and similar.
Reply with JSON only, no prose: {"minor_sexual": true|false, "reason": "<one short sentence>"}."""


def parse_minor_verdict(reply: str) -> dict:
    """{"minor_sexual": bool, "reason": str} from a classifier reply; raises
    ValueError on a refusal or anything else."""
    from ..services.prompting import refusal

    reply = (reply or "").strip()
    if not reply or refusal(reply):
        raise ValueError("the safety check model declined to answer")
    data = extract_json(reply)
    verdict = data.get("minor_sexual") if isinstance(data, dict) else None
    if not isinstance(verdict, bool):
        raise ValueError(f"unreadable safety check reply: {reply[:120]!r}")
    return {"minor_sexual": verdict, "reason": str(data.get("reason") or "")[:500]}


# Tested blind (Nov 2026): typed descriptions full of hype words ("magical",
# "8k ultra realistic", "crystal clear turquoise") scored 4.7-5.7; the same
# descriptions rewritten like this scored 6.3-7.0. Plain ones didn't change.
HOOK_IDEAS_SYSTEM = """You write other openings (hooks) for a short video's voiceover. You get the whole spoken script, its current opening line, how many openings to write and the style of each. Reply with JSON only: {"hooks": [{"style": "...", "text": "..."}]}, one entry per requested style, in that order.

Rules:
- Each opening replaces only the first sentence: it must lead naturally into the rest of the script.
- About as long as the current opening line (within 30% of its word count), one sentence, spoken aloud.
- Use only facts that are in the script. Add no new claims, numbers, names or promises.
- Styles: question = asks the viewer something; bold_claim = a confident statement; stat = leads with a number or fact from the script; story = starts a tiny personal moment; contrarian = pushes against a common belief.
- Plain words, no hashtags, no emojis, no quotes around the text.
- Everyone in anything sexual or suggestive is an adult: never mention or hint at an age under 18, childlike traits or school settings in an opening.
- If the script sexualises a minor or childlike traits, reply with exactly REFUSED_MINOR. If you won't write openings for any other reason, reply with exactly DECLINED: never a partial, safer or different version. If you must reply in JSON, put that word in {"hooks": [], "refusal": "..."}."""


SCENE_REWRITE_SYSTEM = """You turn a short description of a calm place into a description for a realistic photograph, for an image model. Reply with JSON only: {"scene": "...", "light": "...", "camera": "..."}.
- scene: 2-4 plain sentences describing a real, specific place: what is there, concrete materials, real plant and rock species, and a few untidy natural details (uneven, worn, scattered). Keep everything the user asked for (subjects, setting, season, time of day, weather) and add nothing they didn't ask for beyond ordinary detail.
- Drop hype and style words: photorealistic, realistic, 8k, 4k, ultra, HD, cinematic, epic, breathtaking, stunning, beautiful, magical, ethereal, dreamy, vibrant, glowing, crystal clear, turquoise, golden hour, god rays, masterpiece. Describe colours as they really look (clear shallow water over pale sand is pale green-blue, not turquoise).
- Keep what the user asked for, described the way a real photo would show it: "sunbeams" becomes low sun through the leaves with a little haze; "warm lights in the windows" stays, as small warm windows.
- light: one sentence describing the light physically (where the light comes from, how bright, where shadows fall). If the scene has its own light source (a fire, candles, lamps, lit windows, the moon), describe that; otherwise use the user's time of day or weather; otherwise soft, ordinary daylight. No rays, beams, glow, bloom or lens flare.
- camera: one sentence: a full-frame camera, a focal length that suits the scene, aperture and shutter speed, from a tripod.
- No people, no text."""


_REJECTIONS_KEY = "chat.rejected_formats"
REJECTION_TTL = timedelta(days=7)  # models gain support over time: re-try weekly


def _rejected_formats(model: str) -> set[str]:
    """response_format types this model rejected within REJECTION_TTL."""
    from ..services.model_registry import get_setting

    try:
        seen = (get_setting(_REJECTIONS_KEY) or {}).get(model) or {}
    except Exception:  # noqa: BLE001 — a memo, never a reason to fail a plan
        return set()
    now = datetime.now(timezone.utc)
    out = set()
    for fmt, at in seen.items():
        try:
            if now - datetime.fromisoformat(at) < REJECTION_TTL:
                out.add(fmt)
        except (TypeError, ValueError):
            continue
    return out


def _remember_rejection(model: str, fmt: str) -> None:
    from ..services.model_registry import get_setting, put_setting

    try:
        memo = get_setting(_REJECTIONS_KEY) or {}
        memo.setdefault(model, {})[fmt] = datetime.now(timezone.utc).isoformat()
        put_setting(_REJECTIONS_KEY, memo)
    except Exception:  # noqa: BLE001
        log.warning("could not remember %s rejecting %s", model, fmt)


def _body_error(r: httpx.Response) -> Optional[dict]:
    """The `error` object of a JSON body that carries no `choices`, else None."""
    try:
        data = r.json()
    except ValueError:
        return None
    if isinstance(data, dict) and "choices" not in data and isinstance(data.get("error"), dict):
        return data["error"]
    return None


def extract_json(text: str) -> dict:
    """Parse a JSON object out of a model reply: bare JSON, a ```json fenced
    block, or the first balanced {...} span amid prose."""
    text = (text or "").strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    if "```" in text:
        for part in text.split("```")[1::2]:
            part = part.strip()
            if part.lower().startswith("json"):
                part = part[4:].strip()
            try:
                return json.loads(part)
            except ValueError:
                continue
    start = text.find("{")
    while start >= 0:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                if esc:
                    esc = False          # this char is escaped (\", \\, …)
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except ValueError:
                        break  # not JSON after all; try the next "{"
        start = text.find("{", start + 1)
    raise ValueError(f"no JSON object in model reply: {text[:200]!r}")


# "Enhance my prompt" guides, one per kind. Hype words are banned on purpose:
# they made pictures look more AI in blind tests (see SCENE_REWRITE_SYSTEM).
_ENHANCE_COMMON = """Keep the user's subject and intent exactly; add detail, don't replace anything they asked for, and don't add people, objects or claims they didn't ask for beyond ordinary detail. No hype words (8k, 4k, ultra, HD, masterpiece, stunning, breathtaking, epic, award-winning, hyperrealistic). Reply with the improved prompt only: no preamble, no quotes, no notes, no markdown. Everyone in anything sexual or suggestive is an adult: never keep or add childlike traits, school settings or ages under 18. If the text sexualises a minor or childlike traits, reply with exactly REFUSED_MINOR. If you won't rewrite it for any other reason, reply with exactly DECLINED: never a partial, safer or different version."""

ENHANCE_GUIDES: dict[str, str] = {
    "image": """You improve a prompt for an image model. Describe, in this order: the subject, the setting, the composition and framing, the light (physically: where it comes from, how hard or soft, where shadows fall), the lens and camera, and the style. Photoreal unless the text asks for a style (illustration, watercolor, 3D, anime...). At most about 120 words.""",
    "video": """You improve a prompt for a video model. Describe one continuous shot (no cuts): the subject and what it does, the camera movement (or a locked-off camera), pacing in real time (never slow motion unless the user asks for it), and the light. At most about 80 words.""",
    "script": """You tighten a voiceover script for a short vertical video. Put a hook in the first line; use plain spoken sentences that are easy to say aloud; keep the same facts and claims (never invent facts, numbers, prices, names or promises); keep the same length within 15%. Reply with the script text only, no stage directions or labels.""",
    "scene": """You improve a description of a calm ambient place for an image model, like a real photograph: what is there (concrete materials, real plant and rock species, a few untidy natural details), the composition, the light described physically, the lens and camera. No people unless the user asks for them, no text. At most about 120 words.""",
}

_VIDEO_MOTION = ("seedance", "kling", "wan", "veo", "hailuo", "minimax", "ltx", "sora", "runway", "pixverse")
_NATURAL = ("gpt-image", "gpt-5-image", "dall-e", "nano-banana", "gemini", "imagen", "seedream")
_TAGGY = ("sogni/", "chroma", "flux", "z-image", "krea", "stable-diffusion", "sdxl", "sd3", "sd-")


def model_family_hint(kind: str, model: str) -> str:
    """One line nudging the wording toward what the target model reads best."""
    m = (model or "").lower()
    if not m or kind == "script":
        return ""
    if m.startswith("runware/"):
        # Runware ids (AIRs like runware:107@1) don't say the family: the name does.
        from .runware import display_name

        m = f"{m} {display_name(model)}".lower()
    if kind == "video" and any(f in m for f in _VIDEO_MOTION):
        return ("The target is a video model that responds to explicit camera and motion words: "
                "name the camera move and the subject's motion plainly (e.g. 'the camera pushes in', "
                "'she turns toward the window').")
    if any(f in m for f in _NATURAL):
        return "The target model reads natural sentences best: write full, plain sentences, not tag lists."
    if any(f in m for f in _TAGGY):
        return ("The target is a diffusion model (SD/Flux family): short comma-separated phrases are fine, "
                "most important first; no weight syntax like (word:1.2).")
    return ""


def enhance_system_prompt(kind: str, model: str = "") -> str:
    guide = ENHANCE_GUIDES.get(kind)
    if guide is None:
        raise ValueError(f"unknown prompt kind {kind!r}")
    hint = model_family_hint(kind, model)
    return "\n".join(x for x in (guide, _ENHANCE_COMMON, hint) if x)


_LABEL = re.compile(r"^\s*(?:improved |enhanced |final )?(?:prompt|script)\s*:\s*", re.I)


def clean_enhanced(text: str) -> str:
    """The reply as a bare prompt: no code fences, wrapping quotes or a
    leading "Prompt:" label; capped at 4000 chars."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    t = _LABEL.sub("", t.strip())
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'\u201c":
        t = t[1:-1]
    elif len(t) >= 2 and t[0] == "\u201c" and t[-1] == "\u201d":
        t = t[1:-1]
    return t.strip()[:4000]
