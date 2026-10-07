from httpx import AsyncClient


async def test_liveness_reports_ok(client: AsyncClient) -> None:
    response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_liveness_does_not_depend_on_the_database(
    client_without_database: AsyncClient,
) -> None:
    # A database outage must not make the orchestrator restart healthy API processes.
    response = await client_without_database.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readiness_reports_ok_when_the_database_is_reachable(client: AsyncClient) -> None:
    response = await client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readiness_reports_503_when_the_database_is_unreachable(
    client_without_database: AsyncClient,
) -> None:
    response = await client_without_database.get("/health/ready")

    assert response.status_code == 503
    # Exact body: no driver error, host, port or other infrastructure detail leaks out.
    assert response.json() == {"status": "unavailable"}
