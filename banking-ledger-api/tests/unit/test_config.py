import pytest
from pydantic import ValidationError

from ledger_api.config import Settings
from ledger_api.main import create_app


def test_settings_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "test")

    assert Settings().app_env == "test"


def test_settings_reject_unknown_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "prod-ish")

    with pytest.raises(ValidationError):
        Settings()


def test_app_refuses_to_start_with_invalid_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "prod-ish")

    with pytest.raises(ValidationError):
        create_app()
