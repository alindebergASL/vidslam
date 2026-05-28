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
    elevenlabs_base_url: str = "https://api.elevenlabs.io/v1"

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
