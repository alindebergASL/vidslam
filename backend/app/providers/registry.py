from __future__ import annotations

import re
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
    from .base import ImageOpProvider, VideoCapabilities



class _Guarded:
    """A provider with the safety check (services/minor_check.py) run on
    every prompt before it's delegated; everything else passes through.
    The adapters keep their own word filter on the exact text they send."""

    def __init__(self, inner) -> None:
        self.inner = inner

    @property
    def __class__(self):  # noqa: D105 — isinstance(p, RunwareImageProvider) etc. still holds
        return type(self.inner)

    def __getattr__(self, name: str):
        return getattr(self.inner, name)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.inner!r})"


def _guard(prompt: str, negative: str, kind: str) -> None:
    from ..services.minor_check import check_generation

    check_generation(prompt or "", negative or "", kind=kind)


def _explicit(text: str) -> None:
    """A clean deployment: no explicit words are voiced or lip-synced,
    whichever screen they came from (services/explicit_check.py)."""
    from ..services import explicit_check

    explicit_check.check(text or "")


# A negated "explicit" or "sexual" in a sound or music prompt ("nothing
# explicit", "no sexual content", "non-explicit", "without explicit
# lyrics"), right before a break or the end: not the word itself.
_NEGATED_ADULT_RE = re.compile(
    r"(?<![\w-])(?:no|nothing|not|non-?|without)\s*(?:explicit|sexual)"
    r"(?:\s+(?:content|material|lyrics|themes|language|words))?(?=\s*(?:[,.;:!)\]]|$))(?![^\n]*\?)", re.I)
# A word that turns the negation round or makes it a question ("never not
# sexual", "not non-explicit", "why not explicit", "if not explicit").
_UNNEGATING_RE = re.compile(r"\b(?:never|not|no|non|nothing|without|why|if|hardly|barely)[\W_]*$", re.I)


def _negations_out(text: str) -> str:
    """A copy of `text` for the word filter only: a plain negated
    "explicit" or "sexual" ("nothing explicit", "no sexual content") taken
    out. Never what the safety model or the explicit-content check reads."""
    def drop(m: "re.Match") -> str:
        return m.group(0) if _UNNEGATING_RE.search(m.string[:m.start()]) else " "

    return _NEGATED_ADULT_RE.sub(drop, text).strip() or text


def _explicit_prompt(prompt: str) -> None:
    """A clean deployment: a prompt sent to a lip-sync model (its motion)
    gets the explicit-content check (services/explicit_check.py)."""
    from ..services import explicit_check

    if (prompt or "").strip() and explicit_check.active():
        explicit_check.check_prompt(prompt)


class GuardedImageProvider(_Guarded):
    def generate(self, *, model: str, prompt: str, references, negative_prompt: str = "",
                 settings: dict | None = None, **kw) -> bytes:
        _guard(prompt, negative_prompt, "image")
        return self.inner.generate(model=model, prompt=prompt, references=references,
                                   negative_prompt=negative_prompt, settings=settings, **kw)


class GuardedVideoProvider(_Guarded):
    def submit(self, *, model: str, prompt: str, references, negative_prompt: str = "",
               reference_strategy: str = "input_references", duration_seconds: float = 5.0,
               settings: dict | None = None, **kw):
        _guard(prompt, negative_prompt, "video")
        return self.inner.submit(model=model, prompt=prompt, references=references,
                                 negative_prompt=negative_prompt, reference_strategy=reference_strategy,
                                 duration_seconds=duration_seconds, settings=settings, **kw)


class GuardedAvatarProvider(_Guarded):
    def submit_avatar(self, *, script: str = "", motion_prompt: str = "", **kw):
        from ..services.minor_check import check_generation
        from ..services.safety import check_prompt

        if (motion_prompt or "").strip():
            check_prompt(motion_prompt)
        check_generation(" ".join(x for x in (motion_prompt or "", script or "") if x.strip()),
                         kind="lipsync")
        # A clean deployment: the motion and the script, each judged once
        # (the script as the voiceover's words are).
        _explicit_prompt(motion_prompt)
        _explicit(script)
        return self.inner.submit_avatar(script=script, motion_prompt=motion_prompt, **kw)


