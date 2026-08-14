---
name: video-analyzing
description: 茶叶电商短视频的隔离盲预测、T+7真实投放复盘、Observation归池与模型沉淀闭环。用于导入或升级视频Pattern规则、构建版本化案例库、单条或批量预测八字段视频内容、分析预测与真实结果偏差、生成内容和模型建议、查询Observation Cluster进度，以及审批或拒绝沉淀提案；当用户提到茶叶视频预测、Commercial Pattern、五维诊断、T+7复盘、真实投放偏差、Observation、案例池或模型沉淀时使用。
---

# 茶叶视频预测分析闭环

使用简体中文。运行数据目录必须由 bootstrap 解析，不得猜测或硬编码。只读取用户明确提供的结构化内容和真实数据；不读取 MP4，不连接投放平台，不调用外部模型 API。

## 必须先执行 bootstrap

每次业务路由（包括只读查询）开始时，先运行且只运行一次：

```powershell
python <skill-root>\scripts\bootstrap.py --seed-dir <plugin-root>\seed --plugin-version 1.0.0
```

需要指定目录时可传 `--data-root`，否则解析顺序包括 `VIDEO_ANALYZING_DATA_ROOT`、Plugin 数据目录和本机用户数据目录。命令成功时，必须解析 bootstrap 成功时输出的唯一 JSON 对象，并在本次路由的所有后续命令中使用返回的 `data_root`；不得自行拼接目录，也不得回退到任何开发机路径。

- `initialized`：完整历史快照已初始化，可以继续当前业务路由。
- `reused`：已有数据库校验通过，可以继续当前业务路由；不得覆盖已有数据库。
- `read_only`：仅允许安全的状态和历史读取；禁止正式预测、复盘、提案、审批和任何写入。
- `error` 不是当前 CLI 返回的结构化状态；它表示 bootstrap 非零退出、抛出异常、没有产生有效 JSON 或后续完整性校验失败。此时立即停止，保留可取得的 stderr、已解析的 `data_root` 和恢复路径；初始化失败时禁止正式预测，并且禁止正式预测和任何写入。

Plugin 安装或升级都不是重建数据库的授权。bootstrap 不得用 seed 覆盖非空数据目录，也不得以升级版本为理由替换本机历史。

## 路由

bootstrap 通过后，再用 `scripts/router.py` 识别意图并执行一个操作：

- 导入规则或重建案例库：`prepare-rule-version`
- 基于已批准 Cluster 校准现有 V 类规则：`prepare-pattern-upgrade`
- 仅以指定训练集重建 Content Pattern：`prepare-training-pattern-rebuild`
- 明确批准规则提案：`activate-rule-version`
- 单条八字段内容盲预测：`predict`
- 八字段 JSON 目录盲预测：`predict-batch`
- 冻结预测加 T+7 真实数据：`review`
- 多条复盘汇总：`review-batch`
- 查询 Observation 或整体进度：`observation-status` 或 `status`
- 生成达到门槛的沉淀提案：`propose-sedimentation`
- 明确批准或拒绝具体沉淀提案：`approve-sedimentation` / `reject-sedimentation`

“继续”“看看”“可以”不等于批准。批准或拒绝必须包含具体 `proposal_id`。缺少用户级输入时只列缺失项，不创建业务数据。

## 正式 predict：隔离盲预测

先完整读取 [盲预测协议](references/blind-prediction-protocol.md) 和 [预测方法](references/prediction-method.md)。正式新预测只能走以下流程：

1. 检查八字段内容，拒绝当前素材真实投放数据。
2. 用 `scripts/blind_prediction.py prepare-classification` 创建 classification request。
3. 在主上下文读取 `../../internal/video-analyzing-blind-score/SKILL.md`，把该协议与 classification request 一并交给一个全新 Agent；必须使用 `fork_turns=none` 或平台等价的空上下文机制。
4. 新 Agent 不得调用任何工具，也不得收到主对话、历史预测、复盘、Commercial Pattern、Bucket 或案例实绩。
5. 校验 classification response。失败最多重试两次，每次都新建空上下文，不得把失败响应带入下一次。
6. 用 `accept-classification` 生成 similarity request。候选只能含盲 ID、内容字段和内容标签。
7. 把 similarity request 发送给步骤 3 的同一个干净 Agent；不得附带预测数值或任何历史表现。
8. 用 `accept-similarity` 校验响应、恢复本地 case ID 并生成 blind receipt。
9. 用 `draft` 创建只读 schema v4 草稿，完整展示给用户。此时不得写入正式 Prediction。
10. 只有收到精确文本 `确认预测 <draft_id>` 后，才用 `confirm` 重算并冻结正式 v4 快照。
11. 用户纠正内容事实或标签理解时，废弃原 run 和 draft，从步骤 2 新建 blind run；不得在主 Agent 看过预测数值后直接改评分 JSON。

