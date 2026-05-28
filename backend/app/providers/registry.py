from __future__ import annotations

from ..config import get_settings
from .base import ChatProvider, ImageProvider, MusicProvider, TTSProvider, VideoProvider

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


def provider_status() -> dict:
    return {
        "mock_providers_env": _settings.mock_providers,
        "chat": "mock" if _settings.chat_is_mocked() else "openrouter",
        "image": "mock" if _settings.image_is_mocked() else "openrouter",
        "video": "mock" if _settings.video_is_mocked() else "openrouter",
        "tts": "mock" if _settings.tts_is_mocked() else "elevenlabs",
        "music": "mock" if _settings.music_is_mocked() else "elevenlabs",
    }
