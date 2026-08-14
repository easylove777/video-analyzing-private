from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
from typing import Any


DIMENSION_SECTIONS = {
    "core_hook": ("### 4.1", "### 4.2", "H"),
    "pain_point": ("### 4.2", "### 4.3", "P"),
    "main_selling_point": ("### 4.3", "### 4.4", "S"),
    "audience_angle": ("### 4.4", "### 4.5", "A"),
    "content_form": ("### 4.5", "## 5.", "F"),
}

SOURCE_NAMES = {
    "Spend": "spend",
    "展现": "shows",
    "点击": "clicks",
    "支付订单": "pay_orders",
    "支付GMV": "pay_gmv",
    "结算金额": "settle_amount",
    "结算订单": "settle_orders",
    "退款订单": "refund_orders",
}

METRIC_DEFINITIONS = {
    "daily_spend": ("日均Spend", "spend", "high", "currency"),
    "daily_gmv": ("日均GMV", "pay_gmv", "high", "currency"),
    "daily_orders": ("日均订单", "pay_orders", "high", "count"),
    "ctr": ("CTR", "clicks / shows", "high", "rate"),
    "cvr": ("CVR", "pay_orders / clicks", "high", "rate"),
    "settle_roi": ("结算ROI", "settle_amount / spend", "high", "ratio"),
    "settle_cpo": ("结算CPO", "spend / settle_orders", "low", "currency"),
    "refund_rate": ("退款率", "refund_orders / pay_orders", "low", "rate"),
    "settle_rate": ("结算率", "settle_orders / pay_orders", "high", "rate"),
}

THRESHOLD_NAMES = {
    "CTR": "ctr",
    "CVR": "cvr",
    "结算ROI": "settle_roi",
    "结算CPO": "settle_cpo",
    "日均Spend": "daily_spend",
    "日均GMV": "daily_gmv",
    "日均订单": "daily_orders",
}

CONDITION_NAMES = {
    "SpendLevel": "spend_level",
    "AttractionLevel": "attraction_level",
    "ConversionLevel": "conversion_level",
    "EfficiencyLevel": "efficiency_level",
    "ScaleLevel": "scale_level",
    "QualityLevel": "quality_level",
    "日均订单": "daily_orders",
    "日均GMV": "daily_gmv",
}


def compile_markdown(rule_file: str | Path) -> dict[str, Any]:
    path = Path(rule_file)
    text = path.read_text(encoding="utf-8")
    manifest = {
        "schema_version": "1.0",
        "rule_version": _match(text, r"^- 版本：([^\s]+)", "规则版本"),
        "source_name": path.name,
        "input_fields": _input_fields(text),
        "field_fallbacks": {
            "core_hook": ["core_hook", "first_three_seconds", "caption"],
            "pain_point": ["pain_point", "caption"],
            "main_selling_point": ["main_selling_point", "caption"],
            "audience_angle": ["audience_angle", "caption"],
            "content_form": ["content_form", "caption"],
        },
        "dimensions": _dimensions(text),
        "content_patterns": _content_patterns(text),
        "content_pattern_weights": _content_weights(text),
        "tie_breakers": _tie_breakers(text),
        "source_fields": _source_fields(text),
        "commercial_metrics": _commercial_metrics(text),
        "level_config": _level_config(text),
        "commercial_patterns": _commercial_patterns(text),
    }
    manifest["prediction_targets"] = {
        key: {
            "display_name": value["display_name"],
            "direction": value["direction"],
            "unit": value["unit"],
        }
        for key, value in manifest["commercial_metrics"].items()
    }
    from validate_rules import validate_manifest

    return validate_manifest(manifest)


def _match(text: str, pattern: str, label: str) -> str:
    match = re.search(pattern, text, re.MULTILINE)
    if not match:
        raise ValueError(f"无法解析{label}")
    return match.group(1).strip()


def _section(text: str, start: str, end: str) -> str:
    start_index = text.index(start)
    end_index = text.index(end, start_index + len(start))
    return text[start_index:end_index]


def _table_rows(section: str) -> list[list[str]]:
    rows = []
    for line in section.splitlines():
        if not line.startswith("|") or re.match(r"^\|[-:| ]+\|$", line):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if cells and cells[0] not in {"字段", "标准含义", "编码与名称", "V类", "指标", "优先级", "类别"}:
            rows.append(cells)
    return rows


def _input_fields(text: str) -> list[dict[str, Any]]:
    section = _section(text, "### 2.1", "### 2.2")
    fields = []
    for cells in _table_rows(section):
        if len(cells) < 4:
            continue
        fields.append({
            "name": cells[0].strip("`"),
            "type": cells[1],
            "required": cells[2] == "是",
            "purpose": cells[3],
        })
    return fields


def _dimensions(text: str) -> dict[str, Any]:
    dimensions = {}
    for name, (start, end, prefix) in DIMENSION_SECTIONS.items():
        section = _section(text, start, end)
        labels = []
        for cells in _table_rows(section):
            match = re.match(rf"({prefix}\d{{2}})｜(.+)", cells[0])
            if match and len(cells) >= 3:
                labels.append({
                    "code": match.group(1),
                    "name": match.group(2),
                    "evidence": cells[1],
                    "exclusion": cells[2],
                })
        fallback = re.search(r"兜底：(.+)", section)
        dimensions[name] = {
            "labels": labels,
            "fallback": fallback.group(1).strip() if fallback else "",
        }
    return dimensions


