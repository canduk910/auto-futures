#!/usr/bin/env python3
"""Runtime sync helper shared by entrypoint, service loop, and UI."""
from __future__ import annotations

import os
import threading
import logging
from pathlib import Path
from typing import Iterable, List

try:
    from google.cloud import storage
except Exception as exc:  # pragma: no cover
    raise RuntimeError(
        "google-cloud-storage 패키지가 필요합니다. requirements.txt에 추가하고 설치하세요."
    ) from exc

log = logging.getLogger("runtime_sync")
RUNTIME_DIR = Path("runtime")
DEFAULT_PATTERNS = ["**/*.jsonl", "**/*.json", "**/*.ndjson"]
DEFAULT_PREFIX = "runtime/"
_lock = threading.Lock()
_last_status = {"ts": None, "success": None, "error": None}

def _normalize_prefix(prefix: str | None) -> str:
    prefix = prefix or ""
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    return prefix

def _gather_files(patterns: Iterable[str]) -> List[Path]:
    files: List[Path] = []
    for pattern in patterns or DEFAULT_PATTERNS:
        files.extend(RUNTIME_DIR.glob(pattern))
    return [f for f in files if f.is_file()]

def _bucket_and_prefix() -> tuple[storage.Bucket | None, str]:
    bucket_name = os.getenv("GCS_BUCKET")
    if not bucket_name:
        log.debug("GCS_BUCKET 미설정, runtime sync skip")
        return None, ""
    prefix = _normalize_prefix(os.getenv("GCS_PREFIX") or DEFAULT_PREFIX)
    client = storage.Client()
    return client.bucket(bucket_name), prefix

def upload_runtime(patterns: Iterable[str] | None = None) -> bool:
    bucket, prefix = _bucket_and_prefix()
    if not bucket:
        return False
    files = _gather_files(patterns or DEFAULT_PATTERNS)
    if not files:
        log.info("runtime 디렉터리에 업로드할 파일이 없습니다.")
        return True
    ok = True
    for f in files:
        blob = bucket.blob(f"{prefix}{f.relative_to(RUNTIME_DIR)}")
        try:
            blob.upload_from_filename(str(f))
            log.info("업로드 완료: %s -> gs://%s/%s", f, bucket.name, blob.name)
        except Exception as exc:
            log.warning("업로드 실패: %s (%s)", f, exc)
            ok = False
    return ok

def download_runtime() -> bool:
    bucket, prefix = _bucket_and_prefix()
    if not bucket:
        return False
    blobs = list(bucket.list_blobs(prefix=prefix))
    if not blobs:
        log.info("버킷에 %s 아래 파일이 없습니다.", prefix)
        return True
    for blob in blobs:
        if not blob.name or blob.name.endswith("/"):
            continue
        rel = Path(blob.name).relative_to(prefix.rstrip("/"))
        dest = RUNTIME_DIR / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            blob.download_to_filename(str(dest))
            log.info("다운로드 완료: gs://%s/%s -> %s", bucket.name, blob.name, dest)
        except Exception as exc:
            log.warning("다운로드 실패: %s (%s)", blob.name, exc)
            return False
    return True

def safe_upload(patterns: Iterable[str] | None = None):
    import time
    ts = time.time()
    try:
        result = upload_runtime(patterns)
        status = {"ts": ts, "success": result, "error": None if result else "upload_failed"}
    except Exception as exc:  # pragma: no cover
        log.exception("runtime 업로드 중 예외")
        status = {"ts": ts, "success": False, "error": str(exc)}
    with _lock:
        _last_status.update(status)
    return status

def safe_download():
    import time
    ts = time.time()
    try:
        result = download_runtime()
        status = {"ts": ts, "success": result, "error": None if result else "download_failed"}
    except Exception as exc:
        log.exception("runtime 다운로드 중 예외")
        status = {"ts": ts, "success": False, "error": str(exc)}
    with _lock:
        _last_status.update(status)
    return status

def get_last_status():
    with _lock:
        return dict(_last_status)

