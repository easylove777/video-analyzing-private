from __future__ import annotations

from pathlib import Path
from typing import Any

from errors import ValidationError
from sedimentation import create_sedimentation_proposal, evaluate_cluster
from store import (
    exclusive_lock,
    file_transaction,
    file_transaction_locked,
    initialize_data_root,
    now_utc,
    read_json,
    stable_hash,
    write_json_atomic,
    write_json_new,
)


def create_observation(data_root: str | Path, review: dict[str, Any]) -> dict[str, Any]:
    if review.get("quality_gate", {}).get("status") != "eligible":
        raise ValidationError("未通过质量门槛的复盘不能生成正式Observation")
    root = Path(data_root)
    initialize_data_root(root)
    observation_id = _observation_id(review)
    observation_path = root / "observations" / "items" / review["material_id"] / f"{observation_id}.json"
    cluster_entries = [_cluster_entry(root, review, code) for code in review["advice_codes"]]
    new_observation = {
        **review,
        "observation_id": observation_id,
        "cluster_keys": [entry["key"] for entry in cluster_entries],
        "created_at": now_utc(),
        "observation_path": str(observation_path.resolve()),
    }
    paths = [observation_path, *(entry["path"] for entry in cluster_entries)]
    with file_transaction(root, paths):
        if observation_path.exists():
            observation = read_json(observation_path)
            clusters = [read_json(entry["path"]) for entry in cluster_entries]
        else:
            observation = new_observation
            clusters = [
                _updated_cluster(entry["path"], entry["id"], entry["key"], observation)
                for entry in cluster_entries
            ]
            write_json_new(observation_path, observation)
            for entry, cluster in zip(cluster_entries, clusters):
                write_json_atomic(entry["path"], cluster)
    proposals = []
    for entry in cluster_entries:
        proposal = _maybe_create_proposal(root, entry["path"])
        if proposal:
            proposals.append(proposal)
    clusters = [read_json(entry["path"]) for entry in cluster_entries]
    return _result(root, observation, clusters, proposals)


def _observation_id(review: dict[str, Any]) -> str:
    identity = [
        review["prediction_id"],
        review["actual_data_fingerprint"],
        review["review_algorithm_version"],
    ]
    return f"obs_{stable_hash(identity)[:20]}"


def _cluster_key(review: dict[str, Any], advice_code: str) -> str:
    major = str(review["rule_version"]).split(".")[0]
    patterns = review["patterns"]
    return "|".join(
        (
            major,
            patterns["content_pattern"],
            patterns["predicted_commercial_pattern"],
            patterns["actual_commercial_pattern"],
            review["primary_deviation_dimension"],
            review["direction"],
            advice_code,
        )
    )


def _cluster_entry(root: Path, review: dict[str, Any], advice_code: str) -> dict[str, Any]:
    key = _cluster_key(review, advice_code)
    cluster_id = f"cluster_{stable_hash(key)[:16]}"
    return {
        "key": key,
        "id": cluster_id,
        "path": root / "observations" / "clusters" / f"{cluster_id}.json",
    }


def _updated_cluster(
    path: Path,
    cluster_id: str,
    cluster_key: str,
    observation: dict[str, Any],
) -> dict[str, Any]:
    cluster = read_json(path) if path.exists() else {
        "cluster_id": cluster_id,
        "cluster_key": cluster_key,
        "rule_major_version": int(cluster_key.split("|", 1)[0]),
        "observation_ids": [],
        "valid_observation_ids": [],
        "material_observation_ids": {},
        "direction_counts": {},
        "consistency_scores": {},
        "status": "accumulating",
        "open_proposal_id": None,
        "last_proposal_observation_ids": [],
    }
    material_id = observation["material_id"]
    if material_id in cluster["material_observation_ids"]:
        return cluster
    observation_id = observation["observation_id"]
    cluster["material_observation_ids"][material_id] = observation_id
    cluster["observation_ids"].append(observation_id)
    if observation.get("is_valid_for_sedimentation", False):
        cluster["valid_observation_ids"].append(observation_id)
        direction = observation["direction"]
        cluster["direction_counts"][direction] = cluster["direction_counts"].get(direction, 0) + 1
        cluster["consistency_scores"][observation_id] = float(
            observation.get("evidence_consistency", 1.0)
        )
    cluster["updated_at"] = now_utc()
    return cluster


def _maybe_create_proposal(root: Path, path: Path) -> dict[str, Any] | None:
    with exclusive_lock(root / ".write.lock"):
        cluster = read_json(path)
        config = read_json(root / "config.json")
        if not evaluate_cluster(cluster, config)["ready"]:
            return None
        proposal_id = f"sed_{stable_hash([cluster['cluster_id'], sorted(cluster['valid_observation_ids'])])[:16]}"
        proposal_path = root / "proposals" / f"{proposal_id}.json"
        with file_transaction_locked((path, proposal_path)):
            proposal = create_sedimentation_proposal(root, cluster)
            cluster["status"] = "proposed"
            cluster["open_proposal_id"] = proposal["proposal_id"]
            cluster["last_proposal_observation_ids"] = sorted(
                cluster["valid_observation_ids"]
            )
            write_json_atomic(path, cluster)
        return proposal


def _result(
    root: Path,
    observation: dict[str, Any],
    clusters: list[dict[str, Any]],
    proposals: list[dict[str, Any]],
) -> dict[str, Any]:
    progress = [_cluster_progress(root, cluster) for cluster in clusters]
    return {
        **observation,
        "cluster_progress": progress[0],
        "cluster_progresses": progress,
        "proposal": proposals[0] if proposals else None,
        "proposals": proposals,
    }


def _cluster_progress(root: Path, cluster: dict[str, Any]) -> dict[str, Any]:
    evaluation = evaluate_cluster(cluster, read_json(root / "config.json"))
    return {
        **evaluation,
        "cluster_id": cluster["cluster_id"],
        "unique_material_count": len(cluster["material_observation_ids"]),
        "status": cluster["status"],
    }


def _open_proposal(root: Path, cluster: dict[str, Any]) -> dict[str, Any] | None:
    proposal_id = cluster.get("open_proposal_id")
    path = root / "proposals" / f"{proposal_id}.json"
    return read_json(path) if proposal_id and path.exists() else None
