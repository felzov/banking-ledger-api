from typing import Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ledger_api.domain.passwords import DEFAULT_PARAMETERS, Argon2Parameters
from ledger_api.domain.rate_limits import Limit, RateLimits

ASYNCPG_URL_PREFIX = "postgresql+asyncpg://"
REDIS_URL_PREFIXES = ("redis://", "rediss://")

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

    # Level of the JSON log lines on stderr (ADR 0017), for the application and its libraries.
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # Rate limits (ADR 0016), counted in Redis (ADR 0008). Unset: no limits, for development and
    # the test suite; production refuses to start without it. Secret: a URL may hold a password.
    redis_url: SecretStr | None = None
    # Prefix of every counter key: several deployments (or test runs) can share one Redis.
    rate_limit_namespace: str = Field("ledger", pattern=r"^[a-z0-9-]{1,64}$")
    # Per client address, every attempt.
    login_attempts_per_minute: int = Field(20, ge=1)
    # Per email address: attempts since its last successful login (in effect, failures), then a
    # throttle until the window ends.
    login_failures_per_email: int = Field(10, ge=1)
    login_failure_window_seconds: int = Field(900, ge=60, le=86_400)
    # Per client address.
    registrations_per_hour: int = Field(10, ge=1)
    # Per authenticated user, deposits, withdrawals and transfers together.
    postings_per_minute: int = Field(60, ge=1)

    @field_validator("log_level", mode="before")
    @classmethod
    def _log_level_in_capitals(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator("database_url")
    @classmethod
    def _require_asyncpg_driver(cls, value: SecretStr) -> SecretStr:
        # A plain postgresql:// URL makes SQLAlchemy pick psycopg2, which is not installed.
        if not value.get_secret_value().startswith(ASYNCPG_URL_PREFIX):
            raise ValueError(f"DATABASE_URL must start with {ASYNCPG_URL_PREFIX}")
        return value

    @field_validator("redis_url")
    @classmethod
    def _require_redis_scheme(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not value.get_secret_value().startswith(REDIS_URL_PREFIXES):
            raise ValueError(f"REDIS_URL must start with {' or '.join(REDIS_URL_PREFIXES)}")
        return value

    @model_validator(mode="after")
    def _require_rate_limits_in_production(self) -> Self:
        # Without Redis there are no rate limits: nothing stops password guessing (ADR 0016).
        if self.app_env == "production" and self.redis_url is None:
            raise ValueError("REDIS_URL is required in production: it holds the rate limits")
        return self

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

    def rate_limits(self) -> RateLimits:
        return RateLimits(
            login_address=Limit("login_address", self.login_attempts_per_minute, 60),
            login_failures=Limit(
                "login_failures", self.login_failures_per_email, self.login_failure_window_seconds
            ),
            registration_address=Limit("registration_address", self.registrations_per_hour, 3600),
            postings_user=Limit("postings_user", self.postings_per_minute, 60),
        )

    def argon2_parameters(self) -> Argon2Parameters:
        return Argon2Parameters(
            time_cost=self.argon2_time_cost,
            memory_cost=self.argon2_memory_cost_kib,
            parallelism=self.argon2_parallelism,
        )
