import pytest
from pydantic import ValidationError

from ledger_api.config import Settings
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
