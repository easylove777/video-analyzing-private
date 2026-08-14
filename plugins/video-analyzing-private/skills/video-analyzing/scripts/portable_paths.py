from __future__ import annotations

from pathlib import Path

from errors import StoreError


def encode_store_path(root: Path, target: Path) -> str:
    base = Path(root).resolve()
    resolved = Path(target).resolve()
    try:
        return resolved.relative_to(base).as_posix()
    except ValueError as exc:
        raise StoreError(f"路径超出数据目录: {resolved}") from exc


def resolve_store_path(root: Path, stored: str) -> Path:
    base = Path(root).resolve()
    value = Path(stored)
    resolved = value.resolve() if value.is_absolute() else (base / value).resolve()
    try:
        resolved.relative_to(base)
    except ValueError as exc:
        raise StoreError(f"路径超出数据目录: {stored}") from exc
    return resolved
