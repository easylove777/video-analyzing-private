from __future__ import annotations

import math
import re
from typing import Any

from errors import ValidationError


PRIMITIVE_FIELDS = {
    "spend", "shows", "clicks", "pay_orders", "pay_gmv",
    "settle_amount", "settle_orders", "refund_orders",
}

METRIC_FIELDS = {
    "daily_spend", "daily_gmv", "daily_orders", "ctr", "cvr",
    "settle_roi", "settle_cpo", "refund_rate", "settle_rate",
}


def require_fields(value: dict[str, Any], fields: set[str], label: str) -> None:
    missing = fields - set(value)
    if missing:
        raise ValidationError(f"{label}缺少字段: {sorted(missing)}")


def validate_prediction_v2(value: dict[str, Any]) -> None:
    require_fields(
        value,
        {
            "prediction_id",
            "material_id",
            "metric_predictions",
            "commercial_dimension_predictions",
            "predicted_commercial_pattern",
        },
        "prediction v2",
    )
    if value.get("schema_version") != "2.0":
        raise ValidationError("prediction schema_version必须为2.0")


def validate_prediction(value: dict[str, Any]) -> None:
    version = value.get("schema_version")
    if version == "2.0":
        validate_prediction_v2(value)
        return
    if version == "3.0":
        validate_prediction_v3(value)
        return
    if version == "4.0":
        validate_prediction_v4(value)
        return
    raise ValidationError(f"unsupported prediction schema_version: {version}")


def validate_prediction_v3(value: dict[str, Any]) -> None:
    require_fields(
        value,
        {
            "prediction_id", "material_id", "metric_predictions",
            "primitive_predictions", "commercial_dimension_predictions",
            "predicted_commercial_pattern",
        },
        "prediction v3",
    )
    primitives = value["primitive_predictions"]
    if not isinstance(primitives, dict) or set(primitives) != PRIMITIVE_FIELDS:
        raise ValidationError("prediction v3 primitive_predictions incomplete")
    for name, primitive in primitives.items():
        _validate_primitive(name, primitive)
    metrics = value["metric_predictions"]
    if not isinstance(metrics, dict) or set(metrics) != METRIC_FIELDS:
        raise ValidationError("prediction v3 metric_predictions incomplete")
    for name, metric in metrics.items():
        if not isinstance(metric, dict):
            raise ValidationError(f"prediction v3 metric invalid: {name}")
        _validate_point(name, metric.get("overall_point_prediction"))


def validate_prediction_v4(value: dict[str, Any]) -> None:
    validate_prediction_v3({**value, "schema_version": "3.0"})
    provenance = value.get("blind_provenance")
    if not isinstance(provenance, dict):
        raise ValidationError("prediction v4 blind_provenance missing")
    required = {
        "blind_status",
        "run_id",
        "receipt_sha256",
        "prompt_version",
        "scorer_identity",
        "classification_request_sha256",
        "classification_response_sha256",
        "similarity_request_sha256",
        "similarity_response_sha256",
    }
    if set(provenance) != required:
        raise ValidationError("prediction v4 blind_provenance fields invalid")
    if provenance.get("blind_status") != "isolated":
        raise ValidationError("prediction v4 blind_status must be isolated")
    for field in ("run_id", "prompt_version", "scorer_identity"):
        if not isinstance(provenance.get(field), str) or not provenance[field].strip():
            raise ValidationError(f"prediction v4 blind provenance invalid: {field}")
    for field in (
        "receipt_sha256",
        "classification_request_sha256",
        "classification_response_sha256",
        "similarity_request_sha256",
        "similarity_response_sha256",
    ):
        value_hash = provenance.get(field)
        if not isinstance(value_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", value_hash):
            raise ValidationError(f"prediction v4 blind provenance invalid: {field}")


def _validate_primitive(name: str, primitive: Any) -> None:
    if not isinstance(primitive, dict):
        raise ValidationError(f"primitive prediction invalid: {name}")
    status = primitive.get("status")
    if not isinstance(primitive.get("source"), str) or not primitive["source"].strip():
        raise ValidationError(f"primitive prediction source invalid: {name}")
    sample_count = primitive.get("sample_count")
    if isinstance(sample_count, bool) or not isinstance(sample_count, int) or sample_count < 0:
        raise ValidationError(f"primitive prediction sample_count invalid: {name}")
    if status == "not_available":
        return
    if status != "available":
        raise ValidationError(f"primitive prediction status invalid: {name}")
    if sample_count == 0:
        raise ValidationError(f"primitive prediction sample_count invalid: {name}")
    values = [primitive.get(key) for key in ("p25", "p50", "p75")]
    if any(
        isinstance(item, bool)
        or not isinstance(item, (int, float))
        or item < 0
        or not math.isfinite(item)
        for item in values
    ):
        raise ValidationError(f"primitive prediction quantiles invalid: {name}")
    if values != sorted(values):
        raise ValidationError(f"primitive prediction quantiles unordered: {name}")


def _validate_point(name: str, point: Any) -> None:
    if not isinstance(point, dict):
        raise ValidationError(f"overall point missing: {name}")
    status = point.get("status")
    if status == "available":
        value = point.get("value")
        state = point.get("state")
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or value < 0
            or not math.isfinite(value)
        ):
            raise ValidationError(f"overall point value invalid: {name}")
        if name in {"ctr", "cvr", "refund_rate", "settle_rate"} and value > 1:
            raise ValidationError(f"overall point rate out of range: {name}")
        expected = "positive" if value > 0 else "zero"
        if state != expected:
            raise ValidationError(f"overall point state invalid: {name}")
        if not isinstance(point.get("derivation"), str) or not point["derivation"].strip():
            raise ValidationError(f"overall point derivation invalid: {name}")
        return
    if status not in {"undefined", "not_available"}:
        raise ValidationError(f"overall point status invalid: {name}")
    if (
        point.get("state") != "undefined"
        or point.get("value") is not None
        or not str(point.get("reason", "")).strip()
    ):
        raise ValidationError(f"overall point reason invalid: {name}")


def validate_observation(value: dict[str, Any]) -> None:
    require_fields(
        value,
        {"observation_id", "prediction_id", "material_id", "patterns", "advice_codes"},
        "Observation",
    )
