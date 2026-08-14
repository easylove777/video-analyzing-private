from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
from typing import Any

from compile_rules import compile_markdown
from errors import ValidationError
from pattern_calibration import calibrate_pattern_rules, remap_anchor
from runtime_paths import add_data_root_argument, resolved_data_root
from store import (
    create_rule_proposal,
    get_active_assets,
    read_json,
    stable_hash,
    write_json_atomic,
    write_jsonl_atomic,
    write_text_atomic,
)
from validate_classifications import classify_content_pattern


CODE_ORDER = {"H": 0, "S": 1, "P": 2, "A": 3, "F": 4}


def render_rule_source(
    source_text: str,
    target_version: str,
    calibrated: dict[str, Any],
) -> str:
    text = _replace_version(source_text, target_version)
    text = _insert_boundary_notes(text, calibrated.get("boundary_notes", {}))
    text = _replace_between(text, "### 5.2", "### 5.3", _weight_section(calibrated["weights"]))
    return _replace_between(
        text,
        "### 5.3",
        "## 6.",
        _tie_breaker_section(
            calibrated["hook_mapping"], calibrated["selling_mapping"]
        ),
    )


def rebuild_cases(
    cases: list[dict[str, Any]], manifest: dict[str, Any]
) -> list[dict[str, Any]]:
    material_ids = [str(case["material_id"]) for case in cases]
    if len(material_ids) != len(set(material_ids)):
        raise ValidationError("活动案例库包含重复 material_id")
    manifest_hash = stable_hash(manifest)
    return [_rebuild_case(case, manifest, manifest_hash) for case in cases]


def _rebuild_case(
    case: dict[str, Any], manifest: dict[str, Any], manifest_hash: str
) -> dict[str, Any]:
    classification = classify_content_pattern(case["labels"], manifest)
    identity = stable_hash(
        [case["material_id"], case["content_fingerprint"], manifest_hash]
    )
    return {
        **case,
        "case_id": f"case_{identity[:20]}",
        "case_version": "2",
        "rule_version": manifest["rule_version"],
        "rule_manifest_sha256": manifest_hash,
        "content_pattern_scores": classification["scores"],
        "content_pattern": classification["content_pattern"],
        "content_pattern_decision": classification["decision"],
    }


def prepare_upgrade(
    data_root: str | Path,
    source_rule: str | Path,
    target_version: str,
) -> dict[str, Any]:
    root = Path(data_root)
    source_path = Path(source_rule)
    active, old_manifest, current_cases = get_active_assets(root)
    _validate_source(source_path, active, old_manifest)
    cluster_evidence, observation_ids = _load_cluster_evidence(root)
    calibrated = _calibrate(old_manifest, cluster_evidence)
    rendered = render_rule_source(
        source_path.read_text(encoding="utf-8"), target_version, calibrated
    )
    work_root = root / "rules" / "work" / target_version
    rule_path = work_root / source_path.name
    write_text_atomic(rule_path, rendered)
    new_manifest = compile_markdown(rule_path)
    _validate_manifest_change(old_manifest, new_manifest, target_version)
    rebuilt_cases = rebuild_cases(current_cases, new_manifest)
    _validate_rebuilt_cases(current_cases, rebuilt_cases)
    report = _build_report(
        active,
        old_manifest,
        new_manifest,
        current_cases,
        rebuilt_cases,
        cluster_evidence,
        observation_ids,
        calibrated,
    )
    write_json_atomic(work_root / "manifest.json", new_manifest)
    write_jsonl_atomic(work_root / "cases.jsonl", rebuilt_cases)
    write_json_atomic(work_root / "report.json", report)
    return create_rule_proposal(
        root,
        rule_path,
        new_manifest,
        rebuilt_cases,
        report,
    )


def _validate_source(
    source_path: Path,
    active: dict[str, Any],
    old_manifest: dict[str, Any],
) -> None:
    if not source_path.is_file():
        raise ValidationError(f"规则源文件不存在: {source_path}")
    compiled = compile_markdown(source_path)
    if stable_hash(compiled) != active["manifest_sha256"]:
        raise ValidationError("规则源与活动manifest不一致")
    if compiled != old_manifest:
        raise ValidationError("活动manifest内容不一致")


def _load_cluster_evidence(
    root: Path,
) -> tuple[list[dict[str, Any]], list[str]]:
    evidence = []
    all_observation_ids = set()
    for path in sorted((root / "observations" / "clusters").glob("*.json")):
        cluster = read_json(path)
        observation_ids = [str(item) for item in cluster.get("observation_ids", [])]
        label_sets = []
        for observation_id in observation_ids:
            observation = _load_observation(root, observation_id)
            candidate = observation.get("candidate_case") or {}
            labels = candidate.get("labels")
            if not isinstance(labels, dict) or not labels:
                raise ValidationError(f"Observation缺少候选案例标签: {observation_id}")
            label_sets.append(set(labels.values()))
            all_observation_ids.add(observation_id)
        parts = str(cluster.get("cluster_key", "")).split("|")
        if len(parts) < 2 or not re.fullmatch(r"V\d{2}", parts[1]):
            raise ValidationError(f"Cluster键缺少Content Pattern: {cluster.get('cluster_id')}")
        evidence.append(
            {
                "cluster_id": cluster["cluster_id"],
                "content_pattern": parts[1],
                "label_sets": label_sets,
            }
        )
    if not evidence:
        raise ValidationError("没有可用于Pattern升级的Cluster")
    return evidence, sorted(all_observation_ids)


