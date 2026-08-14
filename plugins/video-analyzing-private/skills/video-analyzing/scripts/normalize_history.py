from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


class HistoryNormalizationError(ValueError):
    pass


def normalize_workbook_export(source_file: str | Path, manifest: dict[str, Any]) -> dict[str, Any]:
    sheets = json.loads(Path(source_file).read_text(encoding="utf-8"))
    if not isinstance(sheets, list):
        raise HistoryNormalizationError("工作簿导出JSON必须是sheet数组")
    content_sheet = _get_sheet(sheets, "视频内容")
    metrics_sheet = _get_sheet(sheets, "视频数据")
    content_fields = [field["name"] for field in manifest["input_fields"]]
    metric_fields = ["material_id", *manifest["source_fields"].values()]
    return {
        "content": _extract_records(content_sheet["values"], content_fields),
        "metrics": _extract_records(metrics_sheet["values"], metric_fields),
    }


def load_normalized_records(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path)
    text = source.read_text(encoding="utf-8")
    if source.suffix.lower() == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    value = json.loads(text)
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise HistoryNormalizationError("规范历史文件必须是JSON对象数组或JSONL")
    return value


def _get_sheet(sheets: list[dict[str, Any]], name: str) -> dict[str, Any]:
    matches = [sheet for sheet in sheets if sheet.get("name") == name]
    if len(matches) != 1:
        raise HistoryNormalizationError(f"必须且只能有一个{name}sheet")
    return matches[0]


def _extract_records(rows: list[list[Any]], fields: list[str]) -> list[dict[str, Any]]:
    header_index = next((index for index, row in enumerate(rows) if "material_id" in row), None)
    if header_index is None:
        raise HistoryNormalizationError("找不到material_id表头")
    headers = rows[header_index]
    missing = [field for field in fields if field not in headers]
    if missing:
        raise HistoryNormalizationError(f"缺少字段: {missing}")
    indexes = {field: headers.index(field) for field in fields}
    records = []
    for row in rows[header_index + 1:]:
        material_id = _cell(row, indexes["material_id"])
        if material_id in (None, ""):
            continue
        record = {field: _cell(row, index) for field, index in indexes.items()}
        record["material_id"] = str(record["material_id"])
        records.append(record)
    return records


def _cell(row: list[Any], index: int) -> Any:
    return row[index] if index < len(row) else None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-file", required=True)
    parser.add_argument("--manifest-file", required=True)
    parser.add_argument("--output")
    parser.add_argument("--content-output")
    parser.add_argument("--metrics-output")
    args = parser.parse_args()
    if not args.output and not (args.content_output and args.metrics_output):
        parser.error("必须提供--output，或同时提供--content-output和--metrics-output")
    manifest = json.loads(Path(args.manifest_file).read_text(encoding="utf-8"))
    value = normalize_workbook_export(args.source_file, manifest)
    if args.output:
        Path(args.output).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.content_output:
        Path(args.content_output).write_text(
            json.dumps(value["content"], ensure_ascii=False, indent=2), encoding="utf-8"
        )
    if args.metrics_output:
        Path(args.metrics_output).write_text(
            json.dumps(value["metrics"], ensure_ascii=False, indent=2), encoding="utf-8"
        )


if __name__ == "__main__":
    main()
