from __future__ import annotations

from typing import Any


DIMENSION_METRICS = {
    "attraction": ("ctr",),
    "conversion": ("cvr",),
    "efficiency": ("settle_roi", "settle_cpo"),
    "scale": ("daily_spend", "daily_gmv", "daily_orders"),
    "quality": ("settle_rate", "refund_rate"),
}

CONTENT_ELEMENTS = {
    "attraction": ["H", "F", "A"],
    "conversion": ["S", "P", "A"],
    "efficiency": ["S", "P", "F"],
    "scale": ["A", "F", "H"],
    "quality": ["S", "P", "F"],
}


def build_advice(
    metrics: dict[str, dict[str, Any]], dimensions: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    negative_dimensions = []
    positive_dimensions = []
    for dimension, analysis in dimensions.items():
        if _should_optimize(dimension, analysis, metrics):
            negative_dimensions.append(dimension)
        elif analysis.get("direction") == "positive":
            positive_dimensions.append(dimension)
    if negative_dimensions:
        codes = [f"OPTIMIZE_{dimension.upper()}" for dimension in negative_dimensions]
        codes.append("CALIBRATE_POSITIVE_INTERVAL")
        return {
            "overall": "negative_outlier",
            "advice_codes": codes,
            "content_execution_advice": [
                {
                    "dimension": dimension,
                    "content_elements": CONTENT_ELEMENTS[dimension],
                    "action": "根据偏差证据优化下一条视频",
                }
                for dimension in negative_dimensions
            ],
            "prediction_calibration_proposal": ["CALIBRATE_POSITIVE_INTERVAL"],
        }
    if positive_dimensions or any(item.get("direction") == "positive" for item in metrics.values()):
        return {
            "overall": "positive_outlier",
            "advice_codes": ["REPLICATE_SUCCESS"],
            "content_execution_advice": [],
            "prediction_calibration_proposal": [],
        }
    return {
        "overall": "prediction_hit",
        "advice_codes": ["NO_CHANGE_SAMPLE_ONLY"],
        "content_execution_advice": [],
        "prediction_calibration_proposal": [],
    }


def _should_optimize(
    dimension: str,
    analysis: dict[str, Any],
    metrics: dict[str, dict[str, Any]],
) -> bool:
    if analysis.get("direction") != "negative":
        return False
    if analysis.get("confidence") == "low" and analysis.get("level_gap", 0) > -2:
        return False
    if analysis.get("level_gap") is not None and analysis["level_gap"] <= -2:
        return True
    negative = [
        metrics[name]
        for name in DIMENSION_METRICS[dimension]
        if name in metrics and metrics[name].get("direction") == "negative"
    ]
    if any(item.get("severity") == "severe" for item in negative):
        return True
    return len(negative) >= 2
