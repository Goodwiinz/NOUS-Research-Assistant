"""Private artifact byte storage over the configured backend.

Keys are `artifacts/{org}/{artifact_or_upload}/{id}`; nothing here produces a
public or long-lived URL.
"""

import asyncio
from pathlib import Path
from typing import Protocol

from src.core.config import settings


class ArtifactStorage(Protocol):
    async def put(self, key: str, content: bytes, mime_type: str) -> None: ...

    async def get(self, key: str) -> bytes | None: ...

    async def exists(self, key: str) -> bool: ...


class MemoryArtifactStorage:
    """Test double; also documents the contract."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put(self, key: str, content: bytes, mime_type: str) -> None:
        self.objects[key] = content

    async def get(self, key: str) -> bytes | None:
        return self.objects.get(key)

    async def exists(self, key: str) -> bool:
        return key in self.objects


class LocalArtifactStorage:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if self.root.resolve() not in path.parents:
            raise ValueError("artifact key escapes storage root")
        return path

    async def put(self, key: str, content: bytes, mime_type: str) -> None:
        path = self._path(key)

        def write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".part")
            tmp.write_bytes(content)
            tmp.replace(path)

        await asyncio.to_thread(write)

    async def get(self, key: str) -> bytes | None:
        path = self._path(key)
        return await asyncio.to_thread(
            lambda: path.read_bytes() if path.is_file() else None
        )

    async def exists(self, key: str) -> bool:
        return await asyncio.to_thread(self._path(key).is_file)


class S3ArtifactStorage:
    def __init__(self) -> None:
        from src.core.s3_client import S3StorageHelper

        self.helper = S3StorageHelper()

    async def put(self, key: str, content: bytes, mime_type: str) -> None:
        await asyncio.to_thread(self.helper.upload_file, key, content, mime_type)

    async def get(self, key: str) -> bytes | None:
        if not await self.exists(key):
            return None
        return await asyncio.to_thread(self.helper.download_file, key)

    async def exists(self, key: str) -> bool:
        return await asyncio.to_thread(self.helper.object_exists, key)


class SupabaseArtifactStorage:
    def __init__(self) -> None:
        from src.core.supabase_client import StorageHelper

        self.helper = StorageHelper()
        # Same private bucket the file service uses; keys stay under artifacts/.
        self.bucket = "documents"

    async def put(self, key: str, content: bytes, mime_type: str) -> None:
        await asyncio.to_thread(
            self.helper.upload_file, self.bucket, key, content, mime_type
        )

    async def get(self, key: str) -> bytes | None:
        if not await self.exists(key):
            return None
        return await asyncio.to_thread(self.helper.download_file, self.bucket, key)

    async def exists(self, key: str) -> bool:
        return await asyncio.to_thread(self.helper.object_exists, self.bucket, key)


def get_artifact_storage() -> ArtifactStorage:
    backend = settings.STORAGE_BACKEND
    if backend == "local" and settings.SUPABASE_STORAGE_ENABLED:
        backend = "supabase"
    if backend == "s3":
        return S3ArtifactStorage()
    if backend == "supabase":
        return SupabaseArtifactStorage()
    return LocalArtifactStorage(Path(settings.UPLOAD_DIR))
