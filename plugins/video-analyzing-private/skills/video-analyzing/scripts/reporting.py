from __future__ import annotations

from typing import Any


def render_review_markdown(review: dict[str, Any]) -> str:
    if review["review_status"] == "insufficient_data":
        return _render_insufficient(review)
    patterns = review["patterns"]
    lines = [
        "# 茶叶视频T+7预测分析复盘",
        "",
        "## 一、复盘结论",
        "",
        f"- 素材ID：`{review['material_id']}`",
        f"- Content Pattern：`{patterns['content_pattern']}`",
        f"- Commercial Pattern：{patterns['transition']}",
        f"- Observation：`{review['observation_id']}`",
        "",
    ]
    if "NO_CHANGE_SAMPLE_ONLY" in review["advice_codes"]:
        lines.extend(["> 预测命中，无需修改，增加样本池。", ""])
    lines.extend(_metric_section(review["metric_analysis"]))
    lines.extend(_dimension_section(review["dimension_analysis"]))
    lines.extend(
        [
            "## 六、建议与沉淀",
            "",
            f"- 建议代码：{', '.join(review['advice_codes'])}",
            f"- 样本池动作：`{review['sample_pool_action']}`",
        ]
    )
    progresses = review.get("cluster_progresses") or [review["cluster_progress"]]
    for progress in progresses:
        cluster_id = progress.get("cluster_id", "未记录")
        lines.append(
            f"- Cluster `{cluster_id}`：`{progress['status']}`；"
            f"有效Observation {progress['valid_observation_count']}条"
        )
    proposals = review.get("proposals")
    if proposals is None:
        proposals = [review["proposal"]] if review.get("proposal") else []
    for proposal in proposals:
        lines.append(f"- 自动生成沉淀提案：`{proposal['proposal_id']}`（等待人工审批）")
    return "\n".join(lines) + "\n"


def _render_insufficient(review: dict[str, Any]) -> str:
    return (
        "# 茶叶视频T+7预测分析复盘\n\n"
        "## 数据质量门槛\n\n"
        "结果：`insufficient_data`。不判断预测准确性，不生成修改建议，不进入样本池。\n"
    )


def _metric_section(metrics: dict[str, dict[str, Any]]) -> list[str]:
    lines = ["## 四、九项指标预测与实际偏差", "", "| 指标 | 实际状态 | 幅度判断 | 方向 | 严重度 |", "|---|---|---|---|---|"]
    for name, item in metrics.items():
        lines.append(
            f"| {name} | {item['actual']['state']} | {item['magnitude']} | {item['direction']} | {item['severity']} |"
        )
    lines.append("")
    return lines


def _dimension_section(dimensions: dict[str, dict[str, Any]]) -> list[str]:
    lines = ["## 五、Commercial Pattern五维诊断", "", "| 维度 | 预测Level | 实际Level | Level差 | 结果 |", "|---|---:|---:|---:|---|"]
    for name, item in dimensions.items():
        lines.append(
            f"| {name.title()} | {item.get('predicted_level', '-')} | {item.get('actual_level', '-')} | {item.get('level_gap', '-')} | {item['direction']} |"
        )
    lines.append("")
    return lines