class GuardedTTSProvider(_Guarded):
    """`story`: a Sound Studio voice (get_tts_for), whose text is story
    markup: every request is sent with settings story_markup set. Any other
    caller (a video voiceover through pipeline.synthesize, the hook lab) is
    checked exactly as before story voices existed: check_generation on the
    raw text, kind "script". `voice_provider`: the voice provider a story
    voice was made for (get_tts_for), checked against the voice the story
    pass binds each speaker to."""

    def __init__(self, inner, *, story: bool = False, voice_provider: str = "") -> None:
        super().__init__(inner)
        self.story = story
        self.voice_provider = voice_provider

    def synthesize(self, *, text: str, voice_id: str, settings: dict | None = None, context: str = "",
                   story_pass: str = "", speaker: str = "", **kw) -> bytes:
        """The story path (a story voice, story markup in `settings`, or a
        story `context`): `context` is the story's context and `story_pass`
        its signed pass (by default the one the context carries), both from
        story_script.check_story; `speaker` is who says the line (a cast
        member). The line is sent only when the pass covers this speaker,
        text and delivery instructions with this context, and `voice_id` is
        the voice the pass binds the speaker to (check_story's `voices`;
        the default voice "" when none was given); anything else (no
        speaker, a speaker not in the cast, a line or instructions changed
        since the check, a different cast or voice, a forged or expired
        pass) is refused: run check_story on the current story again. On a
        clean deployment, what a pass covers was judged by the
        explicit-content check in check_story, so it isn't asked again."""
        from ..services import story_pass as passes
        from ..services import story_script
        from ..services.minor_check import split_context
        from ..services.safety import UnsafePromptError

        token = passes.of(context, story_pass)

        if self.story and not story_script.is_story(settings):
            settings = {**(settings or {}), story_script.STORY_MARKUP: True}
        instructions = str((settings or {}).get("instructions") or "")
        if not story_script.is_story(settings) and not (context or "").strip():
            _guard(text, "", "script")
            if instructions.strip():
                _guard(instructions, "", "script")
                _explicit(f"{text}\n{instructions}")
            else:
                _explicit(text)
            return self.inner.synthesize(text=text, voice_id=voice_id, settings=settings, **kw)

        # The story path. Fullwidth brackets and look-alikes ("［moans］")
        # read as the tags they look like, in the checks and in what is
        # sent; dash look-alikes are folded in the checks only (an em dash
        # is a pause to a voice).
        who = " ".join((speaker or "").split()).upper()
        if not who:
            raise UnsafePromptError("A story line needs its speaker to be voiced. Check the story again.")
        cast = {line.split(":", 1)[0].strip().upper()
                for line in split_context(str(context or ""))[1].split("\n")[1:] if line.strip()}
        if who not in cast:
            raise UnsafePromptError(f"{who} isn't in this story's cast. Check the story again.")
        text = story_script.normalize(text, dashes=False)
        if instructions:
            instructions = story_script.normalize(instructions, dashes=False)
            settings = {**(settings or {}), "instructions": instructions}
        if not passes.covers(token, context, passes.line_item(who, text, instructions)):
            raise UnsafePromptError(passes.STALE)
        # The voice really used: an empty or unlisted id falls back to the
        # provider's own (OpenRouter's configured voice, ElevenLabs' default),
        # which may be another speaker's. That is what the binding is
        # compared with, and what is sent.
        resolve = getattr(self.inner, "resolve_voice", None)
        resolved = resolve(voice_id) if callable(resolve) else voice_id
        default = resolve("") if callable(resolve) else ""
        if not passes.voice_bound(token, who, resolved, self.voice_provider, default_voice=default):
            raise UnsafePromptError(passes.WRONG_VOICE.format(speaker=who))
        voice_id = resolved
        # A clean deployment voices general-audience directions only: an
        # adult tag, or any other words in brackets a tag-reading model
        # would perform ("[moaning]"), are taken out (check_story judged the
        # line with them).
        if story_script.allowed_tags() == story_script.GENERAL_TAGS:  # a clean deployment
            text = story_script.drop_directions(text, story_script.GENERAL_TAGS)
        if not passes.clean_checked(token):
            _explicit("\n".join(x for x in (text, instructions) if x.strip()) if instructions.strip() else text)
        return self.inner.synthesize(text=text, voice_id=voice_id, settings=settings, **kw)


# What the app adds to a music prompt for each part of a long bed
# (ambient.generate_music_bed): fixed words, only the numbers change.
MUSIC_PART_NOTE = (" Part {i} of {n} of one continuous piece: keep the same key, tempo, instruments and mood; "
                   "let it evolve gently, no ending or fade.")
