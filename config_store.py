from __future__ import annotations
#!/usr/bin/env python3
"""Central configuration storage helpers for local and Cloud Run deployments."""

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Tuple

from dotenv import dotenv_values, set_key

try:
    from google.cloud import secretmanager  # type: ignore
    from google.api_core import exceptions as gcloud_exceptions  # type: ignore
except Exception:  # pragma: no cover
    secretmanager = None
    gcloud_exceptions = None

ENV_FILE_PATH = Path(__file__).resolve().parent / ".env"
CONFIG_SECRET_NAME = os.getenv("CONFIG_SECRET_NAME", "auto-futures-config")
PROJECT_ID = os.getenv("PROJECT_ID") or os.getenv("GOOGLE_CLOUD_PROJECT")


@dataclass
class ConfigData:
    values: Dict[str, str]
    source: str  # "env_file" or "secret_manager"


def _is_cloud_run() -> bool:
    return bool(os.getenv("K_SERVICE"))


def _load_from_env_file() -> ConfigData:
    env_values = dotenv_values(ENV_FILE_PATH)
    stringified = {k: str(v) for k, v in env_values.items() if v is not None}
    return ConfigData(values=stringified, source="env_file")


def _ensure_secret_client():
    if secretmanager is None:
        raise RuntimeError("google-cloud-secret-manager is not installed")
    return secretmanager.SecretManagerServiceClient()


def _secret_resource_name() -> str:
    if not PROJECT_ID:
        raise RuntimeError("PROJECT_ID 또는 GOOGLE_CLOUD_PROJECT 환경변수가 필요합니다.")
    return f"projects/{PROJECT_ID}/secrets/{CONFIG_SECRET_NAME}"


def _ensure_secret_exists(client: Any) -> str:
    resource = _secret_resource_name()
    if gcloud_exceptions is None:
        return resource
    try:
        client.get_secret(name=resource)
    except gcloud_exceptions.NotFound:
        parent = resource.split("/secrets/")[0]
        client.create_secret(
            parent=parent,
            secret_id=CONFIG_SECRET_NAME,
            secret={"replication": {"automatic": {}}},
        )
    return resource


def _load_from_secret_manager() -> ConfigData:
    client = _ensure_secret_client()
    resource = _secret_resource_name()
    try:
        response = client.access_secret_version(name=f"{resource}/versions/latest")
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(f"Secret Manager 값 조회 실패: {exc}") from exc
    payload = response.payload.data.decode("utf-8")
    try:
        values = json.loads(payload)
        if not isinstance(values, dict):
            raise ValueError("config payload must be a JSON object")
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(f"Secret Manager payload 파싱 실패: {exc}") from exc
    return ConfigData(values={k: str(v) for k, v in values.items()}, source="secret_manager")


def load_config() -> ConfigData:
    if _is_cloud_run():
        return _load_from_secret_manager()
    if not ENV_FILE_PATH.exists():
        return ConfigData(values={}, source="missing_env_file")
    return _load_from_env_file()


def save_config(updates: Dict[str, str]) -> Tuple[ConfigData, str]:
    """Persist configuration updates and return new snapshot and source."""
    if _is_cloud_run():
        return _save_via_secret_manager(updates)
    return _save_via_env_file(updates)


def _save_via_env_file(updates: Dict[str, str]) -> Tuple[ConfigData, str]:
    if not ENV_FILE_PATH.exists():
        raise RuntimeError("로컬 환경에서 .env 파일을 찾을 수 없습니다.")
    for key, value in updates.items():
        set_key(str(ENV_FILE_PATH), key, value, quote_mode="never")
    new_config = _load_from_env_file()
    return new_config, "env_file"


def _save_via_secret_manager(updates: Dict[str, str]) -> Tuple[ConfigData, str]:
    client = _ensure_secret_client()
    resource = _ensure_secret_exists(client)

    # Fetch existing values
    current = {}
    try:
        current = _load_from_secret_manager().values.copy()
    except RuntimeError:
        current = {}

    current.update(updates)
    payload = json.dumps(current, ensure_ascii=False)

    try:
        client.add_secret_version(
            parent=resource,
            payload={"data": payload.encode("utf-8")},
        )
    except gcloud_exceptions.NotFound:
        resource = _ensure_secret_exists(client)
        client.add_secret_version(
            parent=resource,
            payload={"data": payload.encode("utf-8")},
        )
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(f"Secret Manager 업데이트 실패: {exc}") from exc

    return ConfigData(values=current, source="secret_manager"), "secret_manager"
