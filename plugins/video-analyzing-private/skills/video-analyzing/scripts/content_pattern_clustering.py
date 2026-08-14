from __future__ import annotations

from collections import Counter
from difflib import SequenceMatcher
import re
import statistics
import unicodedata
from typing import Any


CONTENT_FIELDS = (
    "caption",
    "first_three_seconds",
    "core_hook",
    "pain_point",
    "main_selling_point",
    "audience_angle",
    "content_form",
)
DIMENSION_WEIGHTS = {
    "core_hook": 0.2,
    "pain_point": 0.2,
    "main_selling_point": 0.2,
    "audience_angle": 0.2,
    "content_form": 0.2,
}
COMMERCIAL_MARKERS = {
    "actual_metrics",
    "actual_raw",
    "metric_states",
    "commercial_pattern",
    "commercial_levels",
    "spend_7d",
    "impressions_7d",
    "clicks_7d",
    "pay_orders_7d",
    "pay_gmv_7d",
    "settle_amount_7d",
    "settle_orders_7d",
    "refund_orders_7d",
}


class ContentPatternClusteringError(ValueError):
    pass


def normalize_content_text(video: dict[str, Any]) -> str:
    joined = "\n".join(str(video.get(field, "")) for field in CONTENT_FIELDS)
    normalized = unicodedata.normalize("NFKC", joined).lower()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", normalized)


def text_similarity(left: dict[str, Any], right: dict[str, Any]) -> float:
    first = normalize_content_text(left)
    second = normalize_content_text(right)
    if not first and not second:
        return 1.0
    if not first or not second:
        return 0.0
    first_pairs = _bigrams(first)
    second_pairs = _bigrams(second)
    union = first_pairs | second_pairs
    jaccard = len(first_pairs & second_pairs) / len(union) if union else 0.0
    return round((jaccard + SequenceMatcher(None, first, second).ratio()) / 2, 8)


def sample_similarity(left: dict[str, Any], right: dict[str, Any]) -> float:
    category = sum(
        weight
        for name, weight in DIMENSION_WEIGHTS.items()
        if left["labels"][name] == right["labels"][name]
    )
    semantic = text_similarity(left["video_content"], right["video_content"])
    return round(0.7 * category + 0.3 * semantic, 8)