_MUSIC_PART_RE = re.compile(re.escape(MUSIC_PART_NOTE).replace(r"\{i\}", r"\d+").replace(r"\{n\}", r"\d+") + "$")


def music_part_prompt(prompt: str, i: int, n: int) -> str:
    """`prompt` for part `i` of `n` of one long music bed."""
    return prompt + MUSIC_PART_NOTE.format(i=i, n=n)


def _base_music_prompt(prompt: str) -> str:
    """The prompt without the app's part note: every part of a bed is the
    same prompt to the checks (checked, and paid for, once)."""
    return _MUSIC_PART_RE.sub("", prompt or "")


def _audio_guard(prompt: str, context: str = "", story_pass: str = "", *, force: bool = False) -> None:
    """A sound or music prompt: the word filter, the minors check and, on a
    clean deployment, the explicit-content check. Every caller of
    get_sound() and get_music() comes through here, whatever screen the
    prompt came from.

    `context`: a story's context (story_script.check_story): with one, the
    prompt is a story part, sent only when `story_pass` (by default the one
    the context carries) covers this prompt with this context (check_story
    judged it, with the story's facts and cast in front); anything else is
    refused (story_pass.STALE: check the story again).

    Without one: the word filter reads the prompt as written, then the
    minors check (kind "audio") asks the model about every prompt, in every
    mode, and fails closed: when the check can't run (no key, out of
    credit, an error or timeout after one retry) the prompt waits. The
    model reads the prompt marked by story_script.safety_text (an adult
    direction, "(ghosts moaning)", gets a marker after it); the word filter
    of each window reads it without those markers, so a marked direction
    near a child is the model's to judge, not a refusal by the words alone
    when all it says may be an everyday sound ("(ghosts moaning)", "(panting
    dog)", "(moaning softly)"); an adult voice tag in brackets ("[moans]")
    or a word that is never an everyday sound ("(orgasmic moans)") still is
    (minor_check.unmarked). The one exception is
    an app-written preset, word for word (services/vetted_audio.py): vetted
    at test time, so it needs no live call, unless `force` (lyrics or a
    composition plan are joined to it)."""
    from ..services import story_pass as passes
    from ..services import vetted_audio
    from ..services.minor_check import _word_filter, check_long
    from ..services.safety import UnsafePromptError, check_prompt
    from ..services.story_script import safety_text

    token = passes.of(context, story_pass)
    text = (prompt or "").strip()
    if not text:
        return
    # "family friendly, nothing explicit", "no sexual content": the word
    # filter reads a copy without a plain negated "explicit" or "sexual";
    # the safety model, the explicit-content check and the vetted-preset
    # lookup read the text as written.
    words = _negations_out(text) if not (context or "").strip() else text
    check_prompt(words)
    context = (context or "").strip()
    if context:
        if not passes.covers(token, context, passes.prompt_item(text)):
            raise UnsafePromptError(passes.STALE)
        # Covered: on a clean deployment check_story's explicit-content
        # check judged it (the pass says so), so it isn't asked again.
        if passes.clean_checked(token):
            return
    else:
        if not force and vetted_audio.is_vetted(text):
            # Exactly an app-written preset, vetted at test time
            # (services/vetted_audio.py): no live call, so a rain bed
            # doesn't wait on OpenRouter. Anything else is asked.
            return
        # The word filter read `text` as written (check_prompt above), then
        # once more with the markers safety_text adds after a direction
        # group near a minor, of which it keeps only those after an
        # unmistakable sexual word or an exact adult voice tag
        # (minor_check.unmarked). The model reads the prompt as the user
        # wrote it: the markers are the app's notes, which it would read as
        # the user's ("(waves crashing, seagulls) (sensual)").
        _word_filter(safety_text(words, "", prose=True), "", "audio", marked=True)
        # The whole text's word filter has run (above), so the windows'
        # word filter would only read less of it: the model reads each
        # window as written.
        check_long(text, kind="audio", force=force, words=False)
    _explicit_prompt(text)


def audio_guard(prompt: str, context: str = "", story_pass: str = "") -> None:
    """The checks get_sound() and get_music() run on a prompt, for a caller
    that wants a refusal before anything is paid for (ambient.precheck)."""
    _audio_guard(_base_music_prompt(prompt), context, story_pass)


class GuardedSoundProvider(_Guarded):
    def generate(self, *, prompt: str, duration_seconds: float = 30.0, context: str = "", story_pass: str = "",
                 **kw) -> bytes:
        """`context`: the story's context (story callers pass it); never
        sent. `story_pass`: check_story's pass (see _audio_guard)."""
        _audio_guard(prompt, context, story_pass)
        return self.inner.generate(prompt=prompt, duration_seconds=duration_seconds, **kw)


