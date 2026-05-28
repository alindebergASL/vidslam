from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

import httpx

from ..config import get_settings
from ..schemas.storyboard import StoryboardPlan
from .base import CastContext, ChatProvider

settings = get_settings()


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
    def __init__(self, *, api_key: str | None = None, model: str | None = None) -> None:
        self.api_key = api_key or settings.openrouter_api_key
        self.model = model or settings.openrouter_chat_model
        if not self.api_key:
            raise RuntimeError("OPENROUTER_API_KEY missing for real chat provider")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "HTTP-Referer": settings.openrouter_site_url,
            "X-Title": settings.openrouter_app_title,
            "Content-Type": "application/json",
        }

    def generate_storyboard(self, *, project: dict, cast: CastContext) -> StoryboardPlan:
        body = {
            "model": self.model,
            "response_format": {"type": "json_object"},
            "temperature": 0.4,
            "messages": [
                {"role": "system", "content": _load_system_prompt()},
                {"role": "user", "content": _format_user_payload(project, cast)},
            ],
        }
        url = f"{settings.openrouter_base_url.rstrip('/')}/chat/completions"
        with httpx.Client(timeout=120.0) as client:
            r = client.post(url, headers=self._headers(), json=body)
            r.raise_for_status()
            data = r.json()
        content = data["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        return StoryboardPlan.model_validate(parsed)
