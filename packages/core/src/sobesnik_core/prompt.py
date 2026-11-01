"""Промпты в файлах с версиями (002 §6).

Промпт — файл prompts/<name>/v<N>.yaml с полями system, user, temperature, seed.
Версия — номер и первые 8 символов sha256 файла: правка без смены номера видна в данных.
"""

from dataclasses import dataclass
from importlib.resources.abc import Traversable
from string import Template

import yaml

from sobesnik_core.versioned import file_versions, read_version, resource_dir, version_tag


@dataclass(frozen=True)
class RenderedPrompt:
    system: str
    user: str


@dataclass(frozen=True)
class PromptTemplate:
    name: str
    version: str
    """Например `v1#3f2a9c1e`."""
    system: str
    user: str
    temperature: float
    seed: int | None

    def render(self, **values: str) -> RenderedPrompt:
        """Подставляет `$имя` в system и user. Пропущенная переменная — KeyError."""
        return RenderedPrompt(
            system=Template(self.system).substitute(values),
            user=Template(self.user).substitute(values),
        )


def _prompt_dir(name: str) -> Traversable:
    return resource_dir("prompts", name)


def prompt_versions(name: str) -> list[int]:
    return file_versions(_prompt_dir(name))


def parse_prompt(name: str, number: int, raw: bytes) -> PromptTemplate:
    data = yaml.safe_load(raw.decode("utf-8"))
    seed = data.get("seed")
    return PromptTemplate(
        name=name,
        version=version_tag(number, raw),
        system=data["system"],
        user=data["user"],
        temperature=float(data["temperature"]),
        seed=int(seed) if seed is not None else None,
    )


def load_prompt(name: str, number: int | None = None) -> PromptTemplate:
    """Промпт `name` версии `number`; без номера — последняя версия."""
    number, raw = read_version(_prompt_dir(name), f"промпт {name}", number)
    return parse_prompt(name, number, raw)
