from httpx import AsyncClient


async def test_liveness_reports_ok(client: AsyncClient) -> None:
    response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
