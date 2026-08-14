# 预测方法

## 1. 隔离评分

正式新预测使用 schema v4。H/P/S/A/F 分类和候选语义相似度必须在不继承主对话的全新上下文中完成；该上下文不得使用工具，也看不到历史案例实际商业结果。

流程分两段：

1. `classification` 只接收八字段视频内容和内容 taxonomy，返回五维标签与逐维证据。
2. Python 根据分类选择候选案例，删除真实 `case_id`、历史 `material_id` 和全部商业结果，只向同一个隔离上下文发送匿名候选内容；`similarity` 返回每个盲 ID 的 0–100 分数和理由。

两阶段请求、响应、候选映射、活动 manifest 与案例库均由 SHA-256 绑定。任一自检为污染、字段集合不完整、候选多余或缺失、哈希不一致时失败关闭。

## 2. 案例选择与权重

先确定 H/P/S/A/F 和 Content Pattern。同 Pattern 案例不少于 5 条时使用同 Pattern 池，否则按规则决定是否扩展到全部活动案例并降低置信度。prototype-v1 强制使用 `same_pattern_only`。

五维分类相似度各占 20%；总相似度由分类相似度 70% 和隔离语义相似度 30% 组成；组合分数平方作为案例权重。Top-K 为 `min(N, clamp(ceil(sqrt(N)), 5, 20))`。本次隔离升级不修改这些权重、Top-K、商业 Level 阈值或 Commercial Pattern 规则。

## 3. 数值预测

九项指标分别保存 `undefined/zero/positive` 加权概率，只对 positive 样本计算 P25/P50/P75，`undefined` 不得转成 0。

schema v3/v4 同时对 `spend/shows/clicks/pay_orders/pay_gmv/settle_amount/settle_orders/refund_orders` 生成包含零值的 `primitive_predictions` P25/P50/P75。日均消耗、GMV、订单直接取相应基础量 P50；CTR、CVR、ROI、CPO、退款率和结算率由对应基础量 P50 相除得到 `overall_point_prediction`。预测分母为 0 时输出 `undefined` 并保留原因。

五个 Commercial 维度直接按 Top-K 案例已有 Level 加权，保存 Level 概率、P25/P50/P75 和中心 Level。Commercial Pattern 概率独立按 Top-K 实际 Pattern 权重计算。

## 4. 草稿、确认与身份

隔离回执先生成只读 draft，不写入 Prediction。用户必须回复 `确认预测 <draft_id>`；确认时重新校验回执、内容指纹、活动 manifest、案例库和重算预测哈希，全部一致才冻结正式 v4。

v4 Prediction ID 共同绑定内容指纹、manifest 哈希、案例库哈希、v4 算法版本、盲分析和 receipt 哈希。相同身份复用同一不可变快照；任何输入、算法、盲回执或资产变化都生成新快照。

历史 v2/v3 继续只读和复盘，不回写。内部离线评估可以使用 v3 无写入入口，但不得冒充正式盲预测。
