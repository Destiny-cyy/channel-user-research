---
name: channel-user-research
description: 把不同渠道的数值指标（预约数/评分/量级）与玩家评论文本统一采集、清洗、分层聚类，做可追溯的用户研究分析，并输出辅助运营策略的证据链报告。当用户要看各渠道玩家口碑、做差评归因、主题聚类、用户分层、版本口碑对比、素材或活动前调研，或说「用户研究」「玩家评论分析」「VOC」「渠道数据分析」「分层聚类」「运营策略建议」时应加载。触发词：渠道用户研究、玩家评论分析、差评归因、主题聚类、用户分层、口碑对比、用户研究、运营策略建议、VOC 分析、玩家分层。
agent_created: true
---

# 渠道用户研究（Channel User Research）

## 定位

把各渠道的数字和玩家原话，变成**能追溯、能分层、能指导运营动作**的结论。给 AI 助手执行用；给人看的说明书是同目录 `README.md`，两份分工见下。

| | 本文件 `SKILL.md` | `README.md` |
|---|---|---|
| 读者 | AI 助手（执行时读） | 使用者与同事（人读） |
| 回答 | 什么话触发、按什么铁律走、哪步读哪个子文件、调哪个脚本 | 有啥用、怎么点、跑完给什么、为什么这么设计、出问题找谁 |
| 写法 | 命令式、只挂接不复制正文、全部相对路径 | 业务大白话、带图示与例子、带 FAQ |

**开工前必读** `references/data-schema.md`（字段契约）与 `references/redlines.md`（合规边界）。

## 何时使用

- 采多个渠道的数值指标与玩家评论，并想看两者交叉（如「评分下滑 ↔ 差评主题上升」）
- 对评论做主题聚类、用户分层、痛点排序、机会点排序
- 版本口碑对比、差评归因、分渠道诉求差异、素材／活动前的玩家洞察
- 需要一份每条结论都能回溯到原话的报告，用于辅助运营策略

## 何时不用

- 要「全体玩家真实比例」、实时舆情监控、或自动去对方系统发帖／改状态
- 需要登录他人账号、绕验证码／加密／风控才能拿到的数据
- 只有一个渠道、且只想要一个数字（直接读原表更快）

## 硬性规则（先读，违反即停止，不是建议）

1. **只读**：仅采公开匿名可见数据，或自己名下账号／自有 App 的官方后台接口。仅 `GET`/`HEAD`，禁带他人 cookie 与 authorization，跳登录页即停。**绝不**抓包解密、逆向、绕验证码／加密／风控、破解签名参数、登录他人账号、代理轮换规避封禁、点任何会改变对方系统状态的按钮。违反立即停止整轮。
2. **本档放宽边界（相对既有采集红线的显式变更）**：允许用自己／团队名下账号登录态与自有 App 官方后台（{{OWNED_CHANNEL_BACKEND}} `{{OWNED_REVIEW_METHOD_A}}`、{{OWNED_CHANNEL_API}} `{{OWNED_REVIEW_METHOD_B}}`、各商店自有后台导出），须标 `manual_confirmed` 并带填写人。**放宽变不出接口**：{{CHANNEL_B}}／{{CHANNEL_C}}／各应用商店他人游戏评论无官方接口，自动采集默认关，只走 `manual_import` 人工投放。自有账号操作由账号持有人本人执行、平台条款责任由使用方承担。
3. **原值入库**：采到多少写多少，一个系数不乘；不做口径估算／推算／按趋势插值；不可比数据禁止聚合（5 分制与 10 分制不合并算均分）。
4. **不给假精确**：精度不够就不硬算，算不出来如实说并明确告知。变化小于最小可分辨单位写「—」**绝不填 0**；样本不足标「仅供参考」；百分比永不给两位小数；分母口径必须写明且不可混用。
5. **采不到必须明说，绝不伪装成功**：空结果一律告警并写明网页四步法做到第几步；「扫不到 ≠ 没有」，先确认扫描本身完整。
6. **逐字原句**：评论原文不改写、不摘要、不翻译。每条洞察必须能回溯 `review_id` + 逐字原句；`--grounding-check` 查不到即**整条丢弃并写审计**，不进报告。
7. **AI 输出永远是建议**：报告严格分「数据事实 / 模型推断 / 运营建议」三段，推断内容出现在事实段即拒绝出报告；所有 AI 结论带 `needs_human_confirm`，人工确认后才标「已采纳」。主题用显式纳入／排除标准定义，不靠一个漂亮名字。
8. **不外推、不写因果**：占比只代表「评论语料内部结构」，不能外推为全体玩家比例；只写「同期出现」，**禁写「导致／因为」**。所有出口（Markdown／Excel／JSON）只消费 `ur_report.py::build_rows()`，不许各算一套。

## 工作流

> 完整 15 步护栏见 `references/collection-playbook.md` 与 `references/analysis-playbook.md`；本文件只挂接。

### 阶段 A · 采集（`ur_collect.py`）
A1 配置与红线预检（硬禁用项拒绝启动；`--offline` 下任何联网即报错）→ A2 网页四步法／多路降级（任何「没数」结论必须说明做到第几步）→ A3 两路落库 + 原始快照 → A4 正常缺数白名单 + 收尾重尝试 + 日期护栏 + 精度不倒退 + 空结果告警。
📖 此时读 `references/collection-playbook.md`、`references/channel-adapters.md`。

### 阶段 B · 归一（`ur_clean.py`）
B5 schema 三态校验（存疑数据隔离）→ B6 去重／脱敏／空结果告警／多标签初始化 → B7 **先定 slice**（切片先于聚类，防「先聚类再找差异」）。

