from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from pathlib import Path
import os
import shutil
from typing import Any, Callable
from uuid import uuid4

from integrity import READY, check_integrity
from store import read_json


Migration = Callable[[Path], None]
MIGRATIONS: dict[tuple[str, str], Migration] = {}
SUPPORTED_DATA_SCHEMA = "1.0"

IMMUTABLE_JSON_DIRECTORIES = (
    "predictions",
    "blind-runs",
    "batch-runs",
    "observations/items",
    "observations/clusters",
    "proposals",
    "reports",
    "rules/proposals",
)
IMMUTABLE_JSONL_DIRECTORIES = ("case-libraries", "audit")


def migrate_if_required(
    data_root: str | Path,
    supported_version: str,
    registry: dict[tuple[str, str], Migration],
) -> dict[str, Any]:
    root = Path(data_root).resolve()
    current_version = _data_schema_version(root)
    if current_version is None:
        return _result(root, "read_only", "data_schema_missing")
    current_key = _version_key(current_version)
    supported_key = _version_key(supported_version)
    if current_key is None or supported_key is None:
        return _result(root, "read_only", "unsupported_data_schema")
    if current_key > supported_key:
        return _result(root, "read_only", "newer_data_schema")
    if current_key == supported_key:
        integrity = check_integrity(
            root,
            supported_data_schema=supported_version,
        )
        if integrity["state"] != READY:
            return _result(root, "read_only", integrity["code"])
        return _result(root, "unchanged", "ok", state="ready")

    chain = _migration_chain(current_version, supported_version, registry)
    if chain is None:
        return _result(root, "read_only", "migration_missing")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = root.with_name(f"{root.name}.backup-{timestamp}")
    work = root.with_name(f".{root.name}.migration-{uuid4().hex}")
    retired = root.with_name(f".{root.name}.retired-{uuid4().hex}")
    before_counts = _immutable_record_counts(root)
    try:
        shutil.copytree(root, backup)
        shutil.copytree(root, work)
        for migration in chain:
            migration(work)
    except BaseException as exc:
        return _migration_error(
            root,
            "migration_failed",
            backup,
            work,
            exc,
        )

    target_version = _data_schema_version(work)
    if target_version != supported_version:
        return _migration_error(
            root,
            "migration_target_invalid",
            backup,
            work,
            ValueError(f"expected data schema {supported_version}, got {target_version}"),
        )
    after_counts = _immutable_record_counts(work)
    if before_counts != after_counts:
        return _migration_error(
            root,
            "immutable_record_count_changed",
            backup,
            work,
            ValueError("immutable record counts changed"),
            before_counts=before_counts,
            after_counts=after_counts,
        )
    integrity = check_integrity(work, supported_data_schema=supported_version)
    if integrity["state"] != READY:
        return _migration_error(
            root,
            "migration_integrity_failed",
            backup,
            work,
            ValueError(f"integrity failed [{integrity['code']}]"),
            before_counts=before_counts,
            after_counts=after_counts,
        )

    try:
        _replace_directory(root, retired)
    except BaseException as exc:
        return _migration_error(
            root,
            "migration_activation_failed",
            backup,
            work,
            exc,
            before_counts=before_counts,
            after_counts=after_counts,
            recovery_path=str(root if root.exists() else backup),
        )
    try:
        _replace_directory(work, root)
    except BaseException as exc:
        rollback_error: BaseException | None = None
        try:
            _replace_directory(retired, root)
        except BaseException as rollback_exc:
            rollback_error = rollback_exc
        recovery = root if root.exists() else retired if retired.exists() else backup
        return _migration_error(
            root,
            "migration_activation_failed",
            backup,
            work,
            exc,
            before_counts=before_counts,
            after_counts=after_counts,
            recovery_path=str(recovery),
            rollback_error=str(rollback_error) if rollback_error is not None else None,
        )

    cleanup_warning: str | None = None
    retired_path: str | None = None
    try:
        shutil.rmtree(retired)
    except OSError as exc:
        cleanup_warning = str(exc)
        retired_path = str(retired)
    return {
        **_result(root, "migrated", "ok", state="ready"),
        "from_version": current_version,
        "to_version": supported_version,
        "before_counts": before_counts,
        "after_counts": after_counts,
        "backup_path": str(backup),
        "work_path": str(work),
        "cleanup_warning": cleanup_warning,
        "retired_path": retired_path,
    }


def _data_schema_version(root: Path) -> str | None:
    try:
        metadata = read_json(root / "metadata.json")
    except (OSError, UnicodeError, ValueError, TypeError):
        return None
    if not isinstance(metadata, dict):
        return None
    value = metadata.get("data_schema_version")
    return value if isinstance(value, str) and value else None


def _version_key(version: str) -> tuple[int, ...] | None:
    parts = version.split(".")
    if not parts or any(not part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def _migration_chain(
    start: str,
    target: str,
    registry: dict[tuple[str, str], Migration],
) -> list[Migration] | None:
    queue = deque([(start, [])])
    visited = {start}
    while queue:
        version, chain = queue.popleft()
        candidates = sorted(
            (
                next_version,
                migration,
            )
            for (from_version, next_version), migration in registry.items()
            if from_version == version
            and _version_key(next_version) is not None
            and _version_key(next_version) <= _version_key(target)
        )
        for next_version, migration in candidates:
            next_chain = [*chain, migration]
            if next_version == target:
                return next_chain
            if next_version not in visited:
                visited.add(next_version)
                queue.append((next_version, next_chain))
    return None


def _immutable_record_counts(root: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    for relative in IMMUTABLE_JSON_DIRECTORIES:
        directory = root / relative
        counts[relative] = sum(1 for path in directory.rglob("*.json") if path.is_file())
    for relative in IMMUTABLE_JSONL_DIRECTORIES:
        directory = root / relative
        counts[relative] = sum(
            1
            for path in directory.rglob("*.jsonl")
            if path.is_file()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    return counts


def _result(
    root: Path,
    status: str,
    code: str,
    *,
    state: str = "read_only",
) -> dict[str, Any]:
    return {
        "status": status,
        "state": state,
        "code": code,
        "data_root": str(root),
        "backup_path": None,
        "work_path": None,
    }


def _migration_error(
    root: Path,
    code: str,
    backup: Path,
    work: Path,
    error: BaseException,
    **extra: Any,
) -> dict[str, Any]:
    return {
        **_result(root, "error", code),
        "error": str(error),
        "backup_path": str(backup),
        "work_path": str(work),
        **extra,
    }


def _replace_directory(source: Path, target: Path) -> None:
    os.replace(source, target)
