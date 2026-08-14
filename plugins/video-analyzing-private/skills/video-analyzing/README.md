# video-analyzing

茶叶电商短视频的内容分类、隔离盲预测、T+7真实投放复盘、Observation归池与模型沉淀闭环。

本 README 面向使用者说明实际运行方式。模型执行时仍以 [SKILL.md](SKILL.md)、`references/` 中的协议和 `scripts/` 中的确定性代码为准。

## 1. 能做什么

- 导入新规则并构建版本化案例库。
- 使用结构化八字段内容完成单条或批量盲预测。
- 输出九项商业指标、五个商业维度和 Commercial Pattern 概率。
- 冻结经用户确认的正式 Prediction 快照。
- 使用 T+7真实数据复盘正式 Prediction。
- 把有效复盘写成不可变 Observation 并归入 Cluster。
- 达到门槛后生成沉淀提案，由用户审批或拒绝。
- 基于已批准 Cluster 校准 Pattern，或只用指定训练内容重建 Content Pattern。

不做以下事情：

- 不读取 MP4；必须先提供结构化内容。
- 不连接投放平台。
- 不调用外部模型 API。
- 不允许预测阶段读取当前素材的真实投放结果。
- 不允许单条复盘直接修改 taxonomy、Pattern 或商业阈值。

## 2. 实际组成

```text
video-analyzing-private/
├─ skills/
│  └─ video-analyzing/
│     ├─ SKILL.md
│     ├─ README.md
│     ├─ agents/openai.yaml
│     ├─ scripts/                   # 路由、预测、复盘、存储、沉淀
│     ├─ references/                # 协议、方法、契约
│     └─ assets/
└─ internal/
   └─ video-analyzing-blind-score/  # 主Skill内部加载，不作为用户Skill发布
      └─ SKILL.md
```

隔离评分协议位于 `internal/video-analyzing-blind-score/SKILL.md`。主 Skill 在创建 `fork_turns=none` 的全新 Agent 前读取它；`internal/` 位于 Plugin 的 `skills/` 发现范围之外，因此评分器不会作为独立用户 Skill 出现。

数据目录保存活动规则、案例库、盲运行、Prediction、Review、Observation、Cluster、提案和审计记录。Skill目录是程序与方法，数据目录是运行状态；迁移时两者需要分别处理。数据目录不绑定开发者账号或固定绝对路径。

### 2.1 本地私有 Marketplace 安装

本发行版只通过本地私有 Marketplace 分发。生成的 Marketplace 根目录应同时包含 `.agents/plugins/marketplace.json` 和 `plugins/video-analyzing-private/`。对于非默认 Marketplace，先在本机注册并安装：

```powershell
codex plugin marketplace add <private-marketplace-root>
codex plugin add video-analyzing-private@<marketplace-name>
```

`<marketplace-name>` 必须读取该 Marketplace 的 `.agents/plugins/marketplace.json`，不能猜测。安装后新建任务，让 Codex 重新发现 Skill。安装只登记 Plugin 文件，不会在安装时执行 bootstrap，也不会写入业务数据库；首次调用 `$video-analyzing` 时，宿主才按 [SKILL.md](SKILL.md) 自动加载并运行 bootstrap。

### 2.2 首次调用与数据目录

每次业务路由都先执行：

```powershell
python <skill-root>\scripts\bootstrap.py --seed-dir <plugin-root>\seed --plugin-version 1.0.0
```

后续命令只使用 bootstrap 返回的 `data_root`。数据目录按以下优先级解析，前者存在时不再检查后者：

1. 命令行 `--data-root`。
2. 环境变量 `VIDEO_ANALYZING_DATA_ROOT`。
3. `PLUGIN_DATA/video-analyzing-data`。
4. `LOCALAPPDATA/video-analyzing/data`。
5. 最终回退 `~/.local/share/video-analyzing/data`。

bootstrap 成功时输出一个 JSON 对象，其结构化 `status` 只有以下三种；失败时没有结构化成功结果：

