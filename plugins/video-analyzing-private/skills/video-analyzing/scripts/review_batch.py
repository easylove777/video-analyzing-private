from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from review import review_prediction
from runtime_paths import add_data_root_argument, resolved_data_root
from store import stable_hash, write_json_new


def evaluate_reviews(reviews: list[dict[str, Any]]) -> dict[str, Any]:
    completed = [item for item in reviews if item.get("review_status") == "completed"]
    metric_names = sorted({
        name
        for item in completed
        for name, analysis in item["metric_analysis"].items()
        if _is_probability_evaluable(analysis)
    })
    dimensions = sorted({name for item in completed for name in item["dimension_analysis"]})
    positive_rows = [
        analysis
        for item in completed
        for analysis in item["metric_analysis"].values()
        if analysis.get("actual", {}).get("state") == "positive"
        and analysis.get("magnitude") in {"within", "below", "above"}
    ]
    point_rows = [
        analysis
        for item in completed
        for analysis in item["metric_analysis"].values()
        if _is_point_error_evaluable(analysis)
    ]
    interval_total = len(positive_rows)
    pattern_hits = sum(
        item["patterns"]["predicted_commercial_pattern"] == item["patterns"]["actual_commercial_pattern"]
        for item in completed
    )
    return {
        "review_count": len(completed),
        "positive_probability_calibration": {
            name: _probability_calibration(completed, name) for name in metric_names
        },
        "positive_interval_coverage": _ratio(sum(row["magnitude"] == "within" for row in positive_rows), interval_total),
        "below_rate": _ratio(sum(row["magnitude"] == "below" for row in positive_rows), interval_total),
        "above_rate": _ratio(sum(row["magnitude"] == "above" for row in positive_rows), interval_total),
        "p50_error": _p50_error(point_rows),
        "pattern_accuracy": _ratio(pattern_hits, len(completed)),
        "pattern_transition_matrix": _transition_matrix(completed),
        "dimension_level_error": {
            name: _dimension_error(completed, name) for name in dimensions
        },
    }


def _probability_calibration(reviews: list[dict[str, Any]], metric: str) -> dict[str, float]:
    rows = [
        item["metric_analysis"][metric]
        for item in reviews
        if metric in item["metric_analysis"]
        and _is_probability_evaluable(item["metric_analysis"][metric])
    ]
    predicted = sum(row["prediction"]["state_probabilities"]["positive"] for row in rows) / len(rows)
    actual = sum(row["actual"]["state"] == "positive" for row in rows) / len(rows)
    return {"predicted_positive_rate": predicted, "actual_positive_rate": actual, "gap": actual - predicted}


def _is_probability_evaluable(analysis: dict[str, Any]) -> bool:
    probabilities = analysis.get("prediction", {}).get("state_probabilities")
    return isinstance(probabilities, dict) and "positive" in probabilities


def _p50_error(rows: list[dict[str, Any]]) -> float | None:
    values = [
        abs(float(row["actual"]["value"]) - point)
        for row in rows
        if (point := _point_value(row["prediction"])) is not None
    ]
    return sum(values) / len(values) if values else None


def _point_value(prediction: dict[str, Any]) -> float | None:
    overall = prediction.get("overall_point_prediction")
    if overall is not None:
        if overall.get("status") != "available" or overall.get("value") is None:
            return None
        return float(overall["value"])
    interval = prediction.get("positive_interval")
    return float(interval["p50"]) if interval else None


def _is_point_error_evaluable(analysis: dict[str, Any]) -> bool:
    actual = analysis.get("actual", {})
    prediction = analysis.get("prediction", {})
    if actual.get("value") is None or actual.get("state") == "undefined":
        return False
    if "overall_point_prediction" in prediction:
        return _point_value(prediction) is not None
    return actual.get("state") == "positive" and _point_value(prediction) is not None


def _transition_matrix(reviews: list[dict[str, Any]]) -> dict[str, int]:
    matrix = {}
    for item in reviews:
        patterns = item["patterns"]
        key = f"{patterns['predicted_commercial_pattern']}->{patterns['actual_commercial_pattern']}"
        matrix[key] = matrix.get(key, 0) + 1
    return matrix


def _dimension_error(reviews: list[dict[str, Any]], dimension: str) -> float | None:
    gaps = [abs(item["dimension_analysis"][dimension]["level_gap"]) for item in reviews if dimension in item["dimension_analysis"] and item["dimension_analysis"][dimension].get("level_gap") is not None]
    return sum(gaps) / len(gaps) if gaps else None


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def review_batch(review_directory: str | Path, output_directory: str | Path) -> dict[str, Any]:
    rows = []
    failures = []
    files = sorted(Path(review_directory).glob("*.json"))
    for path in files:
        try:
            rows.append(json.loads(path.read_text(encoding="utf-8-sig")))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            failures.append({"file": str(path.resolve()), "error": str(error)})
    return _save_batch_report(rows, failures, len(files), output_directory)


def review_actual_batch(
    data_root: str | Path,
    actual_directory: str | Path,
    output_directory: str | Path,
) -> dict[str, Any]:
    rows = []
    failures = []
    files = sorted(Path(actual_directory).glob("*.json"))
    for path in files:
        try:
            actual = json.loads(path.read_text(encoding="utf-8-sig"))
            prediction_id = str(actual["prediction_id"])
            rows.append(review_prediction(data_root, prediction_id, actual))
        except Exception as error:
            failures.append({"file": str(path.resolve()), "error": str(error)})
    return _save_batch_report(rows, failures, len(files), output_directory)


def _save_batch_report(
    rows: list[dict[str, Any]],
    failures: list[dict[str, str]],
    input_count: int,
    output_directory: str | Path,
) -> dict[str, Any]:
    report = {
        **evaluate_reviews(rows),
        "input_count": input_count,
        "success_count": len(rows),
        "failure_count": len(failures),
        "failures": failures,
    }
    report_id = f"review_batch_{stable_hash(report)[:16]}"
    path = Path(output_directory) / f"{report_id}.json"
    if not path.exists():
        write_json_new(path, {**report, "report_id": report_id})
    return {**report, "report_id": report_id, "report_path": str(path.resolve())}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--review-directory")
    add_data_root_argument(parser)
    parser.add_argument("--actual-directory")
    parser.add_argument("--output-directory", required=True)
    args = parser.parse_args()
    args.data_root = str(resolved_data_root(args))
    if args.actual_directory:
        if not args.data_root:
            parser.error("--actual-directory需要--data-root")
        result = review_actual_batch(
            args.data_root, args.actual_directory, args.output_directory
        )
    elif args.review_directory:
        result = review_batch(args.review_directory, args.output_directory)
    else:
        parser.error("必须提供--review-directory或--actual-directory")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
