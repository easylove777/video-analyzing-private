from __future__ import annotations

import ast
from copy import deepcopy
import math
import re
from typing import Any


class RuleValidationError(ValueError):
    pass


ALLOWED_FORMULA_NODES = (
    ast.Expression,
    ast.BinOp,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.Name,
    ast.Load,
    ast.Constant,
)


def validate_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    value = deepcopy(manifest)
    _validate_input_fields(value)
    _validate_dimensions(value)
    _validate_patterns(value)
    _validate_content_pattern_model(value)
    _validate_metrics(value)
    _validate_level_config(value)
    _validate_commercial_patterns(value)
    _validate_targets(value)
    return value


def _validate_input_fields(manifest: dict[str, Any]) -> None:
    expected = {
        "material_id",
        "caption",
        "first_three_seconds",
        "core_hook",
        "pain_point",
        "main_selling_point",
        "audience_angle",
        "content_form",
    }
    fields = manifest.get("input_fields", [])
    names = [field.get("name") for field in fields]
    if set(names) != expected or len(names) != len(expected):
        raise RuleValidationError("八字段输入定义不完整或重复")
    if any(field.get("type") != "string" for field in fields):
        raise RuleValidationError("八字段输入类型必须为string")
    required = {field["name"] for field in fields if field.get("required")}
    if not {"material_id", "caption"} <= required:
        raise RuleValidationError("material_id和caption必须为必填字段")
    fallbacks = manifest.get("field_fallbacks", {})
    dimensions = {"core_hook", "pain_point", "main_selling_point", "audience_angle", "content_form"}
    if set(fallbacks) != dimensions:
        raise RuleValidationError("字段回退关系不完整")
    for dimension, chain in fallbacks.items():
        if not isinstance(chain, list) or not chain or chain[0] != dimension or not set(chain) <= expected:
            raise RuleValidationError(f"{dimension}字段回退关系无效")


def _validate_dimensions(manifest: dict[str, Any]) -> None:
    expected = {"core_hook", "pain_point", "main_selling_point", "audience_angle", "content_form"}
    if set(manifest.get("dimensions", {})) != expected:
        raise RuleValidationError("必须定义五个内容维度")
    all_codes = []
    for name, dimension in manifest["dimensions"].items():
        labels = dimension.get("labels", [])
        codes = [label.get("code") for label in labels]
        if len(codes) != len(set(codes)):
            raise RuleValidationError(f"{name}存在重复标签编码")
        if not labels or not dimension.get("fallback"):
            raise RuleValidationError(f"{name}缺少标签或兜底规则")
        all_codes.extend(codes)
    if len(all_codes) != len(set(all_codes)):
        raise RuleValidationError("不同维度存在重复标签编码")


def _validate_patterns(manifest: dict[str, Any]) -> None:
    patterns = manifest.get("content_patterns", {})
    weights = manifest.get("content_pattern_weights", {})
    if set(patterns) != set(weights):
        raise RuleValidationError("Content Pattern与分值表不一致")
    if manifest.get("tie_breakers", {}).get("order") != [
        "score", "hook_mapping", "selling_mapping", "smallest_code"
    ]:
        raise RuleValidationError("同分裁决顺序无效")
    valid_codes = {
        label["code"]
        for dimension in manifest["dimensions"].values()
        for label in dimension["labels"]
    }
    for pattern, entries in weights.items():
        unknown = set(entries) - valid_codes
        if unknown:
            raise RuleValidationError(f"{pattern}引用未知标签: {sorted(unknown)}")
        if any(not isinstance(score, int) or score < 0 for score in entries.values()):
            raise RuleValidationError(f"{pattern}包含非法分值")
    mapping_dimensions = {"hook_mapping": "core_hook", "selling_mapping": "main_selling_point"}
    for mapping, dimension in mapping_dimensions.items():
        dimension_codes = {item["code"] for item in manifest["dimensions"][dimension]["labels"]}
        for source, target in manifest["tie_breakers"].get(mapping, {}).items():
            if source not in dimension_codes or target not in patterns:
                raise RuleValidationError(f"{mapping}包含未知映射")


def _validate_content_pattern_model(manifest: dict[str, Any]) -> None:
    model = manifest.get("content_pattern_model")
    if model is None:
        return
    if model.get("type") != "prototype-v1":
        raise RuleValidationError("未知Content Pattern模型")
    if model.get("candidate_scope") != "same_pattern_only":
        raise RuleValidationError("prototype-v1必须使用same_pattern_only")
    if model.get("min_cluster_size") != 3 or model.get("min_support_after_exclusion") != 2:
        raise RuleValidationError("prototype-v1最小支持配置无效")
    profiles = model.get("profiles", {})
    if set(profiles) != set(manifest["content_patterns"]):
        raise RuleValidationError("Pattern定义与训练原型不一致")
    all_ids = []
    for code, profile in profiles.items():
        material_ids = profile.get("material_ids", [])
        prototypes = profile.get("prototypes", [])
        if len(material_ids) < 3 or len(prototypes) < 3:
            raise RuleValidationError(f"{code}训练样本必须至少3条")
        prototype_ids = [str(item.get("material_id", "")) for item in prototypes]
        if material_ids != sorted(material_ids) or set(material_ids) != set(prototype_ids):
            raise RuleValidationError(f"{code}训练原型material_id不一致")
        all_ids.extend(material_ids)
    if len(all_ids) != len(set(all_ids)):
        raise RuleValidationError("训练material_id重复归入多个Pattern")


