# 统一数据字典（动手前必读）

> 本文件是整个 skill **唯一**的数据契约说明。所有脚本的字段名、落库格式、报告口径都以本表为准。
> 改字段 = 改契约：必须同步改 `scripts/ur_core.py` 的 `COMMON_FIELDS` / `METRIC_FIELDS` / `REVIEW_FIELDS` / `Record`、
> `scripts/ur_report.py` 的 `build_rows()`、`examples/fixtures/*`，并跑一遍 `scripts/ur_verify.py`。
> 完整口径规则见 `references/analysis-playbook.md`，防臆测规则见 `references/anti-hallucination.md`。

## 一、一句话设计：一个契约、两类行、三份文件

只做数值采集时是「一天一渠道一个数」；现在多了「一条评论一句话」。如果拆成两套完全无关的表，做「评分下滑 ↔ 差评主题上升」这类交叉时就要临时拼，极容易两边各算一套口径——这正是要设唯一口径计算中心 `build_rows()` 来防的毛病。

所以 **共用同一套公共字段**，用 `record_type` 区分这一行是数值还是评论；两边都能按同一把钥匙对齐到同一个「切片」。

- 数值 → 长表 CSV（沿用常见做法：一行 = 一天一渠道一指标，`utf-8-sig`，列结构定稿）
- 评论 → JSONL（一行一条原文，append-only，便于多标签数组和逐字原句）
- 两者由 `ur_core.py` 的同一份字段契约约束，`ur_report.py::build_rows()` 是**唯一**口径计算中心

三份源文件：`data/metrics.csv`、`data/reviews.jsonl`、`data/snapshots/**`（原始响应副本）。其余都是可随时重算的派生结果。

## 二、公共字段（18 列，定稿，不轻易增删）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `record_id` | str | ✅ | 数值 `metric:{channel_key}:{day}:{metric_key}:{metric_kind}`；评论 `{channel_key}:{review_id}` |
| `record_type` | `metric` / `review` | ✅ | 一表两型的开关 |
| `game_key` | str | ✅ | 项目/游戏代号，**不带死具体游戏名**——可迁移的关键 |
| `channel_key` | str | ✅ | 渠道代号，来自 `channels.yaml` |
| `channel_name` | str | ✅ | 展示名，来自配置 |
| `day` | `YYYY-MM-DD` | ✅ | 业务日期（不是采集时刻） |
| `captured_at` | ISO 8601 | ✅ | **真实采集时刻**。补录/平移填补必须标真实时刻，禁止假装当场采到 |
| `source` | str | ✅ | 具体来源（接口名 / 页面 / 投放文件名） |
| `source_url` | str | | 公开可访问地址，便于合规核查 |
| `status` | `ok` / `failed` / `skipped` | ✅ | 状态。**排名等独立维度不用一个 status 硬套** |
| `precision` | float | ✅ | **最小可分辨单位**（数值量纲，可直接比较/参与计算）；评论行填 `0` 表示不适用 |
| `raw` | str | ✅ | 原始文案 / 原始 JSON 片段，**一个系数不乘** |
| `warn` | str | | 告警：数据本身可信、但要人看一眼。**不拦数据、照常入库**，只进报告异常区 |
| `error` | str | | 错误：拦数据，不入库 |
| `note` | str | | 备注只写业务三件事（从哪来 / 能用多久 / 要不要人确认） |
| `snapshot_path` | str | | 原始响应不可变副本的相对路径（证据链） |
| `ingest_batch` | str | ✅ | 采集批次号，便于重跑与审计 |
| `compliance_tag` | `public_anonymous` / `manual_confirmed` | ✅ | 合规标记；自有账号/自有后台取数一律 `manual_confirmed` 且 `note` 带填写人 |

## 三、数值专用字段（`record_type = metric`）

| 字段 | 类型 | 说明 |
|---|---|---|
| `metric_key` | str | `reservations` / `rating` / `review_count` / `rank` / `positive_rate` |
| `metric_value` | float / 空 | 数值或空。**禁止填 0 冒充没数** |
| `metric_unit` | str | `人` / `分` / `名` / `%` |
| `metric_kind` | `outer` / `real` | 外显值 / 回传真值。**两类不可混着看、更不可混加** |

### `precision` 数值量纲（最值钱的一层抽象）

`precision` 存的是「最小可分辨单位」，不是标签，所以能直接比较、能参与计算：

| 值 | 含义 | 能算增量吗 |
|---|---|---|
| `1` | 个位精确（人） | ✅ 能 |
| `1000` | 万级一位（±500） | ❌ 只能看趋势 |
| `10000` | 万级整数（±5000） | ❌ 只能看量级 |
| `0.1` | 评分一位小数 | ✅ 能 |

**铁律**：`abs(变化量) < precision` → 变化写「—」，**绝不写 0**。由 `build_rows()` 统一执行，任何下游出口都不许自己算。

`precision` 映射出的可信度三档文案：精确·可算增量 / 万级一位·仅趋势 / 万级整数·仅量级。**口径 ≠ 精度**：`metric_kind`（外显/回传）与 `precision` 是正交两维，报告里两列分开。

## 四、评论专用字段（`record_type = review`）

