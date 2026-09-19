---
name: auto-summary
description: 外贸询盘助手的「自动摘要」能力定义。把外贸客户来信（英文/小语种）概括成简体中文要点，供后台看板与详情页展示。涉及邮件摘要、翻译成中文、提炼询盘要点、判断紧急度时使用。触发词：邮件摘要、中文摘要、翻译邮件、提炼要点、auto summary。
agent_created: true
---

# 自动摘要（AI Summary）

外贸询盘助手 收信流水线的一环。每封新到的客户来信，在打分量类之后、起草回复之前，
生成一份**简体中文**摘要，让 Davy 不用读原文就能判断这封信值不值得马上处理。

## 代码落点

| 文件 | 职责 |
|---|---|
| `app/ai_summary.py` | 全部逻辑：提示词、模型调用、中文后置校验、规则降级 |
| `app/ai_client.py` | 底层模型调用（凭据解析、HTTP、JSON 抽取） |
| `app/pipeline.py` → `_make_summary()` | 在流水线里调用并写库 |
| `app/schema.sql` | `messages` 表的中文摘要字段 |

## 数据库字段（messages 表）

```
summary_cn      TEXT     一句话中文摘要（<= 800 字）
key_points_cn   TEXT     JSON 数组，2~5 条中文要点
urgency_cn      TEXT     紧急度：高/中/低 + 理由
translated_cn   TEXT     来信正文的完整中文翻译
summary_source  TEXT     'ai' 或 'rule'，标明来源
summarized_ts   INTEGER  生成时间戳
```

> 注意：`ai_summary` 是老字段，存的是**模型对自己打分的解释**，与本 skill 的
> `summary_cn` 不是一回事，不要混用。

## 输出契约

模型必须返回严格 JSON（无 markdown 围栏）：

```json
{
  "summary_cn": "一句话说清客户想要什么（不超过 60 字）",
  "key_points_cn": ["要点1", "要点2", "要点3"],
  "intent_cn": "客户的核心诉求，一句话",
  "urgency_cn": "紧急程度：高 / 中 / 低，并说明理由",
  "translated_body_cn": "把客户来信正文翻译成通顺的中文，保留段落结构"
}
```

字段规则：

- `key_points_cn`：2~5 条，覆盖「要什么产品」「数量/规格」「价格/贸易条款」「交期」「其他要求」
- `urgency_cn`：出现 `asap` / `urgent` / `尽快` / `紧急`，或明确交期压力 → 高
- `translated_body_cn`：**完整翻译**，不省略不总结，供人工核对时对照原文
- 原文没提到的字段填 `null`，**禁止编造**

## 三条硬约束

### 1. 必须输出简体中文（最重要）

外贸来信多是英文、德文、西班牙文。摘要的全部意义就是「不用读原文」，
所以中文是硬性要求，不是「尽量」。

提示词里已写死，但**模型仍可能偷懒照抄原文**，所以有后置校验：

```python
_looks_chinese(text)   # 计算中日韩统一表意文字占比
_contains_cjk(text)    # 是否含任意汉字
```

`ai_summary()` 在拿到结果后检查 `summary_cn` 是否含汉字。不含 →
把上一轮输出作为 assistant 消息塞回对话，追加一句「以上输出不合格，必须简体中文」**重试一次**。

专有名词（公司名、型号、人名）**保留原文不翻译**，例如 `Mueller Handel GmbH`、
`model A-1200` 保持原样。

### 2. 与「意向打分」解耦

打分（`ai_agent.ai_analyze`）要客观、要稳定，用低温；
摘要是给人读的，允许更通顺的表达。两者**用各自的提示词**，互不干扰，
改摘要不会影响打分精度。

### 3. 失败必须降级，不能卡死流水线

调用链：有 Key → 模型摘要；无 Key / 调用失败 / JSON 解析失败 → `rule_summary()`。

`rule_summary()` 是纯规则兜底，从正文里按权重挑要点句：
含数字 +3、含贸易术语（FOB/CIF/MOQ/price/order…）+3、含问号 +1，
取前 3 条拼中文模板。产出标注 `source='rule'`，界面上显示「规则生成」徽标。

`ai_summary()` **不抛异常**，任何分支都返回 dict，调用方不必 try。

## 何时触发

- **自动**：`pipeline._analyze_and_draft()` 每处理一封新邮件都会调 `_make_summary()`
- **手动单封**：邮件详情页「立即生成」→ `POST /messages/{id}/summarize`
- **批量补跑**：设置页「补跑中文摘要 + 客户背调」→ `POST /settings/ai/reprocess`
  （典型场景：刚配好 API Key，把历史邮件一次性补上）

手动和批量最终都走 `pipeline.reprocess_message()` → `_analyze_and_draft()`。
注意它是**整封重跑**（分析 + 摘要 + 背调），不是只补摘要。

## 调优提示

- 中文校验阈值 `_looks_chinese(text, threshold=0.25)`：英文文本占比接近 0，
  中文通常 > 0.5，0.25 是安全分界。若出现误判（中英混排被判不合格）就调低。
- 重试只做一次。两次都不合格就用第二次的结果，**不要死循环烧 token**。
- `max_tokens=2000`：翻译全文比较长，给小了会被截断。
- 想换摘要风格（更短/更细）只改 `SUMMARY_SYSTEM`，不动其他代码。
