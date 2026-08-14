# Observation沉淀与审批

同一Cluster同时满足以下条件才生成提案：唯一有效Observation不少于5、偏差方向一致率不少于60%、有效样本率不少于70%、规则主版本一致、没有未处理同类提案。

状态流转为`accumulating → proposed → approved/rejected → incorporated`。达到门槛自动创建`proposed`提案并展示；不得自动批准。批准必须包含具体`proposal_id`，并重新验证Observation、manifest和活动案例库哈希。批准后创建新的不可变case-library快照；拒绝保存原因，不删除原提案。
