from __future__ import annotations
#!/usr/bin/env python3
"""Central configuration storage helpers for local and Cloud Run deployments."""

import json
import os
import logging
import threading
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
RUNTIME_DIR = Path(__file__).resolve().parent / "runtime"
RUNTIME_SETTINGS_PATH = RUNTIME_DIR / "settings.json"
_SETTINGS_LOCK = threading.Lock()
CONFIG_SECRET_NAME = os.getenv("CONFIG_SECRET_NAME", "auto-futures-config")

MANAGED_RUNTIME_KEYS: Dict[str, Dict[str, Any]] = {
    "SYMBOL": {"type": "str", "default": "ETHUSDT"},
    "LEVERAGE": {"type": "int", "default": 5},
    "ENV": {"type": "str", "default": "paper"},
    "DRY_RUN": {"type": "bool", "default": True},
    "LOOP_ENABLE": {"type": "bool", "default": True},
    "LOOP_TRIGGER": {"type": "str", "default": "event"},
    "LOOP_INTERVAL_SEC": {"type": "int", "default": 60},
    "LOOP_COOLDOWN_SEC": {"type": "int", "default": 8},
    "LOOP_BACKOFF_MAX_SEC": {"type": "int", "default": 30},
    "MP_WINDOW_SEC": {"type": "int", "default": 10},
    "MP_DELTA_PCT": {"type": "float", "default": 0.25},
    "KLINE_RANGE_PCT": {"type": "float", "default": 0.4},
    "VOL_LOOKBACK": {"type": "int", "default": 20},
    "VOL_MULT": {"type": "float", "default": 2.0},
    "USE_QUOTE_VOLUME": {"type": "bool", "default": True},
}

# Runtime env is used as a last resort fallback (e.g., Cloud Run env vars)
def _runtime_env_values() -> Dict[str, str]:
    return {k: str(v) for k, v in os.environ.items() if v is not None}


def _ensure_runtime_dir() -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)


def _cast_runtime_value(key: str, value: Any) -> Any:
    meta = MANAGED_RUNTIME_KEYS.get(key)
    if not meta:
        return value
    kind = meta.get("type")
    try:
        if kind == "bool":
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return bool(value)
            return str(value).strip().lower() in {"1", "true", "yes", "on"}
        if kind == "int":
            return int(value)
        if kind == "float":
            return float(value)
        return str(value)
    except Exception:
        return meta.get("default")


def _stringify_runtime_value(key: str, value: Any) -> str:
    meta = MANAGED_RUNTIME_KEYS.get(key)
    kind = meta.get("type") if meta else None
    if kind == "bool":
        return "true" if bool(value) else "false"
    return str(value)


def _runtime_defaults(seed: Dict[str, Any] | None = None) -> Dict[str, Any]:
    seed = seed or {}
    defaults: Dict[str, Any] = {}
    for key, meta in MANAGED_RUNTIME_KEYS.items():
        raw = seed.get(key)
        if raw is None:
            raw = os.getenv(key, meta.get("default"))
        defaults[key] = _cast_runtime_value(key, raw)
    return defaults


