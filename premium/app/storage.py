"""Private local storage with internal names and atomic artifact replacement."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID, uuid4


class Storage:
    def __init__(self, data_dir: Path | str):
        self.root = Path(data_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.staging_dir = self.root / "staging"
        self.staging_dir.mkdir(parents=True, exist_ok=True)

    def _contained(self, path: Path) -> Path:
        resolved = path.resolve()
        if not resolved.is_relative_to(self.root):
            raise ValueError("Path is outside private storage")
        return resolved

    @staticmethod
    def _document_id(document_id: str) -> str:
        try:
            return str(UUID(str(document_id)))
        except (ValueError, TypeError, AttributeError):
            raise ValueError("Document ID must be a UUID") from None

    def document_dir(self, document_id: str) -> Path:
        path = self._contained(self.root / "documents" / self._document_id(document_id))
        path.mkdir(parents=True, exist_ok=True)
        return path

    def source_path(self, document_id: str) -> Path:
        return self.document_dir(document_id) / "source.pdf"

    def revision_dir(self, document_id: str, revision: int) -> Path:
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise ValueError("Revision must be a positive integer")
        path = self._contained(self.document_dir(document_id) / f"revision-{revision}")
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _name(name: str) -> str:
        if not isinstance(name, str) or not name or name in {".", ".."} or any(
            char in name for char in ("/", "\\", ":", "\0")
        ):
            raise ValueError("Artifact name must be a plain filename")
        return name

    @staticmethod
    def _write_atomic(path: Path, content: bytes) -> Path:
        temporary = path.parent / f".{uuid4().hex}.part"
        try:
            with temporary.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return path

    def stage_upload(self, content: bytes) -> Path:
        return self._write_atomic(self.staging_dir / f"{uuid4().hex}.upload", content)

    def _staged(self, staged_path: str | Path) -> Path:
        path = self._contained(Path(staged_path))
        if path.parent != self.staging_dir.resolve() or path.suffix != ".upload":
            raise ValueError("Invalid staged upload")
        return path

    def commit_upload(self, staged_path: str | Path, document_id: str) -> Path:
        staged = self._staged(staged_path)
        target = self.source_path(document_id)
        # Same-filesystem hard link installs the complete source without overwriting.
        os.link(staged, target)
        staged.unlink()
        return target

    def delete_staged(self, staged_path: str | Path) -> None:
        self._staged(staged_path).unlink(missing_ok=True)

    def atomic_write(self, document_id: str, revision: int, name: str, content: bytes) -> Path:
        target = self._contained(self.revision_dir(document_id, revision) / self._name(name))
        return self._write_atomic(target, content)

    def read(self, path: str | Path) -> bytes:
        return self._contained(Path(path)).read_bytes()
