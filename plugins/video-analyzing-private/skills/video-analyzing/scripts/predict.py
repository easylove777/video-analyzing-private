from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re
from typing import Any

from store import StoreError, file_transaction, get_active_assets, read_json, stable_hash, write_json_new
from metric_states import (
    RAW_FIELDS,
    build_metric_prediction,
    build_overall_metric_points,
    build_primitive_prediction,
)
from validate_classifications import DIMENSION_WEIGHTS, classify_content_pattern
from schemas import validate_prediction
from errors import ValidationError
from integrity import require_formal_prediction_ready
from blind_contract import validate_blind_receipt
from runtime_paths import add_data_root_argument, resolved_data_root


PREDICTION_ALGORITHM_VERSION = "3.1.0"
PREDICTION_V4_ALGORITHM_VERSION = "4.0.0"


class PredictionError(ValueError):
    pass


def _prediction_id(
    fingerprint: str,
    manifest_sha256: str,
    case_library_sha256: str,
    algorithm_version: str = PREDICTION_ALGORITHM_VERSION,
    analysis_fingerprint: str = "",
) -> str:
    value = stable_hash([
        fingerprint,
        manifest_sha256,
        case_library_sha256,
        algorithm_version,
        analysis_fingerprint,
    ])
    return f"pred_{value[:20]}"


def predict_video(
    data_root: str | Path, video: dict[str, Any], analysis: dict[str, Any]
) -> dict[str, Any]:
    require_formal_prediction_ready(data_root)
    active, manifest, cases = get_active_assets(data_root)
    _validate_prediction_input(video, manifest)
    _validate_analysis(video, analysis, manifest)
    classification = classify_content_pattern(
        analysis.get("labels", {}), manifest, video_content=video
    )
    _validate_evidence(analysis.get("evidence", {}), manifest)
    fingerprint = stable_hash(video)
    analysis_fingerprint = stable_hash(analysis)
    prediction_id = _prediction_id(
        fingerprint,
        active["manifest_sha256"],
        active["case_library_sha256"],
        analysis_fingerprint=analysis_fingerprint,
    )
    snapshot_path = Path(data_root) / "predictions" / str(video["material_id"]) / f"{prediction_id}.json"
    with file_transaction(data_root, (snapshot_path,)):
        if snapshot_path.is_file():
            snapshot = read_json(snapshot_path)
            _validate_snapshot_identity(
                snapshot,
                prediction_id=prediction_id,
                content_fingerprint=fingerprint,
                analysis_fingerprint=analysis_fingerprint,
                active=active,
            )
            return snapshot
        eligible_cases = _exclude_current_cases(cases, video)
        prediction = _calculate_prediction(
            video,
            analysis,
            classification,
            eligible_cases,
            manifest,
            active,
            prediction_id,
            snapshot_path,
        )
        prediction["analysis_fingerprint"] = analysis_fingerprint
        validate_prediction(prediction)
        write_json_new(snapshot_path, prediction)
        return prediction


def predict_video_read_only(
    active: dict[str, Any],
    manifest: dict[str, Any],
    cases: list[dict[str, Any]],
    video: dict[str, Any],
    analysis: dict[str, Any],
) -> dict[str, Any]:
    _validate_prediction_input(video, manifest)
    _validate_analysis(video, analysis, manifest)
    _validate_evidence(analysis.get("evidence", {}), manifest)
    classification = classify_content_pattern(
        analysis["labels"], manifest, video_content=video
    )
    eligible = _exclude_current_cases(cases, video)
    prediction_id = _prediction_id(
        stable_hash(video),
        active["manifest_sha256"],
        active["case_library_sha256"],
        analysis_fingerprint=stable_hash(analysis),
    )
    prediction = _calculate_prediction(
        video,
        analysis,
        classification,
        eligible,
        manifest,
        active,
        prediction_id,
        Path("<read-only>"),
    )
    prediction["analysis_fingerprint"] = stable_hash(analysis)
    validate_prediction(prediction)
    return prediction