def _read_runtime_settings(seed: Dict[str, Any] | None = None) -> Dict[str, Any]:
    _ensure_runtime_dir()
    if not RUNTIME_SETTINGS_PATH.exists():
        settings = _runtime_defaults(seed)
        _write_runtime_settings(settings)
        return settings
    with _SETTINGS_LOCK:
        try:
            data = json.loads(RUNTIME_SETTINGS_PATH.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        changed = False
        for key, meta in MANAGED_RUNTIME_KEYS.items():
            if key not in data:
                data[key] = meta.get("default")
                changed = True
            else:
                data[key] = _cast_runtime_value(key, data[key])
    if changed:
        _write_runtime_settings(data)
    return data


def _write_runtime_settings(data: Dict[str, Any]) -> None:
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    with _SETTINGS_LOCK:
        _ensure_runtime_dir()
        temp_path = RUNTIME_SETTINGS_PATH.with_suffix(".tmp")
        temp_path.write_text(payload, encoding="utf-8")
        os.replace(temp_path, RUNTIME_SETTINGS_PATH)


def apply_runtime_settings_to_env() -> None:
    settings = _read_runtime_settings()
    for key, value in settings.items():
        os.environ[key] = _stringify_runtime_value(key, value)


def _get_project_id() -> str:
    return os.getenv("PROJECT_ID") or os.getenv("GOOGLE_CLOUD_PROJECT")

PROJECT_ID = _get_project_id()


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
    project_id = _get_project_id()
    if not project_id:
        raise RuntimeError("PROJECT_ID 또는 GOOGLE_CLOUD_PROJECT 환경변수가 필요합니다.")
    return f"projects/{project_id}/secrets/{CONFIG_SECRET_NAME}"


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
    except gcloud_exceptions.NotFound:
        logging.warning("Secret %s 이 없어 새로 생성합니다.", resource)
        parent = resource.split("/secrets/")[0]
        client.create_secret(
            parent=parent,
            secret_id=CONFIG_SECRET_NAME,
            secret={"replication": {"automatic": {}}},
        )
        client.add_secret_version(
            parent=resource,
            payload={"data": json.dumps({}, ensure_ascii=False).encode("utf-8")},
        )
        return ConfigData(values={}, source="secret_manager")
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(f"Secret Manager 값 조회 실패: {exc}") from exc
    payload = response.payload.data.decode("utf-8")
    try:
        values = json.loads(payload)
        if not isinstance(values, dict):
            raise ValueError("config payload must be a JSON object")
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(f"Secret Manager payload 파싱 실패: {exc}") from exc
    stringified = {k: str(v) for k, v in values.items()}
    if not stringified:
        env_values = _runtime_env_values()
        if env_values:
            logging.info("Secret %s 값이 비어 있어 런타임 환경에서 초기값을 채웁니다.", resource)
            _write_secret_payload(client, resource, env_values)
            return ConfigData(values=env_values, source="secret_manager_seeded")
    return ConfigData(values=stringified, source="secret_manager")


def _merge_runtime_settings(base: ConfigData) -> ConfigData:
    runtime_settings = _read_runtime_settings(base.values)
    merged = base.values.copy()
    for key, value in runtime_settings.items():
        merged[key] = _stringify_runtime_value(key, value)
    return ConfigData(values=merged, source=base.source)


def load_config() -> ConfigData:
    if _is_cloud_run():
        if not _get_project_id():
            logging.warning("PROJECT_ID가 설정되지 않아 Secret Manager를 사용할 수 없습니다. .env로 폴백합니다.")
            if ENV_FILE_PATH.exists():
                return _merge_runtime_settings(_load_from_env_file())
            runtime_values = _runtime_env_values()
            config = ConfigData(values=runtime_values, source="runtime_env")
            return _merge_runtime_settings(config)
        try:
            return _merge_runtime_settings(_load_from_secret_manager())
        except RuntimeError as exc:
            if str(exc) == "secret_manager_permission_denied":
                if ENV_FILE_PATH.exists():
                    return _merge_runtime_settings(_load_from_env_file())
                runtime_values = _runtime_env_values()
                config = ConfigData(values=runtime_values, source="runtime_env_permission_fallback")
                return _merge_runtime_settings(config)
            raise
    if not ENV_FILE_PATH.exists():
        return _merge_runtime_settings(ConfigData(values={}, source="missing_env_file"))
    return _merge_runtime_settings(_load_from_env_file())


def save_config(updates: Dict[str, str]) -> Tuple[ConfigData, str]:
    managed_updates = {k: updates[k] for k in updates if k in MANAGED_RUNTIME_KEYS}
    other_updates = {k: updates[k] for k in updates if k not in MANAGED_RUNTIME_KEYS}
    result_source = []
    if managed_updates:
        settings = _read_runtime_settings()
        for key, value in managed_updates.items():
            settings[key] = _cast_runtime_value(key, value)
        _write_runtime_settings(settings)
        apply_runtime_settings_to_env()
        result_source.append("runtime_settings")
    if other_updates:
        base_result = _save_base_config(other_updates)
        result_source.append(base_result)
    if not result_source:
        result_source.append("noop")
    return load_config(), "+".join(result_source)


def _save_base_config(updates: Dict[str, str]) -> str:
    if _is_cloud_run():
        if not _get_project_id():
            logging.warning("PROJECT_ID가 없어 Secret Manager 저장을 건너뜁니다. .env로 저장합니다.")
            _save_via_env_file(updates)
            return "env_file"
        _save_via_secret_manager(updates)
        return "secret_manager"
    _save_via_env_file(updates)
    return "env_file"


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

    current = {}
    try:
        current = _load_from_secret_manager().values.copy()
    except RuntimeError:
        current = {}

    current.update(updates)
    try:
        _write_secret_payload(client, resource, current)
    except gcloud_exceptions.NotFound:
        resource = _ensure_secret_exists(client)
        _write_secret_payload(client, resource, current)
    except gcloud_exceptions.PermissionDenied as exc:  # pragma: no cover
        logging.error("Secret Manager 쓰기 권한이 없어 .env로 폴백합니다: %s", exc)
        if ENV_FILE_PATH.exists():
            return _save_via_env_file(updates)
        raise RuntimeError("secret_manager_permission_denied") from exc
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(f"Secret Manager 업데이트 실패: {exc}") from exc

    return ConfigData(values=current, source="secret_manager"), "secret_manager"