def build_content_patterns(
    samples: list[dict[str, Any]],
    min_cluster_size: int = 3,
    max_clusters: int = 15,
) -> dict[str, Any]:
    ordered = _validate_samples(samples, min_cluster_size, max_clusters)
    similarities = _similarity_matrix(ordered)
    partitions = _agglomerative_partitions(ordered, similarities, max_clusters)
    repaired = [_repair_small_clusters(item, similarities, min_cluster_size) for item in partitions]
    unique = {
        tuple(sorted(tuple(cluster) for cluster in partition)): partition
        for partition in repaired
    }
    minimum_pattern_count = min(3, len(samples) // min_cluster_size)
    valid = [
        item
        for item in unique.values()
        if len(item) >= minimum_pattern_count and min(map(len, item)) >= min_cluster_size
    ]
    if not valid:
        raise ContentPatternClusteringError("无法生成满足最小样本数的Content Pattern")
    best_silhouette = max(_silhouette(item, similarities) for item in valid)
    near_best = [
        item
        for item in valid
        if _silhouette(item, similarities) >= best_silhouette - 0.02
    ]
    clusters = max(
        near_best,
        key=lambda item: (
            -statistics.pstdev(len(cluster) for cluster in item),
            _mean_intra(item, similarities),
            len(item),
        ),
    )
    clusters = sorted(clusters, key=lambda indexes: tuple(ordered[i]["material_id"] for i in indexes))
    profiles: dict[str, Any] = {}
    assignment: dict[str, str] = {}
    for number, indexes in enumerate(clusters, 1):
        code = f"V{number:02d}"
        members = [ordered[index] for index in indexes]
        profile = _build_profile(members, indexes, similarities)
        profiles[code] = profile
        assignment.update({member["material_id"]: code for member in members})
    return {
        "profiles": profiles,
        "assignment": assignment,
        "silhouette": round(_silhouette(clusters, similarities), 8),
        "mean_intra_similarity": round(_mean_intra(clusters, similarities), 8),
        "mean_inter_similarity": round(_mean_inter(clusters, similarities), 8),
        "parameters": {
            "algorithm": "average_linkage_silhouette_v1",
            "min_cluster_size": min_cluster_size,
            "max_clusters": max_clusters,
            "category_similarity_weight": 0.7,
            "semantic_similarity_weight": 0.3,
            "silhouette_tolerance_for_balance": 0.02,
        },
    }


def _bigrams(text: str) -> set[str]:
    if len(text) < 2:
        return {text}
    return {text[index:index + 2] for index in range(len(text) - 1)}


def _validate_samples(
    samples: list[dict[str, Any]], min_cluster_size: int, max_clusters: int
) -> list[dict[str, Any]]:
    if min_cluster_size < 3 or max_clusters < 1 or len(samples) < min_cluster_size:
        raise ContentPatternClusteringError("聚类样本或约束无效")
    ids = [str(item.get("material_id", "")).strip() for item in samples]
    if not all(ids) or len(ids) != len(set(ids)):
        raise ContentPatternClusteringError("material_id必须非空且唯一")
    for item in samples:
        if COMMERCIAL_MARKERS & set(item):
            raise ContentPatternClusteringError("Content Pattern输入不得包含商业数据")
        if set(item.get("labels", {})) != set(DIMENSION_WEIGHTS):
            raise ContentPatternClusteringError("每条样本必须包含完整H/P/S/A/F标签")
        video = item.get("video_content")
        if not isinstance(video, dict) or COMMERCIAL_MARKERS & set(video):
            raise ContentPatternClusteringError("视频内容缺失或包含商业数据")
    return sorted(samples, key=lambda item: str(item["material_id"]))


def _similarity_matrix(samples: list[dict[str, Any]]) -> list[list[float]]:
    size = len(samples)
    matrix = [[1.0 if left == right else 0.0 for right in range(size)] for left in range(size)]
    for left in range(size):
        for right in range(left + 1, size):
            value = sample_similarity(samples[left], samples[right])
            matrix[left][right] = matrix[right][left] = value
    return matrix


def _agglomerative_partitions(
    samples: list[dict[str, Any]], similarities: list[list[float]], max_clusters: int
) -> list[list[tuple[int, ...]]]:
    clusters = [(index,) for index in range(len(samples))]
    partitions = []
    while len(clusters) > 1:
        candidates = []
        for left in range(len(clusters)):
            for right in range(left + 1, len(clusters)):
                score = _cross_mean(clusters[left], clusters[right], similarities)
                key = (clusters[left], clusters[right])
                candidates.append((score, key, left, right))
        _, _, left, right = max(candidates, key=lambda item: (item[0], tuple(-v for group in item[1] for v in group)))
        merged = tuple(sorted((*clusters[left], *clusters[right])))
        clusters = [cluster for index, cluster in enumerate(clusters) if index not in {left, right}]
        clusters.append(merged)
        clusters.sort()
        if len(clusters) <= max_clusters:
            partitions.append(list(clusters))
    return partitions


def _repair_small_clusters(
    clusters: list[tuple[int, ...]], matrix: list[list[float]], minimum: int
) -> list[tuple[int, ...]]:
    result = sorted(tuple(cluster) for cluster in clusters)
    while any(len(cluster) < minimum for cluster in result) and len(result) > 1:
        small_index = min(
            (index for index, cluster in enumerate(result) if len(cluster) < minimum),
            key=lambda index: (len(result[index]), result[index]),
        )
        target_index = max(
            (index for index in range(len(result)) if index != small_index),
            key=lambda index: (
                _cross_mean(result[small_index], result[index], matrix),
                tuple(-value for value in result[index]),
            ),
        )
        merged = tuple(sorted((*result[small_index], *result[target_index])))
        result = [
            cluster
            for index, cluster in enumerate(result)
            if index not in {small_index, target_index}
        ]
        result.append(merged)
        result.sort()
    return result


def _silhouette(clusters: list[tuple[int, ...]], matrix: list[list[float]]) -> float:
    if len(clusters) == 1:
        return 0.0
    scores = []
    for cluster in clusters:
        for index in cluster:
            own = [matrix[index][other] for other in cluster if other != index]
            a = 1 - (sum(own) / len(own) if own else 1.0)
            b = min(1 - sum(matrix[index][other] for other in rival) / len(rival) for rival in clusters if rival != cluster)
            scores.append((b - a) / max(a, b) if max(a, b) else 0.0)
    return sum(scores) / len(scores)


def _cluster_mean(cluster: tuple[int, ...], matrix: list[list[float]]) -> float:
    pairs = [matrix[left][right] for pos, left in enumerate(cluster) for right in cluster[pos + 1:]]
    return sum(pairs) / len(pairs) if pairs else 1.0


def _cross_mean(left: tuple[int, ...], right: tuple[int, ...], matrix: list[list[float]]) -> float:
    values = [matrix[a][b] for a in left for b in right]
    return sum(values) / len(values)


def _mean_intra(clusters: list[tuple[int, ...]], matrix: list[list[float]]) -> float:
    return sum(_cluster_mean(cluster, matrix) for cluster in clusters) / len(clusters)


def _mean_inter(clusters: list[tuple[int, ...]], matrix: list[list[float]]) -> float:
    values = [_cross_mean(left, right, matrix) for pos, left in enumerate(clusters) for right in clusters[pos + 1:]]
    return sum(values) / len(values) if values else 0.0


def _build_profile(
    members: list[dict[str, Any]], indexes: tuple[int, ...], matrix: list[list[float]]
) -> dict[str, Any]:
    representative_index = max(
        indexes,
        key=lambda index: (
            sum(matrix[index][other] for other in indexes if other != index) / max(1, len(indexes) - 1),
            tuple(-ord(char) for char in members[indexes.index(index)]["material_id"]),
        ),
    )
    supports: dict[str, dict[str, float]] = {}
    dominant: dict[str, str] = {}
    for dimension in DIMENSION_WEIGHTS:
        counts = Counter(member["labels"][dimension] for member in members)
        dominant[dimension] = sorted(counts, key=lambda code: (-counts[code], code))[0]
        supports[dimension] = {
            code: round(count / len(members), 8) for code, count in sorted(counts.items())
        }
    ordered = sorted(members, key=lambda item: item["material_id"])
    representative_id = next(
        item["material_id"]
        for item in members
        if item["material_id"] == members[indexes.index(representative_index)]["material_id"]
    )
    return {
        "sample_count": len(members),
        "material_ids": [item["material_id"] for item in ordered],
        "representative_material_id": representative_id,
        "dominant_labels": dominant,
        "label_support": supports,
        "prototypes": [
            {
                "material_id": item["material_id"],
                "labels": item["labels"],
                "video_content": item["video_content"],
            }
            for item in ordered
        ],
        "cohesion": round(_cluster_mean(indexes, matrix), 8),
    }
