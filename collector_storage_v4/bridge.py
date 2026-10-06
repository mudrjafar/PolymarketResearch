import json
import os
import tempfile
from pathlib import Path

SNAPSHOT_WINDOW_SECONDS = 20 * 60


def replace_file(source, destination):
    os.replace(source, destination)


def _fsync_directory(path):
    if os.name == "nt":
        return
    flags = getattr(os, "O_DIRECTORY", 0) | os.O_RDONLY
    try:
        fd = os.open(str(path), flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_bytes(destination, payload):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=destination.name + ".", suffix=".tmp", dir=str(destination.parent)
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        replace_file(tmp, destination)
        _fsync_directory(destination.parent)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _backup_existing_once(destination):
    destination = Path(destination)
    backup = destination.with_name("live_trades_before_sqlite.jsonl")
    if not destination.exists() or backup.exists():
        return
    _atomic_bytes(backup, destination.read_bytes())


def publish_snapshot(store, destination, window_seconds=SNAPSHOT_WINDOW_SECONDS):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _backup_existing_once(destination)
    rows = store.recent_trades(window_seconds)
    payload = b"".join(
        (
            json.dumps(
                row,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
        for row in rows
    )
    _atomic_bytes(destination, payload)
    return len(rows)


class CollectorLock:
    def __init__(self, path):
        self.path = Path(path)
        self._handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError):
            handle.close()
            raise RuntimeError(
                "Another collector instance already holds the collector lock"
            ) from None
        self._handle = handle
        return self

    def __exit__(self, exc_type, exc, tb):
        handle, self._handle = self._handle, None
        if handle is not None:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
        return False
