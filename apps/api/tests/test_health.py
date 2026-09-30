from httpx import AsyncClient

from sobesnik_llm import FakeProvider


async def test_health(client: AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": "ok"}


async def test_health_llm_ok(client: AsyncClient) -> None:
    response = await client.get("/health/llm")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "provider": "fake", "model": "fake-model"}


async def test_health_llm_unavailable(client: AsyncClient, llm: FakeProvider) -> None:
    llm.available = False
    response = await client.get("/health/llm")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["detail"]
