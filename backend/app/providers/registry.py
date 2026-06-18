from __future__ import annotations

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

_settings = get_settings()


def get_chat() -> ChatProvider:
    if _settings.chat_is_mocked():
        from .mock_chat import MockChatProvider

        return MockChatProvider()
    from .openrouter_chat import OpenRouterChatProvider

    return OpenRouterChatProvider()


def get_image() -> ImageProvider:
    if _settings.image_is_mocked():
        from .mock_image import MockImageProvider

        return MockImageProvider()
    from .openrouter_image import OpenRouterImageProvider

    return OpenRouterImageProvider()


def get_video() -> VideoProvider:
    if _settings.video_is_mocked():
        from .mock_video import get_singleton

        return get_singleton()
    from .openrouter_video import OpenRouterVideoProvider

    return OpenRouterVideoProvider()


def get_tts() -> TTSProvider:
    if _settings.tts_is_mocked():
        from .mock_tts import MockTTSProvider

        return MockTTSProvider()
    from .elevenlabs_tts import ElevenLabsTTSProvider

    return ElevenLabsTTSProvider()


def get_music() -> MusicProvider:
    if _settings.music_is_mocked():
        from .mock_music import MockMusicProvider

        return MockMusicProvider()
    from .elevenlabs_music import ElevenLabsMusicProvider

    return ElevenLabsMusicProvider()


def get_lipsync() -> LipSyncProvider:
    # No real provider yet; the slot exists so a drop-in (Wav2Lip / D-ID /
    # HeyGen) only has to implement LipSyncProvider + flip a feature flag.
    from .mock_lipsync import MockLipSyncProvider

    return MockLipSyncProvider()


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
    s = _settings
    training_lora = (
        "replicate"
        if (not s.mock_providers and s.replicate_api_token)
        else "mock"
    )
    training_voice = "elevenlabs" if not s.tts_is_mocked() else "mock"
    return {
        "mock_providers_env": s.mock_providers,
        "chat": "mock" if s.chat_is_mocked() else "openrouter",
        "image": "mock" if s.image_is_mocked() else "openrouter",
        "video": "mock" if s.video_is_mocked() else "openrouter",
        "tts": "mock" if s.tts_is_mocked() else "elevenlabs",
        "music": "mock" if s.music_is_mocked() else "elevenlabs",
        "lipsync": "mock",
        "training_lora": training_lora,
        "training_voice": training_voice,
    }
