from __future__ import annotations

from typing import TYPE_CHECKING

from ..config import get_settings
from .base import (
    ChatProvider,
    ImageProvider,
    LipSyncProvider,
    MusicProvider,
    TrainingProvider,
    TTSProvider,
    VideoProvider,
)

if TYPE_CHECKING:
    from .base import VideoCapabilities

_settings = get_settings()


def _active(kind: str) -> tuple[str, str]:
    """(model, provider) for `kind`, read at call time so a switch in the
    Settings page applies to the next job without a restart."""
    from ..services import model_registry as reg

    model = reg.active_model(kind)
    return model, reg.provider_for(kind, model)


def get_chat() -> ChatProvider:
    model, provider = _active("chat")
    if provider == "mock":
        from .mock_chat import MockChatProvider

        return MockChatProvider()
    from .openrouter_chat import OpenRouterChatProvider

    return OpenRouterChatProvider(model=model)


def get_image() -> ImageProvider:
    model, provider = _active("image")
    if provider == "mock":
        from .mock_image import MockImageProvider

        return MockImageProvider()
    from .openrouter_image import OpenRouterImageProvider

    return OpenRouterImageProvider(model=model)


def get_video() -> VideoProvider:
    model, provider = _active("video")
    if provider == "mock":
        from .mock_video import get_singleton

        return get_singleton()
    from .openrouter_video import OpenRouterVideoProvider

    return OpenRouterVideoProvider(model=model)


def video_capabilities(model: str = "") -> "VideoCapabilities":
    """Capabilities of `model` on the active video provider, falling back to
    the configured default cap when the provider can't say."""
    from .base import VideoCapabilities

    provider = get_video()
    fn = getattr(provider, "capabilities", None)
    if callable(fn):
        try:
            return fn(model)
        except Exception:  # noqa: BLE001
            pass
    cap = max(5, _settings.ambient_default_max_clip_seconds)
    return VideoCapabilities(durations=sorted({5, cap}), first_frame=True, known=False)


def get_tts() -> TTSProvider:
    from ..services import model_catalog
    from ..services import model_registry as reg

    model, provider = _active("tts")
    if provider == "mock":
        from .mock_tts import MockTTSProvider

        return MockTTSProvider()
    if provider == "openrouter":
        from .openrouter_tts import OpenRouterTTSProvider

        entry = model_catalog.find("tts", model) or {}
        outs = entry.get("output_modalities") or []
        return OpenRouterTTSProvider(
            model=model,
            voice=reg.active_options("tts").get("voice", ""),
            voices=entry.get("supported_voices") or [],
            chat_audio=("audio" in outs and "speech" not in outs) if outs else None,
        )
    from .elevenlabs_tts import ElevenLabsTTSProvider

    return ElevenLabsTTSProvider()


def get_music() -> MusicProvider:
    model, provider = _active("music")
    if provider == "mock":
        from .mock_music import MockMusicProvider

        return MockMusicProvider()
    if provider == "gemini":
        from .lyria_music import LyriaMusicProvider

        return LyriaMusicProvider(model=model)
    if provider == "openrouter":
        from .openrouter_music import OpenRouterMusicProvider

        return OpenRouterMusicProvider(model=model)
    from .elevenlabs_music import ElevenLabsMusicProvider

    # "elevenlabs" = configured default model; "elevenlabs/<model_id>" pins one.
    return ElevenLabsMusicProvider(model_id=model.split("/", 1)[1] if model.startswith("elevenlabs/") else None)


def get_lipsync() -> LipSyncProvider:
    # Kept for the legacy render(source_video, target_audio) slot; the
    # talking-head path uses get_avatar() below.
    from .mock_lipsync import MockLipSyncProvider

    return MockLipSyncProvider()


def get_avatar():
    """Portrait → talking-head clip provider, or None when lip-sync is off."""
    model, provider = _active("lipsync")
    if provider == "off":
        return None
    if provider == "mock":
        from .mock_avatar import MockAvatarProvider

        return MockAvatarProvider(model=model)
    from .openrouter_avatar import OpenRouterAvatarProvider

    return OpenRouterAvatarProvider(model=model)


# Module-level singleton so the mock training provider's per-job poll
# counter survives across pipeline polls (poll() is called repeatedly).
_training_singleton: TrainingProvider | None = None


def get_training(kind: str | None = None) -> TrainingProvider:
    """Pick a training provider by job kind. Voice clones go through
    ElevenLabs Voice Lab when the key is set; LoRA flavors go through
    Replicate when configured; both fall back to the mock provider that's
    deterministic and instant.

    `kind` may be None when the caller just wants any provider (e.g. UI
    listing what kinds are trainable); then the mock impl is the
    canonical truth source."""
    global _training_singleton
    s = _settings
    if kind == "voice_clone" and not s.tts_is_mocked():
        from .elevenlabs_voice_lab import ElevenLabsVoiceLabProvider

        return ElevenLabsVoiceLabProvider()
    if kind in {"character_lora", "style_lora"} and s.replicate_api_token:
        from .replicate_training import ReplicateTrainingProvider

        return ReplicateTrainingProvider()
    if _training_singleton is None:
        from .mock_training import MockTrainingProvider

        _training_singleton = MockTrainingProvider()
    return _training_singleton


def provider_status() -> dict:
    from ..services import model_registry as reg

    s = _settings
    snap = reg.snapshot()
    training_lora = (
        "replicate"
        if (not s.mock_providers and s.replicate_api_token)
        else "mock"
    )
    training_voice = "elevenlabs" if not s.tts_is_mocked() else "mock"
    return {
        "mock_providers_env": s.mock_providers,
        "chat": snap["chat"]["provider"],
        "image": snap["image"]["provider"],
        "video": snap["video"]["provider"],
        "tts": snap["tts"]["provider"],
        "music": snap["music"]["provider"],
        "lipsync": snap["lipsync"]["provider"],
        "training_lora": training_lora,
        "training_voice": training_voice,
        "models": {k: v["model"] for k, v in snap.items()},
    }
