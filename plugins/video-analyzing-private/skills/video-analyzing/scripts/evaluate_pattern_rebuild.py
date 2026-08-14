from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
from typing import Any

from content_pattern_clustering import text_similarity
from metric_states import calculate_metric_states
from predict import _candidate_cases, _exclude_current_cases, predict_video_read_only
from portable_paths import resolve_store_path
from prepare_training_pattern_rebuild import _canonical_actual
from runtime_paths import add_data_root_argument, resolved_data_root
from store import get_active_assets, read_json, read_jsonl, stable_hash, write_json_atomic
from validate_classifications import classify_content_pattern


METRIC_ORDER = (
    "daily_spend",
    "daily_gmv",
    "daily_orders",
    "ctr",
    "cvr",
    "settle_roi",
    "settle_cpo",
    "refund_rate",
    "settle_rate",
)


def relative_error(point: dict[str, Any], actual: dict[str, Any]) -> dict[str, Any]:
    if point.get("status") != "available" or point.get("value") is None:
        return {
            "status": "not_evaluable",
            "relative_error": None,
            "reason": point.get("reason", "prediction_not_available"),
        }
    if actual.get("state") == "undefined" or actual.get("value") is None:
        return {"status": "not_evaluable", "relative_error": None, "reason": "actual_undefined"}
    actual_value = float(actual["value"])
    if actual_value == 0:
        return {"status": "not_evaluable", "relative_error": None, "reason": "actual_zero"}
    error = abs(float(point["value"]) - actual_value) / abs(actual_value)
    return {"status": "evaluable", "relative_error": error, "reason": None}


def evaluate_assets(
    active: dict[str, Any],
    manifest: dict[str, Any],
    cases: list[dict[str, Any]],
    content_rows: list[dict[str, Any]],
    analyses: list[dict[str, Any]],
    actual_states: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    content = {str(row["material_id"]): row for row in content_rows}
    analysis = {str(row["material_id"]): row for row in analyses}
    case_map = {str(case["material_id"]): case for case in cases}
    rows = []
    for material_id in sorted(content):
        video = content[material_id]
        base_analysis = analysis[material_id]
        classification = classify_content_pattern(
            base_analysis["labels"], manifest, video_content=video
        )
        eligible = _exclude_current_cases(cases, video)
        candidates, _ = _candidate_cases(eligible, classification, manifest)
        semantic_scores = {
            case["case_id"]: {
                "score": round(100 * text_similarity(video, case["video_content"]), 4),
                "reason": "按七字段文本相似度确定的只读留一评分。",
            }
            for case in candidates
        }
        prepared = deepcopy(base_analysis)
        prepared["semantic_scores"] = semantic_scores
        prediction = predict_video_read_only(active, manifest, cases, video, prepared)
        actual = (
            actual_states[material_id]
            if actual_states is not None
            else case_map[material_id]["metric_states"]
        )
        errors = {
            metric: relative_error(
                prediction["metric_predictions"][metric]["overall_point_prediction"],
                actual[metric],
            )
            for metric in METRIC_ORDER
        }
        evaluable = [item["relative_error"] for item in errors.values() if item["relative_error"] is not None]
        rows.append({
            "material_id": material_id,
            "content_pattern": prediction["content_classification"]["content_pattern"],
            "candidate_material_ids": [item["material_id"] for item in prediction["candidates"]],
            "same_pattern_case_count": prediction["same_pattern_case_count"],
            "warnings": prediction["warnings"],
            "relative_errors": errors,
            "sample_mean_relative_error": sum(evaluable) / len(evaluable) if evaluable else None,
        })
    summaries = {}
    for metric in METRIC_ORDER:
        values = [row["relative_errors"][metric]["relative_error"] for row in rows]
        evaluable = [value for value in values if value is not None]
        reasons = Counter(
            row["relative_errors"][metric]["reason"]
            for row in rows
            if row["relative_errors"][metric]["reason"]
        )
        summaries[metric] = {
            "mean_relative_error": sum(evaluable) / len(evaluable) if evaluable else None,
            "evaluable_count": len(evaluable),
            "total_count": len(rows),
            "coverage": len(evaluable) / len(rows) if rows else 0.0,
            "not_evaluable_reasons": dict(sorted(reasons.items())),
        }
    sample_means = [row["sample_mean_relative_error"] for row in rows if row["sample_mean_relative_error"] is not None]
    return {
        "rule_version": active["rule_version"],
        "sample_count": len(rows),
        "samples": rows,
        "metric_summary": summaries,
        "sample_equal_mean_relative_error": sum(sample_means) / len(sample_means) if sample_means else None,
    }


def _actual_states(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(row["material_id"]): calculate_metric_states(_canonical_actual(row))
        for row in rows
    }


def _load_directory(path: str | Path) -> list[dict[str, Any]]:
    return [json.loads(item.read_text(encoding="utf-8-sig")) for item in sorted(Path(path).glob("*.json"))]


def _load_proposal_assets(
    data_root: str | Path,
    proposal: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    root = Path(data_root)
    manifest_path = resolve_store_path(root, proposal["manifest_path"])
    case_library_path = resolve_store_path(root, proposal["case_library_path"])
    return read_json(manifest_path), read_jsonl(case_library_path)


def main() -> None:
    parser = argparse.ArgumentParser()
    add_data_root_argument(parser)
    parser.add_argument("--proposal-result", required=True)
    parser.add_argument("--content-dir", required=True)
    parser.add_argument("--analysis-dir", required=True)
    parser.add_argument("--actual-dir", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    args.data_root = str(resolved_data_root(args))
    proposal_result = read_json(args.proposal_result)
    proposal = proposal_result["proposal"]
    candidate_manifest, candidate_cases = _load_proposal_assets(args.data_root, proposal)
    candidate_active = {
        "rule_version": proposal["rule_version"],
        "manifest_sha256": proposal["manifest_sha256"],
        "case_library_sha256": proposal["case_library_sha256"],
    }
    baseline_active, baseline_manifest, baseline_cases = get_active_assets(args.data_root)
    content = _load_directory(args.content_dir)
    analyses = _load_directory(args.analysis_dir)
    states = _actual_states(_load_directory(args.actual_dir))
    candidate = evaluate_assets(candidate_active, candidate_manifest, candidate_cases, content, analyses, states)
    baseline = evaluate_assets(baseline_active, baseline_manifest, baseline_cases, content, analyses, states)
    comparison = {}
    for metric in METRIC_ORDER:
        candidate_mean = candidate["metric_summary"][metric]["mean_relative_error"]
        baseline_mean = baseline["metric_summary"][metric]["mean_relative_error"]
        comparison[metric] = {
            "candidate_mean_relative_error": candidate_mean,
            "baseline_mean_relative_error": baseline_mean,
            "delta": candidate_mean - baseline_mean if candidate_mean is not None and baseline_mean is not None else None,
        }
    result = {
        "candidate": candidate,
        "baseline": baseline,
        "comparison": comparison,
        "writes": {"prediction": 0, "review": 0, "observation": 0, "cluster": 0, "sedimentation": 0},
        "evaluation_sha256": stable_hash([candidate, baseline, comparison]),
    }
    write_json_atomic(args.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
