from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import httpx

from ..config import get_settings
from ..schemas.storyboard import StoryboardPlan
from .base import CastContext, ChatProvider


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
        self.api_key = api_key or get_settings().openrouter_api_key
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
