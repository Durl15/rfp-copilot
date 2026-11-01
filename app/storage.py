"""Whole-file sync of the SQLite database to a GCS bucket (Cloud Run's filesystem is ephemeral).

No-op unless GCS_BUCKET is set. Single-writer by design: the service runs with max-instances 1.
Uploads are a consistent snapshot (sqlite backup API, since WAL keeps recent commits out of the main file),
debounced so a burst of writes costs one upload at most DEBOUNCE_SECONDS after the first one.
"""
import logging
import os
import sqlite3
import tempfile
import threading
from pathlib import Path

from app.config import get_settings

log = logging.getLogger(__name__)

DEBOUNCE_SECONDS = 5.0

_lock = threading.Lock()  # guards _timer and serializes uploads
_timer: threading.Timer | None = None


def bucket_name() -> str:
    return os.environ.get("GCS_BUCKET", "").strip()


def _blob():
    from google.cloud import storage  # imported lazily: local dev and tests never need credentials

    path = get_settings().db_path
    return storage.Client().bucket(bucket_name()).blob(path.name)


def download_if_present() -> bool:
    """Before migrations: replace the local file with the bucket copy if one exists. Raises on any storage error,
    so a transient failure can never lead to a fresh empty database overwriting the real one."""
    if not bucket_name():
        return False
    path = get_settings().db_path
    blob = _blob()
    if not blob.exists():
        log.info("storage: gs://%s/%s not found, starting with a new database", bucket_name(), path.name)
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)
    blob.download_to_filename(str(path))
    log.info("storage: downloaded gs://%s/%s (%d bytes)", bucket_name(), path.name, path.stat().st_size)
    return True


def upload_now() -> int:
    """Snapshot the database and upload it. Returns bytes uploaded (0 when disabled or there is no file)."""
    global _timer
    if not bucket_name():
        return 0
    path = get_settings().db_path
    if not path.exists():
        return 0
    with _lock:
        if _timer is not None:
            _timer.cancel()
            _timer = None
        fd, tmp = tempfile.mkstemp(suffix=".sqlite3", dir=path.parent)
        os.close(fd)
        try:
            src, dst = sqlite3.connect(path, timeout=30), sqlite3.connect(tmp)
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()
            size = Path(tmp).stat().st_size
            _blob().upload_from_filename(tmp)
        finally:
            Path(tmp).unlink(missing_ok=True)
    log.info("storage: uploaded gs://%s/%s (%d bytes)", bucket_name(), path.name, size)
    return size


def _upload_logged() -> None:
    global _timer
    with _lock:
        _timer = None
    try:
        upload_now()
    except Exception:
        log.exception("storage: debounced upload failed; the next write will retry")


def schedule_upload() -> None:
    """After a write transaction: upload within DEBOUNCE_SECONDS. A pending upload absorbs further writes."""
    global _timer
    if not bucket_name():
        return
    with _lock:
        if _timer is not None:
            return
        _timer = threading.Timer(DEBOUNCE_SECONDS, _upload_logged)
        _timer.daemon = True
        _timer.start()


def flush() -> None:
    """On shutdown: upload immediately, whether or not a debounce is pending."""
    try:
        upload_now()
    except Exception:
        log.exception("storage: shutdown upload failed")
