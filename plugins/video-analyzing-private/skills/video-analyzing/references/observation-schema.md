# Observation契约

Observation至少保存`observation_id`、`material_id`、`prediction_id`、规则和案例库哈希、质量门槛、Content Pattern、预测和实际Commercial Pattern、九项指标分析、五维分析、内容建议、模型建议、建议代码、Cluster键、样本池动作及候选案例。

Observation ID绑定prediction ID、真实数据指纹和复盘算法版本。完全相同输入幂等复用。历史Observation不可覆盖。同一Cluster中同一material只计一次；不同规则主版本不得混合。

标准代码包括`NO_CHANGE_SAMPLE_ONLY`、`REPLICATE_SUCCESS`、五个`OPTIMIZE_*`、`OCCURRENCE_MISS`、`MAGNITUDE_MISS`、`CALIBRATE_POSITIVE_PROBABILITY`、`CALIBRATE_POSITIVE_INTERVAL`、`CALIBRATE_CASE_MATCHING`和`CALIBRATE_PATTERN_MAPPING`。
