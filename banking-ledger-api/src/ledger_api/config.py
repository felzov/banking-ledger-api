from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ASYNCPG_URL_PREFIX = "postgresql+asyncpg://"


class Settings(BaseSettings):
    """Application configuration, read from environment variables only."""

    # hide_input_in_errors: a rejected DATABASE_URL must not echo its password into startup logs.
    model_config = SettingsConfigDict(frozen=True, hide_input_in_errors=True)

    app_env: Literal["development", "test", "production"] = "development"
    # Secret because the URL embeds the database password; it must never appear in logs.
    database_url: SecretStr

    @field_validator("database_url")
    @classmethod
    def _require_asyncpg_driver(cls, value: SecretStr) -> SecretStr:
        # A plain postgresql:// URL makes SQLAlchemy pick psycopg2, which is not installed.
        if not value.get_secret_value().startswith(ASYNCPG_URL_PREFIX):
            raise ValueError(f"DATABASE_URL must start with {ASYNCPG_URL_PREFIX}")
        return value