def _load_observation(root: Path, observation_id: str) -> dict[str, Any]:
    matches = list(
        (root / "observations" / "items").glob(f"*/{observation_id}.json")
    )
    if len(matches) != 1:
        raise ValidationError(f"Observation文件数量异常: {observation_id}")
    return read_json(matches[0])


def _calibrate(
    manifest: dict[str, Any], clusters: list[dict[str, Any]]
) -> dict[str, Any]:
    patterns = set(manifest["content_patterns"])
    covered = {cluster["content_pattern"] for cluster in clusters}
    old_ties = manifest["tie_breakers"]
    result = calibrate_pattern_rules(
        manifest["content_pattern_weights"],
        clusters,
        uncovered_patterns=patterns - covered,
        hook_anchors=old_ties["hook_mapping"],
        selling_anchors=old_ties["selling_mapping"],
    )
    weights = result["weights"]
    result["hook_mapping"] = _remap_dimension("H", weights, old_ties["hook_mapping"])
    result["selling_mapping"] = _remap_dimension(
        "S", weights, old_ties["selling_mapping"]
    )
    result["uncovered_patterns"] = sorted(patterns - covered)
    result["boundary_notes"] = _boundary_notes(manifest, result)
    return result


def _remap_dimension(
    prefix: str,
    weights: dict[str, dict[str, int]],
    old_mapping: dict[str, str],
) -> dict[str, str]:
    codes = sorted(
        {
            code
            for pattern_weights in weights.values()
            for code in pattern_weights
            if code.startswith(prefix)
        }
    )
    return {
        code: remap_anchor(code, weights, old_mapping.get(code))
        for code in codes
        if max(pattern_weights.get(code, 0) for pattern_weights in weights.values()) > 0
    }


def _boundary_notes(
    manifest: dict[str, Any], calibrated: dict[str, Any]
) -> dict[str, str]:
    label_names = {
        item["code"]: item["name"]
        for dimension in manifest["dimensions"].values()
        for item in dimension["labels"]
    }
    notes = {}
    for pattern in sorted(manifest["content_patterns"]):
        if pattern in calibrated.get("uncovered_patterns", []):
            notes[pattern] = "本轮无Cluster覆盖，沿用1.0.0定义、权重与排除边界。"
            continue
        support = calibrated["support"].get(pattern, {})
        top_codes = [
            code
            for code, value in sorted(
                support.items(), key=lambda item: (-item[1], item[0])
            )
            if value > 0
        ][:3]
        evidence = "、".join(
            f"{code}（{label_names.get(code, code)}）" for code in top_codes
        )
        competitor = _closest_competitor(pattern, calibrated["weights"])
        notes[pattern] = (
            f"Cluster等权高频证据为{evidence or '无新增标签'}；"
            f"与{competitor}冲突时按主叙事总分、Hook映射、卖点映射依次裁决。"
        )
    return notes


def _closest_competitor(
    pattern: str, weights: dict[str, dict[str, int]]
) -> str:
    current = weights[pattern]
    candidates = []
    for other, other_weights in weights.items():
        if other == pattern:
            continue
        codes = set(current) | set(other_weights)
        distance = sum(abs(current.get(code, 0) - other_weights.get(code, 0)) for code in codes)
        candidates.append((distance, other))
    return min(candidates)[1]


def _validate_manifest_change(
    old: dict[str, Any], new: dict[str, Any], target_version: str
) -> None:
    if new["rule_version"] != target_version:
        raise ValidationError("新规则版本与目标版本不一致")
    expected_patterns = {f"V{index:02d}" for index in range(1, 11)}
    if set(new["content_patterns"]) != expected_patterns:
        raise ValidationError("新规则必须完整保留V01-V10")
    for field in ("commercial_metrics", "level_config", "commercial_patterns"):
        if new[field] != old[field]:
            raise ValidationError(f"Pattern升级不得修改{field}")


def _validate_rebuilt_cases(
    old_cases: list[dict[str, Any]], new_cases: list[dict[str, Any]]
) -> None:
    old_materials = {str(case["material_id"]) for case in old_cases}
    new_materials = {str(case["material_id"]) for case in new_cases}
    if len(old_cases) != len(new_cases) or old_materials != new_materials:
        raise ValidationError("案例库重建发生数量或material丢失")