def build_prediction_v4(
    data_root: str | Path,
    video: dict[str, Any],
    receipt: dict[str, Any],
) -> dict[str, Any]:
    active, manifest, cases = get_active_assets(data_root)
    _validate_prediction_input(video, manifest)
    validate_blind_receipt(receipt, expected_material_id=str(video["material_id"]))
    if receipt["manifest_sha256"] != active["manifest_sha256"]:
        raise PredictionError("盲预测回执绑定的规则已不是当前活动规则")
    if receipt["case_library_sha256"] != active["case_library_sha256"]:
        raise PredictionError("盲预测回执绑定的案例库已不是当前活动案例库")
    analysis = {
        "material_id": str(video["material_id"]),
        "labels": receipt["labels"],
        "evidence": receipt["evidence"],
        "semantic_scores": receipt["semantic_scores"],
    }
    _validate_analysis(video, analysis, manifest)
    _validate_evidence(analysis["evidence"], manifest)
    classification = classify_content_pattern(
        analysis["labels"], manifest, video_content=video
    )
    analysis_fingerprint = stable_hash(
        {"analysis": analysis, "receipt_sha256": receipt["receipt_sha256"]}
    )
    prediction_id = _prediction_id(
        stable_hash(video),
        active["manifest_sha256"],
        active["case_library_sha256"],
        algorithm_version=PREDICTION_V4_ALGORITHM_VERSION,
        analysis_fingerprint=analysis_fingerprint,
    )
    snapshot_path = (
        Path(data_root)
        / "predictions"
        / str(video["material_id"])
        / f"{prediction_id}.json"
    )
    prediction = _calculate_prediction(
        video,
        analysis,
        classification,
        _exclude_current_cases(cases, video),
        manifest,
        active,
        prediction_id,
        snapshot_path,
        schema_version="4.0",
        algorithm_version=PREDICTION_V4_ALGORITHM_VERSION,
    )
    prediction["analysis_fingerprint"] = analysis_fingerprint
    prediction["blind_provenance"] = receipt_provenance(receipt)
    validate_prediction(prediction)
    return prediction


def freeze_prediction_v4(
    data_root: str | Path,
    video: dict[str, Any],
    receipt: dict[str, Any],
) -> dict[str, Any]:
    require_formal_prediction_ready(data_root)
    prediction = build_prediction_v4(data_root, video, receipt)
    snapshot_path = Path(prediction["snapshot_path"])
    with file_transaction(data_root, (snapshot_path,)):
        if snapshot_path.is_file():
            existing = read_json(snapshot_path)
            if stable_hash(existing) != stable_hash(prediction):
                raise StoreError("已有v4预测快照与重算结果不一致")
            return existing
        write_json_new(snapshot_path, prediction)
    return prediction


def receipt_provenance(receipt: dict[str, Any]) -> dict[str, str]:
    return {
        "blind_status": "isolated",
        "run_id": receipt["run_id"],
        "receipt_sha256": receipt["receipt_sha256"],
        "prompt_version": receipt["prompt_version"],
        "scorer_identity": receipt["scorer_identity"],
        "classification_request_sha256": receipt["classification_request_sha256"],
        "classification_response_sha256": receipt["classification_response_sha256"],
        "similarity_request_sha256": receipt["similarity_request_sha256"],
        "similarity_response_sha256": receipt["similarity_response_sha256"],
    }


def _validate_snapshot_identity(
    snapshot: dict[str, Any],
    *,
    prediction_id: str,
    content_fingerprint: str,
    analysis_fingerprint: str,
    active: dict[str, Any],
) -> None:
    expected = {
        "prediction_id": prediction_id,
        "content_fingerprint": content_fingerprint,
        "analysis_fingerprint": analysis_fingerprint,
        "manifest_sha256": active["manifest_sha256"],
        "case_library_sha256": active["case_library_sha256"],
    }
    mismatches = [key for key, value in expected.items() if snapshot.get(key) != value]
    algorithm = snapshot.get("parameters", {}).get("algorithm_version")
    if algorithm != PREDICTION_ALGORITHM_VERSION:
        mismatches.append("parameters.algorithm_version")
    if snapshot.get("schema_version") != "3.0":
        mismatches.append("schema_version")
    if set(snapshot.get("primitive_predictions", {})) != set(RAW_FIELDS):
        mismatches.append("primitive_predictions")
    try:
        validate_prediction(snapshot)
    except ValidationError as error:
        raise StoreError(f"invalid prediction snapshot: {error}") from error
    if mismatches:
        raise StoreError(f"已有预测快照身份或完整性校验失败: {', '.join(mismatches)}")


