from __future__ import annotations

import math
import re
from typing import Any

from errors import ValidationError
from store import stable_hash
from validate_classifications import ClassificationValidationError, validate_labels


SCHEMA_VERSION = "1.0"
STAGES = {"classification", "similarity"}
SELF_CHECK_FIELDS = {
    "context_inherited",
    "commercial_data_seen",
    "tool_use",
    "contamination_signal",
}
CONTENT_FIELDS = {
    "material_id",
    "caption",
    "first_three_seconds",
    "core_hook",
    "pain_point",
    "main_selling_point",
    "audience_angle",
    "content_form",
}
CANDIDATE_CONTENT_FIELDS = CONTENT_FIELDS - {"material_id"}
COMMERCIAL_FIELD_NAMES = {
    "actual",
    "actual_raw",
    "actual_metrics",
    "metrics",
    "commercial_pattern",
    "actual_commercial_pattern",
    "predicted_commercial_pattern",
    "spend",
    "shows",
    "impressions",
    "clicks",
    "pay_orders",
    "orders",
    "pay_gmv",
    "gmv",
    "settle_amount",
    "settle_orders",
    "refund_orders",
    "daily_spend",
    "daily_gmv",
    "daily_orders",
    "ctr",
    "cvr",
    "roi",
    "settle_roi",
    "cpo",
    "settle_cpo",
    "refund_rate",
    "settle_rate",
}
HASH_PATTERN = re.compile(r"[0-9a-f]{64}")
SAFE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def assert_no_commercial_data(value: Any, path: str = "$") -> None:
    """Reject commercial outcome fields recursively without scanning free-form copy."""
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).strip().lower()
            if normalized in COMMERCIAL_FIELD_NAMES:
                raise ValidationError(f"盲预测上下文包含商业字段: {path}.{key}")
            assert_no_commercial_data(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            assert_no_commercial_data(child, f"{path}[{index}]")


def validate_classification_request(request: dict[str, Any]) -> dict[str, Any]:
    _require_exact_fields(
        request,
        {
            "schema_version",
            "run_id",
            "stage",
            "video",
            "taxonomy",
            "prompt_version",
            "request_sha256",
        },
        "分类请求",
    )
    _validate_common_request(request, "classification")
    _validate_video(request["video"])
    taxonomy = _require_dict(request["taxonomy"], "taxonomy")
    _require_exact_fields(
        taxonomy,
        {"rule_version", "manifest_sha256", "dimensions"},
        "taxonomy",
    )
    _require_nonempty_string(taxonomy["rule_version"], "taxonomy.rule_version")
    _require_hash(taxonomy["manifest_sha256"], "taxonomy.manifest_sha256")
    dimensions = _require_dict(taxonomy["dimensions"], "taxonomy.dimensions")
    if not dimensions:
        raise ValidationError("taxonomy.dimensions不能为空")
    for dimension, spec in dimensions.items():
        _require_nonempty_string(dimension, "dimension")
        labels = _require_dict(spec, f"taxonomy.dimensions.{dimension}").get("labels")
        if not isinstance(labels, list) or not labels:
            raise ValidationError(f"{dimension}.labels不能为空")
        for item in labels:
            if not isinstance(item, dict) or not isinstance(item.get("code"), str):
                raise ValidationError(f"{dimension}.labels必须包含字符串code")
    assert_no_commercial_data(request)
    return request


def validate_classification_response(
    request: dict[str, Any], response: dict[str, Any], manifest: dict[str, Any]
) -> dict[str, Any]:
    validate_classification_request(request)
    _require_exact_fields(
        response,
        {
            "schema_version",
            "run_id",
            "stage",
            "request_sha256",
            "material_id",
            "labels",
            "evidence",
            "self_check",
            "scorer_identity",
        },
        "分类响应",
    )
    _validate_response_binding(request, response, "classification")
    if response["material_id"] != request["video"]["material_id"]:
        raise ValidationError("分类响应material_id与请求不一致")
    try:
        validate_labels(_require_dict(response["labels"], "labels"), manifest)
    except ClassificationValidationError as exc:
        raise ValidationError(str(exc)) from exc
    _validate_evidence(response["evidence"], set(manifest["dimensions"]))
    validate_self_check(response["self_check"])
    _require_nonempty_string(response["scorer_identity"], "scorer_identity")
    assert_no_commercial_data(response)
    return response


def validate_similarity_request(request: dict[str, Any]) -> dict[str, Any]:
    _require_exact_fields(
        request,
        {
            "schema_version",
            "run_id",
            "stage",
            "video",
            "classification",
            "candidates",
            "candidate_map_sha256",
            "manifest_sha256",
            "case_library_sha256",
            "prompt_version",
            "request_sha256",
        },
        "相似度请求",
    )
    _validate_common_request(request, "similarity")
    _validate_video(request["video"])
    classification = _require_dict(request["classification"], "classification")
    _require_exact_fields(classification, {"labels", "evidence"}, "classification")
    if not isinstance(classification["labels"], dict) or not classification["labels"]:
        raise ValidationError("classification.labels不能为空")
    _validate_evidence(classification["evidence"], set(classification["labels"]))
    candidates = request["candidates"]
    if not isinstance(candidates, list) or not candidates:
        raise ValidationError("candidates必须是非空数组")
    seen = set()
    for candidate in candidates:
        _require_exact_fields(
            _require_dict(candidate, "candidate"),
            {"blind_candidate_id", "labels", "video_content"},
            "candidate",
        )
        blind_id = candidate["blind_candidate_id"]
        if not isinstance(blind_id, str) or not re.fullmatch(r"candidate_[0-9]{3,}", blind_id):
            raise ValidationError("blind_candidate_id格式无效")
        if blind_id in seen:
            raise ValidationError("blind_candidate_id不得重复")
        seen.add(blind_id)
        if not isinstance(candidate["labels"], dict) or not candidate["labels"]:
            raise ValidationError("candidate.labels不能为空")
        video_content = _require_dict(candidate["video_content"], "candidate.video_content")
        unknown = set(video_content) - CANDIDATE_CONTENT_FIELDS
        if unknown:
            raise ValidationError(f"候选内容包含未授权字段: {sorted(unknown)}")
    for name in ("candidate_map_sha256", "manifest_sha256", "case_library_sha256"):
        _require_hash(request[name], name)
    assert_no_commercial_data(request)
    return request


def validate_similarity_response(
    request: dict[str, Any], response: dict[str, Any]
) -> dict[str, Any]:
    validate_similarity_request(request)
    _require_exact_fields(
        response,
        {
            "schema_version",
            "run_id",
            "stage",
            "request_sha256",
            "semantic_scores",
            "self_check",
            "scorer_identity",
        },
        "相似度响应",
    )
    _validate_response_binding(request, response, "similarity")
    scores = _require_dict(response["semantic_scores"], "semantic_scores")
    expected = {item["blind_candidate_id"] for item in request["candidates"]}
    if set(scores) != expected:
        raise ValidationError("semantic_scores必须精确覆盖盲候选ID")
    for blind_id, item in scores.items():
        _require_exact_fields(_require_dict(item, blind_id), {"score", "reason"}, blind_id)
        score = item["score"]
        if (
            not isinstance(score, (int, float))
            or isinstance(score, bool)
            or not math.isfinite(score)
            or not 0 <= score <= 100
        ):
            raise ValidationError(f"{blind_id}.score必须是0到100的有限数值")
        _require_nonempty_string(item["reason"], f"{blind_id}.reason")
    validate_self_check(response["self_check"])
    _require_nonempty_string(response["scorer_identity"], "scorer_identity")
    assert_no_commercial_data(response)
    return response


def validate_self_check(value: Any) -> dict[str, bool]:
    self_check = _require_dict(value, "self_check")
    _require_exact_fields(self_check, SELF_CHECK_FIELDS, "self_check")
    for name, state in self_check.items():
        if state is not False:
            raise ValidationError(f"盲预测自检未通过: {name}")
    return self_check


def validate_blind_receipt(
    receipt: dict[str, Any], expected_material_id: str | None = None
) -> dict[str, Any]:
    required = {
        "schema_version",
        "run_id",
        "blind_status",
        "material_id",
        "classification_request_sha256",
        "classification_response_sha256",
        "similarity_request_sha256",
        "similarity_response_sha256",
        "prompt_version",
        "scorer_identity",
        "labels",
        "evidence",
        "semantic_scores",
        "candidate_map_sha256",
        "manifest_sha256",
        "case_library_sha256",
        "created_at",
        "receipt_sha256",
    }
    _require_exact_fields(receipt, required, "盲预测回执")
    if receipt["schema_version"] != SCHEMA_VERSION or receipt["blind_status"] != "passed":
        raise ValidationError("盲预测回执版本或状态无效")
    _require_nonempty_string(receipt["run_id"], "run_id")
    _require_nonempty_string(receipt["material_id"], "material_id")
    if expected_material_id is not None and receipt["material_id"] != expected_material_id:
        raise ValidationError("盲预测回执material_id不一致")
    for name in (
        "classification_request_sha256",
        "classification_response_sha256",
        "similarity_request_sha256",
        "similarity_response_sha256",
        "candidate_map_sha256",
        "manifest_sha256",
        "case_library_sha256",
        "receipt_sha256",
    ):
        _require_hash(receipt[name], name)
    expected_hash = stable_hash({key: value for key, value in receipt.items() if key != "receipt_sha256"})
    if receipt["receipt_sha256"] != expected_hash:
        raise ValidationError("盲预测回执哈希不一致")
    if not isinstance(receipt["labels"], dict) or not receipt["labels"]:
        raise ValidationError("盲预测回执labels不能为空")
    _validate_evidence(receipt["evidence"], set(receipt["labels"]))
    scores = _require_dict(receipt["semantic_scores"], "semantic_scores")
    if not scores:
        raise ValidationError("盲预测回执semantic_scores不能为空")
    for case_id, item in scores.items():
        _require_nonempty_string(case_id, "case_id")
        _require_exact_fields(_require_dict(item, case_id), {"score", "reason"}, case_id)
        score = item["score"]
        if not isinstance(score, (int, float)) or isinstance(score, bool) or not math.isfinite(score) or not 0 <= score <= 100:
            raise ValidationError(f"{case_id}.score无效")
        _require_nonempty_string(item["reason"], f"{case_id}.reason")
    _require_nonempty_string(receipt["prompt_version"], "prompt_version")
    _require_nonempty_string(receipt["scorer_identity"], "scorer_identity")
    _require_nonempty_string(receipt["created_at"], "created_at")
    return receipt


def _validate_common_request(request: dict[str, Any], stage: str) -> None:
    if request["schema_version"] != SCHEMA_VERSION or request["stage"] != stage:
        raise ValidationError(f"{stage}请求版本或阶段无效")
    _require_nonempty_string(request["run_id"], "run_id")
    _require_nonempty_string(request["prompt_version"], "prompt_version")
    _require_hash(request["request_sha256"], "request_sha256")
    expected = stable_hash({key: value for key, value in request.items() if key != "request_sha256"})
    if request["request_sha256"] != expected:
        raise ValidationError("request_sha256与请求内容不一致")


def _validate_response_binding(
    request: dict[str, Any], response: dict[str, Any], stage: str
) -> None:
    if response["schema_version"] != SCHEMA_VERSION or response["stage"] != stage:
        raise ValidationError(f"{stage}响应版本或阶段无效")
    if response["run_id"] != request["run_id"]:
        raise ValidationError("响应run_id与请求不一致")
    if response["request_sha256"] != request["request_sha256"]:
        raise ValidationError("响应未绑定当前请求")


def _validate_video(video: Any) -> None:
    value = _require_dict(video, "video")
    _require_exact_fields(value, CONTENT_FIELDS, "video")
    for field in CONTENT_FIELDS:
        _require_nonempty_string(value[field], f"video.{field}")
    if not SAFE_ID_PATTERN.fullmatch(value["material_id"]):
        raise ValidationError("material_id格式无效")


def _validate_evidence(value: Any, dimensions: set[str]) -> None:
    evidence = _require_dict(value, "evidence")
    if set(evidence) != dimensions:
        raise ValidationError("evidence必须精确覆盖全部内容维度")
    for dimension, text in evidence.items():
        _require_nonempty_string(text, f"evidence.{dimension}")


def _require_exact_fields(value: dict[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        raise ValidationError(
            f"{label}字段不匹配: missing={sorted(expected - actual)}, unknown={sorted(actual - expected)}"
        )


def _require_dict(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError(f"{label}必须是对象")
    return value


def _require_nonempty_string(value: Any, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label}必须是非空字符串")


def _require_hash(value: Any, label: str) -> None:
    if not isinstance(value, str) or not HASH_PATTERN.fullmatch(value):
        raise ValidationError(f"{label}必须是SHA-256十六进制字符串")
