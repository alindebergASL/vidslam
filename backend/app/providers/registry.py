from __future__ import annotations

from typing import TYPE_CHECKING

from ..config import get_settings
from ..services import provider_keys
from .base import (
    ChatProvider,
    ImageProvider,
    MusicProvider,
    TrainingProvider,
    TTSProvider,
    VideoProvider,
)

if TYPE_CHECKING:
    from .base import VideoCapabilities



def _checked(kind: str, model: str) -> str:
    """The provider to build for `kind` with `model`; raises
    ProviderUnavailable (before any provider call) when it's switched off."""
    from ..services import model_registry as reg

    provider, blocked = reg.route_info(kind, model)
    if provider == "unavailable":
        raise reg.unavailable(kind, model, blocked)
    return provider


def _active(kind: str) -> tuple[str, str]:
    """(model, provider) for `kind`, read at call time so a switch in the
    Settings page applies to the next job without a restart."""
    from ..services import model_registry as reg

    model = reg.active_model(kind)
    return model, _checked(kind, model)


def get_chat() -> ChatProvider:
    """The planner (Settings → Models), or in adult mode the adult text model
    (services/content_mode.py): storyboards, ✨ Enhance, scene rewrites."""
    from ..services import content_mode

    adult = content_mode.text_model()
    model, provider = (adult, _checked("chat", adult)) if adult else _active("chat")
    if provider == "mock":
        from .mock_chat import MockChatProvider

        return MockChatProvider()
    from .openrouter_chat import OpenRouterChatProvider

    return OpenRouterChatProvider(model=model)


def get_vision(model: str) -> ChatProvider:
    """A chat provider for the clip check's vision model (services/inspector.py),
    routed like any chat model; raises ProviderUnavailable when switched off."""
    if _checked("chat", model) == "mock":
        from .mock_chat import MockChatProvider

        return MockChatProvider()
    from .openrouter_chat import OpenRouterChatProvider

    return OpenRouterChatProvider(model=model, json_mode=False)


def is_sogni_model(model: str | None) -> bool:
    return bool(model) and str(model).startswith("sogni/")


def is_routed_model(model: str | None) -> bool:
    """An id whose provider comes from its prefix (sogni/…, runware/…, and
    the reserved replicate/…)."""
    from ..services import model_registry as reg

    m = str(model or "")
    return bool(m) and any(m.startswith(p) for p in (*reg.ROUTED_PREFIXES, *reg.RESERVED_PREFIXES))


def routes_per_model(kind: str, model: str | None) -> bool:
    """True when a call for a concrete `model` must pick its provider from
    that model rather than from the active one: a prefixed id (sogni/…), or
    any id while a prefixed model is active. Everything else keeps the old
    routing (the active model's provider, with the per-call model passed)."""
    if not model:
        return False
    if is_routed_model(model):
        return True
    from ..services import model_registry as reg

    return is_routed_model(reg.active_model(kind))


def _route(kind: str, model: str | None) -> tuple[str, str]:
    """(model, provider) to build for `kind`, given the call's model if any."""
    if routes_per_model(kind, model):
        return str(model), _checked(kind, str(model))
    return _active(kind)


def get_image(model: str | None = None) -> ImageProvider:
    """Image provider for `model` (a `sogni/` id goes to Sogni whatever is
    active), or for the active image model when no model is given."""
    model, provider = _route("image", model)
    if provider == "mock":
        from .mock_image import MockImageProvider

        return MockImageProvider()
    if provider == "sogni":
        from .sogni import SogniImageProvider

        return SogniImageProvider(model=model)
    if provider == "runware":
        from .runware import RunwareImageProvider

        return RunwareImageProvider(model=model)
    from .openrouter_image import OpenRouterImageProvider

    return OpenRouterImageProvider(model=model)


def get_video(model: str | None = None) -> VideoProvider:
    """Video provider for `model` (see get_image), else the active one."""
    model, provider = _route("video", model)
    if provider == "mock":
        from .mock_video import get_singleton

        return get_singleton()
    if provider == "sogni":
        from .sogni import SogniVideoProvider

        return SogniVideoProvider(model=model)
    if provider == "runware":
        from .runware import RunwareVideoProvider

        return RunwareVideoProvider(model=model)
    from .openrouter_video import OpenRouterVideoProvider

    return OpenRouterVideoProvider(model=model)


