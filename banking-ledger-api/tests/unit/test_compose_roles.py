"""Which service gets which database credential, as compose.yaml declares it (ADR 0015).

The static half of the check; the CI smoke test (scripts/compose-smoke.sh) inspects the running
containers' environments too. Anchors and merge keys are resolved by the YAML loader, so this
sees what each service really receives.
"""

from pathlib import Path
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]  # PyYAML ships no type information

COMPOSE = Path(__file__).resolve().parents[2] / "compose.yaml"
ADMIN_REFERENCES = ("${POSTGRES_PASSWORD", "ADMIN_DATABASE_URL")
OWNER_REFERENCE = "${LEDGER_OWNER_PASSWORD"
APP_REFERENCE = "${LEDGER_APP_PASSWORD"


@pytest.fixture(scope="module")
def services() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(COMPOSE.read_text())
    services: dict[str, Any] = loaded["services"]
    return services


def _environment(service: dict[str, Any]) -> dict[str, str]:
    environment: dict[str, str] = service.get("environment", {})
    return environment


def _text(environment: dict[str, str]) -> str:
    return "\n".join(f"{key}={value}" for key, value in environment.items())


def test_the_api_gets_only_the_app_role(services: dict[str, Any]) -> None:
    environment = _environment(services["api"])
    assert set(environment) == {"DATABASE_URL", "APP_ENV", "PYTHONPATH"}
    assert environment["DATABASE_URL"].startswith("postgresql+asyncpg://ledger_app:")
    everything = _text(environment)
    for reference in (*ADMIN_REFERENCES, OWNER_REFERENCE):
        assert reference not in everything


def test_migrations_get_only_the_owner_role(services: dict[str, Any]) -> None:
    environment = _environment(services["migrate"])
    assert set(environment) == {"DATABASE_URL", "APP_ENV"}
    assert environment["DATABASE_URL"].startswith("postgresql+asyncpg://ledger_owner:")
    everything = _text(environment)
    for reference in (*ADMIN_REFERENCES, APP_REFERENCE):
        assert reference not in everything


def test_only_the_bootstrap_gets_the_admin(services: dict[str, Any]) -> None:
    environment = _environment(services["bootstrap"])
    assert set(environment) == {
        "ADMIN_DATABASE_URL",
        "LEDGER_OWNER_PASSWORD",
        "LEDGER_APP_PASSWORD",
    }
    assert services["bootstrap"]["command"] == ["python", "-m", "ledger_api.admin.bootstrap"]
    holders = {
        name for name, service in services.items() if "ADMIN_DATABASE_URL" in _environment(service)
    }
    assert holders == {"bootstrap"}


def test_start_order_is_db_bootstrap_migrate_api(services: dict[str, Any]) -> None:
    assert services["bootstrap"]["depends_on"] == {"db": {"condition": "service_healthy"}}
    assert services["migrate"]["depends_on"]["bootstrap"] == {
        "condition": "service_completed_successfully"
    }
    assert services["api"]["depends_on"]["migrate"] == {
        "condition": "service_completed_successfully"
    }
    for one_shot in ("bootstrap", "migrate"):
        assert services[one_shot]["restart"] == "no"


def test_missing_role_passwords_fail_with_a_clear_message(services: dict[str, Any]) -> None:
    # ${VAR:?message}: compose refuses to start without them, naming the variable.
    for service, variable in (("migrate", "LEDGER_OWNER_PASSWORD"), ("api", "LEDGER_APP_PASSWORD")):
        assert (
            f"${{{variable}:?set {variable} in .env}}"
            in _environment(services[service])["DATABASE_URL"]
        )