# Music `settings` reach the provider's request as they are (ElevenLabs
# merges them into the body), so only these keys are allowed: plain
# switches and numbers, and for model_id and output_format a short plain
# name (MUSIC_NAME_RE). A text-bearing key (a second prompt, lyrics, a
# composition plan with sections and lines) is refused unless it is one of
# MUSIC_TEXT_KEYS, whose every string is joined to the prompt and judged
# with it as one text (the model always asked). Any other key, or a text
# value under a plain key, is refused.
MUSIC_SETTING_KEYS = frozenset({"force_instrumental", "music_length_ms", "seed", "output_format", "model_id",
                                "respect_sections_durations"})
MUSIC_NAME_KEYS = frozenset({"output_format", "model_id"})
MUSIC_NAME_RE = re.compile(r"[a-z0-9_.-]{1,64}")
MUSIC_TEXT_KEYS = frozenset({"composition_plan", "lyrics"})


def _strings(value) -> list[str]:
    """Every string inside a settings value (dicts and lists walked)."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in (*_strings(k), *_strings(v))]
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in _strings(v)]
    return []


def _music_texts(settings: dict | None) -> list[str]:
    """Every string of the MUSIC_TEXT_KEYS values; raises ValueError for a
    key that could carry unchecked text (see MUSIC_SETTING_KEYS)."""
    texts: list[str] = []
    for key, value in (settings or {}).items():
        if key in MUSIC_TEXT_KEYS:
            texts += [t for t in _strings(value) if t.strip()]
        elif key in MUSIC_NAME_KEYS and isinstance(value, str):
            if not MUSIC_NAME_RE.fullmatch(value):
                raise ValueError(f"music setting {key!r} is not allowed")
        elif key not in MUSIC_SETTING_KEYS or _strings(value):
            raise ValueError(f"music setting {key!r} is not allowed")
    return texts


class GuardedMusicProvider(_Guarded):
    """Every get_music() caller's prompt goes through the guard, joined with
    any text in its `settings` (lyrics, a composition plan) and judged as
    one text (MUSIC_SETTING_KEYS: anything else, such as a second "prompt",
    is refused)."""

    def generate(self, *, prompt: str, duration_seconds: float, settings: dict | None = None,
                 context: str = "", story_pass: str = "", **kw) -> bytes:
        """`context`: the story's context (story callers pass it); never
        sent. `story_pass`: check_story's pass, for a prompt alone."""
        texts = _music_texts(settings)
        base = _base_music_prompt(prompt)
        _audio_guard("\n".join(x for x in (base, *texts) if (x or "").strip()), context, story_pass,
                     force=bool(texts))
        return self.inner.generate(prompt=prompt, duration_seconds=duration_seconds, settings=settings, **kw)


def unwrap(provider):
    """The adapter inside a guarded provider (tests, type checks)."""
    return provider.inner if isinstance(provider, _Guarded) else provider


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
    active), or for the active image model when no model is given; every
    prompt goes through the safety check first."""
    return GuardedImageProvider(_image(model))


def _image(model: str | None) -> ImageProvider:
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
    """Video provider for `model` (see get_image), else the active one,
    with the safety check on every prompt."""
    return GuardedVideoProvider(_video(model))


def _video(model: str | None) -> VideoProvider:
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


def get_image_op(kind: str, model: str | None = None) -> "ImageOpProvider":
    """Provider for a picture operation (`kind` "bg_removal" | "upscale")
    with `model` (else the active one): Runware, the free local upscale or
    the mock. Never OpenRouter; raises ProviderUnavailable (before any
    provider call) when it can't be served."""
    from ..services import model_registry as reg

    if kind not in reg.OP_KINDS:
        raise ValueError(f"not a picture operation: {kind!r}")
    model = model or reg.active_model(kind)
    provider = _checked(kind, model)
    if provider == "mock":
        from .mock_image_ops import MockImageOpProvider

        return MockImageOpProvider()
    if provider == "local":
        from .local_upscale import LocalUpscaleProvider

        return LocalUpscaleProvider()
    if provider == "runware":
        from .runware import RunwareImageOpProvider

        return RunwareImageOpProvider(model=model)
    raise reg.unavailable(kind, model, None)


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
    """The voice provider; every script goes through the safety check."""
    return GuardedTTSProvider(_tts())


