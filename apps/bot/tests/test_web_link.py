"""`/web` — вход в веб-кабинет по ссылке или коду (009 §5.2)."""

from aiogram.methods import SendMessage

from sobesnik_bot import render
from sobesnik_bot.api import ApiUnavailable

from .conftest import USER, Chat_, FakeApi, FakeSession


async def test_web_sends_link_and_code(user: Chat_, api: FakeApi, tg: FakeSession) -> None:
    await user.send("/web")
    assert api.names() == ["upsert_user", "web_link"]
    assert api.calls[-1][1] == {"user_id": api.user_id}
    text = tg.last_text()
    assert "http://127.0.0.1:8080/login/s3cr3t-link" in text
    assert "http://127.0.0.1:8080/login и введите код: <code>K7MQ-2XDA</code>" in text
    assert "один раз, 10 минут" in text


async def test_web_message_has_no_preview(user: Chat_, tg: FakeSession) -> None:
    """Предпросмотр не должен открывать ссылку и тратить её (009 §5.2)."""
    await user.send("/web")
    sent = tg.of(SendMessage)[-1]
    assert sent.link_preview_options.is_disabled


async def test_web_url_from_settings(api: FakeApi) -> None:
    text = render.web_link_text("http://127.0.0.1:9090/", "abc", "AAAA-BBBB")
    assert "http://127.0.0.1:9090/login/abc" in text
    assert "<code>AAAA-BBBB</code>" in text


async def test_web_api_error(user: Chat_, api: FakeApi, tg: FakeSession) -> None:
    api.errors["web_link"] = ApiUnavailable("ConnectError")
    await user.send("/web")
    assert tg.last_text() == render.API_ERRORS["api_unavailable"]


async def test_help_mentions_web(user: Chat_, tg: FakeSession) -> None:
    await user.send("/help")
    assert "/web" in tg.last_text()
    assert USER  # тот же пользователь, что и в остальных тестах бота
