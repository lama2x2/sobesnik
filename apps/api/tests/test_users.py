from httpx import AsyncClient


async def test_put_telegram_user_is_idempotent(client: AsyncClient) -> None:
    first = await client.put("/users/telegram/42")
    second = await client.put("/users/telegram/42")

    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["telegram_id"] == 42


async def test_different_telegram_ids_are_different_users(client: AsyncClient) -> None:
    a = (await client.put("/users/telegram/1")).json()
    b = (await client.put("/users/telegram/2")).json()
    assert a["id"] != b["id"]
