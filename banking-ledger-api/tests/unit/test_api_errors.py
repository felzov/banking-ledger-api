import pytest

from ledger_api.api.errors import status_for
from ledger_api.domain.errors import (
    DomainError,
    InsufficientFundsError,
    StatementInconsistencyError,
    TemporarilyUnavailableError,
)
from tests.unit.test_domain_errors import CONCRETE_ERRORS


@pytest.mark.parametrize("error_type", CONCRETE_ERRORS, ids=lambda cls: cls.__name__)
def test_every_domain_error_maps_to_an_error_status(error_type: type[DomainError]) -> None:
    assert status_for(error_type()) in {404, 409, 422, 500, 503}


def test_unmapped_domain_error_category_fails_loudly() -> None:
    class UnmappedError(DomainError):
        code = "unmapped"
        message = "Unmapped."

    with pytest.raises(LookupError):
        status_for(UnmappedError())


def test_financial_errors_map_to_the_approved_statuses() -> None:
    assert status_for(InsufficientFundsError()) == 422
    assert status_for(TemporarilyUnavailableError()) == 503
    # A detected inconsistency looks exactly like any other internal error to the client.
    assert status_for(StatementInconsistencyError()) == 500
    assert StatementInconsistencyError.code == "internal_error"