- `initialized`：空目标已从已校验 seed 初始化，可以继续。
- `reused`：已有数据库校验通过，可以继续；业务文件保持不变。
- `read_only`：仅可读取安全的状态和历史，禁止正式预测和所有写入。
- `error` 行为：它不是当前 CLI 返回的结构化 `status`，而是命令非零退出、抛出异常、没有有效 JSON 或后续完整性校验失败。停止路由，保留可取得的 stderr、备份、工作目录和已知 `data_root`，禁止正式预测和所有写入。

### 2.3 数据所有权与升级

seed 和每台机器首次初始化出的数据库都含完整历史数据，包括商业案例、预测、复盘、Observation 和审计记录，属于敏感商业资料。发行包和数据副本只在获授权的本机保存，不上传 GitHub，也不进入公共仓库、云盘或公开 Marketplace；GitHub 上传不属于当前发行范围。

每台机器使用自己的 `data_root`。机器 A 与机器 B 从相同 seed 初始化后即成为相互独立的数据库：一台机器的预测、复盘、审批或故障不会同步到另一台机器。需要共享新历史时必须走单独审批的数据迁移流程。

升级 Plugin 不得覆盖已有数据库。新版本只更新 Skill、脚本和随包 seed；bootstrap 遇到已有有效数据库时返回 `reused`，遇到较新或无法迁移的数据时进入 `read_only` 或报错，绝不能重新播种覆盖现有业务历史。

### 2.4 恢复

- 损坏的 seed：如果尚未初始化，停止首次调用，从可信的本地构建重新取得并校验完整发行包后再对空目录初始化。已有数据库不得用新 seed 覆盖。
- 损坏的活动资产：停止正式预测和写入，只做仍可安全完成的状态/历史读取；从最近一次完整且已校验的本机备份恢复整个 `data_root`，不要从 seed 或其他版本零散复制规则、manifest、案例库文件。
- 迁移失败：保留错误中报告的 backup 和 work 路径，不要再次写入原目录。先验证自动回滚结果；需要人工处理时，在副本上修复并通过完整性校验，再原子恢复经过验证的完整备份。

任何恢复都先保留当前目录并计算校验值。不要删除唯一副本，也不要把恢复操作变成覆盖式升级。

## 3. 当前运行基线

截至2026-08-13，本机活动快照为：

| 项目 | 当前值 |
|---|---|
| 规则版本 | `2.0.0` |
| Content Pattern模型 | `prototype-v1` |
| 候选范围 | `same_pattern_only` |
| 活动案例数 | 45 |
| Content Pattern | V01–V08 |
| 正式Prediction schema | `4.0` |
| 复盘窗口 | T+7 |

这些值是本机当前快照，不是永久常量。每次运行应以 `status` 返回的活动版本、manifest哈希和案例库哈希为准。

## 4. 输入：八字段视频内容

```json
{
  "material_id": "唯一素材ID",
  "caption": "完整口播或字幕",
  "first_three_seconds": "前三秒内容",
  "core_hook": "核心钩子描述",
  "pain_point": "主要痛点描述",
  "main_selling_point": "主要卖点描述",
  "audience_angle": "目标人群和购买动机",
  "content_form": "主要表达形式"
}
```

`material_id`和`caption`必填。其余字段缺失时，分类按manifest中的回退顺序使用 `caption`，但证据越完整，盲分类越稳定。

预测输入禁止包含实际指标、Commercial Pattern或训练案例表现。发现商业数据、未知字段或不安全的 `material_id` 时直接拒绝。

## 5. 视频分类思路

分类分两层，且都只看内容：

### 5.1 H/P/S/A/F五维单标签

| 维度 | 回答的问题 | 判断重点 |
|---|---|---|
| H core_hook | 为什么停留 | 优先看前三秒和第一主张 |
| P pain_point | 为什么犹豫 | 只选最主要的用户问题 |
| S main_selling_point | 为什么购买 | 只保留最核心购买理由 |
| A audience_angle | 谁会买、为什么买 | 人群与动机必须同时成立 |
| F content_form | 主要怎么讲 | 看主体形式，不看辅助镜头 |

