"""Filesystem store for staged upload bytes; metadata lives in the control database."""

from __future__ import annotations

import os
from pathlib import Path


class StagingStore:
    """Write staged objects atomically under one engine-owned directory."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def path_for(self, upload_id: str) -> Path:
        """Return the on-disk location of a staged object."""

        # Upload identifiers are engine-minted, so they cannot traverse outside the root.
        return self.root / upload_id

    def write(self, upload_id: str, data: bytes) -> Path:
        """Persist bytes so a partially written object is never visible."""

        self.root.mkdir(parents=True, exist_ok=True)
        target = self.path_for(upload_id)
        temporary = target.with_suffix(".part")
        with open(temporary, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        return target

    def read(self, upload_id: str) -> bytes:
        """Return the staged bytes; raises FileNotFoundError when they were released."""

        return self.path_for(upload_id).read_bytes()

    def exists(self, upload_id: str) -> bool:
        """Return whether the staged bytes are present."""

        return self.path_for(upload_id).is_file()

    def delete(self, upload_id: str) -> None:
        """Remove staged bytes when present."""

        try:
            self.path_for(upload_id).unlink()
        except FileNotFoundError:
            pass
