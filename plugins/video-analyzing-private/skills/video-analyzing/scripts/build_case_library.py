from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from normalize_history import load_normalized_records
from metric_states import calculate_metric_states
from store import file_hash, stable_hash, write_jsonl_new
from validate_classifications import (
    ClassificationValidationError,
    calculate_commercial_result,
    classify_content_pattern,
)


def build_case_records(
    content_records: list[dict[str, Any]],
    metric_records: list[dict[str, Any]],
    analyses: list[dict[str, Any]],
    manifest: dict[str, Any],
    source_hashes: dict[str, str] | None = None,
    case_version: str = "2",
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    content_map, content_issues = _index_by_id(content_records, "内容")
    metric_map, metric_issues = _index_by_id(metric_records, "商业数据")
    analysis_map, analysis_issues = _index_by_id(analyses, "分类结果")
    cases = []
    rejected = [*content_issues, *metric_issues, *analysis_issues]
    for material_id in sorted(content_map):
        if material_id not in metric_map:
            rejected.append({"material_id": material_id, "error": "商业数据缺失或已被隔离"})
            continue
        if material_id not in analysis_map:
            rejected.append({"material_id": material_id, "error": "分类结果缺失或已被隔离"})
            continue
        try:
            cases.append(
                _build_case(
                    material_id,
                    content_map[material_id],
                    metric_map[material_id],
                    analysis_map[material_id],
                    manifest,
                    source_hashes or {},
                    case_version,
                )
            )
        except (KeyError, ValueError, ClassificationValidationError) as error:
            rejected.append({"material_id": material_id, "error": str(error)})
    return cases, rejected


def _index_by_id(
    records: list[dict[str, Any]], label: str
) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    issues = []
    for index, record in enumerate(records, start=1):
        material_id = str(record.get("material_id", "")).strip()
        if not material_id:
            issues.append({"material_id": "", "error": f"{label}第{index}条material_id缺失"})
            continue
        grouped.setdefault(material_id, []).append(record)
    result = {}
    for material_id, matches in grouped.items():
        if len(matches) == 1:
            result[material_id] = matches[0]
        else:
            issues.append({"material_id": material_id, "error": f"{label}material_id重复，已隔离"})
    return result, issues


def _build_case(
    material_id: str,
    content: dict[str, Any],
    metrics: dict[str, Any],
    analysis: dict[str, Any],
    manifest: dict[str, Any],
    source_hashes: dict[str, str],
    case_version: str,
) -> dict[str, Any]:
    _validate_content(content, manifest)
    classification = classify_content_pattern(
        analysis["labels"], manifest, video_content=content
    )
    evidence = analysis.get("evidence", {})
    if set(evidence) != set(manifest["dimensions"]):
        raise ValueError("分类证据必须覆盖五个维度")
    commercial = calculate_commercial_result(metrics, manifest)
    canonical_raw = {
        canonical: metrics[source] for canonical, source in manifest["source_fields"].items()
    }
    manifest_hash = stable_hash(manifest)
    fingerprint = stable_hash({"content": content, "metrics": metrics, "manifest": manifest_hash})
    return {
        "case_id": f"case_{fingerprint[:20]}",
        "material_id": material_id,
        "case_version": case_version,
        "rule_version": manifest["rule_version"],
        "rule_manifest_sha256": manifest_hash,
        "source_hashes": dict(source_hashes),
        "content_fingerprint": stable_hash(content),
        "video_content": content,
        "labels": classification["labels"],
        "evidence": evidence,
        "content_pattern_scores": classification["scores"],
        "content_pattern": classification["content_pattern"],
        "content_pattern_decision": classification["decision"],
        "actual_raw": {key: metrics[key] for key in manifest["source_fields"].values()},
        "actual_metrics": commercial["metrics"],
        "metric_states": calculate_metric_states(canonical_raw),
        "commercial_levels": commercial["levels"],
        "commercial_pattern": commercial["commercial_pattern"],
    }


def _validate_content(content: dict[str, Any], manifest: dict[str, Any]) -> None:
    for field in manifest["input_fields"]:
        value = content.get(field["name"])
        if field["required"] and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"必填内容字段无效: {field['name']}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--content-file", required=True)
    parser.add_argument("--metrics-file", required=True)
    parser.add_argument("--analysis-file", required=True)
    parser.add_argument("--manifest-file", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--rejected-output", required=True)
    parser.add_argument("--content-source-file")
    parser.add_argument("--metrics-source-file")
    parser.add_argument("--analysis-source-file")
    parser.add_argument("--case-version", default="2")
    args = parser.parse_args()
    values = [
        load_normalized_records(args.content_file),
        load_normalized_records(args.metrics_file),
        load_normalized_records(args.analysis_file),
        json.loads(Path(args.manifest_file).read_text(encoding="utf-8")),
    ]
    source_hashes = {
        name: file_hash(path)
        for name, path in {
            "content": args.content_source_file,
            "metrics": args.metrics_source_file,
            "classification": args.analysis_source_file,
        }.items()
        if path
    }
    cases, rejected = build_case_records(
        *values, source_hashes=source_hashes, case_version=args.case_version
    )
    write_jsonl_new(args.output, cases)
    Path(args.rejected_output).write_text(json.dumps(rejected, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
