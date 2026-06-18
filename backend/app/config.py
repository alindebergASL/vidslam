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

    # rate limiting (budget-burning generation endpoints only)
    rate_limit_enabled: bool = True
    # Bucket capacity: how many generation calls a client can burst.
    rate_limit_generation_burst: int = 10
    # Steady-state refill: tokens added back per minute.
    rate_limit_generation_per_minute: float = 10.0

    # uploads
    max_upload_bytes: int = 15 * 1024 * 1024
    allowed_image_mimes: tuple[str, ...] = (
        "image/png",
        "image/jpeg",
        "image/webp",
    )

    # openrouter
    openrouter_api_key: str = ""
    openrouter_site_url: str = "http://localhost:3000"
    openrouter_app_title: str = "AvatarVideoStudio"
    openrouter_chat_model: str = "openai/gpt-4o-mini"
    openrouter_image_model: str = ""
    openrouter_video_model: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    # elevenlabs
    elevenlabs_api_key: str = ""
    elevenlabs_default_voice_id: str = ""
    elevenlabs_model_id: str = "eleven_turbo_v2_5"
    elevenlabs_music_model_id: str = ""
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

    # cost estimate rates (USD). Rough placeholders — override per your providers' pricing.
    cost_chat_per_plan: float = 0.01
    cost_image_per_item: float = 0.04
    cost_video_per_second: float = 0.10
    cost_tts_per_1k_chars: float = 0.20
    cost_music_per_generation: float = 0.05

    @property
    def data_path(self) -> Path:
        p = Path(self.data_dir)
        p.mkdir(parents=True, exist_ok=True)
        (p / "uploads").mkdir(parents=True, exist_ok=True)
        (p / "renders").mkdir(parents=True, exist_ok=True)
        (p / "studio").mkdir(parents=True, exist_ok=True)
        return p

    def chat_is_mocked(self) -> bool:
        return self.mock_providers or not self.openrouter_api_key

    def image_is_mocked(self) -> bool:
        return self.mock_providers or not self.openrouter_api_key

    def video_is_mocked(self) -> bool:
        return self.mock_providers or not self.openrouter_api_key

    def tts_is_mocked(self) -> bool:
        return self.mock_providers or not self.elevenlabs_api_key

    def music_is_mocked(self) -> bool:
        return self.mock_providers or not self.elevenlabs_api_key


@lru_cache
def get_settings() -> Settings:
    return Settings()
