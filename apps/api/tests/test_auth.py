"""Вход в веб-кабинет и проверка владельца (009 §5, §9, §11)."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sobesnik_api.app import create_app
from sobesnik_api.db.models import AuthToken
from sobesnik_api.db.session import make_sessionmaker
from sobesnik_api.services.auth import normalize_code
from sobesnik_api.settings import Settings
from sobesnik_asr import MemoryStorage
from sobesnik_llm import FakeProvider

SERVICE = "service-secret"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def link_for(client: AsyncClient, user_id: str) -> dict[str, Any]:
    response = await client.post("/auth/links", json={"user_id": user_id})
    assert response.status_code == 201, response.text
    data: dict[str, Any] = response.json()
    return data


async def login(client: AsyncClient, user_id: str) -> dict[str, str]:
    """Заголовок с токеном пользователя — как у веба после входа по ссылке."""
    link = await link_for(client, user_id)
    response = await client.post("/auth/links/redeem", json={"secret": link["secret"]})
    assert response.status_code == 200, response.text
    return bearer(response.json()["token"])


async def test_link_by_secret_gives_same_user(client: AsyncClient, user: dict[str, Any]) -> None:
    link = await link_for(client, user["id"])
    assert len(link["code"]) == 9 and link["code"][4] == "-"

    response = await client.post("/auth/links/redeem", json={"secret": link["secret"]})
    assert response.status_code == 200
    token = response.json()["token"]
    assert token.startswith("sbu_")
    assert response.json()["user_id"] == user["id"]

    me = await client.get("/auth/me", headers=bearer(token))
    assert me.json() == {"user_id": user["id"], "telegram": True, "local": False}


async def test_link_by_code_any_spelling(client: AsyncClient, user: dict[str, Any]) -> None:
    link = await link_for(client, user["id"])
    typed = " " + link["code"].lower().replace("-", " ") + " "
    response = await client.post("/auth/links/redeem", json={"code": typed})
    assert response.status_code == 200
    assert response.json()["user_id"] == user["id"]


async def test_link_works_once(client: AsyncClient, user: dict[str, Any]) -> None:
    link = await link_for(client, user["id"])
    assert (await client.post("/auth/links/redeem", json={"secret": link["secret"]})).is_success
    again = await client.post("/auth/links/redeem", json={"code": link["code"]})
    assert again.status_code == 404
    assert again.json()["error"]["code"] == "link_invalid"


async def test_new_link_cancels_old(client: AsyncClient, user: dict[str, Any]) -> None:
    old = await link_for(client, user["id"])
    new = await link_for(client, user["id"])
    stale = await client.post("/auth/links/redeem", json={"secret": old["secret"]})
    assert stale.status_code == 404
    assert (await client.post("/auth/links/redeem", json={"secret": new["secret"]})).is_success


async def test_expired_link(client: AsyncClient, user: dict[str, Any], engine: AsyncEngine) -> None:
    link = await link_for(client, user["id"])
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE auth_token SET expires_at = now() - interval '1 second'"))
    response = await client.post("/auth/links/redeem", json={"secret": link["secret"]})
    assert response.status_code == 404


@pytest.mark.parametrize("body", [{"secret": "nope"}, {"code": "AAAA-AAAA"}, {"code": "123"}])
async def test_unknown_link(client: AsyncClient, body: dict[str, str]) -> None:
    response = await client.post("/auth/links/redeem", json=body)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "link_invalid"


async def test_redeem_needs_exactly_one_field(client: AsyncClient) -> None:
    assert (await client.post("/auth/links/redeem", json={})).status_code == 422
    both = {"secret": "a", "code": "b"}
    assert (await client.post("/auth/links/redeem", json=both)).status_code == 422


async def test_link_for_unknown_user(client: AsyncClient) -> None:
    response = await client.post(
        "/auth/links", json={"user_id": "00000000-0000-0000-0000-000000000000"}
    )
    assert response.status_code == 404


async def test_secrets_not_stored_in_clear(
    client: AsyncClient, user: dict[str, Any], engine: AsyncEngine
) -> None:
    link = await link_for(client, user["id"])
    token = (await client.post("/auth/links/redeem", json={"secret": link["secret"]})).json()
    async with engine.connect() as conn:
        rows = (await conn.execute(text("SELECT token_hash, code_hash FROM auth_token"))).all()
    stored = " ".join(str(v) for row in rows for v in row)
    for secret in (link["secret"], normalize_code(link["code"]), token["token"]):
        assert secret not in stored


async def test_local_login_is_one_user(client: AsyncClient) -> None:
    first = await client.post("/auth/local")
    second = await client.post("/auth/local")
    assert first.status_code == 200
    assert first.json()["user_id"] == second.json()["user_id"]
    assert first.json()["token"] != second.json()["token"]
    me = await client.get("/auth/me", headers=bearer(first.json()["token"]))
    assert me.json()["local"] is True and me.json()["telegram"] is False


async def test_logout_and_logout_everywhere(client: AsyncClient, user: dict[str, Any]) -> None:
    a = await login(client, user["id"])
    b = await login(client, user["id"])

    assert (await client.delete("/auth/session", headers=a)).status_code == 204
    gone = await client.get("/auth/me", headers=a)
    assert gone.status_code == 401
    assert gone.json()["error"]["code"] == "unauthorized"
    assert (await client.get("/auth/me", headers=b)).is_success

    c = await login(client, user["id"])
    assert (await client.delete("/auth/sessions", headers=c)).status_code == 204
    assert (await client.get("/auth/me", headers=b)).status_code == 401
    assert (await client.get("/auth/me", headers=c)).status_code == 401


async def test_expired_session_is_not_service(
    client: AsyncClient, user: dict[str, Any], engine: AsyncEngine
) -> None:
    """Даже без сервисного токена просроченный токен пользователя не превращается в «сервис»."""
    headers = await login(client, user["id"])
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE auth_token SET expires_at = now() - interval '1 second'"))
    response = await client.get(f"/users/{user['id']}/progress", headers=headers)
    assert response.status_code == 401
    unknown = await client.get(f"/users/{user['id']}/progress", headers=bearer("sbu_nope"))
    assert unknown.status_code == 401


async def test_session_extends_from_last_use(
    client: AsyncClient, user: dict[str, Any], engine: AsyncEngine
) -> None:
    headers = await login(client, user["id"])
    old = datetime.now(UTC) - timedelta(days=29)
    sessionmaker = make_sessionmaker(engine)
    async with sessionmaker() as db:
        await db.execute(
            update(AuthToken)
            .where(AuthToken.kind == "session")
            .values(last_used_at=old, expires_at=old + timedelta(days=30))
        )
        await db.commit()
    assert (await client.get("/auth/me", headers=headers)).is_success
    async with sessionmaker() as db:
        row = await db.scalar(select(AuthToken).where(AuthToken.kind == "session"))
        assert row is not None
        assert row.expires_at > datetime.now(UTC) + timedelta(days=29)


async def test_user_token_cannot_do_service_things(
    client: AsyncClient, user: dict[str, Any]
) -> None:
    headers = await login(client, user["id"])
    assert (await client.put("/users/telegram/5", headers=headers)).status_code == 403
    response = await client.post("/auth/links", json={"user_id": user["id"]}, headers=headers)
    assert response.status_code == 403
    assert (await client.post("/auth/local", headers=headers)).status_code == 403
    # а сервис — не пользователь
    assert (await client.get("/auth/me")).status_code == 403


async def test_own_data_with_user_token(
    client: AsyncClient,
    llm: FakeProvider,
    user: dict[str, Any],
    replies: Any,
    profile: dict[str, Any],
) -> None:
    headers = await login(client, user["id"])
    llm.replies = [replies.parse(profile)]
    created = await client.post("/vacancies", json={"text": replies.vacancy_text}, headers=headers)
    assert created.status_code == 201, created.text
    vacancy = created.json()

    listed = await client.get(f"/users/{user['id']}/vacancies", headers=headers)
    assert [v["id"] for v in listed.json()] == [vacancy["id"]]
    assert (await client.get(f"/vacancies/{vacancy['id']}", headers=headers)).is_success

    llm.replies = [replies.questions(3)]
    created = await client.post(
        "/sessions", json={"vacancy_id": vacancy["id"], "n": 3}, headers=headers
    )
    assert created.status_code == 201, created.text
    session = created.json()
    question = session["questions"][0]["id"]
    answered = await client.post(
        f"/sessions/{session['id']}/questions/{question}/answer",
        json={"text": "Индекс ускоряет поиск."},
        headers=headers,
    )
    assert answered.status_code == 202
    answer = answered.json()["answer_id"]
    assert (await client.get(f"/answers/{answer}", headers=headers)).is_success
    assert (await client.get(f"/users/{user['id']}/progress", headers=headers)).is_success


async def test_foreign_data_is_not_found(
    client: AsyncClient,
    session: dict[str, Any],
    vacancy: dict[str, Any],
    user: dict[str, Any],
    llm: FakeProvider,
    replies: Any,
) -> None:
    """Сессия и вакансия принадлежат `user`; чужой пользователь их не видит и не трогает."""
    question = session["questions"][0]["id"]
    answered = await client.post(
        f"/sessions/{session['id']}/questions/{question}/answer", json={"text": "Ответ."}
    )
    answer = answered.json()["answer_id"]
    stranger = (await client.put("/users/telegram/777")).json()
    headers = await login(client, stranger["id"])

    checks = [
        ("GET", f"/vacancies/{vacancy['id']}", None),
        ("PATCH", f"/vacancies/{vacancy['id']}", {"level": "senior"}),
        ("POST", "/sessions", {"vacancy_id": vacancy["id"], "n": 3}),
        ("GET", f"/sessions/{session['id']}", None),
        ("POST", f"/sessions/{session['id']}/finish", None),
        ("POST", f"/sessions/{session['id']}/questions/{question}/answer", {"text": "Чужой."}),
        ("GET", f"/answers/{answer}", None),
        ("GET", f"/users/{user['id']}/progress", None),
        ("GET", f"/users/{user['id']}/vacancies", None),
        ("DELETE", f"/users/{user['id']}/data?scope=all", None),
        ("POST", "/vacancies", {"user_id": user["id"], "text": replies.vacancy_text}),
        ("POST", "/sessions", {"mode": "repeat", "user_id": user["id"], "n": 3}),
    ]
    for method, path, body in checks:
        response = await client.request(method, path, json=body, headers=headers)
        assert response.status_code == 404, (method, path, response.text)

    files = {"file": ("a.ogg", b"x", "audio/ogg")}
    voice = await client.post(
        f"/sessions/{session['id']}/questions/{question}/answer/voice",
        files=files,
        headers=headers,
    )
    assert voice.status_code == 404
    assert llm.requests == []  # до модели дело не дошло
    # данные владельца на месте
    assert (await client.get(f"/vacancies/{vacancy['id']}")).json()["level"] == "middle"
    assert (await client.get(f"/answers/{answer}")).is_success


async def test_service_without_user_id(client: AsyncClient, replies: Any) -> None:
    response = await client.post("/vacancies", json={"text": replies.vacancy_text})
    assert response.status_code == 422
    repeat = await client.post("/sessions", json={"mode": "repeat", "n": 3})
    assert repeat.status_code == 422


@pytest.fixture
async def secured(
    database_url: str,
    engine: AsyncEngine,
    llm: FakeProvider,
    web: Any,
    queue: Any,
    storage: MemoryStorage,
) -> AsyncIterator[AsyncClient]:
    """API с сервисным токеном и выключенным локальным входом; клиент без токена."""
    settings = Settings(
        database_url=database_url, api_service_token=SecretStr(SERVICE), web_local_login=False
    )
    app: FastAPI = create_app(
        settings, llm=llm, fetcher=web.fetcher(), queue=queue, storage=storage
    )
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test/api/v1") as client:
            yield client


async def test_secured_api(secured: AsyncClient) -> None:
    user = (await secured.put("/users/telegram/9", headers=bearer(SERVICE))).json()
    assert (await secured.post("/auth/links", json={"user_id": user["id"]})).status_code == 401
    link = (
        await secured.post("/auth/links", json={"user_id": user["id"]}, headers=bearer(SERVICE))
    ).json()
    # погашение ссылки не требует сервисного токена: секрет и есть доступ
    token = (await secured.post("/auth/links/redeem", json={"secret": link["secret"]})).json()
    progress = await secured.get(f"/users/{user['id']}/progress", headers=bearer(token["token"]))
    assert progress.status_code == 200

    local = await secured.post("/auth/local", headers=bearer(SERVICE))
    assert local.status_code == 404
    assert local.json()["error"]["code"] == "local_login_disabled"
