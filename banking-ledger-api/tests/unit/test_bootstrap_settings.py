"""Configuration of the role bootstrap: its settings, role names and command-line failures.

None of these tests reach a database: the command's database behaviour is covered by
tests/integration/test_role_bootstrap.py, through bootstrap() with unique role names.
"""

import subprocess
import sys

import pytest
from pydantic import ValidationError

from ledger_api.admin import bootstrap as bootstrap_module
from ledger_api.admin.bootstrap import DEFAULT_ROLE_NAMES, RoleNames
from ledger_api.admin.settings import BootstrapSettings
from ledger_api.config import Settings

ADMIN_URL = "postgresql+asyncpg://admin:synthetic-admin-secret@127.0.0.1:1/ledger"
OWNER_PASSWORD = "synthetic-owner-password-01"  # noqa: S105 (synthetic test value)
APP_PASSWORD = "synthetic-app-password-0001"  # noqa: S105 (synthetic test value)


@pytest.fixture(autouse=True)
def environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_DATABASE_URL", ADMIN_URL)
    monkeypatch.setenv("LEDGER_OWNER_PASSWORD", OWNER_PASSWORD)
    monkeypatch.setenv("LEDGER_APP_PASSWORD", APP_PASSWORD)


def test_settings_read_the_environment() -> None:
    settings = BootstrapSettings()

    assert settings.admin_database_url.get_secret_value() == ADMIN_URL
    assert settings.ledger_owner_password.get_secret_value() == OWNER_PASSWORD
    assert settings.ledger_app_password.get_secret_value() == APP_PASSWORD


def test_secrets_are_masked_in_repr() -> None:
    shown = repr(BootstrapSettings())

    for secret in ("synthetic-admin-secret", OWNER_PASSWORD, APP_PASSWORD):
        assert secret not in shown


@pytest.mark.parametrize(
    "variable", ["ADMIN_DATABASE_URL", "LEDGER_OWNER_PASSWORD", "LEDGER_APP_PASSWORD"]
)
def test_every_setting_is_required(monkeypatch: pytest.MonkeyPatch, variable: str) -> None:
    monkeypatch.delenv(variable)

    with pytest.raises(ValidationError, match=variable.lower()):
        BootstrapSettings()


def test_admin_url_needs_the_asyncpg_driver_and_is_not_echoed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMIN_DATABASE_URL", "postgresql://admin:do-not-log-me@localhost/ledger")

    with pytest.raises(ValidationError, match=r"postgresql\+asyncpg://") as excinfo:
        BootstrapSettings()

    assert "do-not-log-me" not in str(excinfo.value)


@pytest.mark.parametrize("variable", ["LEDGER_OWNER_PASSWORD", "LEDGER_APP_PASSWORD"])
def test_weak_role_passwords_are_refused_and_not_echoed(
    monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    monkeypatch.setenv(variable, "too-short-pw")

    with pytest.raises(ValidationError, match="16-256 visible ASCII") as excinfo:
        BootstrapSettings()

    assert "too-short-pw" not in str(excinfo.value)


def test_application_settings_never_read_the_admin_configuration() -> None:
    # The API's Settings has no field for any of them, so its process cannot load them.
    fields = set(Settings.model_fields)

    assert not fields & {"admin_database_url", "ledger_owner_password", "ledger_app_password"}


def test_the_application_never_imports_the_admin_package() -> None:
    # A fresh interpreter (-I: no current directory, no user site): whatever the import style,
    # building the application must not load ledger_api.admin.
    probe = (
        "import sys; from ledger_api.main import create_app; "
        "print(sorted(m for m in sys.modules if m.startswith('ledger_api.admin')))"
    )
    result = subprocess.run(  # noqa: S603 (fixed arguments, this interpreter)
        [sys.executable, "-I", "-c", probe], capture_output=True, text=True, check=True
    )

    assert result.stdout.strip() == "[]"


# --- role names ---------------------------------------------------------------------------------


def test_default_role_names() -> None:
    assert (DEFAULT_ROLE_NAMES.owner, DEFAULT_ROLE_NAMES.app, DEFAULT_ROLE_NAMES.runtime_group) == (
        "ledger_owner",
        "ledger_app",
        "ledger_runtime",
    )


@pytest.mark.parametrize(
    "name", ["", "Ledger_owner", "1owner", "owner-x", 'own"er', "pg_owner", "x" * 64]
)
def test_invalid_role_names_are_refused(name: str) -> None:
    with pytest.raises(ValueError, match="invalid role name"):
        RoleNames(owner=name, app="ledger_app", runtime_group="ledger_runtime")


def test_role_names_must_be_distinct() -> None:
    with pytest.raises(ValueError, match="distinct"):
        RoleNames(owner="ledger_x", app="ledger_x", runtime_group="ledger_runtime")


# --- the command's failures ---------------------------------------------------------------------


def test_command_reports_invalid_configuration_without_values(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("LEDGER_APP_PASSWORD", "weak-app-pw")

    assert bootstrap_module.main() == 2

    out, err = capsys.readouterr()
    assert out == ""
    assert "ledger_app_password" in err
    for secret in ("weak-app-pw", "synthetic-admin-secret", OWNER_PASSWORD):
        assert secret not in err


def test_command_reports_an_unreachable_database_without_the_url(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Port 1: nothing listens. Fails before any statement, so no role is touched.
    assert bootstrap_module.main() == 1

    out, err = capsys.readouterr()
    assert out == ""
    assert "bootstrap failed: database error" in err
    for secret in ("synthetic-admin-secret", "127.0.0.1", OWNER_PASSWORD, APP_PASSWORD):
        assert secret not in err
