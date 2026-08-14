from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
from typing import Any
import zipfile

from errors import StoreError
from portable_paths import encode_store_path, resolve_store_path


PATH_FIELDS = {"source_path", "manifest_path", "case_library_path"}
EXCLUDED_NAMES = {".write.lock", "__pycache__", ".tmp"}
SENSITIVE_KEYS = {"token", "password", "secret", "cookie", "api_key"}
SNAPSHOT_SCHEMA_VERSION = "1.0"
FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
TEXT_SUFFIXES = {".csv", ".json", ".jsonl", ".md", ".tsv", ".txt", ".yaml", ".yml"}

WINDOWS_ABSOLUTE_PATH = re.compile(
    r"(?:[A-Za-z]:[\\/]|(?<![A-Za-z0-9:])(?:\\\\|//)[^\\/\s]+[\\/])"
)
UNIX_ABSOLUTE_PATH = re.compile(r'''(?<![\w:/])/(?![/\s])[^\s"'<>]+''')
SENSITIVE_TEXT_KEY = re.compile(
    r"[\"']?(?:token|password|secret|cookie|api_key)[\"']?\s*[:=]",
    re.IGNORECASE,
)


def build_snapshot(
    source_root: Path,
    archive_path: Path,
    manifest_path: Path,
) -> dict[str, Any]:
    source = Path(source_root).resolve()
    if not source.is_dir():
        raise StoreError(f"snapshot source does not exist: {source}")
    archive = Path(archive_path).resolve()
    external_manifest = Path(manifest_path).resolve()
    _validate_output_paths(source, archive, external_manifest)
    archive.parent.mkdir(parents=True, exist_ok=True)
    external_manifest.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="video-analyzing-snapshot-") as directory:
        staging = Path(directory) / "data"
        _copy_source(source, staging)
        _normalize_store_paths(source, staging)
        files = _staged_files(staging)
        _scan_files(files, staging)
        file_records = [_file_record(path, staging) for path in files]
        _write_archive(archive, files, staging)

    config = _read_json(source / "config.json")
    active = _read_json(source / "rules" / "active.json")
    result = {
        "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION,
        "data_schema_version": config.get("schema_version"),
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "rule_version": active.get("rule_version"),
        "case_count": active.get("case_count", 0),
        "archive_sha256": _file_sha256(archive),
        "files": file_records,
    }
    external_manifest.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return result


def verify_snapshot(archive_path: Path, manifest_path: Path) -> dict[str, Any]:
    archive = Path(archive_path)
    manifest = _read_json(Path(manifest_path))
    actual_archive_hash = _file_sha256(archive)
    if actual_archive_hash != manifest.get("archive_sha256"):
        raise StoreError("archive SHA256 mismatch")

    records = manifest.get("files")
    if not isinstance(records, list):
        raise StoreError("snapshot manifest files must be a list")
    validated_records = [
        _validate_file_record(record, index)
        for index, record in enumerate(records)
    ]
    expected_paths = [record["path"] for record in validated_records]
    if expected_paths != sorted(expected_paths) or len(expected_paths) != len(set(expected_paths)):
        raise StoreError("snapshot manifest file paths are not unique and sorted")

    try:
        with zipfile.ZipFile(archive) as snapshot:
            names = snapshot.namelist()
            if names != expected_paths:
                missing = sorted(set(expected_paths) - set(names))
                extra = sorted(set(names) - set(expected_paths))
                detail = (missing or extra or ["entry order"])[0]
                raise StoreError(f"snapshot file list mismatch: {detail}")
            for record in validated_records:
                relative = record["path"]
                payload = snapshot.read(relative)
                if len(payload) != record.get("size"):
                    raise StoreError(f"snapshot file size mismatch: {relative}")
                if hashlib.sha256(payload).hexdigest() != record.get("sha256"):
                    raise StoreError(f"snapshot file SHA256 mismatch: {relative}")
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        if isinstance(exc, StoreError):
            raise
        raise StoreError(f"invalid snapshot archive: {exc}") from exc
    return manifest


def _validate_output_paths(source: Path, archive: Path, manifest: Path) -> None:
    if archive == manifest:
        raise StoreError("snapshot archive and manifest paths must differ")
    for target in (archive, manifest):
        try:
            target.relative_to(source)
        except ValueError:
            continue
        raise StoreError(f"snapshot output must be outside source: {target}")


def _validate_file_record(record: Any, index: int) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise StoreError(f"invalid snapshot file record {index}: expected object")
    relative = record.get("path")
    if not isinstance(relative, str) or not relative:
        raise StoreError(f"invalid snapshot file record {index}: path")
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or "\\" in relative:
        raise StoreError(f"invalid snapshot file path: {relative}")
    size = record.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise StoreError(f"invalid snapshot file size: {relative}")
    sha256 = record.get("sha256")
    if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise StoreError(f"invalid snapshot file SHA256: {relative}")
    return record