每个维度必须选择活动taxonomy中存在的一个编码，并给出非空证据。不能输出多标签、主副标签或自行创建新编码。

### 5.2 Content Pattern

当前 `prototype-v1` 不使用旧的固定V类打分卡。实际算法为：

```text
五维标签相似度 = H/P/S/A/F命中权重之和，每维20%
文本相似度 = 字符二元组Jaccard与SequenceMatcher比例的平均
样本相似度 = 70% × 五维标签相似度 + 30% × 文本相似度
Content Pattern = 与新视频最相似的训练原型所属Pattern
```

如最高分相同，选择编号更小的Pattern。V类是训练内容聚类得到的机制簇，不是视频表现好坏，也不是Commercial Pattern。

完整人工判断卡见 [视频Pattern分类规则_人工操作版.md](references/视频Pattern分类规则_人工操作版.md)。

## 6. 正式预测工作流

```text
八字段校验
  → 生成classification request
  → 全新隔离Agent完成H/P/S/A/F
  → 主流程校验响应并生成匿名候选
  → 同一隔离Agent完成候选语义相似度
  → 主流程恢复本地case ID并生成blind receipt
  → 计算schema v4预测草稿
  → 用户确认
  → 重算哈希并冻结正式Prediction
```

### 6.1 上下文隔离

- 每条素材必须使用一个全新、无历史消息的评分上下文。
- 同一素材的classification与similarity在同一隔离上下文连续完成。
- 批量预测不能跨素材复用上下文。
- 隔离评分器不得使用任何工具、文件、网络或商业结果。
- 任一自检、字段、候选集合、身份或哈希不一致时失败关闭。
- 主Agent不得代替隔离评分器补写标签或相似度。

### 6.2 案例选择和权重

当前模型先把新视频分入Content Pattern，只从相同Pattern中选择候选。预测阶段的案例组合为：

```text
分类相似度 = H/P/S/A/F等权，每维20%
组合相似度 = 70% × 分类相似度 + 30% × 隔离语义相似度
案例权重 = 组合相似度²
Top-K = min(N, clamp(ceil(sqrt(N)), 5, 20))
```

当前 `same_pattern_only` 模型不回退到全局案例。排除相同 `material_id` 和相同内容指纹后，样本不足的指标可以输出 `not_available`，不能伪造数值。

### 6.3 数值输出

先预测八项基础量：

- spend
- shows
- clicks
- pay_orders
- pay_gmv
- settle_amount
- settle_orders
- refund_orders

再派生九项业务指标：

- 日均Spend、日均GMV、日均订单
- CTR、CVR
- 结算ROI、结算CPO
- 退款率、结算率

九项指标分别保存 `undefined/zero/positive` 概率；只在positive样本上计算P25/P50/P75。`overall_point_prediction`由八项基础量的整体P50派生，分母为0时输出 `undefined` 和原因。

另外输出Attraction、Conversion、Efficiency、Scale、Quality的Level概率与区间，以及Commercial Pattern概率和Top-K证据。

### 6.4 草稿与确认

盲评分完成后只生成只读草稿，不立即写入正式Prediction。

单条确认：

```text
确认预测 <draft_id>
```

批量确认：

```text
确认预测批次 <batch_draft_id>
```

确认时重新校验内容指纹、blind receipt、manifest、案例库和预测哈希。任一资产已变化时必须重新盲预测。

## 7. 批量预测

- 输入为八字段JSON目录。
- 每条素材有独立 `run_id`、独立隔离Agent、独立receipt和独立草稿。
- 单条失败不会终止其他素材。
- 先展示批量草稿，收到精确批次确认后才逐条冻结。
- 草稿不能作为T+7复盘依据，只有正式 `prediction_id` 可以复盘。

