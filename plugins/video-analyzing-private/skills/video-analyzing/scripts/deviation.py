from __future__ import annotations

from typing import Any


MIN_TOLERANCE = 1e-9


def analyze_metric_deviation(
    prediction: dict[str, Any],
    actual: dict[str, Any],
    *,
    direction: str = "high",
    prediction_schema_version: str = "2.0",
) -> dict[str, Any]:
    point_analysis = _point_analysis(prediction, actual, prediction_schema_version)
    state = actual["state"]
    if prediction.get("status") != "available" or state == "undefined":
        return _result("not_evaluable", "not_applicable", None, "unknown", "unknown", point_analysis)
    if state == "zero":
        return _result("not_positive", "not_applicable", None, "neutral", "not_applicable", point_analysis)
    interval = prediction.get("positive_interval")
    if not interval:
        return _result("positive", "not_evaluable", None, "unknown", "unknown", point_analysis)
    value = float(actual["value"])
    p25, p75 = float(interval["p25"]), float(interval["p75"])
    width = max(p75 - p25, MIN_TOLERANCE)
    if value < p25:
        score, magnitude = (p25 - value) / width, "below"
    elif value > p75:
        score, magnitude = (value - p75) / width, "above"
    else:
        return _result("positive", "within", 0.0, "neutral", "within", point_analysis)
    performance = _performance_direction(magnitude, direction)
    return _result("positive", magnitude, score, performance, _severity(score), point_analysis)


def _point_analysis(
    prediction: dict[str, Any], actual: dict[str, Any], schema_version: str
) -> dict[str, Any]:
    if schema_version in {"3.0", "4.0"}:
        point = prediction.get("overall_point_prediction")
    else:
        interval = prediction.get("positive_interval")
        point = {"status": "available", "value": interval["p50"]} if interval else None
    if not point or point.get("status") != "available" or point.get("value") is None:
        return {
            "status": "not_evaluable",
            "prediction_value": None,
            "absolute_error": None,
            "relative_error": None,
            "reason": (point or {}).get("reason", "prediction_not_available"),
        }
    if actual.get("state") == "undefined" or actual.get("value") is None:
        return {
            "status": "not_evaluable",
            "prediction_value": float(point["value"]),
            "absolute_error": None,
            "relative_error": None,
            "reason": "actual_undefined",
        }
    actual_value = float(actual["value"])
    prediction_value = float(point["value"])
    absolute_error = abs(prediction_value - actual_value)
    if actual_value == 0:
        return {
            "status": "absolute_only",
            "prediction_value": prediction_value,
            "absolute_error": absolute_error,
            "relative_error": None,
            "reason": "actual_zero",
        }
    return {
        "status": "evaluable",
        "prediction_value": prediction_value,
        "absolute_error": absolute_error,
        "relative_error": absolute_error / abs(actual_value),
        "reason": None,
    }


def _performance_direction(magnitude: str, direction: str) -> str:
    is_positive = (magnitude == "above" and direction == "high") or (
        magnitude == "below" and direction == "low"
    )
    return "positive" if is_positive else "negative"


def _severity(score: float) -> str:
    if score <= 0.5:
        return "mild"
    if score <= 1.0:
        return "medium"
    return "severe"


def _result(
    occurrence: str,
    magnitude: str,
    score: float | None,
    direction: str,
    severity: str,
    point_analysis: dict[str, Any],
) -> dict[str, Any]:
    return {
        "occurrence": occurrence,
        "magnitude": magnitude,
        "deviation_score": score,
        "direction": direction,
        "severity": severity,
        "point_analysis": point_analysis,
    }


def analyze_dimension_deviation(
    prediction: dict[str, Any], actual_level: int, confidence: str
) -> dict[str, Any]:
    if actual_level == 0:
        return {"direction": "unknown", "level_gap": None, "confidence": confidence}
    p25 = prediction["level_p25"]
    p75 = prediction["level_p75"]
    center = prediction["predicted_level"]
    direction = "negative" if actual_level < p25 else "positive" if actual_level > p75 else "within"
    return {
        "direction": direction,
        "level_gap": actual_level - center,
        "confidence": confidence,
        "predicted_interval": [p25, p75],
        "predicted_level": center,
        "actual_level": actual_level,
    }
