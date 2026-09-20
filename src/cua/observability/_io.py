"""Own sealed file writes and OS writer locks; forbid higher-layer imports."""

import os
from pathlib import Path
from typing import BinaryIO

from cua.observability.redaction import RedactedBytes, require_tag


def write_bytes(path: Path, payload: RedactedBytes, *, append: bool = False) -> None:
    require_tag(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    target = path if append else path.with_suffix(path.suffix + ".tmp")
    with target.open("ab" if append else "wb") as stream:
        stream.write(payload.value)
        stream.flush()
        os.fsync(stream.fileno())
    if not append:
        target.replace(path)


class WriterBusyError(RuntimeError):
    """Another writer owns this run; choose a different run or close the owner."""


class WriterLock:
    """One OS-held writer per run, released even on process death; not a stale PID file.

    The lock file remains on disk. Its existence does not indicate ownership. All cooperating
    writers must use this lock; it cannot stop an attacker editing files directly.
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.stream: BinaryIO = path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                # Windows mypy stubs expose an empty fcntl module; this branch runs on POSIX.
                fcntl.flock(  # type: ignore[attr-defined]
                    self.stream.fileno(),
                    fcntl.LOCK_EX | fcntl.LOCK_NB,  # type: ignore[attr-defined]
                )
        except OSError as error:
            self.stream.close()
            raise WriterBusyError("A writer already owns this run") from error

    def close(self) -> None:
        if self.stream.closed:
            return
        if os.name == "nt":
            import msvcrt

            self.stream.seek(0)
            msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]
        self.stream.close()