### 阶段 C · 聚类（`ur_cluster.py`）
C8 中文字符 n-gram 向量化（纯 CPU）→ C9 NMF/KMeans 聚类，**离群点单列 -1**，导出代表原句 → C10 `--emit-brief` 生成主题命名任务包，交会话自带 LLM（脚本不调外部 API、不要 key）。
📖 此时读 `assets/llm_prompt_template.md`、`references/anti-hallucination.md`。

### 阶段 D · 量化（`ur_analyze.py`）
D11 grounding 校验 → D12 占比计算（分母 + 绝对条数 + 小样本警告 + 0 组 + 未指明桶）→ D13 五套交叉矩阵、严重度代理、两等长时间窗趋势、机会点排序（权重显式列出让业务能否决）。
📖 此时读 `references/analysis-playbook.md`。

### 阶段 E · 交付（`ur_report.py`）
E14 三段式洞察 + 结论后核查追问 → E15 唯一口径出口 + append-only 审计 + 跑前跑后 sha256 指纹。
📖 此时读 `assets/report_template.md`。

## 交付物

| 产出 | 给谁 | 内容 | 位置 |
|---|---|---|---|
| `report.md` | 运营／发行 | 三段式洞察、交叉矩阵、痛点排序、运营动作（含优先级与验证指标） | `<工作区>/out/` |
| `report.xlsx` | 需要表格的人 | 同一 `build_rows()` 口径的表格版 | `<工作区>/out/` |
| `report.json` | 下游系统 | 结构化结果 | `<工作区>/out/` |
| `matrix.csv` / `topics.json` / `slices.json` | 复核者 | 交叉矩阵、主题定义、切片定义 | `<工作区>/data/analysis/` |
| `append-only.jsonl` | 审计 | 谁、何时、确认了什么、输入输出指纹 | `<工作区>/data/audit/` |
| `snapshots/` | 复核者 | 原始响应副本（脱敏后 + 哈希指纹） | `<工作区>/data/snapshots/` |

## 交付前自检清单

- [ ] 报告顶部有「评论是自选样本，占比只代表评论语料内部结构，不能外推全体玩家比例」声明
- [ ] 三个固定标题齐全：数据事实 / 模型推断 / 运营建议；推断未混进事实段
- [ ] 每条洞察带 `review_id` + 逐字原句，`--grounding-check` 全绿
- [ ] 每个百分比带绝对条数与分母；n<20 组标「仅供参考」；无两位小数
- [ ] 变化不足最小可分辨单位处写「—」，无填 0 冒充
- [ ] 措辞是「同期出现」，全文无「导致／因为」
- [ ] 空结果已告警并写明网页四步法做到第几步；`failed`/`skipped` 都带理由
- [ ] 中文渠道自动采集默认关、人工投放带填写人；`manual_confirmed` 标记齐全
- [ ] 机会点排序权重显式列出，业务可否决
- [ ] 跑前跑后 sha256：skill 目录与 fixtures 零改动，只有 `out/` 新增

## 关键脚本

| 脚本 | 用途 |
|---|---|
| `scripts/run_pipeline.py` | 一键串起 MVP：`--offline --fixtures --out` |
| `scripts/ur_core.py` | 唯一数据契约、三态校验、原子写、精度不倒退 upsert、证据链、审计 |
| `scripts/ur_fetch.py` | 只读请求层：仅 GET/HEAD、限流退避、快照幂等 |
| `scripts/ur_adapters.py` | 按配置动态加载适配器（无硬编码白名单） |
| `scripts/ur_collect.py` | 采集编排与收尾重尝试 |
| `scripts/ur_clean.py` | 清洗去重、脱敏、先定 slice |
| `scripts/ur_cluster.py` | 轻量聚类 + `--emit-brief` 生成 AI 任务包 |
| `scripts/ur_analyze.py` | 唯一量化中心：占比、交叉矩阵、机会点排序 |
| `scripts/ur_report.py` | 唯一口径中心 `build_rows()`、三段式渲染、`--grounding-check` |
| `scripts/ur_verify.py` | 6 条跑通判定 + 负向用例 + 红线静态断言 |

## 参考文件（什么时候读）

| 文件 | 什么时候读 |
|---|---|
| `references/data-schema.md` | **动手前必读**：字段契约、精度量纲、分母口径 |
| `references/redlines.md` | **动手前必读**：10 条红线、本档放宽边界、各渠道默认状态 |
| `references/collection-playbook.md` | 阶段 A 采集 / 判断「网页到底有没有数」时 |
| `references/channel-adapters.md` | 加渠道、写适配器时 |
| `references/analysis-playbook.md` | 阶段 C/D 分层维度、交叉矩阵、机会点公式 |
| `references/anti-hallucination.md` | 让 AI 起主题名、写洞察、做人工确认时 |
| `references/env-and-risks.md` | 装依赖、排 Windows 报错、看残余风险时 |
| `assets/llm_prompt_template.md` | C10 生成主题命名任务包时 |
| `assets/report_template.md` | E14 出报告时 |
| `examples/channels.example.yaml` | **加渠道唯一要改的那份** |

## 已知环境坑 / 残余风险

只挂接，不复制：`references/env-and-risks.md`。最要紧的三条——CSV 必须 `utf-8-sig`；`os.replace` 被 Excel 占用时明确报错不静默覆盖；工作区无 git 仓库，靠 sha256 指纹 + 快照留痕。
