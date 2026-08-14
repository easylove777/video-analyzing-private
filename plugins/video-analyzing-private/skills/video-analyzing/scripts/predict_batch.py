from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from blind_contract import validate_blind_receipt
from blind_prediction import (
    BlindPredictionError,
    confirm_prediction_draft,
    create_prediction_draft,
)
from predict import PREDICTION_ALGORITHM_VERSION, predict_video
from errors import ValidationError
from integrity import require_formal_prediction_ready
from runtime_paths import add_data_root_argument, resolved_data_root
from store import file_hash, get_active_assets, read_json, stable_hash, write_json_new


def validate_batch_receipts(receipts: list[dict[str, Any]]) -> None:
    run_ids: dict[str, str] = {}
    material_ids = set()
    for receipt in receipts:
        try:
            validate_blind_receipt(receipt)
        except ValidationError as exc:
            raise BlindPredictionError(str(exc)) from exc
        material_id = receipt["material_id"]
        run_id = receipt["run_id"]
        previous_material = run_ids.get(run_id)
        if previous_material is not None and previous_material != material_id:
            raise BlindPredictionError(
                f"blind run_id reused across materials: {run_id}"
            )
        if material_id in material_ids:
            raise BlindPredictionError(f"duplicate receipt for material: {material_id}")
        run_ids[run_id] = material_id
        material_ids.add(material_id)


def _validate_batch_run_isolation(receipts: list[dict[str, Any]]) -> None:
    run_ids: dict[str, str] = {}
    for receipt in receipts:
        material_id = receipt.get("material_id")
        run_id = receipt.get("run_id")
        if not isinstance(material_id, str) or not isinstance(run_id, str):
            continue
        previous_material = run_ids.get(run_id)
        if previous_material is not None and previous_material != material_id:
            raise BlindPredictionError(
                f"blind run_id reused across materials: {run_id}"
            )
        run_ids[run_id] = material_id


def predict_batch_from_receipts(
    data_root: str | Path,
    video_directory: str | Path,
    receipt_directory: str | Path,
) -> dict[str, Any]:
    require_formal_prediction_ready(data_root)
    root = Path(data_root)
    receipt_values = []
    receipt_errors: dict[str, str] = {}
    for receipt_path in sorted(Path(receipt_directory).glob("*.json")):
        try:
            receipt_values.append(read_json(receipt_path))
        except (OSError, ValueError) as error:
            receipt_errors[receipt_path.stem] = str(error)
    _validate_batch_run_isolation(receipt_values)
    receipts = {
        item["material_id"]: item
        for item in receipt_values
        if isinstance(item, dict) and isinstance(item.get("material_id"), str)
    }
    successes = []
    failures = []
    for video_path in sorted(Path(video_directory).glob("*.json")):
        material_id = video_path.stem
        try:
            video = read_json(video_path)
            material_id = str(video["material_id"])
            if material_id in receipt_errors:
                raise BlindPredictionError(receipt_errors[material_id])
            receipt = receipts.get(material_id)
            if receipt is None:
                raise BlindPredictionError("missing blind receipt")
            validate_blind_receipt(receipt, expected_material_id=material_id)
            draft = create_prediction_draft(root, video, receipt)
            successes.append(
                {
                    "material_id": material_id,
                    "run_id": receipt["run_id"],
                    "draft_id": draft["draft_id"],
                    "draft_path": draft["draft_path"],
                }
            )
        except (OSError, ValueError, KeyError, TypeError, ValidationError, BlindPredictionError) as error:
            failures.append(
                {
                    "material_id": material_id,
                    "file": str(video_path.resolve()),
                    "stage": "blind_receipt_validation",
                    "error": str(error),
                }
            )
    report_base = {
        "successes": successes,
        "failures": failures,
        "success_count": len(successes),
        "failure_count": len(failures),
    }
    batch_draft_id = f"batch_draft_{stable_hash(report_base)[:16]}"
    report_path = root / "batch-runs" / batch_draft_id / "draft.json"
    report = {
        **report_base,
        "batch_draft_id": batch_draft_id,
        "status": "pending_confirmation",
        "report_path": str(report_path.resolve()),
    }
    if report_path.is_file():
        existing = read_json(report_path)
        if stable_hash(existing) != stable_hash(report):
            raise BlindPredictionError("已有批量草稿与当前结果不一致")
        return existing
    write_json_new(report_path, report)
    return report


