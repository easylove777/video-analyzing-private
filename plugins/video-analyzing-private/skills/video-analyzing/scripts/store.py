from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
from typing import Any, Iterable, Iterator

from config import DATA_DIRECTORIES, DEFAULT_CONFIG
from errors import StoreError
from portable_paths import encode_store_path, resolve_store_path
from runtime_paths import add_data_root_argument, resolved_data_root


def stable_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def file_hash(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: str | Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise StoreError(f"文件不存在: {path}") from exc
    except json.JSONDecodeError as exc:
        raise StoreError(f"JSON格式错误: {path}: {exc}") from exc


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    target = Path(path)
    if not target.exists():
        return []
    rows = []
    for line_number, line in enumerate(target.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise StoreError(f"JSONL第{line_number}行格式错误: {target}") from exc
    return rows


def _write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def write_json_atomic(path: str | Path, value: Any) -> None:
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    _write_bytes(Path(path), payload)


def write_text_atomic(path: str | Path, text: str) -> None:
    _write_bytes(Path(path), text.encode("utf-8"))


def write_json_new(path: str | Path, value: Any) -> None:
    target = Path(path)
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    _write_bytes_new(target, payload)


def write_jsonl_atomic(path: str | Path, rows: Iterable[dict[str, Any]]) -> None:
    payload = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows
    ).encode("utf-8")
    _write_bytes(Path(path), payload)


def write_jsonl_new(path: str | Path, rows: Iterable[dict[str, Any]]) -> None:
    target = Path(path)
    payload = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows
    ).encode("utf-8")
    _write_bytes_new(target, payload)


def _write_bytes_new(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary_name, path)
        except FileExistsError as exc:
            raise StoreError(f"不可覆盖已有文件: {path}") from exc
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def initialize_data_root(root: str | Path) -> None:
    target = Path(root)
    target.mkdir(parents=True, exist_ok=True)
    for relative in DATA_DIRECTORIES:
        (target / relative).mkdir(parents=True, exist_ok=True)
    config_path = target / "config.json"
    if not config_path.exists():
        try:
            write_json_new(config_path, DEFAULT_CONFIG)
        except StoreError:
            if not config_path.is_file() or read_json(config_path) != DEFAULT_CONFIG:
                raise


@contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    stream.seek(0, os.SEEK_END)
    if stream.tell() == 0:
        stream.write(b"\0")
        stream.flush()
    deadline = time.monotonic() + 5.0
    acquired = False
    while not acquired and time.monotonic() < deadline:
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            time.sleep(0.01)
    if not acquired:
        stream.close()
        raise StoreError("无法获取写入事务锁")
    try:
        yield
    finally:
        stream.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


@contextmanager
def file_transaction(root: str | Path, paths: Iterable[Path]) -> Iterator[None]:
    target = Path(root)
    unique_paths = list(dict.fromkeys(Path(path) for path in paths))
    with exclusive_lock(target / ".write.lock"):
        with file_transaction_locked(unique_paths):
            yield


@contextmanager
def file_transaction_locked(paths: Iterable[Path]) -> Iterator[None]:
    unique_paths = list(dict.fromkeys(Path(path) for path in paths))
    snapshots = {path: path.read_bytes() if path.is_file() else None for path in unique_paths}
    try:
        yield
    except BaseException:
        for path, payload in snapshots.items():
            if payload is None:
                path.unlink(missing_ok=True)
            else:
                _write_bytes(path, payload)
        raise


def append_audit(root: str | Path, operation: str, detail: dict[str, Any]) -> None:
    target = Path(root)
    path = target / "audit" / "events.jsonl"
    with file_transaction(target, (path,)):
        append_audit_locked(target, operation, detail)


def append_audit_locked(root: str | Path, operation: str, detail: dict[str, Any]) -> None:
    path = Path(root) / "audit" / "events.jsonl"
    rows = read_jsonl(path)
    rows.append({"operation": operation, "detail": detail, "created_at": now_utc()})
    write_jsonl_atomic(path, rows)


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_rule_proposal(
    data_root: str | Path,
    rule_file: str | Path,
    manifest: dict[str, Any],
    cases: list[dict[str, Any]],
    report: dict[str, Any],
) -> dict[str, Any]:
    root = Path(data_root)
    initialize_data_root(root)
    plan = _prepare_rule_proposal_plan(root, rule_file, manifest, cases, report)
    if plan["proposal_path"].exists():
        return read_json(plan["proposal_path"])
    with file_transaction(root, plan["transaction_paths"]):
        _write_rule_proposal_plan_locked(root, plan)
    return plan["proposal"]


