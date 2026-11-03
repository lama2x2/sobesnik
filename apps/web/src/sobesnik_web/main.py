"""Точка входа uvicorn: `uvicorn sobesnik_web.main:app --no-access-log` (в адресе страницы входа
есть секрет ссылки — в лог доступа он попасть не должен)."""

from sobesnik_web.app import create_app
from sobesnik_web.logs import configure_logging
from sobesnik_web.settings import WebSettings

settings = WebSettings()
configure_logging(settings.log_level)
app = create_app(settings)
