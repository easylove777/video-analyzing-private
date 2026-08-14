from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
from typing import Any

from content_pattern_clustering import build_content_patterns
from errors import ValidationError
from metric_states import calculate_metric_states
from runtime_paths import add_data_root_argument, resolved_data_root
from store import (
    create_rule_proposal,
    get_active_assets,
    stable_hash,
    write_json_atomic,
    write_jsonl_atomic,
    write_text_atomic,
)
from validate_classifications import calculate_commercial_result
from validate_rules import validate_manifest


IMMUTABLE_FIELDS = (
    "input_fields",
    "field_fallbacks",
    "dimensions",
    "source_fields",
    "commercial_metrics",
    "prediction_targets",
    "level_config",
    "commercial_patterns",
)
ACTUAL_FIELDS = {
    "spend": "spend_7d",
    "shows": "impressions_7d",
    "clicks": "clicks_7d",
    "pay_orders": "pay_orders_7d",
    "pay_gmv": "pay_gmv_7d",
    "settle_amount": "settle_amount_7d",
    "settle_orders": "settle_orders_7d",
    "refund_orders": "refund_orders_7d",
}


def build_candidate_assets(
    base_manifest: dict[str, Any],
    content_rows: list[dict[str, Any]],
    analyses: list[dict[str, Any]],
    actual_rows: list[dict[str, Any]],
    target_version: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any], str]:
    content = _unique_rows(content_rows, "content")
    analysis = _unique_rows(analyses, "analysis")
    actual = _unique_rows(actual_rows, "actual")
    ids = set(content)
    if ids != set(analysis) or ids != set(actual) or len(ids) != 45:
        raise ValidationError("Content Pattern训练池必须是三方一致的45个唯一material_id")
    samples = [
        {
            "material_id": material_id,
            "labels": deepcopy(analysis[material_id]["labels"]),
            "video_content": deepcopy(content[material_id]),
        }
        for material_id in sorted(ids)
    ]
    clustering = build_content_patterns(samples, min_cluster_size=3, max_clusters=15)
    manifest = _build_manifest(base_manifest, clustering, target_version)
    validate_manifest(manifest)
    _validate_immutable(base_manifest, manifest)
    cases = _build_cases(manifest, clustering["assignment"], content, analysis, actual)
    distribution = dict(sorted(Counter(case["content_pattern"] for case in cases).items()))
    report = {
        "operation": "prepare-training-pattern-rebuild",
        "source_rule_version": base_manifest["rule_version"],
        "target_rule_version": target_version,
        "unique_material_count": len(ids),
        "case_count": len(cases),
        "training_material_ids": sorted(ids),
        "training_material_set_sha256": stable_hash(sorted(ids)),
        "new_pattern_distribution": distribution,
        "pattern_count": len(distribution),
        "clustering": {key: value for key, value in clustering.items() if key != "assignment"},
        "commercial_rules_unchanged": all(manifest[key] == base_manifest[key] for key in IMMUTABLE_FIELDS),
        "performance_evaluation_performed": False,
        "activation_performed": False,
        "observation_written": False,
    }
    return manifest, cases, report, _render_rule_source(manifest, report)


def prepare_training_rebuild(
    data_root: str | Path,
    content_dir: str | Path,
    analysis_dir: str | Path,
    actual_dir: str | Path,
    target_version: str = "2.0.0",
) -> dict[str, Any]:
    root = Path(data_root)
    active_before = (root / "rules" / "active.json").read_bytes()
    protected_before = _protected_counts(root)
    active, base_manifest, _ = get_active_assets(root)
    manifest, cases, report, source = build_candidate_assets(
        base_manifest,
        _load_directory(content_dir),
        _load_directory(analysis_dir),
        _load_directory(actual_dir),
        target_version,
    )
    work = root / "rules" / "work" / target_version
    rule_path = work / "content-pattern-rule.md"
    write_text_atomic(rule_path, source)
    write_json_atomic(work / "manifest.json", manifest)
    write_jsonl_atomic(work / "cases.jsonl", cases)
    write_json_atomic(work / "report.json", report)
    proposal = create_rule_proposal(root, rule_path, manifest, cases, report)
    active_unchanged = (root / "rules" / "active.json").read_bytes() == active_before
    protected_unchanged = _protected_counts(root) == protected_before
    if not active_unchanged or not protected_unchanged:
        raise ValidationError("提案准备越界修改了活动规则或业务闭环数据")
    return {
        "proposal": proposal,
        "proposal_id": proposal["proposal_id"],
        "base_active": active,
        "candidate_manifest": manifest,
        "report": {
            **report,
            "active_rule_unchanged": active_unchanged,
            "protected_business_objects_unchanged": protected_unchanged,
        },
    }