def uses_webhooks(model: str | None) -> bool:
    """Whether a video job for `model` can be told to call back (OpenRouter
    only; Sogni has no callbacks and Runware's aren't signed: both are polled)."""
    from ..services import model_registry as reg

    return not any(str(model or "").startswith(p) for p in reg.ROUTED_PREFIXES)


# Explicit names for call sites that always hold a concrete model.
def image_provider_for(model: str | None) -> ImageProvider:
    return get_image(model)


def video_provider_for(model: str | None) -> VideoProvider:
    return get_video(model)


def provider_label(kind: str, model: str | None) -> str:
    """What to store as shot/job `provider` for a call with `model`:
    "sogni" / "mock" for routed models, else the old mock/openrouter label."""
    if routes_per_model(kind, model):
        from ..services import model_registry as reg

        p = reg.provider_for(kind, model)
        return p if p in ("mock", "sogni", "runware") else "openrouter"
    s = get_settings()
    mocked = s.image_is_mocked() if kind == "image" else s.video_is_mocked()
    return "mock" if mocked else "openrouter"


def video_capabilities(model: str = "") -> "VideoCapabilities":
    """Capabilities of `model` on the provider that serves it, falling back
    to the configured default cap when the provider can't say."""
    from .base import VideoCapabilities

    fn = None
    try:
        provider = get_video(model or None)
        fn = getattr(provider, "capabilities", None)
    except Exception:  # noqa: BLE001 — e.g. a key missing: use the defaults
        pass
    if callable(fn):
        try:
            return fn(model)
        except Exception:  # noqa: BLE001
            pass
    cap = max(5, get_settings().ambient_default_max_clip_seconds)
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


def get_sound():
    """Nature-sound beds for ambient scenes: ElevenLabs sound effects when a
    key is set and providers are live, else a generated noise placeholder
    (never when ElevenLabs is merely switched off: then it's refused)."""
    if get_settings().music_is_mocked():
        from .sound import MockSoundProvider

        return MockSoundProvider()
    if not provider_keys.serves("elevenlabs"):
        raise provider_keys.ProviderUnavailable("elevenlabs", kind="sound")
    from .sound import ElevenLabsSoundProvider

    return ElevenLabsSoundProvider()


def get_avatar():
    """Portrait → talking-head clip provider, or None when lip-sync is off
    (or its provider is switched off: talking heads then use the video
    model, as with no portrait)."""
    from ..services import model_registry as reg

    model = reg.active_model("lipsync")
    provider = reg.provider_for("lipsync", model)
    # Switched off, even for a render already running: the video model (which
    # was checked when the render was queued) makes the talking head instead.
    if provider in ("off", "unavailable") or reg.switched_off("lipsync", model):
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
    s = get_settings()
    if kind == "voice_clone" and not s.tts_is_mocked():
        if not provider_keys.serves("elevenlabs"):
            raise provider_keys.ProviderUnavailable("elevenlabs", kind="voice_clone")
        from .elevenlabs_voice_lab import ElevenLabsVoiceLabProvider

        return ElevenLabsVoiceLabProvider()
    # Same gate as provider_status(): MOCK_PROVIDERS=true overrides every
    # key, so a key saved in Settings never starts paid training in mock mode.
    if kind in {"character_lora", "style_lora"} and not s.mock_providers and provider_keys.has("replicate"):
        if not provider_keys.serves("replicate"):
            raise provider_keys.ProviderUnavailable("replicate", kind=kind)
        from .replicate_training import ReplicateTrainingProvider

        return ReplicateTrainingProvider()
    if _training_singleton is None:
        from .mock_training import MockTrainingProvider

        _training_singleton = MockTrainingProvider()
    return _training_singleton


def provider_status() -> dict:
    from ..services import model_registry as reg

    s = get_settings()
    snap = reg.snapshot()
    def _train(name: str, live: bool) -> str:
        if not live:
            return "mock"
        return name if provider_keys.enabled(name) else "unavailable"

    training_lora = _train("replicate", not s.mock_providers and provider_keys.has("replicate"))
    training_voice = _train("elevenlabs", not s.tts_is_mocked())
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
