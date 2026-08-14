from __future__ import annotations

import re
from typing import Any


def route_request(text: str) -> dict[str, Any]:
    request = text.strip()
    proposal_id = _proposal_id(request)
    if "批准" in request and proposal_id and proposal_id.startswith("proposal_"):
        return _confirmed("activate-rule-version", proposal_id=proposal_id)
    if "批准" in request and proposal_id and proposal_id.startswith("sed_"):
        return _confirmed("approve-sedimentation", proposal_id=proposal_id)
    if "拒绝" in request and proposal_id and proposal_id.startswith("sed_"):
        return _confirmed("reject-sedimentation", proposal_id=proposal_id)
    if any(word in request for word in ("导入规则", "导入新的规则", "准备规则")):
        return _confirmed("prepare-rule-version")
    if "生成" in request and "沉淀提案" in request:
        return _confirmed("propose-sedimentation")
    if "批量" in request and "预测" in request:
        return _confirmed("predict-batch")
    if "批量" in request and ("复盘" in request or "分析" in request):
        return _confirmed("review-batch")
    if "复盘" in request or "预测和实际" in request:
        return _confirmed("review")
    if "预测" in request:
        return _confirmed("predict")
    if "Observation" in request or "observation" in request:
        return _confirmed("observation-status")
    if "状态" in request:
        return _confirmed("status")
    return {"status": "ambiguous", "operation": "unknown", "missing_inputs": []}


def _proposal_id(text: str) -> str | None:
    match = re.search(r"(?:proposal|sed)_[A-Za-z0-9_-]+", text)
    return match.group(0) if match else None


def _confirmed(operation: str, **detail: Any) -> dict[str, Any]:
    return {"status": "confirmed", "operation": operation, **detail}
