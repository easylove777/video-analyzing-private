from __future__ import annotations

import ast
import math
import operator
from typing import Any

from content_pattern_clustering import sample_similarity


class ClassificationValidationError(ValueError):
    pass


DIMENSION_WEIGHTS = {
    "core_hook": 0.20,
    "main_selling_point": 0.20,
    "pain_point": 0.20,
    "audience_angle": 0.20,
    "content_form": 0.20,
}

OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: lambda left, right: 0 if right == 0 else left / right,
}


def validate_labels(labels: dict[str, str], manifest: dict[str, Any]) -> dict[str, str]:
    if set(labels) != set(manifest["dimensions"]):
        raise ClassificationValidationError("五维标签字段不完整")
    for dimension, code in labels.items():
        allowed = {item["code"] for item in manifest["dimensions"][dimension]["labels"]}
        if code not in allowed:
            raise ClassificationValidationError(f"{dimension}包含未知标签{code}")
    return dict(labels)


def classify_content_pattern(
    labels: dict[str, str],
    manifest: dict[str, Any],
    video_content: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validated = validate_labels(labels, manifest)
    model = manifest.get("content_pattern_model")
    if model and model.get("type") == "prototype-v1":
        if not isinstance(video_content, dict):
            raise ClassificationValidationError("prototype-v1分类必须提供视频内容")
        return _classify_by_prototype(validated, video_content, manifest, model)
    scores = {
        pattern: sum(weights.get(code, 0) for code in validated.values())
        for pattern, weights in manifest["content_pattern_weights"].items()
    }
    maximum = max(scores.values())
    candidates = sorted(pattern for pattern, score in scores.items() if score == maximum)
    selected, decision = _resolve_pattern_tie(candidates, validated, manifest)
    return {
        "labels": validated,
        "scores": scores,
        "content_pattern": selected,
        "content_pattern_name": manifest["content_patterns"][selected]["name"],
        "decision": decision,
    }


def _classify_by_prototype(
    labels: dict[str, str],
    video_content: dict[str, Any],
    manifest: dict[str, Any],
    model: dict[str, Any],
) -> dict[str, Any]:
    current = {"labels": labels, "video_content": video_content}
    best_by_pattern: dict[str, tuple[float, str]] = {}
    for pattern, profile in model["profiles"].items():
        matches = [
            (sample_similarity(current, prototype), str(prototype["material_id"]))
            for prototype in profile["prototypes"]
        ]
        best_by_pattern[pattern] = max(matches, key=lambda item: (item[0], _reverse_id(item[1])))
    selected = min(
        best_by_pattern,
        key=lambda pattern: (-best_by_pattern[pattern][0], pattern),
    )
    score, material_id = best_by_pattern[selected]
    return {
        "labels": labels,
        "scores": {pattern: value[0] for pattern, value in best_by_pattern.items()},
        "content_pattern": selected,
        "content_pattern_name": manifest["content_patterns"][selected]["name"],
        "decision": "nearest_prototype",
        "match_score": score,
        "matched_material_id": material_id,
    }


def _reverse_id(value: str) -> tuple[int, ...]:
    return tuple(-ord(character) for character in value)


def _resolve_pattern_tie(
    candidates: list[str], labels: dict[str, str], manifest: dict[str, Any]
) -> tuple[str, str]:
    if len(candidates) == 1:
        return candidates[0], "score"
    hook_target = manifest["tie_breakers"]["hook_mapping"].get(labels["core_hook"])
    if hook_target in candidates:
        return hook_target, "hook_mapping"
    selling_target = manifest["tie_breakers"]["selling_mapping"].get(labels["main_selling_point"])
    if selling_target in candidates:
        return selling_target, "selling_mapping"
    return candidates[0], "smallest_code"


def calculate_commercial_result(raw: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    values = _normalize_raw_values(raw, manifest)
    metrics = {
        name: _calculate_metric(spec, values)
        for name, spec in manifest["commercial_metrics"].items()
    }
    levels = _commercial_levels(metrics, values, manifest)
    context = {**metrics, **{f"{key}_level": value for key, value in levels.items()}}
    pattern = _select_commercial_pattern(context, manifest)
    return {"metrics": metrics, "levels": levels, "commercial_pattern": pattern}


def _normalize_raw_values(raw: dict[str, Any], manifest: dict[str, Any]) -> dict[str, float]:
    values = {}
    for canonical, source in manifest["source_fields"].items():
        value = raw.get(source)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0 or not math.isfinite(value):
            raise ClassificationValidationError(f"商业字段{source}必须是非负有限数值")
        values[canonical] = float(value)
    return values


def _calculate_metric(spec: dict[str, Any], values: dict[str, float]) -> float:
    tree = ast.parse(spec["formula"], mode="eval")
    value = float(_evaluate_formula(tree.body, values))
    if spec.get("clamp_rate"):
        value = min(1.0, max(0.0, value))
    return round(value, 8)


def _evaluate_formula(node: ast.AST, values: dict[str, float]) -> float:
    if isinstance(node, ast.Name):
        return values[node.id]
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.BinOp) and type(node.op) in OPERATORS:
        return OPERATORS[type(node.op)](
            _evaluate_formula(node.left, values), _evaluate_formula(node.right, values)
        )
    raise ClassificationValidationError("商业指标公式包含不支持的表达式")


def _commercial_levels(
    metrics: dict[str, float], raw: dict[str, float], manifest: dict[str, Any]
) -> dict[str, int]:
    metric_levels = {
        name: _metric_level(name, value, manifest["commercial_metrics"][name], raw)
        for name, value in metrics.items()
        if manifest["commercial_metrics"][name].get("thresholds")
    }
    efficiency_score = _weighted_level_score(
        manifest["level_config"]["efficiency"], metric_levels
    )
    scale_score = _weighted_level_score(
        manifest["level_config"]["scale"], metric_levels
    )
    if raw["settle_orders"] == 0:
        efficiency = 1
    else:
        efficiency = _score_level(efficiency_score)
    quality_weights = manifest["level_config"]["quality"]
    quality_score = (
        quality_weights["settle_rate"] * metrics["settle_rate"]
        + quality_weights["refund_rate"] * (1 - metrics["refund_rate"])
    )
    quality = 0 if raw["pay_orders"] == 0 else _quality_level(quality_score)
    return {
        "attraction": metric_levels["ctr"],
        "conversion": metric_levels["cvr"],
        "efficiency": efficiency,
        "scale": _score_level(scale_score),
        "quality": quality,
        "spend": metric_levels["daily_spend"],
        "gmv": metric_levels["daily_gmv"],
        "order": metric_levels["daily_orders"],
    }


def _metric_level(name: str, value: float, spec: dict[str, Any], raw: dict[str, float]) -> int:
    if name == "settle_cpo" and raw["settle_orders"] == 0:
        return 1
    thresholds = spec["thresholds"]
    positive = 1 + sum(value > threshold for threshold in thresholds)
    return positive if spec["direction"] == "high" else 6 - positive


def _weighted_level_score(weights: dict[str, float], levels: dict[str, int]) -> float:
    return sum(weight * levels[name] for name, weight in weights.items())


def _score_level(score: float) -> int:
    if score < 1.5:
        return 1
    if score < 2.5:
        return 2
    if score < 3.5:
        return 3
    if score < 4.5:
        return 4
    return 5


def _quality_level(score: float) -> int:
    return min(5, int(score / 0.2) + 1)


def _select_commercial_pattern(context: dict[str, Any], manifest: dict[str, Any]) -> str:
    aliases = {
        "attraction_level": context["attraction_level"],
        "conversion_level": context["conversion_level"],
        "efficiency_level": context["efficiency_level"],
        "scale_level": context["scale_level"],
        "quality_level": context["quality_level"],
        "spend_level": context["spend_level"],
        "daily_orders": context["daily_orders"],
        "daily_gmv": context["daily_gmv"],
    }
    for pattern in manifest["commercial_patterns"]:
        if pattern["condition"] is None or _evaluate_condition(pattern["condition"], aliases):
            return pattern["name"]
    raise ClassificationValidationError("Commercial Pattern没有兜底规则")


def _evaluate_condition(condition: str, values: dict[str, Any]) -> bool:
    for clause in condition.split(" and "):
        match = __import__("re").fullmatch(r"([a-z_]+)(>=|<=|==|>|<)(-?\d+(?:\.\d+)?)", clause.strip())
        if not match:
            raise ClassificationValidationError("Commercial Pattern条件无法解析")
        left, operation, raw_right = match.groups()
        right = float(raw_right)
        comparisons = {
            ">=": operator.ge,
            "<=": operator.le,
            "==": operator.eq,
            ">": operator.gt,
            "<": operator.lt,
        }
        if left not in values or not comparisons[operation](values[left], right):
            return False
    return True