如果无法创建全新上下文、隔离评分器使用了工具、继承了历史上下文、看到了商业数据、返回污染信号或无法通过契约校验，正式预测失败关闭。不得由主 Agent 补写 H/P/S/A/F 或语义分数，不得降级保存为 schema v4。

批量预测时，每条素材必须使用独立 Agent 上下文和唯一 `run_id`。先生成批量草稿；用户回复 `确认预测批次 <batch_draft_id>` 后逐条复核并冻结。单条失败不终止其他条目，也不得跨素材复用上下文。

## Prediction 输出

正式 v4 快照包含八项 primitive predictions、九项指标的 `undefined/zero/positive` 概率、正值条件 P50 和 P25/P75、`overall_point_prediction`、五维 Level 概率与区间、Commercial Pattern 概率、Top-K 证据、活动资产哈希和完整 blind provenance。v2/v3 仅用于历史读取、复盘和内部离线评估，不得作为正式新预测格式。

## prepare-rule-version

读取 [输入输出契约](references/input-output-schema.md) 和 [预测方法](references/prediction-method.md)，依次执行：

1. `scripts/compile_rules.py` 编译 Markdown 规则。
2. `scripts/normalize_history.py` 规范历史数据，或直接使用规范 JSON/JSONL。
3. 为历史内容生成 H/P/S/A/F 标签、逐维证据和语义评分。
4. `scripts/build_case_library.py` 生成案例库和拒绝报告。
5. `scripts/store.py prepare-rule-version` 保存不可变提案。
6. 报告 `proposal_id` 后暂停，等待明确批准。

只有用户明确批准具体提案时，才运行 `scripts/store.py activate-rule-version`。激活前重新校验规则、manifest 和案例库哈希。

## Pattern 升级与重建

`prepare-pattern-upgrade` 只基于已沉淀 Cluster 校准现有 V01–V10 定义边界、H/P/S/A/F 权重和冲突映射，不修改商业指标公式、Level 阈值或 Commercial Pattern 条件。准备阶段只生成不可变提案，不自动激活。

`prepare-training-pattern-rebuild` 只使用指定训练素材的八字段内容与 H/P/S/A/F 标签，不能读取商业结果。新模型使用 `same_pattern_only` 候选池；留一测评排除相同 `material_id` 和内容指纹，不生成 Prediction、Review、Observation、Cluster 或沉淀提案。

## review

读取 [复盘方法](references/review-method.md) 和 [Observation 契约](references/observation-schema.md)。正式复盘必须同时满足观察满 7 天、累计 Spend 不少于 50 元、累计曝光不少于 1,000、累计商品点击不少于 20，以及素材和版本关联完整。

未通过时只保存 `insufficient_data` 报告，不判断准确性、不生成建议、不进入 Observation。通过后完成九项指标 Occurrence/Magnitude 分析、五维诊断、实际 Commercial Pattern、内容建议和模型建议，并创建不可变 Observation。v2 沿用正值条件 P50；v3/v4 使用整体点预测复盘。

批量复盘使用 `scripts/review_batch.py`。每条真实数据必须包含 `prediction_id`，单条失败不终止整批。

## Observation 与沉淀

读取 [沉淀方法](references/sedimentation-method.md)。Observation 按“规则主版本 × Content Pattern × 预测 Commercial Pattern × 实际 Commercial Pattern × 主偏差维度 × 方向 × 建议代码”归入 Cluster。

达到配置门槛时只生成 `proposed` 提案，不自动批准。只有用户明确批准具体 `proposal_id` 后，才把提案绑定的候选案例写入新的不可变案例库并切换活动版本；拒绝必须保存拒绝人和原因。

## 不可突破的边界

- 预测阶段不得读取当前素材真实投放结果。
- 盲评分 Agent 永远拿不到历史案例实际商业结果。
- Prediction、blind run、Observation、提案原文和历史案例库不得覆盖。
- 单条样本不得直接修改 taxonomy、Content Pattern 或 Commercial Pattern 映射。
- 达到门槛只生成提案；没有明确批准不得切换规则或案例库。
- 数据不足时显示“不足”，不得伪造数值或把 `undefined` 写成 0。