def _prepare_rule_proposal_plan(
    root: Path,
    rule_file: str | Path,
    manifest: dict[str, Any],
    cases: list[dict[str, Any]],
    report: dict[str, Any],
) -> dict[str, Any]:
    rule_version = str(manifest.get("rule_version", ""))
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", rule_version):
        raise StoreError("rule_version必须是安全的语义版本号")
    source = Path(rule_file)
    source_sha = file_hash(source)
    manifest_sha = stable_hash(manifest)
    _validate_case_binding(cases, manifest, manifest_sha)
    case_sha = stable_hash(cases)
    report_sha = stable_hash(report)
    proposal_id = f"proposal_{stable_hash([source_sha, manifest_sha, case_sha, report_sha])[:16]}"
    source_path = root / "rules" / "sources" / f"{manifest['rule_version']}-{source_sha[:12]}.md"
    manifest_path = root / "rules" / "manifests" / f"{manifest['rule_version']}-{manifest_sha[:12]}.json"
    case_path = root / "case-libraries" / manifest["rule_version"] / f"{case_sha[:16]}.jsonl"
    proposal = {
        "proposal_id": proposal_id,
        "rule_version": manifest["rule_version"],
        "source_path": encode_store_path(root, source_path),
        "source_sha256": source_sha,
        "manifest_path": encode_store_path(root, manifest_path),
        "manifest_sha256": manifest_sha,
        "case_library_path": encode_store_path(root, case_path),
        "case_library_sha256": case_sha,
        "case_count": len(cases),
        "report": report,
        "report_sha256": report_sha,
        "created_at": now_utc(),
    }
    proposal_path = root / "rules" / "proposals" / f"{proposal_id}.json"
    audit_path = root / "audit" / "events.jsonl"
    return {
        "source": source,
        "source_path": source_path,
        "source_sha": source_sha,
        "manifest": manifest,
        "manifest_path": manifest_path,
        "manifest_sha": manifest_sha,
        "cases": cases,
        "case_path": case_path,
        "case_sha": case_sha,
        "proposal": proposal,
        "proposal_path": proposal_path,
        "audit_path": audit_path,
        "transaction_paths": (
            source_path,
            manifest_path,
            case_path,
            proposal_path,
            audit_path,
        ),
    }


def _write_rule_proposal_plan_locked(root: Path, plan: dict[str, Any]) -> None:
    _copy_new_or_verify(plan["source"], plan["source_path"], plan["source_sha"])
    _write_new_or_verify_json(
        plan["manifest_path"], plan["manifest"], plan["manifest_sha"]
    )
    _write_new_or_verify_jsonl(plan["case_path"], plan["cases"], plan["case_sha"])
    if not plan["proposal_path"].exists():
        write_json_new(plan["proposal_path"], plan["proposal"])
        append_audit_locked(
            root,
            "prepare-rule-version",
            {"proposal_id": plan["proposal"]["proposal_id"]},
        )


def activate_rule_proposal(data_root: str | Path, proposal_id: str) -> dict[str, Any]:
    root = Path(data_root)
    if not re.fullmatch(r"proposal_[0-9a-f]{16}", proposal_id):
        raise StoreError("规则proposal_id格式无效")
    proposal_path = root / "rules" / "proposals" / f"{proposal_id}.json"
    if not proposal_path.is_file():
        raise StoreError(f"找不到规则提案: {proposal_id}")
    proposal = read_json(proposal_path)
    active_path = root / "rules" / "active.json"
    audit_path = root / "audit" / "events.jsonl"
    with file_transaction(root, (active_path, audit_path)):
        proposal = read_json(proposal_path)
        _verify_proposal_assets(proposal, root)
        active = {
            "proposal_id": proposal_id,
            "rule_version": proposal["rule_version"],
            "manifest_path": encode_store_path(
                root,
                resolve_store_path(root, proposal["manifest_path"]),
            ),
            "manifest_sha256": proposal["manifest_sha256"],
            "case_library_path": encode_store_path(
                root,
                resolve_store_path(root, proposal["case_library_path"]),
            ),
            "case_library_sha256": proposal["case_library_sha256"],
            "case_count": proposal["case_count"],
            "activated_at": now_utc(),
        }
        write_json_atomic(active_path, active)
        append_audit_locked(root, "activate-rule-version", {"proposal_id": proposal_id})
    return active


