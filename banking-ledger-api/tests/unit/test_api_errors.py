import pytest

from ledger_api.api.errors import status_for
from ledger_api.domain.errors import DomainError
from tests.unit.test_domain_errors import CONCRETE_ERRORS


@pytest.mark.parametrize("error_type", CONCRETE_ERRORS, ids=lambda cls: cls.__name__)
def test_every_domain_error_maps_to_a_client_error_status(error_type: type[DomainError]) -> None:
    assert status_for(error_type()) in {404, 409}


def test_unmapped_domain_error_category_fails_loudly() -> None:
    class UnmappedError(DomainError):
        code = "unmapped"
        message = "Unmapped."

    with pytest.raises(LookupError):
        status_for(UnmappedError())
