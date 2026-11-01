"""Ресурсы пакета с версиями: файлы <папка>/v<N>.yaml (002 §6, 003 §5.2).

Версия — номер и первые 8 символов sha256 файла: правка без смены номера видна в данных.
"""

import hashlib
import re
from importlib.resources import files
from importlib.resources.abc import Traversable

_FILE_RE = re.compile(r"^v(\d+)\.yaml$")
_VERSION_RE = re.compile(r"^v(\d+)#[0-9a-f]{8}$")


def resource_dir(*parts: str) -> Traversable:
    return files("sobesnik_core").joinpath(*parts)


def file_versions(folder: Traversable) -> list[int]:
    if not folder.is_dir():
        return []
    numbers = []
    for entry in folder.iterdir():
        match = _FILE_RE.match(entry.name)
        if match:
            numbers.append(int(match.group(1)))
    return sorted(numbers)


def read_version(folder: Traversable, what: str, number: int | None = None) -> tuple[int, bytes]:
    """Номер и содержимое файла версии `number`; без номера — последняя версия."""
    if number is None:
        numbers = file_versions(folder)
        if not numbers:
            raise FileNotFoundError(f"нет версий: {what}")
        number = numbers[-1]
    path = folder.joinpath(f"v{number}.yaml")
    if not path.is_file():
        raise FileNotFoundError(f"нет версии v{number}: {what}")
    return number, path.read_bytes()


def version_tag(number: int, raw: bytes) -> str:
    """Например `v1#3f2a9c1e`."""
    return f"v{number}#{hashlib.sha256(raw).hexdigest()[:8]}"


def version_number(tag: str) -> int:
    """Номер из `v1#3f2a9c1e`; другой формат — ValueError."""
    match = _VERSION_RE.match(tag)
    if not match:
        raise ValueError(f"неверная версия: {tag!r}")
    return int(match.group(1))
