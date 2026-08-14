from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import re
from typing import Any

from blind_contract import (
    CANDIDATE_CONTENT_FIELDS,
    validate_blind_receipt,
    validate_classification_request,
    validate_classification_response,
    validate_similarity_request,
    validate_similarity_response,
)
from errors import ValidationError
from integrity import require_formal_prediction_ready
from predict import build_prediction_v4, freeze_prediction_v4, prepare_prediction_candidates
from runtime_paths import add_data_root_argument, resolved_data_root
from store import (
    file_transaction,
    get_active_assets,
    now_utc,
    read_json,
    stable_hash,
    write_json_new,
)


BLIND_SCHEMA_VERSION = "1.0"
BLIND_PROMPT_VERSION = "blind-score-v1"


class BlindPredictionError(ValidationError):
    pass


def create_classification_request(
    manifest: dict[str, Any],
    video: dict[str, Any],
    *,
    prompt_version: str = BLIND_PROMPT_VERSION,
    run_nonce: str = "",
) -> dict[str, Any]:
    taxonomy = {
        "rule_version": manifest["rule_version"],
        "manifest_sha256": stable_hash(manifest),
        "dimensions": deepcopy(manifest["dimensions"]),
    }
    run_id = f"blind_{stable_hash([video, taxonomy['manifest_sha256'], prompt_version, run_nonce])[:16]}"
    request = {
        "schema_version": BLIND_SCHEMA_VERSION,
        "run_id": run_id,
        "stage": "classification",
        "video": deepcopy(video),
        "taxonomy": taxonomy,
        "prompt_version": prompt_version,
    }
    request["request_sha256"] = stable_hash(request)
    try:
        return validate_classification_request(request)
    except ValidationError as exc:
        raise BlindPredictionError(str(exc)) from exc


def create_similarity_request(
    data_root: str,
    classification_request: dict[str, Any],
    classification_response: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, dict[str, str]]]:
    try:
        validate_classification_request(classification_request)
        active, manifest, _ = get_active_assets(data_root)
        validate_classification_response(
            classification_request, classification_response, manifest
        )
    except ValidationError as exc:
        raise BlindPredictionError(str(exc)) from exc
    if classification_request["taxonomy"]["manifest_sha256"] != active["manifest_sha256"]:
        raise BlindPredictionError("分类请求未绑定当前活动规则")

    analysis = {
        "material_id": classification_response["material_id"],
        "labels": deepcopy(classification_response["labels"]),
        "evidence": deepcopy(classification_response["evidence"]),
        "semantic_scores": {},
    }
    prepared = prepare_prediction_candidates(
        data_root, classification_request["video"], analysis
    )
    candidate_map: dict[str, dict[str, str]] = {}
    candidates = []
    for index, candidate in enumerate(prepared["candidates"], 1):
        blind_id = f"candidate_{index:03d}"
        candidate_map[blind_id] = {
            "case_id": str(candidate["case_id"]),
            "material_id": str(candidate["material_id"]),
        }
        content = {
            key: deepcopy(value)
            for key, value in candidate["video_content"].items()
            if key in CANDIDATE_CONTENT_FIELDS
        }
        candidates.append(
            {
                "blind_candidate_id": blind_id,
                "labels": deepcopy(candidate["labels"]),
                "video_content": content,
            }
        )
    if not candidates:
        raise BlindPredictionError("当前案例库没有可用于盲相似度判断的候选")

    request = {
        "schema_version": BLIND_SCHEMA_VERSION,
        "run_id": classification_request["run_id"],
        "stage": "similarity",
        "video": deepcopy(classification_request["video"]),
        "classification": {
            "labels": deepcopy(classification_response["labels"]),
            "evidence": deepcopy(classification_response["evidence"]),
        },
        "candidates": candidates,
        "candidate_map_sha256": stable_hash(candidate_map),
        "manifest_sha256": active["manifest_sha256"],
        "case_library_sha256": active["case_library_sha256"],
        "prompt_version": classification_request["prompt_version"],
    }
    request["request_sha256"] = stable_hash(request)
    try:
        validate_similarity_request(request)
    except ValidationError as exc:
        raise BlindPredictionError(str(exc)) from exc
    return request, candidate_map