def confirm_prediction_batch(
    data_root: str | Path,
    batch_draft_id: str,
    confirmation_text: str,
) -> dict[str, Any]:
    expected = f"确认预测批次 {batch_draft_id}"
    if confirmation_text.strip() != expected:
        raise BlindPredictionError(f"必须明确回复: {expected}")
    root = Path(data_root)
    draft_path = root / "batch-runs" / batch_draft_id / "draft.json"
    if not draft_path.is_file():
        raise BlindPredictionError(f"找不到批量预测草稿: {batch_draft_id}")
    draft = read_json(draft_path)
    if draft.get("batch_draft_id") != batch_draft_id:
        raise BlindPredictionError("批量预测草稿身份无效")
    successes = []
    failures = list(draft["failures"])
    for item in draft["successes"]:
        try:
            prediction = confirm_prediction_draft(
                root,
                item["draft_id"],
                f"确认预测 {item['draft_id']}",
            )
            successes.append(
                {
                    "material_id": item["material_id"],
                    "prediction_id": prediction["prediction_id"],
                    "snapshot_path": prediction["snapshot_path"],
                }
            )
        except (OSError, ValueError, ValidationError, BlindPredictionError) as error:
            failures.append(
                {
                    "material_id": item["material_id"],
                    "stage": "draft_confirmation",
                    "error": str(error),
                }
            )
    result_path = root / "batch-runs" / batch_draft_id / "confirmed.json"
    result = {
        "batch_draft_id": batch_draft_id,
        "status": "confirmed",
        "successes": successes,
        "failures": failures,
        "success_count": len(successes),
        "failure_count": len(failures),
        "report_path": str(result_path.resolve()),
    }
    if result_path.is_file():
        existing = read_json(result_path)
        if stable_hash(existing) != stable_hash(result):
            raise BlindPredictionError("已有批量确认报告与当前结果不一致")
        return existing
    write_json_new(result_path, result)
    return result


def predict_batch(
    data_root: str | Path, video_directory: str | Path, analysis_directory: str | Path
) -> dict[str, Any]:
    require_formal_prediction_ready(data_root)
    video_dir = Path(video_directory)
    analysis_dir = Path(analysis_directory)
    video_files = sorted(video_dir.glob("*.json"))
    active, _, _ = get_active_assets(data_root)
    inputs = [
        {
            "name": path.name,
            "video_sha256": file_hash(path),
            "analysis_sha256": (
                file_hash(analysis_dir / path.name)
                if (analysis_dir / path.name).is_file()
                else None
            ),
        }
        for path in video_files
    ]
    batch_id = f"batch_{stable_hash({
        'inputs': inputs,
        'manifest_sha256': active['manifest_sha256'],
        'case_library_sha256': active['case_library_sha256'],
        'algorithm_version': PREDICTION_ALGORITHM_VERSION,
    })[:16]}"
    report_path = Path(data_root) / "batch-runs" / batch_id / "report.json"
    if report_path.is_file():
        return json.loads(report_path.read_text(encoding="utf-8"))
    successes = []
    failures = []
    for video_file in video_files:
        try:
            video = json.loads(video_file.read_text(encoding="utf-8"))
            analysis_file = analysis_dir / video_file.name
            analysis = json.loads(analysis_file.read_text(encoding="utf-8"))
            prediction = predict_video(data_root, video, analysis)
            successes.append({
                "schema_version": prediction["schema_version"],
                "material_id": prediction["material_id"],
                "content_pattern": prediction["content_classification"]["content_pattern"],
                "content_pattern_name": prediction["content_classification"]["content_pattern_name"],
                "labels": prediction["content_classification"]["labels"],
                "evidence": prediction["evidence"],
                "key_evidence": prediction["key_evidence"],
                "commercial_pattern": prediction["predicted_commercial_pattern"],
                "metric_predictions": prediction["metric_predictions"],
                "primitive_predictions": prediction.get("primitive_predictions", {}),
                "commercial_pattern_probabilities": prediction["commercial_pattern_probabilities"],
                "commercial_dimension_predictions": prediction["commercial_dimension_predictions"],
                "confidence": prediction["confidence"],
                "warnings": prediction["warnings"],
                "reference_case_count": len(prediction["top_k"]),
                "snapshot_path": prediction["snapshot_path"],
            })
        except (OSError, ValueError, json.JSONDecodeError) as error:
            failures.append({"file": str(video_file.resolve()), "error": str(error)})
    report = {
        "batch_id": batch_id,
        "success_count": len(successes),
        "failure_count": len(failures),
        "successes": successes,
        "failures": failures,
        "report_path": str(report_path.resolve()),
    }
    write_json_new(report_path, report)
    return report


def _format_value(value: float, unit: str) -> str:
    if unit == "rate":
        return f"{value:.2%}"
    return f"{value:.2f}"