def _copy_source(source: Path, staging: Path) -> None:
    staging.mkdir(parents=True)
    _copy_source_directory(source, source, staging)


def _copy_source_directory(source: Path, directory: Path, staging: Path) -> None:
    with os.scandir(directory) as stream:
        entries = sorted(stream, key=lambda entry: entry.name)
    for entry in entries:
        path = Path(entry.path)
        relative = path.relative_to(source)
        if _is_excluded(relative):
            continue
        if _is_link_or_reparse_point(path):
            raise StoreError(
                "snapshot source contains symlink or reparse point: "
                f"{relative.as_posix()}"
            )
        target = staging / relative
        if entry.is_dir(follow_symlinks=False):
            target.mkdir(parents=True, exist_ok=True)
            _copy_source_directory(source, path, staging)
        elif entry.is_file(follow_symlinks=False):
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)


def _is_link_or_reparse_point(path: Path) -> bool:
    if path.is_symlink():
        return True
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _is_excluded(relative: Path) -> bool:
    return any(part in EXCLUDED_NAMES for part in relative.parts) or relative.name.endswith(".tmp")


def _normalize_store_paths(source: Path, staging: Path) -> None:
    targets = [staging / "rules" / "active.json"]
    proposal_root = staging / "rules" / "proposals"
    if proposal_root.is_dir():
        targets.extend(sorted(proposal_root.glob("*.json")))
    for path in targets:
        if not path.is_file():
            continue
        value = _read_json(path)
        for field in PATH_FIELDS & value.keys():
            resolved = resolve_store_path(source, value[field])
            value[field] = encode_store_path(source, resolved)
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def _staged_files(staging: Path) -> list[Path]:
    return sorted(
        (path for path in staging.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(staging).as_posix(),
    )


def _scan_files(files: list[Path], staging: Path) -> None:
    for path in files:
        relative = path.relative_to(staging).as_posix()
        text = _read_staged_text(path, relative)
        if text is None:
            continue
        suffix = path.suffix.lower()
        if suffix == ".json":
            _scan_json_value(_parse_snapshot_json(text, relative), relative)
            continue
        if suffix == ".jsonl":
            for line_number, line in enumerate(text.splitlines(), start=1):
                if line.strip():
                    _scan_json_value(
                        _parse_snapshot_json(line, f"{relative}:{line_number}"),
                        relative,
                    )
            continue
        _scan_text_value(text, relative)


def _parse_snapshot_json(text: str, relative: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise StoreError(f"invalid JSON in snapshot: {relative}") from exc


def _scan_json_value(value: Any, relative: str) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            folded_key = key.casefold()
            if any(folded_key.endswith(sensitive) for sensitive in SENSITIVE_KEYS):
                raise StoreError(f"sensitive key remains in snapshot: {relative}")
            _scan_text_value(key, relative)
            _scan_json_value(item, relative)
    elif isinstance(value, list):
        for item in value:
            _scan_json_value(item, relative)
    elif isinstance(value, str):
        _scan_text_value(value, relative)


def _scan_text_value(text: str, relative: str) -> None:
    if WINDOWS_ABSOLUTE_PATH.search(text) or UNIX_ABSOLUTE_PATH.search(text):
        raise StoreError(f"absolute path remains in snapshot: {relative}")
    if SENSITIVE_TEXT_KEY.search(text):
        raise StoreError(f"sensitive key remains in snapshot: {relative}")


def _read_staged_text(path: Path, relative: str) -> str | None:
    payload = path.read_bytes()
    if payload.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            text = payload.decode("utf-16")
        except UnicodeDecodeError as exc:
            raise StoreError(f"invalid text encoding in snapshot: {relative}") from exc
    else:
        try:
            text = payload.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            if path.suffix.lower() in TEXT_SUFFIXES:
                raise StoreError(f"invalid text encoding in snapshot: {relative}") from exc
            return None
    if "\x00" in text:
        raise StoreError(f"invalid text encoding in snapshot: {relative}")
    return text


def _file_record(path: Path, staging: Path) -> dict[str, Any]:
    payload = path.read_bytes()
    return {
        "path": path.relative_to(staging).as_posix(),
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _write_archive(archive: Path, files: list[Path], staging: Path) -> None:
    with zipfile.ZipFile(archive, "w") as snapshot:
        for path in files:
            relative = path.relative_to(staging).as_posix()
            info = zipfile.ZipInfo(relative, FIXED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            snapshot.writestr(info, path.read_bytes(), compresslevel=9)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StoreError(f"invalid snapshot JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise StoreError(f"snapshot JSON must be an object: {path}")
    return value


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise StoreError(f"cannot read snapshot file: {path}: {exc}") from exc
    return digest.hexdigest()
