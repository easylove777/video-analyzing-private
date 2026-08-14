DEFAULT_CONFIG = {
    "schema_version": "1.0",
    "review_window_days": 7,
    "quality_gate": {
        "min_spend": 50,
        "min_impressions": 1000,
        "min_clicks": 20,
    },
    "dimension_confidence": {
        "conversion_high_clicks": 50,
    },
    "sedimentation": {
        "min_observations": 5,
        "direction_consistency": 0.60,
        "valid_ratio": 0.70,
    },
    "blind_prediction": {
        "formal_schema": "4.0",
        "require_isolated_context": True,
        "require_user_confirmation": True,
        "max_agent_retries": 2,
        "batch_context_scope": "per_material",
    },
    "timezone": "Asia/Shanghai",
}


DATA_DIRECTORIES = (
    "rules/sources",
    "rules/manifests",
    "rules/proposals",
    "case-libraries",
    "blind-runs",
    "batch-runs",
    "predictions",
    "observations/items",
    "observations/clusters",
    "proposals",
    "reports",
    "audit",
)
