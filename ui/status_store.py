import json, os, threading, time, tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore

_STATUS_DIR = Path(__file__).resolve().parent.parent / "runtime"
_STATUS_PATH = _STATUS_DIR / "status.json"
_LOCK_PATH = _STATUS_DIR / ".status.lock"
_EVENT_LIMIT = 200
_ORDER_LIMIT = 200
_AI_HISTORY_LIMIT = 300
_CLOSE_HISTORY_LIMIT = 500
_MEM_LOCK = threading.Lock()
_AI_HISTORY_PATH = None  # lazy init
_CLOSE_HISTORY_PATH = None  # lazy init


def _ai_history_path() -> Path:
    global _AI_HISTORY_PATH
    if _AI_HISTORY_PATH is None:
        _AI_HISTORY_PATH = _STATUS_DIR / "ai_history.jsonl"
    return _AI_HISTORY_PATH


def _close_history_path() -> Path:
    global _CLOSE_HISTORY_PATH
    if _CLOSE_HISTORY_PATH is None:
        _CLOSE_HISTORY_PATH = _STATUS_DIR / "close_history.jsonl"
    return _CLOSE_HISTORY_PATH


def _ensure_dir() -> None:
    _STATUS_DIR.mkdir(parents=True, exist_ok=True)


@contextmanager
def _locked() -> Any:
    with _MEM_LOCK:
        _ensure_dir()
        if fcntl:
            with open(_LOCK_PATH, "w") as lock_fp:
                fcntl.flock(lock_fp, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_fp, fcntl.LOCK_UN)
        else:  # pragma: no cover
            yield


def _read_unlocked() -> Dict[str, Any]:
    if not _STATUS_PATH.exists():
        return {}
    try:
        with open(_STATUS_PATH, "r", encoding="utf-8") as fp:
            return json.load(fp)
    except Exception:
        return {}


def _write_unlocked(data: Dict[str, Any]) -> None:
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    with tempfile.NamedTemporaryFile("w", delete=False, dir=_STATUS_DIR, encoding="utf-8") as tmp:
        tmp.write(payload)
        tmp.flush()
        os.fsync(tmp.fileno())
        temp_name = tmp.name
    os.replace(temp_name, _STATUS_PATH)


def _set_key(key: str, value: Any) -> None:
    with _locked():
        data = _read_unlocked()
        data[key] = value
        data["last_update_ts"] = time.time()
        _write_unlocked(data)


def read_status() -> Dict[str, Any]:
    with _locked():
        return _read_unlocked()


def write_status(data: Dict[str, Any]) -> None:
    with _locked():
        _write_unlocked(data)


def update_status(section: str, payload: Dict[str, Any], ts: Optional[float] = None) -> None:
    with _locked():
        data = _read_unlocked()
        node = data.get(section) if isinstance(data.get(section), dict) else {}
        node.update(payload)
        node["updated_ts"] = ts or time.time()
        data[section] = node
        data["last_update_ts"] = ts or time.time()
        _write_unlocked(data)


def set_status(data: Dict[str, Any]) -> None:
    with _locked():
        data["last_update_ts"] = time.time()
        _write_unlocked(data)


def append_event(event: Dict[str, Any]) -> None:
    with _locked():
        data = _read_unlocked()
        events = data.get("events") if isinstance(data.get("events"), list) else []
        event_copy = dict(event)
        event_copy.setdefault("ts", time.time())
        events.append(event_copy)
        if len(events) > _EVENT_LIMIT:
            events = events[-_EVENT_LIMIT:]
        data["events"] = events
        data["last_update_ts"] = time.time()
        _write_unlocked(data)


def clear_events() -> None:
    with _locked():
        data = _read_unlocked()
        data["events"] = []
        data["last_update_ts"] = time.time()
        _write_unlocked(data)


def set_latest_input(payload: Dict[str, Any]) -> None:
    snap_ts = time.time()
    snapshot = {"payload": payload, "ts": snap_ts}
    _set_key("latest_input", snapshot)


def set_latest_advice(payload: Dict[str, Any]) -> None:
    snap_ts = time.time()
    snapshot = {"payload": payload, "ts": snap_ts}
    _set_key("latest_advice", snapshot)


def set_positions(positions: List[Dict[str, Any]]) -> None:
    _set_key("positions", {"items": positions, "ts": time.time()})


def append_order_history(order: Dict[str, Any]) -> None:
    order_copy = dict(order)
    order_copy.setdefault("ts", time.time())
    with _locked():
        data = _read_unlocked()
        raw_orders = data.get("orders")
        if isinstance(raw_orders, dict):
            orders = raw_orders.get("items", []) if isinstance(raw_orders.get("items"), list) else []
        elif isinstance(raw_orders, list):  # backwards compatibility
            orders = raw_orders
        else:
            orders = []
        orders.append(order_copy)
        if len(orders) > _ORDER_LIMIT:
            orders = orders[-_ORDER_LIMIT:]
        data["orders"] = {"items": orders, "ts": time.time()}
        data["last_update_ts"] = time.time()
        _write_unlocked(data)


def append_ai_history(entry: Dict[str, Any]) -> None:
    entry_copy = dict(entry)
    entry_copy.setdefault("ts", time.time())
    with _locked():
        _ensure_dir()
        path = _ai_history_path()
        lines: List[str] = []
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as fp:
                    lines = fp.readlines()
            except Exception:
                lines = []
        lines.append(json.dumps(entry_copy, ensure_ascii=False) + "\n")
        if len(lines) > _AI_HISTORY_LIMIT:
            lines = lines[-_AI_HISTORY_LIMIT:]
        with open(path, "w", encoding="utf-8") as fp:
            fp.writelines(lines)


def read_ai_history(limit: int = 100) -> List[Dict[str, Any]]:
    with _locked():
        path = _ai_history_path()
        if not path.exists():
            return []
        try:
            with open(path, "r", encoding="utf-8") as fp:
                lines = fp.readlines()
        except Exception:
            return []
    recent = lines[-limit:]
    out: List[Dict[str, Any]] = []
    for line in reversed(recent):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def append_close_history(entry: Dict[str, Any]) -> None:
    entry_copy = dict(entry)
    entry_copy.setdefault("ts", time.time())
    with _locked():
        _ensure_dir()
        path = _close_history_path()
        lines: List[str] = []
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as fp:
                    lines = fp.readlines()
            except Exception:
                lines = []
        lines.append(json.dumps(entry_copy, ensure_ascii=False) + "\n")
        if len(lines) > _CLOSE_HISTORY_LIMIT:
            lines = lines[-_CLOSE_HISTORY_LIMIT:]
        with open(path, "w", encoding="utf-8") as fp:
            fp.writelines(lines)


def read_close_history(limit: int = 200) -> List[Dict[str, Any]]:
    with _locked():
        path = _close_history_path()
        if not path.exists():
            return []
        try:
            with open(path, "r", encoding="utf-8") as fp:
                lines = fp.readlines()
        except Exception:
            return []
    recent = lines[-limit:]
    records: List[Dict[str, Any]] = []
    for line in reversed(recent):
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except Exception:
            continue
    return records