def _validate_metrics(manifest: dict[str, Any]) -> None:
    allowed_names = set(manifest.get("source_fields", {}))
    source_columns = list(manifest.get("source_fields", {}).values())
    metrics = manifest.get("commercial_metrics", {})
    if not allowed_names or not metrics:
        raise RuleValidationError("缺少商业字段或指标")
    if len(source_columns) != len(set(source_columns)):
        raise RuleValidationError("商业源字段映射重复")
    for name, metric in metrics.items():
        try:
            tree = ast.parse(metric.get("formula", ""), mode="eval")
        except SyntaxError as error:
            raise RuleValidationError(f"{name}包含非法公式") from error
        if any(not isinstance(node, ALLOWED_FORMULA_NODES) for node in ast.walk(tree)):
            raise RuleValidationError(f"{name}包含非法公式")
        formula_names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        if not formula_names <= allowed_names:
            raise RuleValidationError(f"{name}包含非法公式字段")
        thresholds = metric.get("thresholds")
        if thresholds is not None and (
            len(thresholds) != 4
            or any(not isinstance(item, (int, float)) or not math.isfinite(item) for item in thresholds)
            or any(a >= b for a, b in zip(thresholds, thresholds[1:]))
        ):
            raise RuleValidationError(f"{name}阈值必须严格递增")
        if metric.get("direction") not in {"high", "low"}:
            raise RuleValidationError(f"{name}指标方向无效")


def _validate_level_config(manifest: dict[str, Any]) -> None:
    expected = {
        "efficiency": {"settle_roi", "settle_cpo"},
        "scale": {"daily_spend", "daily_gmv", "daily_orders"},
        "quality": {"settle_rate", "refund_rate"},
    }
    config = manifest.get("level_config", {})
    if set(config) != set(expected):
        raise RuleValidationError("商业层级权重配置不完整")
    for name, keys in expected.items():
        weights = config[name]
        if set(weights) != keys:
            raise RuleValidationError(f"{name}层级权重字段不完整")
        if any(
            not isinstance(weight, (int, float))
            or not math.isfinite(weight)
            or weight < 0
            for weight in weights.values()
        ) or not math.isclose(sum(weights.values()), 1.0, abs_tol=1e-9):
            raise RuleValidationError(f"{name}层级权重必须为非负且合计1")


def _validate_commercial_patterns(manifest: dict[str, Any]) -> None:
    patterns = manifest.get("commercial_patterns", [])
    priorities = [item.get("priority") for item in patterns]
    if not patterns or priorities != list(range(1, len(patterns) + 1)):
        raise RuleValidationError("Commercial Pattern优先级必须连续")
    if patterns[-1].get("condition") is not None:
        raise RuleValidationError("Commercial Pattern缺少最终兜底")
    names = [item.get("name") for item in patterns]
    if len(names) != len(set(names)) or any(not name for name in names):
        raise RuleValidationError("Commercial Pattern名称必须唯一且非空")
    allowed = re.compile(r"^[a-z_0-9. <>=and-]+$")
    condition_fields = {
        "attraction_level",
        "conversion_level",
        "efficiency_level",
        "scale_level",
        "quality_level",
        "spend_level",
        "daily_orders",
        "daily_gmv",
    }
    for item in patterns[:-1]:
        condition = item.get("condition", "")
        if not condition or not allowed.fullmatch(condition):
            raise RuleValidationError(f"{item.get('name')}包含非法条件")
        for clause in condition.split(" and "):
            match = re.fullmatch(r"([a-z_]+)(>=|<=|==|>|<)(-?\d+(?:\.\d+)?)", clause.strip())
            if not match:
                raise RuleValidationError(f"{item.get('name')}包含非法条件")
            if match.group(1) not in condition_fields:
                raise RuleValidationError(f"{item.get('name')}包含未知条件字段")


def _validate_targets(manifest: dict[str, Any]) -> None:
    targets = manifest.get("prediction_targets", {})
    if not targets:
        raise RuleValidationError("prediction_targets不能为空")
    unknown = set(targets) - set(manifest["commercial_metrics"])
    if unknown:
        raise RuleValidationError(f"prediction_targets引用未知指标: {sorted(unknown)}")
