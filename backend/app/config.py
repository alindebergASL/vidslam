from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # core
    mock_providers: bool = True
    mvp_password: str = "changeme"
    session_secret: str = "dev-session-secret-change-me"
    public_base_url: str = "http://localhost:8000"
    frontend_origin: str = "http://localhost:3000"

    # storage / db / queue
    database_url: str = "sqlite:///./data/app.db"
    data_dir: str = "./data"
    redis_url: str = "redis://localhost:6379/0"
    # Job queue: "auto" = RQ when Redis answers, else run inline (dev);
    # "required" = refuse to run jobs inline (503) — use in production so a
    # Redis outage can't pin web workers on hour-long renders; "inline" =
    # never use Redis.
    queue_mode: str = "auto"
    # Per-job ceiling for RQ jobs. Chained ambient loops on real video
    # models and avatar jobs can take well over the old 30 min.
    queue_job_timeout_seconds: int = 4 * 3600

    # Keep the newest N completed renders per project; older ones have their
    # files deleted (local + S3) and are marked expired. 0 = keep all.
    render_retention_keep: int = 0

    # Ops alerts: Slack/Discord-compatible incoming webhook. Empty = log only.
    alert_webhook_url: str = ""
    alert_dedupe_minutes: int = 30
    alert_provider_errors: int = 5

    # rate limiting (budget-burning generation endpoints only)
    rate_limit_enabled: bool = True
    # Bucket capacity: how many generation calls a client can burst.
    rate_limit_generation_burst: int = 10
    # Steady-state refill: tokens added back per minute.
    rate_limit_generation_per_minute: float = 10.0

    # uploads
    # Phone photos (48 MP HEIC/ProRAW-sized JPEGs) run well past 15 MB.
    max_upload_bytes: int = 50 * 1024 * 1024
    # Declared types an image upload may arrive with. The bytes are always
    # decoded and checked. HEIC/HEIF is the iPhone camera default (converted
    # to JPEG on upload). Browsers that don't know .heic send octet-stream or
    # no type at all: storage.accepted_upload_type then takes the file only
    # when its name is an image name (.heic/.heif/.jpg/...).
    allowed_image_mimes: tuple[str, ...] = (
        "image/png",
        "image/jpeg",
        "image/webp",
        "image/heic",
        "image/heif",
    )

    # openrouter
    openrouter_api_key: str = ""
    openrouter_site_url: str = "http://localhost:3000"
    openrouter_app_title: str = "AvatarVideoStudio"
    # Storyboard planner. Any OpenRouter text model works; switch live in
    # Settings → Models. claude-haiku-5.5 (Oct 2026) replaced gpt-4o-mini
    # (Jul 2024) as the default: newer, cheaper, supports structured outputs.
    openrouter_chat_model: str = "anthropic/claude-haiku-5.5"
    # Adult mode (Settings → Adult content overrides this default): text work
    # (planning, ✨ Enhance, scene rewrites) moves to ADULT_TEXT_MODEL and the
    # picture/video filters follow their own settings; off, the planner above
    # and the filters on. See services/content_mode.py. DeepSeek V4 Flash
    # rewrote and planned the owner's adult prompts where the planner refused
    # or toned them down; V4 Pro is Enhance's fallback when Flash declines.
    adult_mode: bool = False
    adult_text_model: str = "deepseek/deepseek-v4-flash"
    adult_text_fallback_model: str = "deepseek/deepseek-v4-pro"
    openrouter_image_model: str = ""
    openrouter_video_model: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    # Video jobs complete by webhook (OpenRouter POSTs to callback_url, which
    # must be HTTPS) with a polling sweeper as safety net. "auto" = webhooks
    # when PUBLIC_BASE_URL is https and providers are live; "off" = poll only.
    video_webhooks: str = "auto"
    # Optional: the signing secret from OpenRouter workspace settings. When
    # set, webhook deliveries must carry a valid X-OpenRouter-Signature.
    openrouter_webhook_secret: str = ""
    video_poll_seconds: int = 10  # poll cadence when webhooks are off
    video_safety_poll_seconds: int = 120  # check-in on webhook jobs that went quiet
    video_job_timeout_minutes: int = 45

    # Per-call clip cap assumed when a model's limits can't be discovered
    # (OpenRouter /videos/models unreachable, or a model it doesn't list).
    # Ambient loops longer than this are built from chained segments.
    ambient_default_max_clip_seconds: int = 10
    # Image model for ambient scene stills (the anchor frame, a generated
    # scene picture, Still + drift, preset previews), and the model to retry
    # with if that call fails. A blind panel ranked GPT Image 2.6 Sunburst at
    # quality "max" first for realism (Nov 2026), with Nano Banana 2.1 at 2K
    # a close, cheaper second. Empty = the active image model (Settings →
    # Models). A scene's own "Picture model" choice wins over both.
    ambient_still_model: str = "openai/gpt-image-2.6-sunburst"
    ambient_still_fallback_model: str = "google/gemini-nano-banana-2.1"

    # Google Gemini API (Lyria music). Optional; enables google/lyria-* music
    # models in the catalog.
    gemini_api_key: str = ""
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    gemini_api_revision: str = "2026-05-20"

    # Model catalog: how often the background poller refreshes the list of
    # available models from OpenRouter (and Gemini). 0 disables polling.
    model_catalog_refresh_hours: float = 6.0

    # YouTube publishing (Google OAuth "Web application" client). The
    # redirect URI registered in Google Cloud must be
    # {PUBLIC_BASE_URL}/api/youtube/callback unless overridden here.
    youtube_client_id: str = ""
    youtube_client_secret: str = ""
    youtube_redirect_uri: str = ""

    # elevenlabs
    elevenlabs_api_key: str = ""
    elevenlabs_default_voice_id: str = ""
    # eleven_turbo_v2_5 is deprecated (ElevenLabs: use Flash instead).
    # eleven_multilingual_v2 is their quality pick for the TTS endpoint;
    # eleven_flash_v2_5 is the low-latency one. Eleven v4 is reachable via
    # OpenRouter (elevenlabs/eleven-v4) under Settings → Models → Voice.
    elevenlabs_model_id: str = "eleven_multilingual_v2"
    # music_v2_5 = current Eleven Music model (music_v1, the API default, is
    # deprecated and removed 2027-01-31).
    elevenlabs_music_model_id: str = "music_v2_5"
    # Sound effects (ambient nature-sound beds). v2 is deprecated (removed
    # 2027-03-01), so the model is always sent explicitly.
    elevenlabs_sound_model_id: str = "eleven_text_to_sound_v3"
    elevenlabs_base_url: str = "https://api.elevenlabs.io/v1"

    # S3-backed object storage (optional, for production deploys). When
    # s3_bucket is set, uploaded asset files are mirrored to S3 and
    # `public_url_for_token()` returns a time-limited presigned URL instead
    # of a route through this app — useful for offloading bandwidth.
    # Reads from disk still work; S3 is the public delivery channel.
    # Empty bucket = local-fs only (the MVP default).
    s3_bucket: str = ""
    s3_region: str = "us-east-1"
    s3_prefix: str = "assets"
    s3_presign_ttl_seconds: int = 3600
    # boto3 picks credentials from the standard chain (env, ~/.aws,
    # instance profile, ECS task role). No need to surface them here.

    # custom model training (LoRA + voice clone). Replicate handles the
    # image/video LoRA path; voice clones piggyback on the existing
    # ELEVENLABS_API_KEY. Leave Replicate empty to keep that surface
    # mocked even with MOCK_PROVIDERS=false.
    replicate_api_token: str = ""
    replicate_base_url: str = "https://api.replicate.com/v1"
    # Default LoRA trainer (Replicate "owner/model:version"; trainings need
    # an explicit version). FLUX.1-dev LoRA trainer, pinned to its latest
    # version as of Nov 2026.
    replicate_lora_trainer: str = "ostris/flux-dev-lora-trainer:26dce37af90b9d997eeb970d92e47de3064d46c300504ae376c75bef6a9022d2"

    # Sogni (sogni.ai) image + video models, ids "sogni/<selector>". Get a
    # key at https://dashboard.sogni.ai/api-keys. Optional; a key saved in
    # Settings → API keys overrides this one.
    sogni_api_key: str = ""
    sogni_base_url: str = "https://api.sogni.ai"
    # Sogni's published tool schemas, which list the model selectors its
    # workflow API accepts (no API endpoint lists them). Synced with the model
    # catalog; empty = use the copy bundled with the app.
    sogni_tools_manifest_url: str = (
        "https://cdn.jsdelivr.net/npm/@sogni-ai/sogni-protocol@alpha/manifests/generation-tools.json")
    # Sogni's own content filter (safe_content_filter on every workflow).
    # Off: the app has no NSFW gate. Vendor models (GPT Image, standard
    # Seedance, HappyHorse, standard Wan 3) refuse mature prompts anyway.
    sogni_safe_content_filter: bool = False
    # Native Sogni models run on Free Spark: by default only vendor (Premium
    # Spark) models count toward Spend and the monthly cap. True counts the
    # free ones at their list price too.
    sogni_count_free_spark: bool = False
    # Image calls start a workflow and poll it (2 s, backing off to 5 s)
    # until it finishes or this many seconds pass.
    sogni_image_timeout_seconds: float = 300.0
    # img2img strength when a picture is passed as the source image.
    sogni_image_strength: float = 0.35
    # Reference pictures on a Sogni picture model go to Sogni's edit_image
    # tool (its generate_image ignores them). The editor used when the
    # picked model has no edit version of its own: for a place or a guide
    # picture, and for keeping a character's likeness. False: text only.
    sogni_edit_references: bool = True
    sogni_scene_edit_model: str = "flux-3-klein"
    sogni_identity_edit_model: str = "krea-identity-edit"
    # Day to night's "same place" stills on a Sogni Picture model: by default
    # the fallback picture model (Nano Banana, the most realistic relight in
    # a blind test, about $0.055) goes first and a Sogni edit (most faithful,
    # $0 on Free Spark) second. True tries the Sogni edit first.
    sogni_same_place_first: bool = False
    # A 429 on a workflow start is retried after Retry-After up to this
    # many seconds; a longer wait fails the call with the wait in the message.
    sogni_max_retry_after_seconds: float = 30.0

    # Clip check (services/inspector.py): a vision-capable chat model on
    # OpenRouter looks at 4 frames of each new storyboard clip. Settings →
    # Clip check overrides it at runtime; when this id isn't in the polled
    # catalog, a current vision model from the catalog is used instead.
    inspector_model: str = "google/gemini-2.5-flash"
    # Rough cost of one check when the catalog has no price for the model.
    cost_clip_check: float = 0.004

    # cost estimate rates (USD). Rough placeholders — override per your providers' pricing.
    cost_chat_per_plan: float = 0.01
    # Runware (runware.ai) image + video models, ids "runware/<AIR>" (e.g.
    # runware/runware:107@1). Get a key at https://my.runware.ai/keys.
    # Optional; a key saved in Settings → API keys wins over this one.
    runware_api_key: str = ""
    runware_base_url: str = "https://api.runware.ai/v1"
    # Public model docs (no key): the index of models and each one's schema.
    runware_models_index_url: str = "https://runware.ai/docs/models/index.json"
    runware_docs_models_base: str = "https://runware.ai/docs/models"
    # Runware's own content check. Off = uncensored output on the open
    # models Runware hosts (vendor models stay moderated by their vendor).
    # Always on for requests carrying a Cast member's picture; a request
    # whose prompt involves minors is refused before it's sent (safety.py).
    runware_check_content: bool = False
    runware_image_timeout_seconds: float = 300.0
    # Long side of a generated still (16:9 → 1920×1080, or 1920×1088 on
    # models that need multiples of 64; the app crops to the aspect ratio).
    runware_image_long_side: int = 1920
    # How much an image-to-image source may change (0 = keep, 1 = replace).
    runware_image_strength: float = 0.4
    # Video output: "1080p" or "720p" (about half the price per second).
    runware_video_resolution: str = "1080p"
    runware_max_retry_after_seconds: float = 30.0

    cost_image_per_item: float = 0.04
    # Ambient stills at their best settings cost more than a default image:
    # GPT Image 2.6 Sunburst at quality "max" bills ~$0.13 (Nov 2026).
    cost_ambient_still: float = 0.13
    # Live average: Wan ~$1.05 per 7 s clip ≈ $0.15/s (first EC2 deploy).
    cost_video_per_second: float = 0.15
    # Lip-sync (talking-head) clips, billed on the speech they're given.
    # Live: HeyGen Avatar IV ~$0.23 for a 4.6 s clip ≈ $0.05/s.
    cost_avatar_per_second: float = 0.05
    cost_tts_per_1k_chars: float = 0.20
    cost_music_per_generation: float = 0.05
    # One 30 s looping sound effect (ElevenLabs bills ~40 credits/s).
    cost_sound_per_generation: float = 0.10
    # Spend ledger defaults (Settings → Spend overrides them at runtime).
    # 0 = no monthly cap. hard_stop: refuse new paid generations (HTTP 402)
    # once the month's spend reaches the cap; otherwise only alert.
    spend_monthly_budget_usd: float = 0.0
    spend_hard_stop: bool = True

    @property
    def data_path(self) -> Path:
        p = Path(self.data_dir)
        p.mkdir(parents=True, exist_ok=True)
        (p / "uploads").mkdir(parents=True, exist_ok=True)
        (p / "renders").mkdir(parents=True, exist_ok=True)
        (p / "studio").mkdir(parents=True, exist_ok=True)
        return p

    # Key presence goes through services.provider_keys (a key saved in
    # Settings → API keys wins over .env). Imported lazily: config must not
    # pull in the DB layer at import time.
    def _has_key(self, name: str) -> bool:
        from .services import provider_keys

        return provider_keys.has(name)

    def chat_is_mocked(self) -> bool:
        return self.mock_providers or not self._has_key("openrouter")

    def image_is_mocked(self) -> bool:
        return self.mock_providers or not self._has_key("openrouter")

    def video_is_mocked(self) -> bool:
        return self.mock_providers or not self._has_key("openrouter")

    def tts_is_mocked(self) -> bool:
        return self.mock_providers or not self._has_key("elevenlabs")

    def music_is_mocked(self) -> bool:
        return self.mock_providers or not self._has_key("elevenlabs")


@lru_cache
def get_settings() -> Settings:
    return Settings()