| 字段 | 类型 | 说明 |
|---|---|---|
| `review_id` | str | 平台原始 ID，**引用定位的唯一锚点**（grounding check 靠它） |
| `review_text` | str | **逐字原句**。不改写、不摘要、不翻译 |
| `rating` | float | 评分 |
| `rating_max` | float | 满分。**5 分制与 10 分制分开存，禁止不可比聚合** |
| `review_time` | ISO 8601 | 评论发布时间 |
| `app_version` | str | 版本（交叉矩阵维度），缺失落「未指明」桶 |
| `playtime_hours` | float | 游玩时长（时长段分层） |
| `lang` / `country` | str | 语言 / 地区 |
| `votes_up` | int | 被赞数（行为层影响力轴） |
| `developer_replied` | bool | 是否有开发者回复 |
| `is_early_access` | bool | 是否 EA 期间 |
| `labels` | list[str] | 多标签数组，**一条评论允许多标签**，禁止强制单簇 |
| `topic_id` | int | 聚类后回填；**离群点固定 `-1` 单列** |
| `topic_confidence` | float | 置信度，**明确标为模型推断** |
| `evidence_kind` | `数据事实` / `模型推断` / `运营建议` | 三段式标记 |

> 评论数据通常**没有稳定用户 ID 与交易金额**，RFM 会退化。不要为了显得专业硬套 RFM，改用「评论影响力 + 参与度 + 时长」三轴更诚实。见 `references/analysis-playbook.md`。

## 五、分母口径（最容易出假精确的地方）

每个百分比必须写明分母属于哪一种，**三者不可混用**：

| 口径 | 含义 | 写法示例 |
|---|---|---|
| `all` | 占全量评论 | `23.1%（14/61，分母=全量评论）` |
| `channel` | 占该渠道评论 | `23.1%（14/61，分母=渠道 A 评论）` |
| `rating_band` | 占该评分段评论 | `23.1%（14/61，分母=1–2 星评论）` |

样本量分档（`ur_core.py::confidence_band` / `fmt_pct` 统一执行）：

| n | 输出 |
|---|---|
| `n < 20` | 不给百分比，只给方向：`方向性：14/61（样本不足，仅供参考）` |
| `20 ≤ n < 50` | 整数百分比 + 仅供参考：`23%（14/61，仅供参考）` |
| `n ≥ 50` | 一位小数：`23.1%（14/61）` |

**永不给两位小数**。任何百分比旁必须有绝对条数与分母。0 出现的组仍要显式显示，无值归入「未指明」桶。

## 六、三段式标记（防臆测的核心）

| 段 | 允许出现什么 | 谁产生 |
|---|---|---|
| 数据事实 | 原值、条数、分母、可直接核对的同期变化 | 脚本 |
| 模型推断 | 主题名、纳入/排除标准、置信度、聚类结果 | 聚类 + AI 命名，**永远带 `needs_human_confirm`** |
| 运营建议 | 优先级、负责方、验证指标、动作 | AI + 人工确认 |

`ur_report.py` 有静态断言：**推断段内容出现在事实段即拒绝出报告**。AI 输出在人工接受前永远是「建议」，每次状态变更写入 `data/audit/append-only.jsonl`。

## 七、存储格式与目录（相对调用方工作区）

| 路径 | 内容 | 写入规则 |
|---|---|---|
| `data/metrics.csv` | 数值长表 | `utf-8-sig`、白名单字段、`.tmp` → `os.replace` **原子写**、**精度不倒退 upsert**、脏行跳过 |
| `data/reviews.jsonl` | 评论原文 | `utf-8` + `ensure_ascii=False`、append-only、按 `review_id` 去重、**不覆盖** |
| `data/snapshots/<channel>/<day>/<hash>.json` | 原始响应副本 | 只增不改；含请求参数、时间戳、HTTP 状态、脱敏后内容 + 哈希指纹 |
| `data/inbox/` | 人工投放 | 只读入库，必须带填写人（写进 `note`） |
| `data/analysis/` | `topics.json` / `slices.json` / `matrix.csv` / `insights.md` | 派生结果，可随时重算，**不算源数据** |
| `data/audit/append-only.jsonl` | 审计轨迹 | 只追加：时间/批次/动作/触发人/输入输出指纹/是否人工确认 |
| `out/` | `report.md` / `report.xlsx` / `report.json` | 一键命令的 `--out` 目标 |

> **脱敏与原始副本的张力**：`redact_pii` 默认开，快照存**脱敏后**内容 + 哈希指纹，牺牲原始 PII 的逐字回溯；但**评论原句仍逐字保留**在 `reviews.jsonl`。详见 `references/env-and-risks.md`。

## 八、两类数据怎么交叉（对齐键）

对齐键：`channel_key + 时间窗 + app_version`（版本缺失落「未指明」桶）。

1. 从 `metrics.csv` 读：渠道 A 评分 4.5 → 4.1（`metric_kind=outer`、`precision=0.1`、附两条 `captured_at`）。
2. 从 `reviews.jsonl` 聚合：同渠道、同两个**等长**时间窗内，`topic_id=T03「闪退/掉线」` 在 1–2 星中的占比 8% → 23%（每条带 `count` 与 `denom`）。
3. `ur_analyze.py` 产出 `matrix.csv` 一格：`{channel:A, window:W2, topic:T03, count:14, denom:61, pct:"23%", warn:""}`，并把评分变化作为**同期标记列**并排。
4. `ur_report.py` 措辞模板强制写：「**渠道 A 评分下滑与『闪退』主题在低分评论中占比上升同期出现**」，**禁止**「导致」「因为」。
5. 任一窗口样本不足 → 整格标「仅供参考」，不给两位小数。报告顶部固定声明「评论是自选样本，占比只代表评论语料内部结构，不能外推全体玩家比例」。

## 九、列结构定稿声明

沿用一条常见工程教训：**列结构定稿后不轻易增删**。新维度优先塞进 `note` / `labels` / `topic_*`，而不是加列。这样老数据照样能读，不用迁移。确需加列时，必须走本文件开头的「改契约」清单并全量重跑验收。
