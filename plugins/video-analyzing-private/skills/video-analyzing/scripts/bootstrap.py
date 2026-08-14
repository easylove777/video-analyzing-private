from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
from typing import Any
import zipfile

from errors import StoreError
from integrity import READY, check_integrity
from migrations import MIGRATIONS, SUPPORTED_DATA_SCHEMA, migrate_if_required
from runtime_paths import add_data_root_argument, resolved_data_root
from snapshot import verify_snapshot
from store import exclusive_lock, get_status, read_json, write_json_atomic


ARCHIVE_NAME = "full-history.zip"
MANIFEST_NAME = "snapshot-manifest.json"


def bootstrap(data_root: Path, seed_dir: Path, plugin_version: str) -> dict[str, Any]:
    target = Path(data_root).resolve()
    seed = Path(seed_dir).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.parent / ".video-analyzing-bootstrap.lock"
    with exclusive_lock(lock_path):
        existing = _reuse_existing(target)
        if existing is not None:
            return existing
        if target.exists() and not target.is_dir():
            raise StoreError(f"data root is not a directory: {target}")
        if target.is_dir() and any(target.iterdir()):
            raise StoreError(f"refusing to overwrite nonempty invalid data root: {target}")
        return _initialize(target, seed, plugin_version)


def _reuse_existing(target: Path) -> dict[str, Any] | None:
    if not target.is_dir() or not any(target.iterdir()):
        return None
    migration = migrate_if_required(target, SUPPORTED_DATA_SCHEMA, MIGRATIONS)
    if migration["status"] == "error":
        raise StoreError(
            "existing data migration failed "
            f"[{migration['code']}]: backup={migration['backup_path']} "
            f"work={migration['work_path']}: {migration['error']}"
        )
    if migration["status"] == "read_only":
        return _read_only_existing(target, migration["code"])
    integrity = check_integrity(
        target,
        supported_data_schema=SUPPORTED_DATA_SCHEMA,
    )
    if integrity["state"] != READY:
        raise StoreError(
            f"existing data root is not reusable [{integrity['code']}]: {target}"
        )
    metadata = read_json(target / "metadata.json")
    snapshot_sha256 = metadata.get("snapshot_sha256")
    if not isinstance(snapshot_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", snapshot_sha256):
        raise StoreError(f"existing metadata has invalid snapshot SHA256: {target}")
    return {
        "status": "reused",
        "data_root": str(target),
        "snapshot_sha256": snapshot_sha256,
        "rule_version": integrity["active_rule_version"],
        "case_count": integrity["case_count"],
    }


def _read_only_existing(target: Path, code: str) -> dict[str, Any]:
    try:
        status = get_status(target)
    except (StoreError, OSError, UnicodeError, TypeError) as exc:
        raise StoreError(
            f"existing data root is not reusable [{code}]: {target}"
        ) from exc
    active = status.get("active_rule")
    if not isinstance(active, dict):
        raise StoreError(f"existing data root is not reusable [{code}]: {target}")
    try:
        metadata = read_json(target / "metadata.json")
    except (StoreError, OSError, UnicodeError, TypeError):
        metadata = {}
    snapshot_sha256 = metadata.get("snapshot_sha256") if isinstance(metadata, dict) else None
    return {
        "status": "read_only",
        "code": code,
        "data_root": str(target),
        "snapshot_sha256": snapshot_sha256,
        "rule_version": active.get("rule_version"),
        "case_count": status.get("case_count", 0),
    }


def _initialize(target: Path, seed: Path, plugin_version: str) -> dict[str, Any]:
    archive_path = seed / ARCHIVE_NAME
    manifest_path = seed / MANIFEST_NAME
    manifest = verify_snapshot(archive_path, manifest_path)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{target.name}.bootstrap-", dir=target.parent)
    )
    removed_empty_target = False
    try:
        _extract_archive(archive_path, staging)
        _verify_extracted_files(staging, manifest)
        status = get_status(staging)
        active = status.get("active_rule")
        if not isinstance(active, dict):
            raise StoreError("seed does not contain an active rule")
        metadata = _build_metadata(
            plugin_version,
            manifest,
            active,
        )
        write_json_atomic(staging / "metadata.json", metadata)
        integrity = check_integrity(staging)
        if integrity["state"] != READY:
            raise StoreError(
                f"staged data root failed integrity [{integrity['code']}]: {staging}"
            )
        if target.is_dir():
            if any(target.iterdir()):
                raise StoreError(f"data root became nonempty during bootstrap: {target}")
            target.rmdir()
            removed_empty_target = True
        if target.exists():
            raise StoreError(f"data root already exists before activation: {target}")
        try:
            os.replace(staging, target)
        except BaseException:
            if removed_empty_target and not target.exists():
                target.mkdir()
            raise
        staging = None
        return {
            "status": "initialized",
            "data_root": str(target),
            "snapshot_sha256": manifest["archive_sha256"],
            "rule_version": integrity["active_rule_version"],
            "case_count": integrity["case_count"],
        }
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging)


