"""Architecture guard: account routes must stay reachable only through their owner (ADR 0009).

A flat route such as GET /accounts/{account_id} could not scope the lookup to an owner, and
Phase 8's authorization (one dependency on the /users/{user_id} router) would not cover it.

Paths come from the OpenAPI schema, the app's public contract: since FastAPI 0.142,
app.routes holds included routers as private wrappers, not their routes. A route declared
with include_in_schema=False would escape this check; there are none.
"""

from ledger_api.main import USER_SCOPE, create_app
from tests.conftest import UNREACHABLE_DATABASE_URL, settings_for


def _api_paths() -> list[str]:
    app = create_app(settings_for(UNREACHABLE_DATABASE_URL))
    paths = list(app.openapi()["paths"])
    assert "/health/live" in paths  # the enumeration itself works
    return paths


def test_every_account_route_is_mounted_under_its_owner() -> None:
    account_paths = [path for path in _api_paths() if "account" in path]

    assert account_paths  # guards against the check passing vacuously
    for path in account_paths:
        assert path.startswith(f"{USER_SCOPE}/"), path


def test_every_route_naming_an_account_also_names_its_user() -> None:
    for path in _api_paths():
        if "{account_id}" in path:
            assert "{user_id}" in path, path


def test_every_user_scoped_route_is_under_the_single_user_scope() -> None:
    # Phase 8 enforces "caller is {user_id}" once, on the router mounted at USER_SCOPE.
    for path in _api_paths():
        if "{user_id}" in path:
            assert path.startswith(USER_SCOPE), path