def _content_patterns(text: str) -> dict[str, Any]:
    section = _section(text, "### 5.1", "### 5.2")
    patterns = {}
    for cells in _table_rows(section):
        match = re.match(r"(V\d{2})｜(.+)", cells[0])
        if match and len(cells) >= 2:
            patterns[match.group(1)] = {"name": match.group(2), "mechanism": cells[1]}
    return patterns


def _content_weights(text: str) -> dict[str, dict[str, int]]:
    section = _section(text, "### 5.2", "### 5.3")
    weights = {}
    for cells in _table_rows(section):
        if not re.fullmatch(r"V\d{2}", cells[0]) or len(cells) < 2:
            continue
        weights[cells[0]] = {
            code: int(value)
            for code, value in re.findall(r"([HPSAF]\d{2})=(\d+)", cells[1])
        }
    return weights


def _mapping(line: str) -> dict[str, str]:
    mapping = {}
    for left, target in re.findall(r"([HPS]\d{2}(?:/[HPS]\d{2})*)→(V\d{2})", line):
        for code in left.split("/"):
            mapping[code] = target
    return mapping


def _tie_breakers(text: str) -> dict[str, Any]:
    section = _section(text, "### 5.3", "## 6.")
    hook_line = _match(section, r"钩子直接映射：(.+)", "钩子映射")
    selling_line = _match(section, r"卖点直接映射：(.+)", "卖点映射")
    return {
        "order": ["score", "hook_mapping", "selling_mapping", "smallest_code"],
        "hook_mapping": _mapping(hook_line),
        "selling_mapping": _mapping(selling_line),
    }


def _source_fields(text: str) -> dict[str, str]:
    section = _section(text, "### 2.2", "## 3.")
    result = {}
    for cells in _table_rows(section):
        if len(cells) >= 2 and cells[0] in SOURCE_NAMES:
            result[SOURCE_NAMES[cells[0]]] = cells[1].strip("`")
    return result


def _thresholds(text: str) -> dict[str, list[float]]:
    section = _section(text, "### 7.1", "### 7.2")
    result = {}
    for cells in _table_rows(section):
        if len(cells) < 5 or cells[0] not in THRESHOLD_NAMES:
            continue
        values = [float(value.rstrip("%")) for value in cells[1:5]]
        if any("%" in value for value in cells[1:5]):
            values = [value / 100 for value in values]
        result[THRESHOLD_NAMES[cells[0]]] = values
    return result


def _commercial_metrics(text: str) -> dict[str, Any]:
    thresholds = _thresholds(text)
    metrics = {}
    for key, (display_name, formula, direction, unit) in METRIC_DEFINITIONS.items():
        metrics[key] = {
            "display_name": display_name,
            "formula": formula,
            "direction": direction,
            "unit": unit,
            "thresholds": thresholds.get(key),
            "clamp_rate": unit == "rate",
        }
    return metrics


def _level_config(text: str) -> dict[str, Any]:
    section = _section(text, "### 7.2", "## 8.")
    efficiency = _weights_from_formula(section, "EfficiencyScore")
    scale = _weights_from_formula(section, "ScaleScore")
    quality = _weights_from_formula(section, "QualityScore")
    return {"efficiency": efficiency, "scale": scale, "quality": quality}


def _weights_from_formula(section: str, name: str) -> dict[str, float]:
    line = _match(section, rf"^{name} = (.+)$", name)
    pairs = re.findall(
        r"(\d+(?:\.\d+)?)\s*×\s*(?:\(1\s*-\s*)?([A-Za-z]+|结算率|退款率)\)?",
        line,
    )
    aliases = {
        "ROILevel": "settle_roi",
        "CPOLevel": "settle_cpo",
        "SpendLevel": "daily_spend",
        "GMVLevel": "daily_gmv",
        "OrderLevel": "daily_orders",
        "结算率": "settle_rate",
        "退款率": "refund_rate",
    }
    return {aliases[label]: float(weight) for weight, label in pairs}


def _commercial_patterns(text: str) -> list[dict[str, Any]]:
    section = _section(text, "## 8.", "## 9.")
    patterns = []
    for cells in _table_rows(section):
        if len(cells) < 3 or not cells[0].isdigit():
            continue
        raw_rule = cells[2].strip("`")
        condition = None if raw_rule.startswith("未命中") else _normalize_condition(raw_rule)
        patterns.append({"priority": int(cells[0]), "name": cells[1], "condition": condition})
    return patterns


def _normalize_condition(condition: str) -> str:
    normalized = condition
    for source, target in CONDITION_NAMES.items():
        normalized = normalized.replace(source, target)
    return normalized.replace(" AND ", " and ").replace("=", "==").replace(">==", ">=").replace("<==", "<=")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rule-file", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    manifest = compile_markdown(args.rule_file)
    Path(args.output).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
