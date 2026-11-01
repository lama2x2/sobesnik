import pytest

from sobesnik_asr import AudioNotFoundError, MemoryStorage, StorageUnavailableError


async def test_memory_storage_roundtrip() -> None:
    storage = MemoryStorage()
    await storage.ensure_bucket()
    await storage.put("audio/1", b"data", "audio/ogg")
    assert await storage.get("audio/1") == b"data"
    await storage.delete("audio/1")
    await storage.delete("audio/1")  # отсутствующий объект — не ошибка
    with pytest.raises(AudioNotFoundError):
        await storage.get("audio/1")


async def test_memory_storage_unavailable() -> None:
    storage = MemoryStorage()
    storage.available = False
    with pytest.raises(StorageUnavailableError):
        await storage.put("audio/1", b"data", "audio/ogg")
