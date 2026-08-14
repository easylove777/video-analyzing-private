from __future__ import annotations

import os
from pathlib import Path
import tempfile
from typing import Any

from errors import StoreError
from portable_paths import resolve_store_path
from store import get_status, read_json


READY = "ready"
READ_ONLY = "read_only"
BLOCKED = "blocked"
SUPPORTED_DATA_SCHEMA = "1.0"
SUPPORTED_CONFIG_SCHEMA = "1.0"
INTEGRITY_READ_ERRORS = (StoreError, OSError, UnicodeError, KeyError, TypeError)


def check_integrity(
    data_root: str | Path,
    *,
    supported_data_schema: str = SUPPORTED_DATA_SCHEMA,
) -> dict[str, Any]:
    root = Path(data_root).resolve()
    try:
        status = get_status(root)
    except INTEGRITY_READ_ERRORS:
        rule_version, case_count = _active_identity(root)
        return _result(root, BLOCKED, "active_assets_invalid", rule_version, case_count)

    active = status.get("active_rule")
    rule_version = active.get("rule_version") if isinstance(active, dict) else None
    case_count = int(status.get("case_count", 0))
    if isinstance(active, dict) and not _active_asset_files_exist(root, active):
        return _result(
            root,
            BLOCKED,
            "active_assets_invalid",
            rule_version,
            case_count,
        )

    config_state = _schema_state(root / "config.json", "schema_version")
    if config_state == "missing":
        return _result(root, READ_ONLY, "config_missing", rule_version, case_count)
    if config_state == "invalid":
        return _result(root, BLOCKED, "config_invalid", rule_version, case_count)
    if config_state != SUPPORTED_CONFIG_SCHEMA:
        return _result(
            root,
            READ_ONLY,
            "unsupported_config_schema",
            rule_version,
            case_count,
        )

    metadata_state = _metadata_schema_state(root / "metadata.json")
    if metadata_state == "missing":
        return _result(root, READ_ONLY, "metadata_missing", rule_version, case_count)
    if metadata_state == "invalid":
        return _result(root, BLOCKED, "metadata_invalid", rule_version, case_count)
    if metadata_state != supported_data_schema:
        return _result(
            root,
            READ_ONLY,
            "unsupported_data_schema",
            rule_version,
            case_count,
        )

    if active is None:
        return _result(root, READ_ONLY, "active_rule_missing", None, 0)
    try:
        _probe_write_access(root)
    except OSError:
        return _result(
            root,
            READ_ONLY,
            "data_root_not_writable",
            rule_version,
            case_count,
        )
    return _result(root, READY, "ok", rule_version, case_count)


def require_formal_prediction_ready(data_root: str | Path) -> dict[str, Any]:
    result = check_integrity(data_root)
    if result["state"] != READY:
        raise StoreError(f"正式预测已关闭 [{result['code']}]: {result['data_root']}")
    return result


def _schema_state(path: Path, field: str) -> str:
    try:
        if not path.is_file():
            return "missing"
        value = read_json(path)
    except INTEGRITY_READ_ERRORS:
        return "invalid"
    version = value.get(field) if isinstance(value, dict) else None
    return version if isinstance(version, str) and version else "invalid"


def _metadata_schema_state(path: Path) -> str:
    try:
        if not path.is_file():
            return "missing"
        value = read_json(path)
    except INTEGRITY_READ_ERRORS:
        return "invalid"
    if not isinstance(value, dict):
        return "invalid"
    version = value.get("data_schema_version", value.get("schema_version"))
    return version if isinstance(version, str) and version else "invalid"


def _active_identity(root: Path) -> tuple[str | None, int]:
    try:
        active = read_json(root / "rules" / "active.json")
    except INTEGRITY_READ_ERRORS:
        return None, 0
    if not isinstance(active, dict):
        return None, 0
    version = active.get("rule_version")
    count = active.get("case_count", 0)
    return (
        version if isinstance(version, str) else None,
        count if isinstance(count, int) and not isinstance(count, bool) else 0,
    )


def _active_asset_files_exist(root: Path, active: dict[str, Any]) -> bool:
    try:
        manifest_path = resolve_store_path(root, active["manifest_path"])
        case_library_path = resolve_store_path(root, active["case_library_path"])
    except INTEGRITY_READ_ERRORS:
        return False
    return manifest_path.is_file() and case_library_path.is_file()


def _probe_write_access(root: Path) -> None:
    descriptor = -1
    temporary_path: str | None = None
    try:
        descriptor, temporary_path = tempfile.mkstemp(prefix=".integrity-", dir=root)
        os.write(descriptor, b"integrity")
        os.fsync(descriptor)
    finally:
        close_error: OSError | None = None
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError as exc:
                close_error = exc
        unlink_error: OSError | None = None
        if temporary_path is not None:
            try:
                Path(temporary_path).unlink(missing_ok=True)
            except OSError as exc:
                unlink_error = exc
        if close_error is not None:
            raise close_error
        if unlink_error is not None:
            raise unlink_error


def _result(
    root: Path,
    state: str,
    code: str,
    rule_version: str | None,
    case_count: int,
) -> dict[str, Any]:
    return {
        "state": state,
        "code": code,
        "data_root": str(root),
        "active_rule_version": rule_version,
        "case_count": case_count,
    }
