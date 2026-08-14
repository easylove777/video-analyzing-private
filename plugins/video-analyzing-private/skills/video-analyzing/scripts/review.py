from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

from advice import build_advice
from config import DEFAULT_CONFIG
from deviation import analyze_dimension_deviation, analyze_metric_deviation
from errors import ValidationError
from metric_states import calculate_metric_states
from observations import create_observation
from reporting import render_review_markdown
from runtime_paths import add_data_root_argument, resolved_data_root
from store import (
    file_transaction,
    initialize_data_root,
    read_json,
    stable_hash,
    write_json_atomic,
    write_text_atomic,
)
from validate_classifications import calculate_commercial_result
from schemas import validate_prediction


ACTUAL_FIELDS = (
    "spend_7d",
    "impressions_7d",
    "clicks_7d",
    "pay_orders_7d",
    "pay_gmv_7d",
    "settle_amount_7d",
    "settle_orders_7d",
    "refund_orders_7d",
)


def evaluate_quality_gate(actual: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    _validate_actual(actual)
    gate = config["quality_gate"]
    checks = {
        "observed_days": _check(actual["observed_days"], config["review_window_days"]),
        "min_spend": _check(actual["spend_7d"], gate["min_spend"]),
        "min_impressions": _check(actual["impressions_7d"], gate["min_impressions"]),
        "min_clicks": _check(actual["clicks_7d"], gate["min_clicks"]),
        "linkage": {"actual": bool(actual.get("material_id") and actual.get("prediction_id")), "required": True},
    }
    for item in checks.values():
        item["passed"] = item["actual"] >= item["required"] if isinstance(item["required"], (int, float)) else item["actual"] is item["required"]
    return {
        "status": "eligible" if all(item["passed"] for item in checks.values()) else "insufficient_data",
        "checks": checks,
    }


def _check(actual: float, required: float) -> dict[str, Any]:
    return {"actual": actual, "required": required}


def _validate_actual(actual: dict[str, Any]) -> None:
    required = {"material_id", "prediction_id", "observed_days", *ACTUAL_FIELDS}
    missing = required - set(actual)
    if missing:
        raise ValidationError(f"真实数据缺少字段: {sorted(missing)}")
    for field in ("observed_days", *ACTUAL_FIELDS):
        value = actual[field]
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0 or not math.isfinite(value):
            raise ValidationError(f"{field}必须是非负有限数值")


def review_snapshot(
    data_root: str | Path,
    prediction: dict[str, Any],
    actual: dict[str, Any],
    manifest: dict[str, Any],
) -> dict[str, Any]:
    validate_prediction(prediction)
    if prediction.get("schema_version") not in {"2.0", "3.0", "4.0"}:
        raise ValidationError("历史v1预测快照缺少三态概率和五维分布，不能进入正式复盘")
    if prediction["prediction_id"] != actual.get("prediction_id"):
        raise ValidationError("真实数据与prediction_id不一致")
    if prediction["material_id"] != actual.get("material_id"):
        raise ValidationError("真实数据与material_id不一致")
    root = Path(data_root)
    initialize_data_root(root)
    config = DEFAULT_CONFIG if not (root / "config.json").exists() else read_json(root / "config.json")
    gate = evaluate_quality_gate(actual, config)
    base = _review_base(prediction, actual, gate)
    if gate["status"] != "eligible":
        result = {**base, "review_status": "insufficient_data", "advice_codes": []}
        return _save_report(root, result)
    actual_raw = _daily_actual_raw(actual)
    states = calculate_metric_states(actual_raw)
    commercial_input = {
        manifest["source_fields"][canonical]: value for canonical, value in actual_raw.items()
    }
    commercial = calculate_commercial_result(commercial_input, manifest)
    metric_analysis = _metric_analysis(prediction, states, manifest)
    dimension_analysis = _dimension_analysis(prediction, commercial["levels"], actual, config)
    advice = build_advice(metric_analysis, dimension_analysis)
    primary = _primary_dimension(dimension_analysis)
    direction = _overall_direction(advice["overall"])
    patterns = {
        "content_pattern": prediction["content_classification"]["content_pattern"],
        "predicted_commercial_pattern": prediction["predicted_commercial_pattern"],
        "actual_commercial_pattern": commercial["commercial_pattern"],
        "transition": f"{prediction['predicted_commercial_pattern']}->{commercial['commercial_pattern']}",
    }
    review = {
        **base,
        "review_status": "completed",
        "actual_data_fingerprint": stable_hash(actual),
        "patterns": patterns,
        "metric_analysis": metric_analysis,
        "dimension_analysis": dimension_analysis,
        "actual_metric_states": states,
        "actual_commercial_levels": commercial["levels"],
        "primary_deviation_dimension": primary,
        "direction": direction,
        "content_execution_advice": advice["content_execution_advice"],
        "prediction_calibration_proposal": advice["prediction_calibration_proposal"],
        "advice_codes": advice["advice_codes"],
        "sample_pool_action": _sample_pool_action(advice["overall"]),
        "evidence_consistency": _evidence_consistency(
            primary, direction, metric_analysis, dimension_analysis
        ),
        "is_valid_for_sedimentation": _is_valid_for_sedimentation(
            primary, dimension_analysis
        ),
        "candidate_case": _candidate_case(
            prediction, commercial_input, states, commercial
        ),
    }
    return _save_report(root, create_observation(root, review))


def _review_base(
    prediction: dict[str, Any], actual: dict[str, Any], gate: dict[str, Any]
) -> dict[str, Any]:
    return {
        "review_schema_version": "1.0",
        "review_algorithm_version": (
            "2.0.0" if prediction.get("schema_version") in {"3.0", "4.0"} else "1.0.1"
        ),
        "material_id": prediction["material_id"],
        "prediction_id": prediction["prediction_id"],
        "review_window_days": actual["observed_days"],
        "rule_version": prediction["rule_version"],
        "manifest_sha256": prediction["manifest_sha256"],
        "case_library_sha256": prediction["case_library_sha256"],
        "quality_gate": gate,
    }


def _daily_actual_raw(actual: dict[str, Any]) -> dict[str, float]:
    days = float(actual["observed_days"])
    return {
        "spend": actual["spend_7d"] / days,
        "shows": actual["impressions_7d"] / days,
        "clicks": actual["clicks_7d"] / days,
        "pay_orders": actual["pay_orders_7d"] / days,
        "pay_gmv": actual["pay_gmv_7d"] / days,
        "settle_amount": actual["settle_amount_7d"] / days,
        "settle_orders": actual["settle_orders_7d"] / days,
        "refund_orders": actual["refund_orders_7d"] / days,
    }


def _metric_analysis(
    prediction: dict[str, Any],
    states: dict[str, dict[str, Any]],
    manifest: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    return {
        name: {
            **analyze_metric_deviation(
                prediction["metric_predictions"][name],
                states[name],
                direction=manifest["commercial_metrics"][name]["direction"],
                prediction_schema_version=prediction.get("schema_version", "2.0"),
            ),
            "actual": states[name],
            "prediction": prediction["metric_predictions"][name],
        }
        for name in manifest["prediction_targets"]
    }


def _dimension_analysis(
    prediction: dict[str, Any],
    actual_levels: dict[str, int],
    actual: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    confidence = {
        "attraction": "high" if actual["impressions_7d"] >= 1000 else "low",
        "conversion": "high" if actual["clicks_7d"] >= config["dimension_confidence"]["conversion_high_clicks"] else "low",
        "efficiency": "high" if actual["settle_orders_7d"] > 0 else "medium",
        "scale": "high",
        "quality": "high" if actual["pay_orders_7d"] >= 5 else "low" if actual["pay_orders_7d"] > 0 else "unknown",
    }
    return {
        name: analyze_dimension_deviation(
            prediction["commercial_dimension_predictions"][name],
            actual_levels[name],
            confidence[name],
        )
        for name in ("attraction", "conversion", "efficiency", "scale", "quality")
    }


def _primary_dimension(dimensions: dict[str, dict[str, Any]]) -> str:
    deviating = [
        (name, abs(item.get("level_gap") or 0))
        for name, item in dimensions.items()
        if item.get("direction") in {"negative", "positive"}
    ]
    return max(deviating, key=lambda item: item[1])[0] if deviating else "none"


def _overall_direction(overall: str) -> str:
    return {
        "prediction_hit": "within",
        "positive_outlier": "positive",
        "negative_outlier": "negative",
    }[overall]


def _sample_pool_action(overall: str) -> str:
    return {
        "prediction_hit": "add_as_hit",
        "positive_outlier": "add_as_positive_outlier",
        "negative_outlier": "add_as_negative_outlier",
    }[overall]


def _evidence_consistency(
    primary: str,
    direction: str,
    metrics: dict[str, dict[str, Any]],
    dimensions: dict[str, dict[str, Any]],
) -> float:
    if primary == "none":
        evaluable = [item for item in dimensions.values() if item.get("direction") != "unknown"]
        return sum(item.get("direction") == "within" for item in evaluable) / len(evaluable) if evaluable else 0.0
    from advice import DIMENSION_METRICS

    expected = direction
    signals = [dimensions[primary].get("direction")]
    signals.extend(metrics[name].get("direction") for name in DIMENSION_METRICS[primary] if name in metrics)
    evaluable = [signal for signal in signals if signal not in {None, "unknown", "neutral"}]
    return sum(signal == expected for signal in evaluable) / len(evaluable) if evaluable else 0.0


def _is_valid_for_sedimentation(
    primary: str, dimensions: dict[str, dict[str, Any]]
) -> bool:
    if primary != "none":
        return dimensions[primary].get("confidence") in {"high", "medium"}
    usable = sum(
        item.get("confidence") in {"high", "medium"} for item in dimensions.values()
    )
    return usable >= 3


def _save_report(root: Path, review: dict[str, Any]) -> dict[str, Any]:
    report_id = review.get("observation_id") or f"pending_{stable_hash(review)[:16]}"
    report_root = root / "reports" / review["material_id"]
    markdown_path = report_root / f"{report_id}.md"
    snapshot_path = report_root / f"{report_id}.json"
    result = {
        **review,
        "report_path": str(markdown_path.resolve()),
        "review_snapshot_path": str(snapshot_path.resolve()),
    }
    with file_transaction(root, (snapshot_path, markdown_path)):
        write_json_atomic(snapshot_path, result)
        write_text_atomic(markdown_path, render_review_markdown(result))
    return result


def _candidate_case(
    prediction: dict[str, Any],
    actual_raw: dict[str, float],
    states: dict[str, dict[str, Any]],
    commercial: dict[str, Any],
) -> dict[str, Any]:
    identity = stable_hash([prediction["prediction_id"], actual_raw])
    classification = prediction["content_classification"]
    return {
        "case_id": f"case_{identity[:20]}",
        "material_id": prediction["material_id"],
        "case_version": "2",
        "rule_version": prediction["rule_version"],
        "rule_manifest_sha256": prediction["manifest_sha256"],
        "source_hashes": {"prediction": prediction["prediction_id"]},
        "content_fingerprint": prediction["content_fingerprint"],
        "video_content": prediction["video_content"],
        "labels": classification["labels"],
        "evidence": prediction.get("evidence", {}),
        "content_pattern_scores": classification.get("scores", {}),
        "content_pattern": classification["content_pattern"],
        "content_pattern_decision": classification.get("decision", "prediction_snapshot"),
        "actual_raw": actual_raw,
        "actual_metrics": commercial["metrics"],
        "metric_states": states,
        "commercial_levels": commercial["levels"],
        "commercial_pattern": commercial["commercial_pattern"],
    }


def review_prediction(
    data_root: str | Path,
    prediction_id: str,
    actual: dict[str, Any],
) -> dict[str, Any]:
    root = Path(data_root)
    matches = list((root / "predictions").glob(f"*/{prediction_id}.json"))
    if len(matches) != 1:
        raise ValidationError(f"prediction_id必须唯一匹配一个快照: {prediction_id}")
    prediction = read_json(matches[0])
    manifest = _load_prediction_manifest(root, prediction["manifest_sha256"])
    return review_snapshot(root, prediction, actual, manifest)


def _load_prediction_manifest(root: Path, manifest_hash: str) -> dict[str, Any]:
    for path in (root / "rules" / "manifests").glob("*.json"):
        manifest = read_json(path)
        if stable_hash(manifest) == manifest_hash:
            return manifest
    raise ValidationError("找不到预测快照绑定的历史manifest")


def main() -> None:
    parser = argparse.ArgumentParser()
    add_data_root_argument(parser)
    parser.add_argument("--prediction-id", required=True)
    parser.add_argument("--actual-file", required=True)
    args = parser.parse_args()
    args.data_root = str(resolved_data_root(args))
    actual = json.loads(Path(args.actual_file).read_text(encoding="utf-8"))
    result = review_prediction(args.data_root, args.prediction_id, actual)
    print(Path(result["report_path"]).read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