def _unique_rows(rows: list[dict[str, Any]], label: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        material_id = str(row.get("material_id", "")).strip()
        if not material_id or material_id in result:
            raise ValidationError(f"{label}包含空或重复material_id")
        result[material_id] = row
    return result


def _build_manifest(
    base: dict[str, Any], clustering: dict[str, Any], target_version: str
) -> dict[str, Any]:
    manifest = deepcopy(base)
    manifest["rule_version"] = target_version
    label_names = {
        item["code"]: item["name"]
        for dimension in base["dimensions"].values()
        for item in dimension["labels"]
    }
    definitions: dict[str, Any] = {}
    weights: dict[str, dict[str, int]] = {}
    used_names: set[str] = set()
    for code, profile in clustering["profiles"].items():
        dominant = profile["dominant_labels"]
        name = f"{label_names[dominant['core_hook']]} × {label_names[dominant['main_selling_point']]}"
        if name in used_names:
            name = f"{name} × {label_names[dominant['content_form']]}"
        used_names.add(name)
        definitions[code] = {
            "name": name,
            "mechanism": " / ".join(label_names[value] for value in dominant.values()),
            "representative_material_id": profile["representative_material_id"],
            "sample_count": profile["sample_count"],
            "cohesion": profile["cohesion"],
        }
        flattened: dict[str, int] = {}
        for supports in profile["label_support"].values():
            flattened.update({label: round(value * 100) for label, value in supports.items()})
        weights[code] = flattened
    manifest["content_patterns"] = definitions
    manifest["content_pattern_weights"] = weights
    manifest["tie_breakers"] = {
        "order": ["score", "hook_mapping", "selling_mapping", "smallest_code"],
        "hook_mapping": _best_mappings(clustering["profiles"], "core_hook"),
        "selling_mapping": _best_mappings(clustering["profiles"], "main_selling_point"),
    }
    manifest["content_pattern_model"] = {
        "type": "prototype-v1",
        "candidate_scope": "same_pattern_only",
        "min_cluster_size": 3,
        "min_support_after_exclusion": 2,
        "category_similarity_weight": 0.7,
        "semantic_similarity_weight": 0.3,
        "profiles": clustering["profiles"],
    }
    return manifest


def _best_mappings(profiles: dict[str, Any], dimension: str) -> dict[str, str]:
    labels = {
        label
        for profile in profiles.values()
        for label in profile["label_support"][dimension]
    }
    return {
        label: min(
            profiles,
            key=lambda code: (-profiles[code]["label_support"][dimension].get(label, 0), code),
        )
        for label in sorted(labels)
    }


def _build_cases(
    manifest: dict[str, Any],
    assignments: dict[str, str],
    content: dict[str, dict[str, Any]],
    analysis: dict[str, dict[str, Any]],
    actual: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    manifest_hash = stable_hash(manifest)
    cases = []
    for material_id in sorted(content):
        canonical = _canonical_actual(actual[material_id])
        source_raw = {
            manifest["source_fields"][key]: value for key, value in canonical.items()
        }
        commercial = calculate_commercial_result(source_raw, manifest)
        pattern = assignments[material_id]
        identity = stable_hash([material_id, stable_hash(content[material_id]), manifest_hash])
        cases.append({
            "case_id": f"case_{identity[:20]}",
            "material_id": material_id,
            "case_version": "2",
            "rule_version": manifest["rule_version"],
            "rule_manifest_sha256": manifest_hash,
            "source_hashes": {"training_material_set": stable_hash(sorted(content))},
            "content_fingerprint": stable_hash(content[material_id]),
            "video_content": content[material_id],
            "labels": analysis[material_id]["labels"],
            "evidence": analysis[material_id]["evidence"],
            "content_pattern_scores": {pattern: 1.0},
            "content_pattern": pattern,
            "content_pattern_decision": "training_assignment",
            "actual_raw": canonical,
            "actual_metrics": commercial["metrics"],
            "metric_states": calculate_metric_states(canonical),
            "commercial_levels": commercial["levels"],
            "commercial_pattern": commercial["commercial_pattern"],
        })
    return cases


def _canonical_actual(row: dict[str, Any]) -> dict[str, float]:
    days = float(row.get("observed_days", 7))
    if days <= 0:
        raise ValidationError("observed_days必须大于0")
    values = {}
    for canonical, source in ACTUAL_FIELDS.items():
        value = row.get(source)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
            raise ValidationError(f"真实字段无效: {source}")
        values[canonical] = float(value) / days
    return values


def _validate_immutable(base: dict[str, Any], candidate: dict[str, Any]) -> None:
    changed = [field for field in IMMUTABLE_FIELDS if base[field] != candidate[field]]
    if changed:
        raise ValidationError(f"Content Pattern重建越界修改: {changed}")


def _render_rule_source(manifest: dict[str, Any], report: dict[str, Any]) -> str:
    lines = [
        "# 茶叶短视频 Content Pattern 规则 2.0",
        "",
        f"- 版本：{manifest['rule_version']}",
        "- 训练边界：仅使用本次45个material_id的内容字段建立Pattern。",
        "- Commercial指标、阈值、五维与Commercial Pattern沿用上一活动版本。",
        "",
        "## Content Patterns",
        "",
        "| Pattern | 名称 | 核心机制 | 样本数 | 代表material_id | 组内一致性 |",
        "|---|---|---|---:|---|---:|",
    ]
    for code, definition in manifest["content_patterns"].items():
        lines.append(
            f"| {code} | {definition['name']} | {definition['mechanism']} | "
            f"{definition['sample_count']} | {definition['representative_material_id']} | {definition['cohesion']:.4f} |"
        )
    lines.extend(["", "## 训练样本分布", ""])
    for code, profile in manifest["content_pattern_model"]["profiles"].items():
        lines.append(f"- {code}: {', '.join(profile['material_ids'])}")
    lines.extend([
        "",
        "## 模型配置",
        "",
        "<!-- CONTENT_PATTERN_MODEL_JSON",
        json.dumps(manifest["content_pattern_model"], ensure_ascii=False, sort_keys=True),
        "CONTENT_PATTERN_MODEL_JSON -->",
        "",
        f"训练集哈希：`{report['training_material_set_sha256']}`",
    ])
    return "\n".join(lines) + "\n"


def _load_directory(directory: str | Path) -> list[dict[str, Any]]:
    paths = sorted(Path(directory).glob("*.json"))
    return [json.loads(path.read_text(encoding="utf-8-sig")) for path in paths]


def _protected_counts(root: Path) -> dict[str, int]:
    return {
        "predictions": len(list((root / "predictions").glob("*/*.json"))),
        "reports": len(list((root / "reports").glob("**/*.json"))),
        "observations": len(list((root / "observations" / "items").glob("*/*.json"))),
        "clusters": len(list((root / "observations" / "clusters").glob("*.json"))),
        "sedimentation": len(list((root / "proposals").glob("*.json"))),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    add_data_root_argument(parser)
    parser.add_argument("--content-dir", required=True)
    parser.add_argument("--analysis-dir", required=True)
    parser.add_argument("--actual-dir", required=True)
    parser.add_argument("--target-version", default="2.0.0")
    parser.add_argument("--result-output", required=True)
    args = parser.parse_args()
    args.data_root = str(resolved_data_root(args))
    result = prepare_training_rebuild(
        args.data_root,
        args.content_dir,
        args.analysis_dir,
        args.actual_dir,
        args.target_version,
    )
    write_json_atomic(args.result_output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
