# 输入输出契约

## 预测输入

单条 JSON 必须包含：`material_id`、`caption`、`first_three_seconds`、`core_hook`、`pain_point`、`main_selling_point`、`audience_angle`、`content_form`。不得包含当前素材的真实投放指标、真实 Commercial Pattern 或复盘结果。

## 盲预测中间对象

- classification request/response：只承载本条内容、五维 taxonomy、标签、证据、自检和哈希。
- similarity request/response：候选只使用 `candidate_001` 形式的盲 ID，只包含内容与标签；响应必须精确覆盖全部候选。
- blind receipt：绑定双阶段四个哈希、候选映射哈希、活动资产哈希、评分器身份和自检通过后的分析结果。它不是 Prediction，也不包含历史实绩。
- prediction draft：包含完整只读 v4 预测和 `pending_confirmation` 状态；不进入待复盘、Observation 或案例池。

## 正式 Prediction

- schema v4：正式隔离盲预测。除 v3 的八项 `primitive_predictions`、九项三状态分布、正值条件 P50/P25/P75、`overall_point_prediction`、五维 Level 和 Commercial Pattern 外，必须包含完整 `blind_provenance`。
- schema v3：历史或内部离线评估快照，继续只读与复盘，不再作为正式新预测写入格式。
- schema v2：历史不可变快照，继续兼容读取与复盘，不回写。

## T+7 真实输入

必须包含：`material_id`、`prediction_id`、`observed_days`、`spend_7d`、`impressions_7d`、`clicks_7d`、`pay_orders_7d`、`pay_gmv_7d`、`settle_amount_7d`、`settle_orders_7d`、`refund_orders_7d`。计数和金额必须是非负有限数值。

## 闭环输出

- review schema v1：质量门槛、九项指标、五维诊断和建议；v3/v4 使用整体点预测。
- Observation schema v1：不可变复盘证据和 Cluster 键。
- sedimentation proposal：支持样本、门槛证据、建议变更和审批状态。
- training pattern rebuild proposal：只包含新规则源、prototype-v1 manifest、指定训练集案例库和只读验证报告；准备阶段不激活，也不写 Prediction、Review、Observation 或 Cluster。