def assemble_blind_receipt(
    classification_request: dict[str, Any],
    classification_response: dict[str, Any],
    similarity_request: dict[str, Any],
    similarity_response: dict[str, Any],
    candidate_map: dict[str, dict[str, str]],
    *,
    created_at: str | None = None,
) -> dict[str, Any]:
    manifest = {
        "rule_version": classification_request.get("taxonomy", {}).get("rule_version"),
        "dimensions": classification_request.get("taxonomy", {}).get("dimensions"),
    }
    try:
        validate_classification_response(
            classification_request, classification_response, manifest
        )
        validate_similarity_response(similarity_request, similarity_response)
    except (ValidationError, KeyError, TypeError) as exc:
        raise BlindPredictionError(str(exc)) from exc
    if classification_request["run_id"] != similarity_request["run_id"]:
        raise BlindPredictionError("两个盲阶段不属于同一次运行")
    if classification_request["prompt_version"] != similarity_request["prompt_version"]:
        raise BlindPredictionError("两个盲阶段prompt_version不一致")
    if classification_response["scorer_identity"] != similarity_response["scorer_identity"]:
        raise BlindPredictionError("两个盲阶段必须由同一隔离评分上下文完成")
    if stable_hash(candidate_map) != similarity_request["candidate_map_sha256"]:
        raise BlindPredictionError("盲候选映射已被修改")

    expected_blind_ids = {
        candidate["blind_candidate_id"] for candidate in similarity_request["candidates"]
    }
    if set(candidate_map) != expected_blind_ids:
        raise BlindPredictionError("盲候选映射与相似度请求不一致")
    semantic_scores = {}
    seen_case_ids = set()
    for blind_id in sorted(expected_blind_ids):
        binding = candidate_map.get(blind_id)
        if not isinstance(binding, dict) or set(binding) != {"case_id", "material_id"}:
            raise BlindPredictionError("盲候选映射结构无效")
        case_id = binding["case_id"]
        if not isinstance(case_id, str) or not case_id or case_id in seen_case_ids:
            raise BlindPredictionError("盲候选映射中的case_id无效或重复")
        seen_case_ids.add(case_id)
        semantic_scores[case_id] = deepcopy(
            similarity_response["semantic_scores"][blind_id]
        )

    receipt = {
        "schema_version": BLIND_SCHEMA_VERSION,
        "run_id": classification_request["run_id"],
        "blind_status": "passed",
        "material_id": classification_response["material_id"],
        "classification_request_sha256": classification_request["request_sha256"],
        "classification_response_sha256": stable_hash(classification_response),
        "similarity_request_sha256": similarity_request["request_sha256"],
        "similarity_response_sha256": stable_hash(similarity_response),
        "prompt_version": classification_request["prompt_version"],
        "scorer_identity": classification_response["scorer_identity"],
        "labels": deepcopy(classification_response["labels"]),
        "evidence": deepcopy(classification_response["evidence"]),
        "semantic_scores": semantic_scores,
        "candidate_map_sha256": similarity_request["candidate_map_sha256"],
        "manifest_sha256": similarity_request["manifest_sha256"],
        "case_library_sha256": similarity_request["case_library_sha256"],
        "created_at": created_at or now_utc(),
    }
    receipt["receipt_sha256"] = stable_hash(receipt)
    try:
        return validate_blind_receipt(
            receipt, expected_material_id=classification_request["video"]["material_id"]
        )
    except ValidationError as exc:
        raise BlindPredictionError(str(exc)) from exc