## 8. T+7复盘工作流

```text
正式prediction_id + T+7真实累计数据
  → 质量门槛
  → 九指标Occurrence/Magnitude分析
  → 五维Level诊断
  → 预测与实际Commercial Pattern对比
  → 内容建议与模型建议
  → 不可变Observation
  → Cluster归池
```

正式复盘必须同时满足：

- 观察不少于7天。
- 累计Spend不少于50元。
- 累计曝光不少于1,000。
- 累计商品点击不少于20。
- prediction、material、规则和案例库关联完整。

未通过时只保存 `insufficient_data` 报告：不判断准确性、不输出修改建议、不进入Observation。

### 8.1 偏差判断

- 实际 `undefined`：不可评估。
- 实际 `zero`：只判断是否出值，不比较正值区间。
- 实际 `positive`：与预测正值P25–P75比较。
- 区间内记为命中；越界距离除以区间宽度得到严重度。
- schema v3/v4使用 `overall_point_prediction`；历史v2使用正值条件P50。

当前代码中的 `relative_error` 是绝对相对误差：

```text
|预测值 - 实际值| ÷ |实际值|
```

如果报表需要展示高估或低估，可额外计算辅助字段：

```text
有符号相对误差 = (预测值 - 实际值) ÷ |实际值|
正数 = 预测偏高；负数 = 预测偏低
```

该辅助字段目前不替代 `review.py` 保存的 `relative_error`。实际值为0时只保留绝对误差和原因。

完整报告字段见 [茶叶视频T+7预测分析复盘模板.md](assets/茶叶视频T+7预测分析复盘模板.md)。

## 9. Observation与沉淀

有效复盘生成不可变Observation，并按以下键归池：

```text
规则主版本 × Content Pattern × 预测Commercial Pattern
× 实际Commercial Pattern × 主偏差维度 × 方向 × 建议代码
```

同一Cluster同时满足以下条件时才生成沉淀提案：

- 唯一有效Observation不少于5。
- 偏差方向一致率不少于60%。
- 有效样本率不少于70%。
- 规则主版本一致。
- 没有未处理的同类提案。

达到门槛只生成 `proposed`，不会自动激活。批准或拒绝必须包含具体 `proposal_id`：

```text
批准沉淀 <proposal_id>
拒绝沉淀 <proposal_id>，原因：<reason>
```

批准后创建新的不可变案例库快照并切换活动版本；历史Prediction、Observation、提案和案例库不得覆盖。

## 10. Pattern升级与训练重建

- `prepare-pattern-upgrade`：只根据已沉淀Cluster校准现有Pattern边界、五维权重和冲突映射；不修改商业公式、Level阈值或Commercial Pattern条件。
- `prepare-training-pattern-rebuild`：只使用指定训练集八字段内容与H/P/S/A/F，不读取商业结果；通过内容聚类重建Pattern，并进行留一评估。

两者都先生成不可变提案，不自动激活。单条样本不能直接升级Pattern。

## 11. 常用请求示例

```text
使用 $video-analyzing 查询当前状态。
使用 $video-analyzing 预测这条八字段视频内容。
使用 $video-analyzing 批量预测这个JSON目录。
确认预测 draft_xxx。
确认预测批次 batch_draft_xxx。
使用 $video-analyzing 复盘 prediction_id=pred_xxx 的T+7数据。
使用 $video-analyzing 批量复盘这个真实数据目录。
使用 $video-analyzing 查询Observation Cluster进度。
批准沉淀 proposal_xxx。
```

## 12. 运行边界

- `undefined`不能写成0。
- 草稿不能冒充正式Prediction。
- 预测不能看到当前素材真实结果。
- 盲评分器不能看到案例商业结果。
- 单条复盘只能形成Observation，不能直接改模型。
- 达到沉淀门槛只能形成提案，没有明确批准不得切换版本。
- 内容事实或标签被用户纠正后，原run与draft作废，必须从新的隔离上下文重跑。