TTS_PROVIDERS = ("elevenlabs", "openrouter", "mock")


def story_voice(provider: str = "", model: str = "") -> tuple[str, str]:
    """(provider, model) a story voice uses: ElevenLabs when no provider is
    set, with the story model (ELEVENLABS_STORY_MODEL_ID) when no model is.
    The one place this default is decided: the generator (get_tts_for) and
    the estimate and fingerprints (sound_estimate.voice_route) both ask it."""
    provider = (provider or "").strip().lower() or "elevenlabs"
    model = (model or "").strip()
    if provider == "elevenlabs" and not model:
        model = get_settings().elevenlabs_story_model_id
    return provider, model


def get_tts_for(provider: str = "", model: str = "", voice: str = "") -> TTSProvider:
    """A voice provider picked per story or per character (Sound Studio):
    `provider` "elevenlabs" with an ElevenLabs model id (default: the story
    model, ELEVENLABS_STORY_MODEL_ID), "openrouter" with an OpenRouter
    voice model id (gpt-4o-mini-tts, gpt-audio...), or "" for the story
    default (story_voice, which the estimate uses too). The mock stands in
    in mock mode or without a key, as for the active voice; a provider
    switched off is refused. Guarded like get_tts(), as a story voice (its
    text is story markup: GuardedTTSProvider story)."""
    from ..services import model_catalog

    provider, model = story_voice(provider, model)
    if provider not in TTS_PROVIDERS:
        raise ValueError(f"unknown voice provider: {provider!r}")
    if provider == "mock":
        from .mock_tts import MockTTSProvider

        return GuardedTTSProvider(MockTTSProvider(), story=True, voice_provider=provider)
    if provider == "elevenlabs":
        route = _checked("tts", "elevenlabs")
    else:
        if not model or model == "elevenlabs":
            raise ValueError("an OpenRouter voice needs a model id")
        route = _checked("tts", model)
    if route == "mock":
        from .mock_tts import MockTTSProvider

        return GuardedTTSProvider(MockTTSProvider(), story=True, voice_provider=provider)
    if provider == "elevenlabs":
        from .elevenlabs_tts import ElevenLabsTTSProvider

        return GuardedTTSProvider(ElevenLabsTTSProvider(model_id=model), story=True,
                                  voice_provider=provider)
    from .openrouter_tts import OpenRouterTTSProvider

    entry = model_catalog.find("tts", model) or {}
    outs = entry.get("output_modalities") or []
    return GuardedTTSProvider(OpenRouterTTSProvider(
        model=model, voice=voice or "",
        voices=entry.get("supported_voices") or [],
        chat_audio=("audio" in outs and "speech" not in outs) if outs else None,
    ), story=True, voice_provider=provider)


def _tts() -> TTSProvider:
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
    """The music provider; every prompt goes through the safety check."""
    return GuardedMusicProvider(_music())


def _music() -> MusicProvider:
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
    """Sound effects and nature-sound beds: ElevenLabs sound effects when a
    key is set and providers are live, else a generated noise placeholder
    (never when ElevenLabs is merely switched off: then it's refused). Every
    prompt goes through the safety check."""
    return GuardedSoundProvider(_sound())


def _sound():
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

        return GuardedAvatarProvider(MockAvatarProvider(model=model))
    from .openrouter_avatar import OpenRouterAvatarProvider

    return GuardedAvatarProvider(OpenRouterAvatarProvider(model=model))


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
    # Kinds that can't run only because their provider has no key yet (not
    # switched off), so the home page can say "needs a key" instead.
    needs_key = []
    for k in reg.KINDS:
        provider, blocked = reg.route_info(k, snap[k]["model"])
        if provider == "unavailable" and blocked in provider_keys.KEYS and not provider_keys.has(blocked):
            needs_key.append(k)
    return {
        "mock_providers_env": s.mock_providers,
        "chat": snap["chat"]["provider"],
        "image": snap["image"]["provider"],
        "video": snap["video"]["provider"],
        "tts": snap["tts"]["provider"],
        "music": snap["music"]["provider"],
        "lipsync": snap["lipsync"]["provider"],
        "bg_removal": snap["bg_removal"]["provider"],
        "upscale": snap["upscale"]["provider"],
        "training_lora": training_lora,
        "training_voice": training_voice,
        "needs_key": needs_key,
        "models": {k: v["model"] for k, v in snap.items()},
    }