def save_blind_run(data_root: str | Path, artifacts: dict[str, Any]) -> dict[str, str]:
    required = {
        "classification_request",
        "classification_response",
        "similarity_request",
        "similarity_response",
        "candidate_map",
        "receipt",
    }
    if set(artifacts) != required:
        raise BlindPredictionError("blind run artifacts字段不完整")
    classification_request = artifacts["classification_request"]
    classification_response = artifacts["classification_response"]
    similarity_request = artifacts["similarity_request"]
    similarity_response = artifacts["similarity_response"]
    candidate_map = artifacts["candidate_map"]
    receipt = artifacts["receipt"]
    manifest = {
        "rule_version": classification_request.get("taxonomy", {}).get("rule_version"),
        "dimensions": classification_request.get("taxonomy", {}).get("dimensions"),
    }
    try:
        validate_classification_response(
            classification_request, classification_response, manifest
        )
        validate_similarity_response(similarity_request, similarity_response)
        validate_blind_receipt(
            receipt, expected_material_id=classification_request["video"]["material_id"]
        )
    except (ValidationError, KeyError, TypeError) as exc:
        raise BlindPredictionError(str(exc)) from exc
    if stable_hash(candidate_map) != receipt["candidate_map_sha256"]:
        raise BlindPredictionError("blind run候选映射哈希不一致")
    expected_receipt = assemble_blind_receipt(
        classification_request,
        classification_response,
        similarity_request,
        similarity_response,
        candidate_map,
        created_at=receipt["created_at"],
    )
    if stable_hash(expected_receipt) != stable_hash(receipt):
        raise BlindPredictionError("blind run回执无法由阶段产物重建")

    run_root = Path(data_root) / "blind-runs" / receipt["run_id"]
    values = {
        run_root / "classification-request.json": classification_request,
        run_root / "classification-response.json": classification_response,
        run_root / "similarity-request.json": similarity_request,
        run_root / "similarity-response.json": similarity_response,
        run_root / "candidate-map.json": candidate_map,
        run_root / "receipt.json": receipt,
    }
    with file_transaction(data_root, values):
        for path, value in values.items():
            _write_new_or_verify(path, value)
    return {name.name: str(name.resolve()) for name in values}


def prediction_payload_hash(prediction: dict[str, Any]) -> str:
    return stable_hash(
        {
            key: value
            for key, value in prediction.items()
            if key not in {"snapshot_path", "created_at"}
        }
    )


def create_prediction_draft(
    data_root: str | Path,
    video: dict[str, Any],
    receipt: dict[str, Any],
) -> dict[str, Any]:
    require_formal_prediction_ready(data_root)
    try:
        validate_blind_receipt(receipt, expected_material_id=str(video["material_id"]))
    except (ValidationError, KeyError) as exc:
        raise BlindPredictionError(str(exc)) from exc
    root = Path(data_root)
    receipt_path = root / "blind-runs" / receipt["run_id"] / "receipt.json"
    if not receipt_path.is_file():
        raise BlindPredictionError("盲运行尚未完整保存，不能创建预测草稿")
    saved_receipt = read_json(receipt_path)
    if stable_hash(saved_receipt) != stable_hash(receipt):
        raise BlindPredictionError("传入回执与已保存盲运行不一致")
    try:
        prediction = build_prediction_v4(root, video, receipt)
    except (ValidationError, ValueError) as exc:
        raise BlindPredictionError(str(exc)) from exc
    prediction_sha256 = prediction_payload_hash(prediction)
    draft_id = f"draft_{stable_hash([receipt['receipt_sha256'], prediction_sha256])[:20]}"
    draft_path = root / "blind-runs" / receipt["run_id"] / "drafts" / f"{draft_id}.json"
    draft = {
        "draft_id": draft_id,
        "run_id": receipt["run_id"],
        "material_id": str(video["material_id"]),
        "content_fingerprint": stable_hash(video),
        "receipt_sha256": receipt["receipt_sha256"],
        "manifest_sha256": receipt["manifest_sha256"],
        "case_library_sha256": receipt["case_library_sha256"],
        "algorithm_version": prediction["parameters"]["algorithm_version"],
        "prediction_sha256": prediction_sha256,
        "prediction": prediction,
        "created_at": now_utc(),
        "status": "pending_confirmation",
        "draft_path": str(draft_path.resolve()),
    }
    with file_transaction(root, (draft_path,)):
        if draft_path.is_file():
            existing = read_json(draft_path)
            stable_existing = {key: value for key, value in existing.items() if key != "created_at"}
            stable_draft = {key: value for key, value in draft.items() if key != "created_at"}
            if stable_hash(stable_existing) != stable_hash(stable_draft):
                raise BlindPredictionError("已有预测草稿与当前重算结果不一致")
            return existing
        write_json_new(draft_path, draft)
    return draft


