import pytest
from pydantic import ValidationError

from ledger_api.config import (
    PRODUCTION_MIN_ARGON2_MEMORY_KIB,
    PRODUCTION_MIN_ARGON2_TIME_COST,
    Settings,
)
from ledger_api.domain.passwords import DEFAULT_PARAMETERS, Argon2Parameters
from ledger_api.main import create_app

VALID_DATABASE_URL = "postgresql+asyncpg://ledger:synthetic@localhost:5432/ledger"


@pytest.fixture(autouse=True)
def database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", VALID_DATABASE_URL)


def test_settings_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "test")

    settings = Settings()

    assert settings.app_env == "test"
    assert settings.database_url.get_secret_value() == VALID_DATABASE_URL


def test_settings_reject_unknown_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "prod-ish")

    with pytest.raises(ValidationError):
        Settings()


def test_settings_require_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL")

    with pytest.raises(ValidationError, match="database_url"):
        Settings()


def test_settings_reject_database_url_without_asyncpg_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://ledger:synthetic@localhost:5432/ledger")

    with pytest.raises(ValidationError, match=r"postgresql\+asyncpg://"):
        Settings()


def test_rejected_database_url_is_not_echoed_in_the_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://ledger:do-not-log-me@localhost/ledger")

    with pytest.raises(ValidationError) as excinfo:
        Settings()

    assert "do-not-log-me" not in str(excinfo.value)


def test_database_url_is_masked_in_repr() -> None:
    assert "synthetic" not in repr(Settings())


def test_app_refuses_to_start_with_invalid_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "prod-ish")

    with pytest.raises(ValidationError):
        create_app()


# --- Argon2 parameters (ADR 0013) -----------------------------------------------------------------


def test_argon2_parameters_default_to_rfc_9106_low_memory() -> None:
    assert Settings().argon2_parameters() == DEFAULT_PARAMETERS


def test_argon2_parameters_are_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("ARGON2_TIME_COST", "1")
    monkeypatch.setenv("ARGON2_MEMORY_COST_KIB", "8")
    monkeypatch.setenv("ARGON2_PARALLELISM", "1")

    assert Settings().argon2_parameters() == Argon2Parameters(
        time_cost=1, memory_cost=8, parallelism=1
    )


@pytest.mark.parametrize(
    ("memory_kib", "time_cost"),
    [
        (PRODUCTION_MIN_ARGON2_MEMORY_KIB - 1, PRODUCTION_MIN_ARGON2_TIME_COST),
        (PRODUCTION_MIN_ARGON2_MEMORY_KIB, PRODUCTION_MIN_ARGON2_TIME_COST - 1),
        (8, 1),
    ],
)
def test_production_refuses_weak_argon2_parameters(
    monkeypatch: pytest.MonkeyPatch, memory_kib: int, time_cost: int
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ARGON2_MEMORY_COST_KIB", str(memory_kib))
    monkeypatch.setenv("ARGON2_TIME_COST", str(time_cost))
    monkeypatch.setenv("ARGON2_PARALLELISM", "1")

    with pytest.raises(ValidationError, match="production minimum"):
        Settings()


def test_production_accepts_the_minimum_and_the_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    assert Settings().argon2_parameters() == DEFAULT_PARAMETERS

    monkeypatch.setenv("ARGON2_MEMORY_COST_KIB", str(PRODUCTION_MIN_ARGON2_MEMORY_KIB))
    monkeypatch.setenv("ARGON2_TIME_COST", str(PRODUCTION_MIN_ARGON2_TIME_COST))
    monkeypatch.setenv("ARGON2_PARALLELISM", "1")
    assert Settings().argon2_memory_cost_kib == PRODUCTION_MIN_ARGON2_MEMORY_KIB


@pytest.mark.parametrize(
    ("variable", "value"),
    [("ARGON2_TIME_COST", "0"), ("ARGON2_MEMORY_COST_KIB", "7"), ("ARGON2_PARALLELISM", "0")],
)
def test_argon2_parameters_below_argon2s_own_minimums_are_rejected(
    monkeypatch: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv(variable, value)

    with pytest.raises(ValidationError):
        Settings()


def test_argon2_memory_must_cover_every_lane(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("ARGON2_MEMORY_COST_KIB", "15")
    monkeypatch.setenv("ARGON2_PARALLELISM", "2")  # needs at least 16 KiB

    with pytest.raises(ValidationError, match="8 x ARGON2_PARALLELISM"):
        Settings()


# --- logging (ADR 0017) ---------------------------------------------------------------------------


def test_log_level_defaults_to_info_and_ignores_case(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings().log_level == "INFO"

    monkeypatch.setenv("LOG_LEVEL", "debug")

    assert Settings().log_level == "DEBUG"


def test_an_unknown_log_level_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "LOUD")

    with pytest.raises(ValidationError):
        Settings()
