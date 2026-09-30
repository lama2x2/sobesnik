from typing import Any

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from sobesnik_api.app import create_app
from sobesnik_api.settings import Settings
from sobesnik_llm import FakeProvider


async def test_health(client: AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": "ok"}


async def test_health_llm_ok(client: AsyncClient) -> None:
    response = await client.get("/health/llm")
    assert response.status_code == 200
    role = {"ok": True, "provider": "fake", "model": "fake-model", "detail": None}
    assert response.json() == {"status": "ok", "roles": {"gen": role, "eval": role}}


async def test_health_llm_unavailable(client: AsyncClient, llm: FakeProvider) -> None:
    llm.available = False
    response = await client.get("/health/llm")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["roles"]["gen"]["detail"]


async def health_with_eval(database_url: str, gen: FakeProvider, eval_: FakeProvider) -> Any:
    app: FastAPI = create_app(Settings(database_url=database_url), llm=gen, llm_eval=eval_)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test/api/v1") as client:
            return await client.get("/health/llm")


async def test_health_llm_separate_eval(database_url: str) -> None:
    gen, eval_ = FakeProvider(model="gen-model"), FakeProvider(model="eval-model")
    response = await health_with_eval(database_url, gen, eval_)
    assert response.status_code == 200
    roles = response.json()["roles"]
    assert (roles["gen"]["model"], roles["eval"]["model"]) == ("gen-model", "eval-model")


async def test_health_llm_eval_down(database_url: str) -> None:
    gen, eval_ = FakeProvider(model="gen-model"), FakeProvider(model="eval-model")
    eval_.available = False
    response = await health_with_eval(database_url, gen, eval_)
    assert response.status_code == 503
    roles = response.json()["roles"]
    assert roles["gen"]["ok"] is True
    assert roles["eval"]["ok"] is False