def _extract_archive(archive_path: Path, staging: Path) -> None:
    try:
        with zipfile.ZipFile(archive_path) as archive:
            for info in archive.infolist():
                relative = _safe_archive_path(info.filename)
                mode = (info.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(mode):
                    raise StoreError(f"snapshot archive contains symlink: {info.filename}")
                target = (staging / Path(*relative.parts)).resolve()
                try:
                    target.relative_to(staging.resolve())
                except ValueError as exc:
                    raise StoreError(f"snapshot path escapes staging: {info.filename}") from exc
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("xb") as destination:
                    shutil.copyfileobj(source, destination)
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise StoreError(f"cannot extract snapshot archive: {exc}") from exc


def _safe_archive_path(name: str) -> PurePosixPath:
    if "\\" in name or re.match(r"^[A-Za-z]:", name):
        raise StoreError(f"unsafe snapshot path: {name}")
    relative = PurePosixPath(name)
    if relative.is_absolute() or not relative.parts:
        raise StoreError(f"unsafe snapshot path: {name}")
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise StoreError(f"unsafe snapshot path: {name}")
    return relative


def _verify_extracted_files(staging: Path, manifest: dict[str, Any]) -> None:
    records = manifest["files"]
    expected = {record["path"]: record for record in records}
    actual = {
        path.relative_to(staging).as_posix(): path
        for path in sorted(item for item in staging.rglob("*") if item.is_file())
    }
    if set(actual) != set(expected):
        difference = sorted(set(actual) ^ set(expected))
        raise StoreError(f"extracted snapshot file list mismatch: {difference[0]}")
    for relative, record in expected.items():
        path = actual[relative]
        payload = path.read_bytes()
        if len(payload) != record["size"]:
            raise StoreError(f"extracted snapshot file size mismatch: {relative}")
        if hashlib.sha256(payload).hexdigest() != record["sha256"]:
            raise StoreError(f"extracted snapshot file SHA256 mismatch: {relative}")


def _build_metadata(
    plugin_version: str,
    manifest: dict[str, Any],
    active: dict[str, Any],
) -> dict[str, Any]:
    initialized_at = manifest.get("created_at")
    if not isinstance(initialized_at, str) or not initialized_at:
        raise StoreError("snapshot manifest has invalid created_at")
    return {
        "plugin_version": plugin_version,
        "snapshot_schema_version": manifest["snapshot_schema_version"],
        "snapshot_sha256": manifest["archive_sha256"],
        "data_schema_version": manifest["data_schema_version"],
        "initialized_at": initialized_at,
        "active_rule_version": active.get("rule_version"),
        "active_case_library_sha256": active.get("case_library_sha256"),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed-dir", required=True)
    add_data_root_argument(parser)
    parser.add_argument("--plugin-version", required=True)
    args = parser.parse_args()
    args.data_root = str(resolved_data_root(args))
    result = bootstrap(
        Path(args.data_root),
        Path(args.seed_dir),
        args.plugin_version,
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
