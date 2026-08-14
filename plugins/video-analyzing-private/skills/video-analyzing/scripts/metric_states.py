from __future__ import annotations

import math
from typing import Any, Iterable

from errors import ValidationError


RAW_FIELDS = (
    "spend",
    "shows",
    "clicks",
    "pay_orders",
    "pay_gmv",
    "settle_amount",
    "settle_orders",
    "refund_orders",
)

DIRECT_METRICS = {
    "daily_spend": "spend",
    "daily_gmv": "pay_gmv",
    "daily_orders": "pay_orders",
}

RATIO_METRICS = {
    "ctr": ("clicks", "shows", "no_impressions", True),
    "cvr": ("pay_orders", "clicks", "no_clicks", True),
    "settle_roi": ("settle_amount", "spend", "no_spend", False),
    "settle_cpo": ("spend", "settle_orders", "no_settle_order", False),
    "refund_rate": ("refund_orders", "pay_orders", "no_pay_order", True),
    "settle_rate": ("settle_orders", "pay_orders", "no_pay_order", True),
}


def calculate_metric_states(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    values = _validated_raw(raw)
    metrics = {
        "daily_spend": _defined(values["spend"]),
        "daily_gmv": _defined(values["pay_gmv"]),
        "daily_orders": _defined(values["pay_orders"]),
        "ctr": _ratio(values["clicks"], values["shows"], "no_impressions"),
        "cvr": _ratio(values["pay_orders"], values["clicks"], "no_clicks"),
        "settle_roi": _ratio(values["settle_amount"], values["spend"], "no_spend"),
        "settle_cpo": _ratio(values["spend"], values["settle_orders"], "no_settle_order"),
        "refund_rate": _ratio(values["refund_orders"], values["pay_orders"], "no_pay_order"),
        "settle_rate": _ratio(values["settle_orders"], values["pay_orders"], "no_pay_order"),
    }
    return metrics


def _validated_raw(raw: dict[str, Any]) -> dict[str, float]:
    values = {}
    for field in RAW_FIELDS:
        value = raw.get(field)
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or value < 0
            or not math.isfinite(value)
        ):
            raise ValidationError(f"{field}必须是非负有限数值")
        values[field] = float(value)
    return values


def _defined(value: float) -> dict[str, Any]:
    return {"state": "positive" if value > 0 else "zero", "value": value}


def _ratio(numerator: float, denominator: float, reason: str) -> dict[str, Any]:
    if denominator == 0:
        return {"state": "undefined", "value": None, "reason": reason}
    return _defined(round(numerator / denominator, 8))


def build_metric_prediction(
    observations: Iterable[tuple[dict[str, Any], float]], *, source: str
) -> dict[str, Any]:
    rows = [(state, float(weight)) for state, weight in observations if weight >= 0]
    if rows and sum(weight for _, weight in rows) <= 0:
        rows = [(state, 1.0) for state, _ in rows]
    total_weight = sum(weight for _, weight in rows)
    if total_weight <= 0:
        return {"status": "not_available", "reason": "no_weighted_samples"}
    probabilities = {
        state: sum(weight for item, weight in rows if item["state"] == state) / total_weight
        for state in ("undefined", "zero", "positive")
    }
    positive = [(float(item["value"]), weight) for item, weight in rows if item["state"] == "positive"]
    interval = None
    if positive:
        interval = {
            "p25": weighted_quantile(positive, 0.25),
            "p50": weighted_quantile(positive, 0.50),
            "p75": weighted_quantile(positive, 0.75),
        }
    return {
        "status": "available",
        "prediction_mode": "zero_plus_positive",
        "state_probabilities": probabilities,
        "sample_count": len(rows),
        "positive_sample_count": len(positive),
        "positive_interval": interval,
        "source": source,
    }


def build_primitive_prediction(
    observations: Iterable[tuple[float, float]], *, source: str
) -> dict[str, Any]:
    values = [_validated_observation(value, weight) for value, weight in observations]
    if not values:
        return {
            "status": "not_available",
            "reason": "no_weighted_samples",
            "source": source,
            "sample_count": 0,
        }
    if sum(weight for _, weight in values) <= 0:
        values = [(value, 1.0) for value, _ in values]
    return {
        "status": "available",
        "source": source,
        "sample_count": len(values),
        "p25": weighted_quantile(values, 0.25),
        "p50": weighted_quantile(values, 0.50),
        "p75": weighted_quantile(values, 0.75),
    }


def build_overall_metric_points(
    primitives: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    points = {
        metric: _direct_point(primitives, raw_field)
        for metric, raw_field in DIRECT_METRICS.items()
    }
    points.update({
        metric: _ratio_point(primitives, numerator, denominator, reason, clamp_rate)
        for metric, (numerator, denominator, reason, clamp_rate) in RATIO_METRICS.items()
    })
    return points


def _validated_observation(value: float, weight: float) -> tuple[float, float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0 or not math.isfinite(value):
        raise ValidationError("primitive value must be a non-negative finite number")
    if isinstance(weight, bool) or not isinstance(weight, (int, float)) or weight < 0 or not math.isfinite(weight):
        raise ValidationError("primitive weight must be a non-negative finite number")
    return float(value), float(weight)


def _direct_point(
    primitives: dict[str, dict[str, Any]], raw_field: str
) -> dict[str, Any]:
    primitive = primitives.get(raw_field, {})
    if primitive.get("status") != "available":
        return {"status": "not_available", "state": "undefined", "value": None, "reason": f"{raw_field}_not_available"}
    return _available_point(float(primitive["p50"]), f"{raw_field}_p50")


def _ratio_point(
    primitives: dict[str, dict[str, Any]],
    numerator: str,
    denominator: str,
    zero_reason: str,
    clamp_rate: bool,
) -> dict[str, Any]:
    if any(primitives.get(name, {}).get("status") != "available" for name in (numerator, denominator)):
        return {"status": "not_available", "state": "undefined", "value": None, "reason": "primitive_not_available"}
    denominator_value = float(primitives[denominator]["p50"])
    if denominator_value == 0:
        return {"status": "undefined", "state": "undefined", "value": None, "reason": zero_reason}
    value = float(primitives[numerator]["p50"]) / denominator_value
    if clamp_rate:
        value = min(1.0, value)
    return _available_point(value, f"{numerator}_p50 / {denominator}_p50")


def _available_point(value: float, derivation: str) -> dict[str, Any]:
    rounded = round(value, 8)
    return {
        "status": "available",
        "state": "positive" if rounded > 0 else "zero",
        "value": rounded,
        "derivation": derivation,
    }


def weighted_quantile(values: list[tuple[float, float]], quantile: float) -> float:
    if not values:
        raise ValidationError("分位数缺少样本")
    ordered = sorted(values)
    total_weight = sum(weight for _, weight in ordered)
    threshold = total_weight * quantile
    cumulative = 0.0
    for value, weight in ordered:
        cumulative += weight
        if cumulative >= threshold:
            return value
    return ordered[-1][0]