def get_active_assets(data_root: str | Path) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    root = Path(data_root)
    active_path = root / "rules" / "active.json"
    if not active_path.is_file():
        raise StoreError("尚未启用规则版本")
    active = read_json(active_path)
    _verify_active(active, root)
    manifest_path = resolve_store_path(root, active["manifest_path"])
    case_library_path = resolve_store_path(root, active["case_library_path"])
    return active, read_json(manifest_path), read_jsonl(case_library_path)


def get_status(data_root: str | Path) -> dict[str, Any]:
    root = Path(data_root)
    active_path = root / "rules" / "active.json"
    if not active_path.is_file():
        return {"active_rule": None, "case_count": 0}
    active = read_json(active_path)
    _verify_active(active, root)
    return {"active_rule": active, "case_count": active["case_count"]}


def get_closed_loop_status(data_root: str | Path) -> dict[str, Any]:
    root = Path(data_root)
    initialize_data_root(root)
    base = get_status(root)
    clusters = [read_json(path) for path in sorted((root / "observations" / "clusters").glob("*.json"))]
    proposal_paths = [
        path
        for path in sorted((root / "proposals").glob("sed_*.json"))
        if not path.name.endswith(".decision.json")
    ]
    proposals = [read_json(path) for path in proposal_paths]
    prediction_paths = list((root / "predictions").glob("*/*.json"))
    observation_paths = list((root / "observations" / "items").glob("*/*.json"))
    observations = [read_json(path) for path in observation_paths]
    prediction_ids = {
        snapshot.get("prediction_id")
        for path in prediction_paths
        for snapshot in (read_json(path),)
        if snapshot.get("schema_version") in {"2.0", "3.0", "4.0"}
    }
    reviewed_ids = {item.get("prediction_id") for item in observations}
    config = read_json(root / "config.json")
    return {
        **base,
        "prediction_count": len(prediction_paths),
        "blind_run_count": len(
            [path for path in (root / "blind-runs").glob("blind_*") if path.is_dir()]
        ),
        "pending_prediction_draft_count": len(
            list((root / "blind-runs").glob("*/drafts/draft_*.json"))
        ),
        "batch_prediction_draft_count": len(
            list((root / "batch-runs").glob("batch_draft_*/draft.json"))
        ),
        "pending_review_count": len(prediction_ids - reviewed_ids),
        "observation_count": len(observations),
        "valid_observation_count": sum(
            bool(item.get("is_valid_for_sedimentation")) for item in observations
        ),
        "cluster_count": len(clusters),
        "proposal_ready_count": sum(_cluster_is_ready(item, config) for item in clusters),
        "clusters": [
            {
                "cluster_id": item["cluster_id"],
                "status": item["status"],
                "valid_observation_count": len(item["valid_observation_ids"]),
                "open_proposal_id": item.get("open_proposal_id"),
            }
            for item in clusters
        ],
        "proposal_count": len(proposals),
        "pending_approval_count": sum(
            not (root / "proposals" / f"{item['proposal_id']}.decision.json").exists()
            for item in proposals
        ),
        "proposals": [
            {
                "proposal_id": item["proposal_id"],
                "status": _proposal_status(root, item["proposal_id"]),
            }
            for item in proposals
        ],
    }


def _cluster_is_ready(cluster: dict[str, Any], config: dict[str, Any]) -> bool:
    valid_ids = set(cluster.get("valid_observation_ids", []))
    all_ids = set(cluster.get("observation_ids", []))
    valid_count = len(valid_ids)
    total_count = len(all_ids)
    scores = cluster.get("consistency_scores", {})
    if scores and valid_count:
        consistency = sum(float(scores.get(item, 0.0)) for item in valid_ids) / valid_count
    else:
        consistency = max(cluster.get("direction_counts", {}).values(), default=0) / valid_count if valid_count else 0.0
    threshold = config["sedimentation"]
    return (
        valid_count >= threshold["min_observations"]
        and consistency >= threshold["direction_consistency"]
        and (valid_count / total_count if total_count else 0.0) >= threshold["valid_ratio"]
        and cluster.get("rule_major_version") is not None
        and not cluster.get("open_proposal_id")
        and (
            not cluster.get("last_proposal_observation_ids")
            or valid_ids != set(cluster["last_proposal_observation_ids"])
        )
    )