def _build_report(
    active: dict[str, Any],
    old_manifest: dict[str, Any],
    new_manifest: dict[str, Any],
    old_cases: list[dict[str, Any]],
    new_cases: list[dict[str, Any]],
    clusters: list[dict[str, Any]],
    observation_ids: list[str],
    calibrated: dict[str, Any],
) -> dict[str, Any]:
    return {
        "operation": "prepare-pattern-upgrade",
        "source_rule_version": active["rule_version"],
        "target_rule_version": new_manifest["rule_version"],
        "cluster_count": len(clusters),
        "observation_count": len(observation_ids),
        "unique_material_count": len({case["material_id"] for case in new_cases}),
        "case_count": len(new_cases),
        "old_pattern_weights": old_manifest["content_pattern_weights"],
        "new_pattern_weights": new_manifest["content_pattern_weights"],
        "old_tie_breakers": old_manifest["tie_breakers"],
        "new_tie_breakers": new_manifest["tie_breakers"],
        "uncovered_patterns": calibrated["uncovered_patterns"],
        "old_case_pattern_distribution": dict(
            sorted(Counter(case["content_pattern"] for case in old_cases).items())
        ),
        "new_case_pattern_distribution": dict(
            sorted(Counter(case["content_pattern"] for case in new_cases).items())
        ),
        "performance_evaluation_performed": False,
        "validation": {
            "commercial_rules_unchanged": True,
            "case_count_preserved": len(old_cases) == len(new_cases),
            "material_set_preserved": {
                case["material_id"] for case in old_cases
            }
            == {case["material_id"] for case in new_cases},
            "manifest_binding_complete": all(
                case["rule_manifest_sha256"] == stable_hash(new_manifest)
                for case in new_cases
            ),
        },
    }


def write_result_file(path: str | Path, result: dict[str, Any]) -> None:
    write_json_atomic(Path(path), result)


def main() -> None:
    parser = argparse.ArgumentParser()
    add_data_root_argument(parser)
    parser.add_argument("--source-rule", required=True)
    parser.add_argument("--target-version", default="1.1.0")
    parser.add_argument("--result-output", required=True)
    args = parser.parse_args()
    args.data_root = str(resolved_data_root(args))
    result = prepare_upgrade(
        Path(args.data_root), Path(args.source_rule), args.target_version
    )
    write_result_file(args.result_output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def _replace_version(text: str, version: str) -> str:
    return re.sub(
        r"^- 版本：[^\s]+",
        f"- 版本：{version}",
        text,
        count=1,
        flags=re.MULTILINE,
    )


def _insert_boundary_notes(text: str, notes: dict[str, str]) -> str:
    marker = "#### 5.1.1 Cluster等权校准边界"
    if marker in text:
        raise ValueError("规则源已包含Cluster校准边界")
    lines = [marker, ""]
    lines.extend(f"- {pattern}：{notes[pattern]}" for pattern in sorted(notes))
    insertion = "\n".join(lines)
    return text.replace("### 5.2", f"{insertion}\n\n### 5.2", 1)


def _weight_section(weights: dict[str, dict[str, int]]) -> str:
    return "\n".join(
        [
            "### 5.2 确定性分值表",
            "",
            "未列出的五维编码对该V类贡献0分。",
            "",
            _render_weight_table(weights),
            "",
            "计算公式：",
            "",
            "```text",
            "score(V) = 五个已选维度编码在V分值表中的分值之和",
            "```",
        ]
    )


def _render_weight_table(weights: dict[str, dict[str, int]]) -> str:
    lines = ["| V类 | 非零证据分值 |", "|---|---|"]
    for pattern in sorted(weights):
        entries = "；".join(
            f"{code}={value}"
            for code, value in sorted(
                weights[pattern].items(),
                key=lambda item: (CODE_ORDER[item[0][0]], item[0]),
            )
        )
        lines.append(f"| {pattern} | {entries} |")
    return "\n".join(lines)


def _tie_breaker_section(
    hook_mapping: dict[str, str],
    selling_mapping: dict[str, str],
) -> str:
    hooks = "，".join(
        f"{code}→{target}" for code, target in sorted(hook_mapping.items())
    )
    selling = "，".join(
        f"{code}→{target}" for code, target in sorted(selling_mapping.items())
    )
    return "\n".join(
        [
            "### 5.3 同分裁决",
            "",
            "按以下顺序选择唯一结果：",
            "",
            "1. 总分最高。",
            "2. 若同分，选择与 `core_hook` 直接映射一致的V类。",
            "3. 仍同分，选择与 `main_selling_point` 直接映射一致的V类。",
            "4. 仍同分，选择编号最小的V类。",
            "",
            f"钩子直接映射：{hooks}。未列出的钩子不提供直接映射。",
            "",
            f"卖点直接映射：{selling}。",
        ]
    )


def _replace_between(text: str, start: str, end: str, replacement: str) -> str:
    left = text.index(start)
    right = text.index(end, left + len(start))
    return text[:left] + replacement.rstrip() + "\n\n" + text[right:]


if __name__ == "__main__":
    main()