def _format_overall(metric: dict[str, Any]) -> str:
    point = metric.get("overall_point_prediction")
    if not point:
        return "not_available"
    if point.get("status") == "available":
        return _format_value(float(point["value"]), metric["unit"])
    return f"undefined ({point.get('reason', point.get('status'))})"


def render_batch_markdown(report: dict[str, Any]) -> str:
    lines = [
        f"批量商业预测完成：成功{report['success_count']}条，失败{report['failure_count']}条。",
        "",
        "| material_id | Content Pattern | Commercial Pattern | 置信度 |",
        "|---|---|---|---|",
    ]
    lines.extend(
        f"| {item['material_id']} | {item['content_pattern']} | {item['commercial_pattern']} | {item['confidence']} |"
        for item in report["successes"]
    )
    for item in report["successes"]:
        if item.get("schema_version") in {"3.0", "4.0"}:
            lines.extend([
                "",
                f"### {item['material_id']} 九项指标",
                "",
                "| 指标 | Overall P50 | 出值概率（positive） | 正值条件P50 | 正值条件P25–P75 |",
                "|---|---:|---:|---:|---:|",
            ])
            for metric in item["metric_predictions"].values():
                interval = metric.get("positive_interval")
                positive = metric.get("state_probabilities", {}).get("positive")
                positive_text = f"{positive:.1%}" if positive is not None else "数据不足"
                p50 = _format_value(interval["p50"], metric["unit"]) if interval else "无正值样本"
                span = (
                    f"{_format_value(interval['p25'], metric['unit'])}–{_format_value(interval['p75'], metric['unit'])}"
                    if interval else "无正值样本"
                )
                lines.append(
                    f"| {metric['display_name']} | {_format_overall(metric)} | {positive_text} | {p50} | {span} |"
                )
            lines.extend(["", "Commercial五维Level："])
            lines.extend(
                f"- {name.title()}：Level {value['predicted_level']}（{value['level_p25']}–{value['level_p75']}）"
                for name, value in item["commercial_dimension_predictions"].items()
            )
            lines.append(f"[打开预测快照]({item['snapshot_path'].replace(chr(92), '/')})")
            continue
        lines.extend(
            [
                "",
                f"### {item['material_id']} 九项指标",
                "",
                "| 指标 | 出值概率 | 有值时P50 | 有值时P25–P75 |",
                "|---|---:|---:|---:|",
            ]
        )
        for metric in item["metric_predictions"].values():
            if metric["status"] == "not_available":
                lines.append(f"| {metric['display_name']} | 数据不足 | 数据不足 | 数据不足 |")
                continue
            interval = metric["positive_interval"]
            probability = metric["state_probabilities"]["positive"]
            if interval:
                lines.append(
                    f"| {metric['display_name']} | {probability:.1%} | {_format_value(interval['p50'], metric['unit'])} | {_format_value(interval['p25'], metric['unit'])}–{_format_value(interval['p75'], metric['unit'])} |"
                )
            else:
                lines.append(f"| {metric['display_name']} | {probability:.1%} | 无正值样本 | 无正值样本 |")
        lines.extend(["", "五维Level："])
        lines.extend(
            f"- {name.title()}：Level {value['predicted_level']}（{value['level_p25']}–{value['level_p75']}）"
            for name, value in item["commercial_dimension_predictions"].items()
        )
        lines.append(f"[打开预测快照]({item['snapshot_path'].replace(chr(92), '/')})")
    lines.extend(["", f"[打开批次报告]({report['report_path'].replace(chr(92), '/')})"])
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    add_data_root_argument(parser)
    parser.add_argument("--video-directory")
    parser.add_argument("--analysis-directory")
    parser.add_argument("--receipt-directory")
    parser.add_argument("--confirm-batch-draft-id")
    parser.add_argument("--confirmation")
    parser.add_argument("--legacy-internal-v3", action="store_true")
    args = parser.parse_args()
    args.data_root = str(resolved_data_root(args))
    if args.confirm_batch_draft_id:
        if not args.confirmation:
            raise SystemExit("批量确认必须提供--confirmation")
        report = confirm_prediction_batch(
            args.data_root, args.confirm_batch_draft_id, args.confirmation
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    if args.receipt_directory:
        if not args.video_directory:
            raise SystemExit("盲批量草稿必须提供--video-directory")
        report = predict_batch_from_receipts(
            args.data_root, args.video_directory, args.receipt_directory
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    if not args.legacy_internal_v3 or not args.video_directory or not args.analysis_directory:
        raise SystemExit(
            "正式批量预测必须提供--receipt-directory；旧analysis目录路径仅可配合--legacy-internal-v3"
        )
    report = predict_batch(args.data_root, args.video_directory, args.analysis_directory)
    print(render_batch_markdown(report))


if __name__ == "__main__":
    main()
