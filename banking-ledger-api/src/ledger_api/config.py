from typing import Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ledger_api.domain.passwords import DEFAULT_PARAMETERS, Argon2Parameters

ASYNCPG_URL_PREFIX = "postgresql+asyncpg://"

# OWASP's weakest acceptable Argon2id configuration (19 MiB, 2 passes): production refuses
# anything cheaper. Development and test may go lower, e.g. to keep the test suite fast.
PRODUCTION_MIN_ARGON2_MEMORY_KIB = 19_456
PRODUCTION_MIN_ARGON2_TIME_COST = 2


class Settings(BaseSettings):
    """Application configuration, read from environment variables only."""

    # hide_input_in_errors: a rejected DATABASE_URL must not echo its password into startup logs.
    model_config = SettingsConfigDict(frozen=True, hide_input_in_errors=True)

    app_env: Literal["development", "test", "production"] = "development"
    # Secret because the URL embeds the database password; it must never appear in logs.
    database_url: SecretStr

    # Argon2id password hashing cost (ADR 0013). Defaults: RFC 9106's low-memory profile.
    argon2_time_cost: int = Field(DEFAULT_PARAMETERS.time_cost, ge=1)
    argon2_memory_cost_kib: int = Field(DEFAULT_PARAMETERS.memory_cost, ge=8)
    argon2_parallelism: int = Field(DEFAULT_PARAMETERS.parallelism, ge=1)

    # Absolute lifetime of a login session (ADR 0013): 30 minutes, no refresh. Bounded by the
    # database's 24-hour cap (ck_auth_sessions_expiry_window).
    auth_session_ttl_seconds: int = Field(1_800, ge=60, le=86_400)

    @field_validator("database_url")
    @classmethod
    def _require_asyncpg_driver(cls, value: SecretStr) -> SecretStr:
        # A plain postgresql:// URL makes SQLAlchemy pick psycopg2, which is not installed.
        if not value.get_secret_value().startswith(ASYNCPG_URL_PREFIX):
            raise ValueError(f"DATABASE_URL must start with {ASYNCPG_URL_PREFIX}")
        return value

    @model_validator(mode="after")
    def _check_argon2_parameters(self) -> Self:
        # Argon2 needs at least 8 KiB per lane; refused here rather than at the first login.
        if self.argon2_memory_cost_kib < 8 * self.argon2_parallelism:
            raise ValueError("ARGON2_MEMORY_COST_KIB must be at least 8 x ARGON2_PARALLELISM")
        if self.app_env == "production" and (
            self.argon2_memory_cost_kib < PRODUCTION_MIN_ARGON2_MEMORY_KIB
            or self.argon2_time_cost < PRODUCTION_MIN_ARGON2_TIME_COST
        ):
            raise ValueError(
                "Argon2 parameters are below the production minimum: "
                f"{PRODUCTION_MIN_ARGON2_MEMORY_KIB} KiB and "
                f"{PRODUCTION_MIN_ARGON2_TIME_COST} passes"
            )
        return self

    def argon2_parameters(self) -> Argon2Parameters:
        return Argon2Parameters(
            time_cost=self.argon2_time_cost,
            memory_cost=self.argon2_memory_cost_kib,
            parallelism=self.argon2_parallelism,
        )
