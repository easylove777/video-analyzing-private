from __future__ import annotations

from collections import defaultdict
from typing import Any


def lift_adjustment(lift: float) -> int:
    if lift >= 0.50:
        return 3
    if lift >= 0.25:
        return 2
    if lift >= 0.10:
        return 1
    if lift > -0.10:
        return 0
    if lift > -0.25:
        return -1
    return -2


def remap_anchor(
    code: str,
    weights: dict[str, dict[str, int]],
    old_target: str | None,
) -> str:
    maximum = max((values.get(code, 0) for values in weights.values()), default=0)
    candidates = sorted(
        pattern for pattern, values in weights.items() if values.get(code, 0) == maximum
    )
    if old_target in candidates:
        return old_target
    return candidates[0]


def calibrate_pattern_rules(
    old_weights: dict[str, dict[str, int]],
    clusters: list[dict[str, Any]],
    *,
    uncovered_patterns: set[str],
    hook_anchors: dict[str, str] | None = None,
    selling_anchors: dict[str, str] | None = None,
) -> dict[str, Any]:
    hook_anchors = hook_anchors or {}
    selling_anchors = selling_anchors or {}
    grouped = _group_cluster_labels(clusters)
    labels = _all_labels(old_weights, grouped)
    support: dict[str, dict[str, float]] = {}
    lifts: dict[str, dict[str, float]] = {}
    revised: dict[str, dict[str, int]] = {}
    for pattern, previous in old_weights.items():
        if pattern in uncovered_patterns:
            support[pattern] = {}
            lifts[pattern] = {}
            revised[pattern] = dict(previous)
            continue
        inside = grouped[pattern]
        outside = [
            label_set
            for other_pattern, rows in grouped.items()
            if other_pattern != pattern
            for label_set in rows
        ]
        support[pattern] = _support(labels, inside)
        outside_support = _support(labels, outside)
        lifts[pattern] = {
            code: support[pattern][code] - outside_support[code] for code in labels
        }
        revised[pattern] = _adjust_weights(
            pattern,
            previous,
            support[pattern],
            lifts[pattern],
            hook_anchors,
            selling_anchors,
        )
    return {"weights": revised, "support": support, "lift": lifts}


def _group_cluster_labels(
    clusters: list[dict[str, Any]],
) -> dict[str, list[set[str]]]:
    grouped: dict[str, list[set[str]]] = defaultdict(list)
    for cluster in clusters:
        rows = cluster.get("label_sets", [])
        grouped[cluster["content_pattern"]].append(
            set().union(*(set(row) for row in rows)) if rows else set()
        )
    return grouped


def _all_labels(
    old_weights: dict[str, dict[str, int]],
    grouped: dict[str, list[set[str]]],
) -> list[str]:
    old_labels = {code for weights in old_weights.values() for code in weights}
    observed_labels = {
        code for rows in grouped.values() for label_set in rows for code in label_set
    }
    return sorted(old_labels | observed_labels)


def _support(labels: list[str], rows: list[set[str]]) -> dict[str, float]:
    if not rows:
        return {code: 0.0 for code in labels}
    return {
        code: sum(code in label_set for label_set in rows) / len(rows)
        for code in labels
    }


def _adjust_weights(
    pattern: str,
    previous: dict[str, int],
    support: dict[str, float],
    lifts: dict[str, float],
    hook_anchors: dict[str, str],
    selling_anchors: dict[str, str],
) -> dict[str, int]:
    weights = {}
    for code in support:
        value = _adjust_weight(code, previous, support[code], lifts[code])
        if hook_anchors.get(code) == pattern:
            value = max(value, 10)
        if selling_anchors.get(code) == pattern:
            value = max(value, 9)
        if value:
            weights[code] = value
    return weights


def _adjust_weight(
    code: str,
    previous: dict[str, int],
    support: float,
    lift: float,
) -> int:
    if code in previous:
        return max(0, min(12, previous[code] + lift_adjustment(lift)))
    if support >= 0.50 and lift >= 0.25:
        return max(2, min(6, round(6 * support)))
    return 0
