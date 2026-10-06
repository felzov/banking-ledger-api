import pytest
from sqlalchemy import make_url

from tests.database import assert_disposable, disposable_database_url


@pytest.mark.parametrize("database", ["ledger", "postgres", "ledger_test_backup", "test"])
def test_guard_refuses_to_drop_non_test_databases(database: str) -> None:
    with pytest.raises(RuntimeError, match="refusing to drop"):
        assert_disposable(database)


def test_guard_allows_test_databases() -> None:
    assert_disposable("ledger_test")


def test_derived_database_urls_are_always_disposable() -> None:
    server_url = make_url("postgresql+asyncpg://ledger:synthetic@localhost:5432/ledger")

    for name in ("", "_migrations"):
        derived = disposable_database_url(server_url, name).database
        assert derived is not None
        assert_disposable(derived)