def _proposal_status(root: Path, proposal_id: str) -> str:
    decision_path = root / "proposals" / f"{proposal_id}.decision.json"
    return read_json(decision_path)["status"] if decision_path.exists() else "proposed"


def _copy_new_or_verify(source: Path, target: Path, expected_hash: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if file_hash(target) != expected_hash:
            raise StoreError(f"规则源文件哈希冲突: {target}")
        return
    shutil.copyfile(source, target)


def _write_new_or_verify_json(path: Path, value: Any, expected_hash: str) -> None:
    if path.exists():
        if stable_hash(read_json(path)) != expected_hash:
            raise StoreError(f"JSON文件哈希冲突: {path}")
        return
    write_json_new(path, value)


def _write_new_or_verify_jsonl(
    path: Path, rows: list[dict[str, Any]], expected_hash: str
) -> None:
    if path.exists():
        if stable_hash(read_jsonl(path)) != expected_hash:
            raise StoreError(f"JSONL文件哈希冲突: {path}")
        return
    write_jsonl_new(path, rows)


def _validate_case_binding(
    cases: list[dict[str, Any]], manifest: dict[str, Any], manifest_hash: str
) -> None:
    for case in cases:
        if case.get("rule_version") != manifest.get("rule_version"):
            raise StoreError("案例库rule_version与manifest不一致")
        if case.get("rule_manifest_sha256") != manifest_hash:
            raise StoreError("案例库manifest哈希绑定不一致")


def _verify_proposal_assets(proposal: dict[str, Any], root: Path) -> None:
    source_path = resolve_store_path(root, proposal["source_path"])
    manifest_path = resolve_store_path(root, proposal["manifest_path"])
    case_library_path = resolve_store_path(root, proposal["case_library_path"])
    if file_hash(source_path) != proposal["source_sha256"]:
        raise StoreError("规则源文件哈希不一致")
    manifest = read_json(manifest_path)
    if stable_hash(manifest) != proposal["manifest_sha256"]:
        raise StoreError("规则manifest哈希不一致")
    cases = read_jsonl(case_library_path)
    if stable_hash(cases) != proposal["case_library_sha256"]:
        raise StoreError("案例库哈希不一致")
    _validate_case_binding(cases, manifest, proposal["manifest_sha256"])


def _verify_active(active: dict[str, Any], root: Path) -> None:
    manifest_path = resolve_store_path(root, active["manifest_path"])
    case_library_path = resolve_store_path(root, active["case_library_path"])
    manifest = read_json(manifest_path)
    cases = read_jsonl(case_library_path)
    if stable_hash(manifest) != active["manifest_sha256"]:
        raise StoreError("活动规则manifest哈希不一致")
    if stable_hash(cases) != active["case_library_sha256"] or len(cases) != active["case_count"]:
        raise StoreError("活动案例库不完整或哈希不一致")
    _validate_case_binding(cases, manifest, active["manifest_sha256"])

def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="operation", required=True)
    prepare = subparsers.add_parser("prepare-rule-version")
    add_data_root_argument(prepare)
    prepare.add_argument("--rule-file", required=True)
    prepare.add_argument("--manifest-file", required=True)
    prepare.add_argument("--case-file", required=True)
    prepare.add_argument("--report-file", required=True)
    activate = subparsers.add_parser("activate-rule-version")
    add_data_root_argument(activate)
    activate.add_argument("--proposal-id", required=True)
    status = subparsers.add_parser("status")
    add_data_root_argument(status)
    args = parser.parse_args()
    args.data_root = str(resolved_data_root(args))
    if args.operation == "prepare-rule-version":
        result = create_rule_proposal(
            args.data_root,
            args.rule_file,
            read_json(args.manifest_file),
            read_jsonl(args.case_file),
            read_json(args.report_file),
        )
    elif args.operation == "activate-rule-version":
        result = activate_rule_proposal(args.data_root, args.proposal_id)
    else:
        result = get_closed_loop_status(args.data_root)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