def prepare_prediction_candidates(
    data_root: str | Path, video: dict[str, Any], analysis: dict[str, Any]
) -> dict[str, Any]:
    active, manifest, cases = get_active_assets(data_root)
    _validate_prediction_input(video, manifest)
    _validate_analysis(video, analysis, manifest)
    classification = classify_content_pattern(
        analysis.get("labels", {}), manifest, video_content=video
    )
    _validate_evidence(analysis.get("evidence", {}), manifest)
    selected, same_pattern_count = _candidate_cases(
        _exclude_current_cases(cases, video), classification, manifest
    )
    return {
        "material_id": str(video["material_id"]),
        "rule_version": active["rule_version"],
        "content_classification": classification,
        "same_pattern_case_count": same_pattern_count,
        "candidates": [
            {
                "case_id": case["case_id"],
                "material_id": case["material_id"],
                "labels": case["labels"],
                "video_content": case["video_content"],
                "category_similarity": _category_similarity(classification["labels"], case["labels"]),
            }
            for case in selected
        ],
    }


def _calculate_prediction(
    video: dict[str, Any],
    analysis: dict[str, Any],
    classification: dict[str, Any],
    cases: list[dict[str, Any]],
    manifest: dict[str, Any],
    active: dict[str, Any],
    prediction_id: str,
    snapshot_path: Path,
    *,
    schema_version: str = "3.0",
    algorithm_version: str = PREDICTION_ALGORITHM_VERSION,
) -> dict[str, Any]:
    same_pattern = [case for case in cases if case["content_pattern"] == classification["content_pattern"]]
    pool, _ = _candidate_cases(cases, classification, manifest)
    strict = _is_strict_pattern_model(manifest)
    minimum = _minimum_support(manifest)
    eligible_count = len(same_pattern) if strict or len(same_pattern) >= 5 else len(cases)
    top_k_count = _top_k_count(eligible_count)
    warnings = [] if len(same_pattern) >= minimum else ["SAME_PATTERN_SMALL_SAMPLE"]
    if strict and len(same_pattern) == minimum:
        warnings.append("MIN_PATTERN_SUPPORT_AFTER_EXCLUSION")
    scored = _score_candidates(pool, analysis, classification["labels"])
    top_k = scored[:top_k_count]
    predictions, fallbacks = _predict_metrics(top_k, same_pattern, cases, manifest)
    primitives, primitive_fallbacks = _predict_primitives(
        top_k, same_pattern, cases, manifest
    )
    overall_points = build_overall_metric_points(primitives)
    predictions = {
        name: {**prediction, "overall_point_prediction": overall_points[name]}
        for name, prediction in predictions.items()
    }
    fallbacks.extend(primitive_fallbacks)
    warnings.extend(fallbacks)
    probabilities = _commercial_probabilities(top_k, same_pattern, cases, manifest)
    dimensions = _commercial_dimension_predictions(top_k, cases)
    predicted_pattern = _highest_priority_probability(probabilities, manifest)
    confidence = _confidence(len(same_pattern), top_k, fallbacks)
    return {
        "schema_version": schema_version,
        "prediction_id": prediction_id,
        "material_id": str(video["material_id"]),
        "content_fingerprint": stable_hash(video),
        "rule_version": active["rule_version"],
        "manifest_sha256": active["manifest_sha256"],
        "case_library_sha256": active["case_library_sha256"],
        "video_content": video,
        "content_classification": classification,
        "evidence": analysis["evidence"],
        "candidate_count": len(pool),
        "candidates": scored,
        "same_pattern_case_count": len(same_pattern),
        "top_k": top_k,
        "metric_predictions": predictions,
        "primitive_predictions": primitives,
        "commercial_pattern_probabilities": probabilities,
        "commercial_dimension_predictions": dimensions,
        "predicted_commercial_pattern": predicted_pattern,
        "confidence": confidence,
        "warnings": sorted(set(warnings)),
        "key_evidence": list(analysis["evidence"].values())[:3],
        "parameters": {
            "algorithm_version": algorithm_version,
            "eligible_case_count": eligible_count,
            "top_k": top_k_count,
            "category_weights": DIMENSION_WEIGHTS,
            "category_similarity_weight": 0.70,
            "semantic_similarity_weight": 0.30,
            "case_weight_exponent": 2,
        },
        "snapshot_path": str(snapshot_path.resolve()),
    }


