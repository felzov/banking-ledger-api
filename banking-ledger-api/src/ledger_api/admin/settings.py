"""Configuration of the role bootstrap (ADR 0015). Never the application's configuration.

Read from environment variables by the bootstrap command only. The admin URL and the role
passwords are deliberately not fields of ledger_api.config.Settings, so the API process never
loads them, even when they happen to be set in its environment.
"""

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ledger_api.admin.scram import check_role_password
from ledger_api.config import ASYNCPG_URL_PREFIX


class BootstrapSettings(BaseSettings):
    # hide_input_in_errors: a rejected value (a URL with its password, a role password) is never
    # echoed into the command's output.
    model_config = SettingsConfigDict(frozen=True, hide_input_in_errors=True)

    # A superuser's connection to the application database: the database to bootstrap.
    admin_database_url: SecretStr
    ledger_owner_password: SecretStr
    ledger_app_password: SecretStr

    @field_validator("admin_database_url")
    @classmethod
    def _require_asyncpg_driver(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith(ASYNCPG_URL_PREFIX):
            raise ValueError(f"ADMIN_DATABASE_URL must start with {ASYNCPG_URL_PREFIX}")
        return value

    @field_validator("ledger_owner_password", "ledger_app_password")
    @classmethod
    def _check_role_password(cls, value: SecretStr) -> SecretStr:
        check_role_password(value.get_secret_value())  # its message never quotes the value
        return value
