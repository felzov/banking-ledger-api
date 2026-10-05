from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration, read from environment variables only."""

    model_config = SettingsConfigDict(frozen=True)

    app_env: Literal["development", "test", "production"] = "development"
