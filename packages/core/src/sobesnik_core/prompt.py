"""Промпты в файлах с версиями (002 §6).

Промпт — файл prompts/<name>/v<N>.yaml с полями system, user, temperature, seed.
Версия — номер и первые 8 символов sha256 файла: правка без смены номера видна в данных.
"""

import hashlib
import re
from dataclasses import dataclass
from importlib.resources import files
from importlib.resources.abc import Traversable
from string import Template

import yaml

_FILE_RE = re.compile(r"^v(\d+)\.yaml$")


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
    return files("sobesnik_core").joinpath("prompts", name)


def prompt_versions(name: str) -> list[int]:
    numbers = []
    for entry in _prompt_dir(name).iterdir():
        match = _FILE_RE.match(entry.name)
        if match:
            numbers.append(int(match.group(1)))
    return sorted(numbers)


def parse_prompt(name: str, number: int, raw: bytes) -> PromptTemplate:
    data = yaml.safe_load(raw.decode("utf-8"))
    digest = hashlib.sha256(raw).hexdigest()[:8]
    seed = data.get("seed")
    return PromptTemplate(
        name=name,
        version=f"v{number}#{digest}",
        system=data["system"],
        user=data["user"],
        temperature=float(data["temperature"]),
        seed=int(seed) if seed is not None else None,
    )


def load_prompt(name: str, number: int | None = None) -> PromptTemplate:
    """Промпт `name` версии `number`; без номера — последняя версия."""
    if number is None:
        versions = prompt_versions(name)
        if not versions:
            raise FileNotFoundError(f"нет версий промпта {name}")
        number = versions[-1]
    raw = _prompt_dir(name).joinpath(f"v{number}.yaml").read_bytes()
    return parse_prompt(name, number, raw)
