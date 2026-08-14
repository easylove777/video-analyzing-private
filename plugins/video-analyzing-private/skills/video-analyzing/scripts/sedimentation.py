from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
from typing import Any

from errors import ValidationError
from portable_paths import encode_store_path, resolve_store_path
from runtime_paths import add_data_root_argument, resolved_data_root
from store import (
    append_audit_locked,
    exclusive_lock,
    file_transaction,
    file_transaction_locked,
    get_active_assets,
    initialize_data_root,
    now_utc,
    read_json,
    stable_hash,
    write_json_atomic,
    write_json_new,
    write_jsonl_new,
)


def evaluate_cluster(cluster: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    valid_ids = set(cluster.get("valid_observation_ids", []))
    all_ids = set(cluster.get("observation_ids", []))
    valid_count = len(valid_ids)
    total_count = len(all_ids)
    consistency_scores = cluster.get("consistency_scores", {})
    if consistency_scores:
        consistency = sum(consistency_scores.get(item, 0.0) for item in valid_ids) / valid_count
    else:
        direction_counts = cluster.get("direction_counts", {})
        consistency = max(direction_counts.values(), default=0) / valid_count if valid_count else 0.0
    valid_ratio = valid_count / total_count if total_count else 0.0
    threshold = config["sedimentation"]
    checks = {
        "min_observations": valid_count >= threshold["min_observations"],
        "direction_consistency": consistency >= threshold["direction_consistency"],
        "valid_ratio": valid_ratio >= threshold["valid_ratio"],
        "same_rule_major": cluster.get("rule_major_version") is not None,
        "no_open_proposal": not cluster.get("open_proposal_id"),
        "new_evidence_since_last_proposal": (
            not cluster.get("last_proposal_observation_ids")
            or valid_ids != set(cluster["last_proposal_observation_ids"])
        ),
    }
    return {
        "ready": all(checks.values()),
        "valid_observation_count": valid_count,
        "total_observation_count": total_count,
        "direction_consistency": consistency,
        "valid_ratio": valid_ratio,
        "checks": checks,
    }


def create_sedimentation_proposal(
    data_root: str | Path, cluster: dict[str, Any]
) -> dict[str, Any]:
    root = Path(data_root)
    initialize_data_root(root)
    config = read_json(root / "config.json")
    evaluation = evaluate_cluster(cluster, config)
    if not evaluation["ready"]:
        raise ValidationError("Observation Cluster尚未达到沉淀门槛")
    proposal_id = _proposal_id_for_cluster(cluster)
    path = root / "proposals" / f"{proposal_id}.json"
    if path.exists():
        return _proposal_view(root, read_json(path))
    parts = cluster["cluster_key"].split("|")
    proposal = {
        "proposal_id": proposal_id,
        "proposal_type": "observation_sedimentation",
        "status": "proposed",
        "cluster_id": cluster["cluster_id"],
        "cluster_key": cluster["cluster_key"],
        "rule_major_version": cluster["rule_major_version"],
        "supporting_observation_ids": sorted(cluster["valid_observation_ids"]),
        "evidence_summary": {
            "content_pattern": parts[1],
            "predicted_commercial_pattern": parts[2],
            "actual_commercial_pattern": parts[3],
            "primary_dimension": parts[4],
            "direction": parts[5],
            "advice_code": parts[6],
            **evaluation,
        },
        "recommended_changes": [
            "复核相似案例选择与Top-K权重",
            "校准出值概率或正值区间",
            "仅在系统性证据充分时调整Pattern映射",
        ],
        "created_at": now_utc(),
    }
    proposal.update(_proposal_evidence(root, cluster))
    write_json_new(path, proposal)
    return proposal


def propose_ready_clusters(data_root: str | Path) -> dict[str, Any]:
    root = Path(data_root)
    initialize_data_root(root)
    proposals = []
    with exclusive_lock(root / ".write.lock"):
        config = read_json(root / "config.json")
        for cluster_path in sorted((root / "observations" / "clusters").glob("*.json")):
            cluster = read_json(cluster_path)
            if not evaluate_cluster(cluster, config)["ready"]:
                continue
            proposal_path = root / "proposals" / f"{_proposal_id_for_cluster(cluster)}.json"
            with file_transaction_locked((cluster_path, proposal_path)):
                proposal = create_sedimentation_proposal(root, cluster)
                cluster["status"] = "proposed"
                cluster["open_proposal_id"] = proposal["proposal_id"]
                cluster["last_proposal_observation_ids"] = sorted(
                    cluster["valid_observation_ids"]
                )
                cluster["updated_at"] = now_utc()
                write_json_atomic(cluster_path, cluster)
            proposals.append(proposal)
    return {"created_count": len(proposals), "proposals": proposals}


def _proposal_id_for_cluster(cluster: dict[str, Any]) -> str:
    return f"sed_{stable_hash([cluster['cluster_id'], sorted(cluster['valid_observation_ids'])])[:16]}"


def approve_proposal(
    data_root: str | Path, proposal_id: str, *, approved_by: str
) -> dict[str, Any]:
    if not approved_by.strip():
        raise ValidationError("批准人不能为空")
    _validate_proposal_id(proposal_id)
    root = Path(data_root)
    proposal_path = root / "proposals" / f"{proposal_id}.json"
    if not proposal_path.is_file():
        raise ValidationError(f"找不到沉淀提案: {proposal_id}")
    decision_path = root / "proposals" / f"{proposal_id}.decision.json"
    if decision_path.exists():
        decision = read_json(decision_path)
        if decision["status"] != "approved":
            raise ValidationError("该提案已有拒绝决定")
        return _proposal_view(root, read_json(proposal_path))
    decision = {
        "proposal_id": proposal_id,
        "status": "approved",
        "approved_by": approved_by.strip(),
        "approved_at": now_utc(),
    }
    proposal = read_json(proposal_path)
    incorporation = _incorporate_cases(root, proposal, decision_path, decision)
    return {**proposal, **decision, "incorporation": incorporation}


def reject_proposal(
    data_root: str | Path, proposal_id: str, *, rejected_by: str, reason: str
) -> dict[str, Any]:
    if not reason.strip():
        raise ValidationError("拒绝原因不能为空")
    if not rejected_by.strip():
        raise ValidationError("拒绝人不能为空")
    _validate_proposal_id(proposal_id)
    root = Path(data_root)
    proposal_path = root / "proposals" / f"{proposal_id}.json"
    if not proposal_path.is_file():
        raise ValidationError(f"找不到沉淀提案: {proposal_id}")
    decision = {
        "proposal_id": proposal_id,
        "status": "rejected",
        "rejected_by": rejected_by.strip(),
        "reason": reason.strip(),
        "rejected_at": now_utc(),
    }
    proposal = read_json(proposal_path)
    decision_path = root / "proposals" / f"{proposal_id}.decision.json"
    cluster_path = root / "observations" / "clusters" / f"{proposal['cluster_id']}.json"
    audit_path = root / "audit" / "events.jsonl"
    paths = [decision_path, audit_path, *( [cluster_path] if cluster_path.exists() else [] )]
    with file_transaction(root, paths):
        write_json_new(decision_path, decision)
        if cluster_path.exists():
            _set_cluster_status_locked(cluster_path, "rejected")
        append_audit_locked(
            root,
            "reject-sedimentation",
            {"proposal_id": proposal_id, "reason": reason.strip()},
        )
    return {**proposal, **decision}


def _proposal_view(root: Path, proposal: dict[str, Any]) -> dict[str, Any]:
    decision_path = root / "proposals" / f"{proposal['proposal_id']}.decision.json"
    return {**proposal, **read_json(decision_path)} if decision_path.exists() else proposal


def _incorporate_cases(
    root: Path,
    proposal: dict[str, Any],
    decision_path: Path,
    decision: dict[str, Any],
) -> dict[str, Any]:
    with exclusive_lock(root / ".write.lock"):
        if decision_path.exists():
            existing = read_json(decision_path)
            if existing.get("status") != "approved":
                raise ValidationError("该提案已有拒绝决定")
            return existing.get(
                "incorporation",
                {"status": "approved_without_active_case_library", "added_case_count": 0},
            )
        audit_path = root / "audit" / "events.jsonl"
        cluster_path = root / "observations" / "clusters" / f"{proposal['cluster_id']}.json"
        cluster_paths = (cluster_path,) if cluster_path.exists() else ()
        if not (root / "rules" / "active.json").is_file():
            with file_transaction_locked((decision_path, audit_path, *cluster_paths)):
                write_json_new(decision_path, decision)
                if cluster_paths:
                    _set_cluster_status_locked(cluster_path, "approved")
                append_audit_locked(root, "approve-sedimentation", {"proposal_id": proposal["proposal_id"], "added_case_count": 0})
            return {"status": "approved_without_active_case_library", "added_case_count": 0}
        active, _, current_cases = get_active_assets(root)
        expected_manifest = proposal.get("source_manifest_sha256")
        expected_cases = proposal.get("source_case_library_sha256")
        if expected_manifest and active["manifest_sha256"] != expected_manifest:
            raise ValidationError("活动manifest已变化，请重新生成沉淀提案")
        if expected_cases and active["case_library_sha256"] != expected_cases:
            raise ValidationError("活动案例库已变化，请重新生成沉淀提案")
        candidates = _proposal_candidate_cases(root, proposal, active["manifest_sha256"])
        existing_materials = {case["material_id"] for case in current_cases}
        additions = [case for case in candidates if case["material_id"] not in existing_materials]
        if not additions:
            with file_transaction_locked((decision_path, audit_path, *cluster_paths)):
                write_json_new(decision_path, decision)
                if cluster_paths:
                    _set_cluster_status_locked(cluster_path, "approved")
                append_audit_locked(root, "approve-sedimentation", {"proposal_id": proposal["proposal_id"], "added_case_count": 0})
            return {"status": "no_new_cases", "added_case_count": 0}
        revised_cases = [*current_cases, *additions]
        case_hash = stable_hash(revised_cases)
        case_path = root / "case-libraries" / active["rule_version"] / f"{case_hash[:16]}.jsonl"
        active_path = root / "rules" / "active.json"
        revised_active = {
            **active,
            "manifest_path": encode_store_path(
                root,
                resolve_store_path(root, active["manifest_path"]),
            ),
            "case_library_path": encode_store_path(root, case_path),
            "case_library_sha256": case_hash,
            "case_count": len(revised_cases),
            "sedimentation_proposal_id": proposal["proposal_id"],
            "activated_at": now_utc(),
        }
        decision_with_result = {
            **decision,
            "incorporation": {
                "status": "incorporated",
                "added_case_count": len(additions),
                "case_library_sha256": case_hash,
            },
        }
        with file_transaction_locked((case_path, active_path, decision_path, audit_path, *cluster_paths)):
            write_jsonl_new(case_path, revised_cases)
            write_json_atomic(active_path, revised_active)
            write_json_new(decision_path, decision_with_result)
            if cluster_paths:
                _set_cluster_status_locked(cluster_path, "incorporated")
            append_audit_locked(
                root,
                "approve-sedimentation",
                {"proposal_id": proposal["proposal_id"], "added_case_count": len(additions)},
            )
    return decision_with_result["incorporation"]


def _proposal_candidate_cases(
    root: Path, proposal: dict[str, Any], manifest_hash: str
) -> list[dict[str, Any]]:
    cases = []
    for observation_id in proposal["supporting_observation_ids"]:
        matches = list((root / "observations" / "items").glob(f"*/{observation_id}.json"))
        if not matches:
            raise ValidationError(f"沉淀提案缺少Observation: {observation_id}")
        observation = read_json(matches[0])
        expected_hash = proposal.get("observation_sha256", {}).get(observation_id)
        if expected_hash and stable_hash(observation) != expected_hash:
            raise ValidationError(f"Observation哈希已变化: {observation_id}")
        case = observation.get("candidate_case")
        if not case:
            continue
        if case["rule_manifest_sha256"] != manifest_hash:
            raise ValidationError("候选案例与活动manifest哈希不一致")
        cases.append(case)
    return cases


def _proposal_evidence(root: Path, cluster: dict[str, Any]) -> dict[str, Any]:
    observations = []
    hashes = {}
    for observation_id in cluster.get("observation_ids", []):
        matches = list((root / "observations" / "items").glob(f"*/{observation_id}.json"))
        if not matches:
            continue
        observation = read_json(matches[0])
        observations.append(observation)
        hashes[observation_id] = stable_hash(observation)
    drivers = {}
    for dimension in ("attraction", "conversion", "efficiency", "scale", "quality"):
        gaps = [
            item.get("dimension_analysis", {}).get(dimension, {}).get("level_gap")
            for item in observations
        ]
        numeric = [gap for gap in gaps if isinstance(gap, (int, float))]
        drivers[dimension] = sum(numeric) / len(numeric) if numeric else None
    labels = {name: {} for name in ("core_hook", "pain_point", "main_selling_point", "audience_angle", "content_form")}
    for observation in observations:
        for name, value in observation.get("candidate_case", {}).get("labels", {}).items():
            labels[name][value] = labels[name].get(value, 0) + 1
    active_path = root / "rules" / "active.json"
    active = read_json(active_path) if active_path.exists() else {}
    supporting = [
        item["observation_id"]
        for item in observations
        if item.get("is_valid_for_sedimentation") and item.get("evidence_consistency", 0) >= 0.60
    ]
    opposing = [item["observation_id"] for item in observations if item["observation_id"] not in supporting]
    return {
        "source_manifest_sha256": active.get("manifest_sha256"),
        "source_case_library_sha256": active.get("case_library_sha256"),
        "cluster_snapshot_sha256": stable_hash(cluster),
        "observation_sha256": hashes,
        "five_dimension_drivers": drivers,
        "content_label_distribution": labels,
        "supporting_sample_ids": supporting,
        "opposing_sample_ids": opposing,
        "affected_case_count": len({item["material_id"] for item in observations}),
        "rule_change_diff": {
            "advice_code": cluster["cluster_key"].split("|")[-1],
            "automatic_activation": False,
        },
    }


def _set_cluster_status_locked(path: Path, status: str) -> None:
    cluster = read_json(path)
    cluster["status"] = status
    cluster["open_proposal_id"] = None
    cluster["updated_at"] = now_utc()
    write_json_atomic(path, cluster)


def _validate_proposal_id(proposal_id: str) -> None:
    if not re.fullmatch(r"sed_[0-9a-f]{16}", proposal_id):
        raise ValidationError("沉淀proposal_id格式无效")


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="operation", required=True)
    approve = subparsers.add_parser("approve")
    add_data_root_argument(approve)
    approve.add_argument("--proposal-id", required=True)
    approve.add_argument("--approved-by", required=True)
    reject = subparsers.add_parser("reject")
    add_data_root_argument(reject)
    reject.add_argument("--proposal-id", required=True)
    reject.add_argument("--rejected-by", required=True)
    reject.add_argument("--reason", required=True)
    propose = subparsers.add_parser("propose")
    add_data_root_argument(propose)
    args = parser.parse_args()
    args.data_root = str(resolved_data_root(args))
    if args.operation == "approve":
        result = approve_proposal(
            args.data_root, args.proposal_id, approved_by=args.approved_by
        )
    elif args.operation == "reject":
        result = reject_proposal(
            args.data_root,
            args.proposal_id,
            rejected_by=args.rejected_by,
            reason=args.reason,
        )
    else:
        result = propose_ready_clusters(args.data_root)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