def confirm_prediction_draft(
    data_root: str | Path,
    draft_id: str,
    confirmation_text: str,
) -> dict[str, Any]:
    require_formal_prediction_ready(data_root)
    if not re.fullmatch(r"draft_[0-9a-f]{20}", draft_id):
        raise BlindPredictionError("draft_id格式无效")
    expected = f"确认预测 {draft_id}"
    if confirmation_text.strip() != expected:
        raise BlindPredictionError(f"必须明确回复: {expected}")
    root = Path(data_root)
    matches = list((root / "blind-runs").glob(f"*/drafts/{draft_id}.json"))
    if len(matches) != 1:
        raise BlindPredictionError(f"找不到唯一预测草稿: {draft_id}")
    draft = read_json(matches[0])
    if draft.get("draft_id") != draft_id or draft.get("status") != "pending_confirmation":
        raise BlindPredictionError("预测草稿身份或状态无效")
    derived_draft_id = f"draft_{stable_hash([draft.get('receipt_sha256'), draft.get('prediction_sha256')])[:20]}"
    if derived_draft_id != draft_id:
        raise BlindPredictionError("draft_id与草稿载荷绑定不一致")
    receipt_path = root / "blind-runs" / str(draft.get("run_id", "")) / "receipt.json"
    if not receipt_path.is_file():
        raise BlindPredictionError("预测草稿缺少盲回执")
    receipt = read_json(receipt_path)
    try:
        validate_blind_receipt(receipt, expected_material_id=str(draft["material_id"]))
    except (ValidationError, KeyError) as exc:
        raise BlindPredictionError(str(exc)) from exc
    if receipt["receipt_sha256"] != draft.get("receipt_sha256"):
        raise BlindPredictionError("blind receipt hash changed")
    active = get_active_assets(root)[0]
    if active["manifest_sha256"] != draft.get("manifest_sha256"):
        raise BlindPredictionError("active manifest changed after draft")
    if active["case_library_sha256"] != draft.get("case_library_sha256"):
        raise BlindPredictionError("active case library changed after draft")
    video = draft.get("prediction", {}).get("video_content")
    if not isinstance(video, dict) or stable_hash(video) != draft.get("content_fingerprint"):
        raise BlindPredictionError("draft video content changed")
    classification_request_path = (
        root / "blind-runs" / str(draft["run_id"]) / "classification-request.json"
    )
    if not classification_request_path.is_file():
        raise BlindPredictionError("预测草稿缺少原始classification request")
    classification_request = read_json(classification_request_path)
    try:
        validate_classification_request(classification_request)
    except ValidationError as exc:
        raise BlindPredictionError(str(exc)) from exc
    if classification_request["request_sha256"] != receipt["classification_request_sha256"]:
        raise BlindPredictionError("classification request与回执不一致")
    if stable_hash(classification_request["video"]) != stable_hash(video):
        raise BlindPredictionError("草稿视频与原始盲分类请求不一致")
    try:
        recalculated = build_prediction_v4(root, video, receipt)
    except (ValidationError, ValueError) as exc:
        raise BlindPredictionError(str(exc)) from exc
    if prediction_payload_hash(recalculated) != draft.get("prediction_sha256"):
        raise BlindPredictionError("prediction changed after draft")
    try:
        return freeze_prediction_v4(root, video, receipt)
    except (ValidationError, ValueError) as exc:
        raise BlindPredictionError(str(exc)) from exc


