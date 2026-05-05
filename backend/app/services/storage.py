import hashlib
from pathlib import Path

import aiofiles

from ..config import settings


class StorageService:
    """Local-filesystem storage. Phase 0 only — swap to S3/MinIO when needed."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or settings.storage_root
        self.root.mkdir(parents=True, exist_ok=True)

    def _project_dir(self, project_id: str) -> Path:
        d = self.root / project_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    async def save(
        self, project_id: str, document_id: str, filename: str, data: bytes
    ) -> tuple[str, str]:
        """Persist bytes; return (relative_storage_path, sha256)."""
        sha = hashlib.sha256(data).hexdigest()
        ext = Path(filename).suffix
        path = self._project_dir(project_id) / f"{document_id}{ext}"
        async with aiofiles.open(path, "wb") as f:
            await f.write(data)
        return str(path.relative_to(self.root)), sha

    def absolute_path(self, relative_path: str) -> Path:
        return self.root / relative_path

    def delete(self, relative_path: str) -> None:
        p = self.absolute_path(relative_path)
        if p.exists():
            p.unlink()


storage = StorageService()
