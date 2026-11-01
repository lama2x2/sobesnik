"""Хранилище аудио (004 §5.3). Код работает с любым S3-совместимым хранилищем, MinIO — только сервис
в compose."""

import asyncio
from typing import Any, Protocol

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError


class StorageError(Exception):
    """Ошибка хранилища."""


class StorageUnavailableError(StorageError):
    """Хранилище недоступно: нет соединения, таймаут, ошибка сервера."""


class AudioNotFoundError(StorageError):
    """Объекта нет: удалён по TTL или по запросу пользователя."""


class AudioStorage(Protocol):
    async def put(self, key: str, data: bytes, mime: str) -> None: ...

    async def get(self, key: str) -> bytes:
        """`AudioNotFoundError`, если объекта нет."""
        ...

    async def delete(self, key: str) -> None:
        """Отсутствующий объект — не ошибка."""
        ...

    async def ensure_bucket(self) -> None: ...


def _not_found(exc: ClientError) -> bool:
    code = str(exc.response.get("Error", {}).get("Code", ""))
    return code in {"NoSuchKey", "404", "NotFound"}


class S3AudioStorage:
    """boto3 в отдельном потоке: вызовы короткие, асинхронный клиент ради них не нужен."""

    def __init__(
        self,
        endpoint_url: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        *,
        region: str = "us-east-1",
        timeout_s: float = 10.0,
    ) -> None:
        self.bucket = bucket
        self._client: Any = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=Config(
                connect_timeout=3,
                read_timeout=timeout_s,
                retries={"max_attempts": 2, "mode": "standard"},
                s3={"addressing_style": "path"},
            ),
        )

    async def _call(self, method: str, **kwargs: Any) -> Any:
        try:
            return await asyncio.to_thread(getattr(self._client, method), **kwargs)
        except ClientError as exc:
            if _not_found(exc):
                raise AudioNotFoundError(kwargs.get("Key", "")) from exc
            raise StorageUnavailableError(f"S3 {method}: {exc}") from exc
        except BotoCoreError as exc:
            raise StorageUnavailableError(f"S3 {method}: {type(exc).__name__}") from exc

    async def put(self, key: str, data: bytes, mime: str) -> None:
        await self._call("put_object", Bucket=self.bucket, Key=key, Body=data, ContentType=mime)

    async def get(self, key: str) -> bytes:
        response = await self._call("get_object", Bucket=self.bucket, Key=key)
        body: bytes = await asyncio.to_thread(response["Body"].read)
        return body

    async def delete(self, key: str) -> None:
        await self._call("delete_object", Bucket=self.bucket, Key=key)

    async def ensure_bucket(self) -> None:
        try:
            await self._call("head_bucket", Bucket=self.bucket)
        except AudioNotFoundError:
            await self._call("create_bucket", Bucket=self.bucket)


class MemoryStorage:
    """Хранилище в памяти для тестов. `available = False` имитирует недоступность."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.available = True

    def _check(self) -> None:
        if not self.available:
            raise StorageUnavailableError("хранилище недоступно")

    async def put(self, key: str, data: bytes, mime: str) -> None:
        self._check()
        self.objects[key] = (data, mime)

    async def get(self, key: str) -> bytes:
        self._check()
        if key not in self.objects:
            raise AudioNotFoundError(key)
        return self.objects[key][0]

    async def delete(self, key: str) -> None:
        self._check()
        self.objects.pop(key, None)

    async def ensure_bucket(self) -> None:
        self._check()