def _validate_prediction_input(video: dict[str, Any], manifest: dict[str, Any]) -> None:
    forbidden = {
        "actual_metrics",
        "metrics",
        "commercial_pattern",
        "actual_commercial_pattern",
        *manifest["source_fields"].values(),
        *manifest["prediction_targets"],
    }
    leaked = forbidden.intersection(video)
    if leaked:
        raise PredictionError(f"预测输入不得包含真实商业指标: {sorted(leaked)}")
    allowed_fields = {field["name"] for field in manifest["input_fields"]}
    unknown = set(video) - allowed_fields
    if unknown:
        raise PredictionError(f"预测输入包含未知字段: {sorted(unknown)}")
    for field in manifest["input_fields"]:
        value = video.get(field["name"])
        if field["required"] and (not isinstance(value, str) or not value.strip()):
            raise PredictionError(f"必填内容字段无效: {field['name']}")
    material_id = str(video.get("material_id", ""))
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", material_id)
        or material_id in {".", ".."}
        or material_id.upper() in reserved
    ):
        raise PredictionError("material_id必须是安全的单段标识符")


def _validate_analysis(
    video: dict[str, Any], analysis: dict[str, Any], manifest: dict[str, Any]
) -> None:
    if str(analysis.get("material_id", "")) != str(video.get("material_id", "")):
        raise PredictionError("analysis与视频material_id不一致")
    forbidden = {
        "actual_metrics",
        "metrics",
        "commercial_pattern",
        "actual_commercial_pattern",
        *manifest["source_fields"].values(),
        *manifest["prediction_targets"],
    }
    leaked = forbidden.intersection(analysis)
    if leaked:
        raise PredictionError(f"预测分析不得包含真实商业指标: {sorted(leaked)}")


def _validate_evidence(evidence: dict[str, str], manifest: dict[str, Any]) -> None:
    if set(evidence) != set(manifest["dimensions"]):
        raise PredictionError("分类证据必须覆盖五个维度")
    if any(not isinstance(value, str) or not value.strip() for value in evidence.values()):
        raise PredictionError("分类证据不能为空")


def _score_candidates(
    cases: list[dict[str, Any]], analysis: dict[str, Any], labels: dict[str, str]
) -> list[dict[str, Any]]:
    semantic_scores = analysis.get("semantic_scores", {})
    scored = []
    for case in cases:
        semantic = semantic_scores.get(case["case_id"])
        if not isinstance(semantic, dict) or not isinstance(semantic.get("score"), (int, float)):
            raise PredictionError(f"缺少候选案例语义评分: {case['case_id']}")
        score = float(semantic["score"])
        if not 0 <= score <= 100:
            raise PredictionError(f"语义评分必须在0到100之间: {case['case_id']}")
        reason = semantic.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise PredictionError(f"语义评分理由不能为空: {case['case_id']}")
        category = _category_similarity(labels, case["labels"])
        combined = 0.70 * category + 0.30 * score / 100
        scored.append({
            "case_id": case["case_id"],
            "material_id": case["material_id"],
            "category_similarity": round(category, 8),
            "semantic_similarity": {"score": score, "reason": reason.strip()},
            "combined_similarity": round(combined, 8),
            "weight": round(combined ** 2, 12),
            "commercial_pattern": case["commercial_pattern"],
        })
    return sorted(scored, key=lambda item: (-item["combined_similarity"], item["case_id"]))


