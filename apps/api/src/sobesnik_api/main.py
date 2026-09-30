"""Точка входа uvicorn: `uvicorn sobesnik_api.main:app`."""

from sobesnik_api.app import create_app
from sobesnik_api.logging_setup import configure_logging
from sobesnik_api.settings import Settings

settings = Settings()
configure_logging(settings.log_level)
app = create_app(settings)