def _write_new_or_verify(path: Path, value: Any) -> None:
    if path.is_file():
        if stable_hash(read_json(path)) != stable_hash(value):
            raise BlindPredictionError(f"不可覆盖已有盲运行文件: {path.name}")
        return
    write_json_new(path, value)


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="operation", required=True)

    prepare = subparsers.add_parser("prepare-classification")
    add_data_root_argument(prepare)
    prepare.add_argument("--video-file", required=True)
    prepare.add_argument("--output", required=True)

    accept_classification = subparsers.add_parser("accept-classification")
    add_data_root_argument(accept_classification)
    accept_classification.add_argument("--request", required=True)
    accept_classification.add_argument("--response", required=True)
    accept_classification.add_argument("--output", required=True)

    accept_similarity = subparsers.add_parser("accept-similarity")
    add_data_root_argument(accept_similarity)
    accept_similarity.add_argument("--request", required=True)
    accept_similarity.add_argument("--response", required=True)
    accept_similarity.add_argument("--output", required=True)

    draft_parser = subparsers.add_parser("draft")
    add_data_root_argument(draft_parser)
    draft_parser.add_argument("--video-file", required=True)
    draft_parser.add_argument("--receipt", required=True)

    confirm = subparsers.add_parser("confirm")
    add_data_root_argument(confirm)
    confirm.add_argument("--draft-id", required=True)
    confirm.add_argument("--confirmation", required=True)

    args = parser.parse_args()
    args.data_root = str(resolved_data_root(args))
    root = Path(args.data_root)
    if args.operation == "prepare-classification":
        require_formal_prediction_ready(root)
        active, manifest, _ = get_active_assets(root)
        request = create_classification_request(
            manifest, read_json(args.video_file), run_nonce=now_utc()
        )
        if request["taxonomy"]["manifest_sha256"] != active["manifest_sha256"]:
            raise BlindPredictionError("活动规则在请求创建期间发生变化")
        run_path = root / "blind-runs" / request["run_id"] / "classification-request.json"
        _write_new_or_verify(run_path, request)
        _write_new_or_verify(Path(args.output), request)
        result = request
    elif args.operation == "accept-classification":
        request = read_json(args.request)
        response = read_json(args.response)
        similarity_request, candidate_map = create_similarity_request(root, request, response)
        run_path = root / "blind-runs" / request["run_id"]
        for path, value in {
            run_path / "classification-request.json": request,
            run_path / "classification-response.json": response,
            run_path / "similarity-request.json": similarity_request,
            run_path / "candidate-map.json": candidate_map,
            Path(args.output): similarity_request,
        }.items():
            _write_new_or_verify(path, value)
        result = similarity_request
    elif args.operation == "accept-similarity":
        similarity_request = read_json(args.request)
        similarity_response = read_json(args.response)
        run_path = root / "blind-runs" / similarity_request["run_id"]
        classification_request = read_json(run_path / "classification-request.json")
        classification_response = read_json(run_path / "classification-response.json")
        candidate_map = read_json(run_path / "candidate-map.json")
        receipt = assemble_blind_receipt(
            classification_request,
            classification_response,
            similarity_request,
            similarity_response,
            candidate_map,
        )
        save_blind_run(
            root,
            {
                "classification_request": classification_request,
                "classification_response": classification_response,
                "similarity_request": similarity_request,
                "similarity_response": similarity_response,
                "candidate_map": candidate_map,
                "receipt": receipt,
            },
        )
        _write_new_or_verify(Path(args.output), receipt)
        result = receipt
    elif args.operation == "draft":
        result = create_prediction_draft(
            root, read_json(args.video_file), read_json(args.receipt)
        )
    else:
        result = confirm_prediction_draft(
            root, args.draft_id, args.confirmation
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
