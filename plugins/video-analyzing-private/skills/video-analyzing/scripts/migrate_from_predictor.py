from __future__ import annotations

import argparse
from pathlib import Path
import shutil
from typing import Any

from errors import ValidationError
from metric_states import calculate_metric_states
from portable_paths import resolve_store_path
from store import (
    _prepare_rule_proposal_plan,
    _write_rule_proposal_plan_locked,
    file_transaction,
    file_hash,
    initialize_data_root,
    read_json,
    read_jsonl,
    stable_hash,
    write_json_atomic,
)


def migrate_predictor_data(
    source_root: str | Path, target_root: str | Path
) -> dict[str, Any]:
    source = Path(source_root)
    target = Path(target_root)
    if not source.is_dir():
        raise ValidationError(f"预测数据目录不存在: {source}")
    initialize_data_root(target)
    prediction_plan = _prediction_plan(source, target)
    rule_plan, rule_proposal = _prepare_active_rule(source, target)
    report = {
        "source_root": str(source.resolve()),
        "target_root": str(target.resolve()),
        "legacy_prediction_count": len(prediction_plan),
        "legacy_review_policy": "legacy_review_unsupported",
        "rule_proposal": rule_proposal,
        "old_loop_observations_imported": False,
    }
    report_id = stable_hash(report)[:16]
    report_path = target / "reports" / f"migration_{report_id}.json"
    transaction_paths = [item[1] for item in prediction_plan]
    if rule_plan:
        transaction_paths.extend(rule_plan["transaction_paths"])
    transaction_paths.append(report_path)
    with file_transaction(target, transaction_paths):
        for source_path, destination in prediction_plan:
            if not destination.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source_path, destination)
        if rule_plan:
            _write_rule_proposal_plan_locked(target, rule_plan)
        write_json_atomic(report_path, report)
    return report


def _prediction_plan(source: Path, target: Path) -> list[tuple[Path, Path]]:
    prediction_root = source / "predictions"
    if not prediction_root.is_dir():
        return []
    files = sorted(prediction_root.rglob("*.json"))
    plan = []
    for path in files:
        destination = target / "predictions" / path.relative_to(prediction_root)
        if destination.exists():
            if file_hash(destination) != file_hash(path):
                raise ValidationError(f"迁移目标文件冲突: {destination}")
        plan.append((path, destination))
    return plan


def _prepare_active_rule(
    source: Path, target: Path
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    active_path = source / "rules" / "active.json"
    if not active_path.is_file():
        return None, None
    active = read_json(active_path)
    manifest_path = resolve_store_path(source, active["manifest_path"])
    case_library_path = resolve_store_path(source, active["case_library_path"])
    manifest = read_json(manifest_path)
    cases = [_upgrade_case(case, manifest) for case in read_jsonl(case_library_path)]
    source_path = (
        resolve_store_path(source, active["source_path"])
        if active.get("source_path")
        else _find_source_rule(source, active["rule_version"])
    )
    plan = _prepare_rule_proposal_plan(
        target,
        source_path,
        manifest,
        cases,
        {"migration": "tea-video-commercial-predictor", "case_count": len(cases)},
    )
    proposal = plan["proposal"]
    return plan, {
        "proposal_id": proposal["proposal_id"],
        "case_count": proposal["case_count"],
        "activation_required": True,
    }


def _upgrade_case(case: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    if "metric_states" in case:
        return case
    canonical = {
        name: case["actual_raw"][source] for name, source in manifest["source_fields"].items()
    }
    return {**case, "case_version": "2", "metric_states": calculate_metric_states(canonical)}


def _find_source_rule(source: Path, rule_version: str) -> Path:
    matches = sorted((source / "rules" / "sources").glob(f"{rule_version}-*.md"))
    if not matches:
        raise ValidationError("活动规则缺少源Markdown文件")
    return matches[0]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--target-root", required=True)
    args = parser.parse_args()
    print(migrate_predictor_data(args.source_root, args.target_root))


if __name__ == "__main__":
    main()
