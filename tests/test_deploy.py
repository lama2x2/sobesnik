"""Развёртывание (008 §4, §6): пример .env не расходится с кодом, порт API не наружу."""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import SecretStr
from pydantic_settings import BaseSettings

from sobesnik_api.settings import Settings
from sobesnik_bot.settings import BotSettings
from sobesnik_web.settings import WebSettings

ROOT = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = ROOT / "deploy" / ".env.example"
COMPOSE_ONLY = {"API_PORT", "WEB_PORT", "COMPOSE_FILE", "COMPOSE_PROJECT_NAME"}
"""Переменные, которые читает сам docker compose, а не код."""


def example_values() -> dict[str, str]:
    """`KEY=value` и `# KEY=value` — строки с переменными, без пояснений."""
    values: dict[str, str] = {}
    for line in ENV_EXAMPLE.read_text().splitlines():
        match = re.fullmatch(r"#? ?([A-Z][A-Z0-9_]*)=(.*)", line)
        if match:
            values.setdefault(match[1], match[2])
    return values


def settings_fields() -> dict[str, tuple[type[BaseSettings], str]]:
    fields: dict[str, tuple[type[BaseSettings], str]] = {}
    for cls in (Settings, BotSettings, WebSettings):
        for name in cls.model_fields:
            fields[name.upper()] = (cls, name)
    return fields


def test_every_setting_is_documented() -> None:
    missing = set(settings_fields()) - set(example_values())
    assert not missing, f"нет в deploy/.env.example: {sorted(missing)}"


def test_no_unknown_variables() -> None:
    unknown = set(example_values()) - set(settings_fields()) - COMPOSE_ONLY
    assert not unknown, f"код не читает: {sorted(unknown)}"


def default_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


@pytest.mark.parametrize("key", sorted(settings_fields()))
def test_shown_default_matches_code(key: str) -> None:
    """Первое упоминание переменной в примере показывает значение по умолчанию из кода.
    Исключение — блоки профилей: там примеры, а не умолчания, и они идут после умолчаний."""
    cls, name = settings_fields()[key]
    shown = defaults_in_example().get(key)
    if shown is None:
        pytest.skip("показана только как пример профиля")
    assert shown == default_text(cls.model_fields[name].default)


def defaults_in_example() -> dict[str, str]:
    """Значения из разделов 4–9: до них — обязательное и примеры профилей."""
    text = ENV_EXAMPLE.read_text()
    tail = text[text.index("# === 4.") :]
    values: dict[str, str] = {}
    for line in tail.splitlines():
        match = re.fullmatch(r"# ([A-Z][A-Z0-9_]*)=(.*)", line)
        if match:
            values.setdefault(match[1], match[2])
    return values


def test_ports_are_local_only() -> None:
    """Порты API и веба — только на этом компьютере (008, решение 3; 009 §10)."""
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    services = compose["services"]
    assert services["api"]["ports"] == ["127.0.0.1:${API_PORT:-8000}:8000"]
    assert services["web"]["ports"] == ["127.0.0.1:${WEB_PORT:-8080}:8000"]
    for name, service in services.items():
        if name not in ("api", "web"):
            assert "ports" not in service, name


def test_web_has_no_access_log() -> None:
    """В адресе страницы входа — секрет ссылки из бота: лог доступа веба выключен (009 §4)."""
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    assert "--no-access-log" in compose["services"]["web"]["command"]


def test_gpu_overlay_points_services_to_ollama() -> None:
    gpu = yaml.safe_load((ROOT / "deploy" / "compose.gpu.yaml").read_text())
    services = gpu["services"]
    assert {"ollama", "ollama-pull", "api", "worker"} <= set(services)
    assert services["api"]["environment"]["LLM_BASE_URL"] == "http://ollama:11434"
    assert services["worker"]["environment"]["ASR_DEVICE"] == "cuda"
    assert services["worker"]["build"]["target"] == "runtime-gpu"
    assert "runtime-gpu" in (ROOT / "Dockerfile").read_text()