def _candidate_cases(
    cases: list[dict[str, Any]],
    classification: dict[str, Any],
    manifest: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    same_pattern = [case for case in cases if case["content_pattern"] == classification["content_pattern"]]
    strict = _is_strict_pattern_model(manifest or {})
    pool = same_pattern if strict or len(same_pattern) >= 5 else cases
    ranked = sorted(
        pool,
        key=lambda case: (
            -_category_similarity(classification["labels"], case["labels"]),
            case["case_id"],
        ),
    )
    count = min(len(ranked), max(20, min(60, 3 * _top_k_count(len(ranked)))))
    return ranked[:count], len(same_pattern)


def _is_strict_pattern_model(manifest: dict[str, Any]) -> bool:
    model = manifest.get("content_pattern_model", {})
    return model.get("type") == "prototype-v1" and model.get("candidate_scope") == "same_pattern_only"


def _minimum_support(manifest: dict[str, Any]) -> int:
    if not _is_strict_pattern_model(manifest):
        return 5
    return int(manifest["content_pattern_model"].get("min_support_after_exclusion", 2))


def _exclude_current_cases(
    cases: list[dict[str, Any]], video: dict[str, Any]
) -> list[dict[str, Any]]:
    material_id = str(video["material_id"])
    fingerprint = stable_hash(video)
    return [
        case
        for case in cases
        if str(case["material_id"]) != material_id
        and case.get("content_fingerprint") != fingerprint
    ]


def _category_similarity(current: dict[str, str], historical: dict[str, str]) -> float:
    return round(sum(
        weight for dimension, weight in DIMENSION_WEIGHTS.items()
        if current[dimension] == historical[dimension]
    ), 8)


def _top_k_count(count: int) -> int:
    if count == 0:
        raise PredictionError("活动案例库为空")
    return min(count, max(5, min(20, math.ceil(math.sqrt(count)))))


def _predict_metrics(
    top_k: list[dict[str, Any]],
    same_pattern: list[dict[str, Any]],
    all_cases: list[dict[str, Any]],
    manifest: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    case_map = {case["case_id"]: case for case in all_cases}
    results = {}
    fallbacks = []
    minimum = _minimum_support(manifest)
    strict = _is_strict_pattern_model(manifest)
    for metric, target in manifest["prediction_targets"].items():
        weighted = [
            (case_map[item["case_id"]]["metric_states"][metric], item["weight"])
            for item in top_k
            if metric in case_map[item["case_id"]].get("metric_states", {})
        ]
        if len(weighted) >= minimum:
            results[metric] = {**target, **build_metric_prediction(weighted, source="top_k")}
            continue
        pattern_values = _unweighted_states(same_pattern, metric)
        if len(pattern_values) >= minimum:
            results[metric] = {
                **target,
                **build_metric_prediction(pattern_values, source="content_pattern"),
            }
            fallbacks.append(f"{metric}:CONTENT_PATTERN_FALLBACK")
            continue
        global_values = [] if strict else _unweighted_states(all_cases, metric)
        if not strict and len(global_values) >= 20:
            results[metric] = {**target, **build_metric_prediction(global_values, source="global")}
            fallbacks.append(f"{metric}:GLOBAL_FALLBACK")
            continue
        results[metric] = {**target, "status": "not_available", "source": "none", "sample_count": len(global_values)}
        fallbacks.append(f"{metric}:NOT_AVAILABLE")
    return results, fallbacks


def _unweighted_states(
    cases: list[dict[str, Any]], metric: str
) -> list[tuple[dict[str, Any], float]]:
    return [
        (case["metric_states"][metric], 1.0)
        for case in cases if metric in case.get("metric_states", {})
    ]


def _predict_primitives(
    top_k: list[dict[str, Any]],
    same_pattern: list[dict[str, Any]],
    all_cases: list[dict[str, Any]],
    manifest: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    case_map = {case["case_id"]: case for case in all_cases}
    results = {}
    fallbacks = []
    minimum = _minimum_support(manifest)
    strict = _is_strict_pattern_model(manifest)
    for field in RAW_FIELDS:
        weighted = [
            (_case_raw_value(case_map[item["case_id"]], field, manifest), item["weight"])
            for item in top_k
            if _case_raw_value(case_map[item["case_id"]], field, manifest) is not None
        ]
        if len(weighted) >= minimum:
            results[field] = build_primitive_prediction(weighted, source="top_k")
            continue
        pattern_values = _unweighted_primitives(same_pattern, field, manifest)
        if len(pattern_values) >= minimum:
            results[field] = build_primitive_prediction(
                pattern_values, source="content_pattern"
            )
            fallbacks.append(f"{field}:CONTENT_PATTERN_PRIMITIVE_FALLBACK")
            continue
        global_values = [] if strict else _unweighted_primitives(all_cases, field, manifest)
        if not strict and len(global_values) >= 20:
            results[field] = build_primitive_prediction(global_values, source="global")
            fallbacks.append(f"{field}:GLOBAL_PRIMITIVE_FALLBACK")
            continue
        results[field] = {
            "status": "not_available",
            "source": "none",
            "sample_count": len(global_values),
        }
        fallbacks.append(f"{field}:PRIMITIVE_NOT_AVAILABLE")
    return results, fallbacks


def _unweighted_primitives(
    cases: list[dict[str, Any]], field: str, manifest: dict[str, Any]
) -> list[tuple[float, float]]:
    return [
        (float(value), 1.0)
        for case in cases
        if (value := _case_raw_value(case, field, manifest)) is not None
    ]


def _case_raw_value(
    case: dict[str, Any], field: str, manifest: dict[str, Any]
) -> float | None:
    actual_raw = case.get("actual_raw", {})
    source_field = manifest["source_fields"][field]
    return actual_raw.get(field, actual_raw.get(source_field))


def _distribution(
    values: list[tuple[float, float]], target: dict[str, Any], source: str
) -> dict[str, Any]:
    observations, weights = zip(*values)
    return {
        **target,
        "status": "available",
        "source": source,
        "sample_count": len(values),
        "p25": round(_weighted_quantile(observations, weights, 0.25), 8),
        "p50": round(_weighted_quantile(observations, weights, 0.50), 8),
        "p75": round(_weighted_quantile(observations, weights, 0.75), 8),
    }


def _weighted_quantile(values: tuple[float, ...], weights: tuple[float, ...], quantile: float) -> float:
    if sum(weights) <= 0:
        weights = tuple(1.0 for _ in weights)
    ordered = sorted(zip(values, weights), key=lambda item: item[0])
    threshold = sum(weights) * quantile
    cumulative = 0.0
    for value, weight in ordered:
        cumulative += weight
        if cumulative >= threshold:
            return float(value)
    return float(ordered[-1][0])


def _commercial_probabilities(
    top_k: list[dict[str, Any]],
    same_pattern: list[dict[str, Any]],
    all_cases: list[dict[str, Any]],
    manifest: dict[str, Any],
) -> dict[str, float]:
    weighted = [(item["commercial_pattern"], item["weight"]) for item in top_k]
    total = sum(weight for _, weight in weighted)
    if total <= 0:
        weighted = [(name, 1.0) for name, _ in weighted]
        total = len(weighted)
    names = [item["name"] for item in manifest["commercial_patterns"]]
    values = {name: 0.0 for name in names}
    for name, weight in weighted:
        values[name] += weight
    return {name: round(value / total, 12) for name, value in values.items()}


def _commercial_dimension_predictions(
    top_k: list[dict[str, Any]], all_cases: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    case_map = {case["case_id"]: case for case in all_cases}
    dimensions = ("attraction", "conversion", "efficiency", "scale", "quality")
    results = {}
    for dimension in dimensions:
        weighted = [
            (case_map[item["case_id"]]["commercial_levels"][dimension], item["weight"])
            for item in top_k
        ]
        if sum(weight for _, weight in weighted) <= 0:
            weighted = [(value, 1.0) for value, _ in weighted]
        total = sum(weight for _, weight in weighted)
        levels = range(0 if dimension == "quality" else 1, 6)
        probabilities = {
            str(level): sum(weight for value, weight in weighted if value == level) / total
            for level in levels
        }
        values, weights = zip(*weighted)
        p25 = int(_weighted_quantile(values, weights, 0.25))
        p50 = int(_weighted_quantile(values, weights, 0.50))
        p75 = int(_weighted_quantile(values, weights, 0.75))
        results[dimension] = {
            "level_probabilities": probabilities,
            "level_p25": p25,
            "level_p50": p50,
            "level_p75": p75,
            "predicted_level": p50,
            "confidence": "high" if len(weighted) >= 20 else "medium" if len(weighted) >= 5 else "low",
            "defined_probability": 1.0 - probabilities.get("0", 0.0),
        }
    return results


def _highest_priority_probability(probabilities: dict[str, float], manifest: dict[str, Any]) -> str:
    maximum = max(probabilities.values())
    candidates = {name for name, value in probabilities.items() if value == maximum}
    return next(item["name"] for item in manifest["commercial_patterns"] if item["name"] in candidates)


def _confidence(same_pattern_count: int, top_k: list[dict[str, Any]], fallbacks: list[str]) -> str:
    average = sum(item["combined_similarity"] for item in top_k) / len(top_k)
    if same_pattern_count >= 20 and not fallbacks and average >= 0.80:
        return "high"
    if same_pattern_count >= 5 and average >= 0.60:
        return "medium"
    return "low"


def _format_value(value: float, unit: str) -> str:
    if unit == "rate":
        return f"{value:.2%}"
    return f"{value:.2f}"


def _render_prediction_markdown_v2(prediction: dict[str, Any]) -> str:
    classification = prediction["content_classification"]
    labels = classification["labels"]
    lines = [
        "# 茶叶视频发布前商业预测",
        "",
        f"Content Pattern：{classification['content_pattern']}（{classification['content_pattern_name']}）",
        f"H/P/S/A/F：{labels['core_hook']} / {labels['pain_point']} / {labels['main_selling_point']} / {labels['audience_angle']} / {labels['content_form']}",
        "",
        "| 商业指标 | 出值概率 | 有值时P50 | 有值时P25–P75 |",
        "|---|---:|---:|---:|",
    ]
    for metric in prediction["metric_predictions"].values():
        if metric["status"] == "not_available":
            lines.append(f"| {metric['display_name']} | 数据不足 | 数据不足 | 数据不足 |")
            continue
        interval = metric["positive_interval"]
        positive = metric["state_probabilities"]["positive"]
        if interval:
            lines.append(
                f"| {metric['display_name']} | {positive:.1%} | {_format_value(interval['p50'], metric['unit'])} | "
                f"{_format_value(interval['p25'], metric['unit'])}–{_format_value(interval['p75'], metric['unit'])} |"
            )
        else:
            lines.append(f"| {metric['display_name']} | {positive:.1%} | 无正值样本 | 无正值样本 |")
    lines.extend(["", "## Commercial五维Level", "", "| 维度 | P25–P75 | 预测Level | 置信度 |", "|---|---:|---:|---|"])
    for name, dimension in prediction["commercial_dimension_predictions"].items():
        lines.append(
            f"| {name.title()} | {dimension['level_p25']}–{dimension['level_p75']} | {dimension['predicted_level']} | {dimension['confidence']} |"
        )
    predicted = prediction["predicted_commercial_pattern"]
    probability = prediction["commercial_pattern_probabilities"][predicted]
    lines.extend(
        [
            "",
            f"预测Commercial Pattern：{predicted}（{probability:.1%}）",
            f"置信度：{prediction['confidence']}",
            f"参考案例：{len(prediction['top_k'])}条",
            "",
            "| Commercial Pattern | 概率 |",
            "|---|---:|",
        ]
    )
    lines.extend(
        f"| {name} | {value:.1%} |"
        for name, value in prediction["commercial_pattern_probabilities"].items()
    )
    if prediction["warnings"]:
        lines.append(f"警告：{'；'.join(prediction['warnings'])}")
    lines.extend(["", "关键依据："])
    lines.extend(f"- {item}" for item in prediction["key_evidence"])
    snapshot_target = prediction["snapshot_path"].replace("\\", "/")
    lines.extend(["", f"[打开完整预测快照]({snapshot_target})"])
    return "\n".join(lines)


def render_prediction_markdown(prediction: dict[str, Any]) -> str:
    if prediction.get("schema_version") not in {"3.0", "4.0"}:
        return _render_prediction_markdown_v2(prediction)
    classification = prediction["content_classification"]
    labels = classification["labels"]
    lines = [
        "# 茶叶视频发布前商业预测",
        "",
        f"Content Pattern：{classification['content_pattern']}（{classification['content_pattern_name']}）",
        f"H/P/S/A/F：{labels['core_hook']} / {labels['pain_point']} / {labels['main_selling_point']} / {labels['audience_angle']} / {labels['content_form']}",
        "",
        "| 商业指标 | Overall P50 | 出值概率（positive） | 正值条件P50 | 正值条件P25–P75 |",
        "|---|---:|---:|---:|---:|",
    ]
    for metric in prediction["metric_predictions"].values():
        point = metric["overall_point_prediction"]
        overall = _format_point_prediction(point, metric["unit"])
        probability = metric.get("state_probabilities", {}).get("positive")
        probability_text = f"{probability:.1%}" if probability is not None else "数据不足"
        interval = metric.get("positive_interval")
        positive_p50 = _format_value(interval["p50"], metric["unit"]) if interval else "无正值样本"
        positive_range = (
            f"{_format_value(interval['p25'], metric['unit'])}–{_format_value(interval['p75'], metric['unit'])}"
            if interval else "无正值样本"
        )
        lines.append(
            f"| {metric['display_name']} | {overall} | {probability_text} | {positive_p50} | {positive_range} |"
        )
    lines.extend(["", "## Commercial五维Level", "", "| 维度 | P25–P75 | 预测Level | 置信度 |", "|---|---:|---:|---|"])
    for name, dimension in prediction["commercial_dimension_predictions"].items():
        lines.append(
            f"| {name.title()} | {dimension['level_p25']}–{dimension['level_p75']} | {dimension['predicted_level']} | {dimension['confidence']} |"
        )
    predicted = prediction["predicted_commercial_pattern"]
    probability = prediction["commercial_pattern_probabilities"][predicted]
    lines.extend([
        "",
        f"预测Commercial Pattern：{predicted}（{probability:.1%}）",
        f"置信度：{prediction['confidence']}",
        f"参考案例：{len(prediction['top_k'])}条",
        "",
        "| Commercial Pattern | 概率 |",
        "|---|---:|",
    ])
    lines.extend(
        f"| {name} | {value:.1%} |"
        for name, value in prediction["commercial_pattern_probabilities"].items()
    )
    if prediction["warnings"]:
        lines.append(f"警告：{'；'.join(prediction['warnings'])}")
    lines.extend(["", "关键依据："])
    lines.extend(f"- {item}" for item in prediction["key_evidence"])
    snapshot_target = prediction["snapshot_path"].replace("\\", "/")
    lines.extend(["", f"[打开完整预测快照]({snapshot_target})"])
    return "\n".join(lines)


def _format_point_prediction(point: dict[str, Any], unit: str) -> str:
    if point.get("status") == "available":
        return _format_value(float(point["value"]), unit)
    reason = point.get("reason", point.get("status", "not_available"))
    return f"undefined（{reason}）"


def main() -> None:
    parser = argparse.ArgumentParser()
    add_data_root_argument(parser)
    parser.add_argument("--video-file", required=True)
    parser.add_argument("--analysis-file", required=True)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--prepared-output")
    parser.add_argument("--legacy-internal-v3", action="store_true")
    args = parser.parse_args()
    args.data_root = str(resolved_data_root(args))
    if not args.legacy_internal_v3:
        raise SystemExit(
            "predict.py仅供离线评估和历史v3兼容；正式盲预测请使用blind_prediction.py"
        )
    video = json.loads(Path(args.video_file).read_text(encoding="utf-8"))
    analysis = json.loads(Path(args.analysis_file).read_text(encoding="utf-8"))
    if args.prepare_only:
        prepared = prepare_prediction_candidates(args.data_root, video, analysis)
        if not args.prepared_output:
            raise SystemExit("--prepare-only必须提供--prepared-output")
        Path(args.prepared_output).write_text(
            json.dumps(prepared, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps({
            "material_id": prepared["material_id"],
            "candidate_count": len(prepared["candidates"]),
            "prepared_output": str(Path(args.prepared_output).resolve()),
        }, ensure_ascii=False))
        return
    print(render_prediction_markdown(predict_video(args.data_root, video, analysis)))


if __name__ == "__main__":
    main()
