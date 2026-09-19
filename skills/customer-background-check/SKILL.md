---
name: customer-background-check
description: 外贸询盘助手的「自动客户背调」能力定义。从外贸客户来信里推断来源国家、公司类型、渠道身份、可信度与风险点，输出带证据的中文档案。涉及客户来源国家分析、公司背景分析、客户背调、可信度评估时使用。触发词：客户背调、背调、来源国家、公司分析、客户画像、background check。
agent_created: true
---

# 自动客户背调（Customer Background Check）

外贸询盘助手 收信流水线的一环。每接触一个新客户，从**邮件本身**推断他是谁、
从哪来、是干什么的、值不值得投入精力。

## 最重要的设计原则：只用邮件，不联网

这是一个**刻意的产品决策**，不要「优化」掉：

1. 服务器在深圳，抓海外企业信息（LinkedIn / 海关数据 / 各国工商库）既慢又常被墙
2. 擅自抓取第三方数据有**合规风险**，不该替用户做这个决定
3. 邮件签名、域名、正文自述里其实已有大量可用信号，模型足以给出有依据的判断

所以本模块的定位是「**基于邮件证据的推断**」，不是「情报核实」。
界面上必须写明「仅基于邮件内容推断，未联网核实」。

## 代码落点

| 文件 | 职责 |
|---|---|
| `app/ai_profile.py` | 信号表 + 规则推断 + 模型提示词 + 结果规范化 |
| `app/ai_client.py` | 底层模型调用 |
| `app/pipeline.py` → `_make_profile()` | 流水线里调用并写库 |
| `app/web.py` → `contact_profile()` | 详情页「重新分析」按钮 |
| `app/schema.sql` | `contacts` 表的背调字段 |

## 数据库字段（contacts 表）

```
background      TEXT     JSON 档案（完整结果）
company_type    TEXT     公司类型，便于列表筛选
channel_role    TEXT     渠道身份，便于列表筛选
credibility     TEXT     可信度：高/中/低
profiled_ts     INTEGER  背调时间
country         TEXT     （已有字段）推断出的国家，写回这里
```

`background` 的 JSON 结构：

```json
{
  "country_conf": "高|中|低",
  "country_evidence": "支撑国家结论的原文片段",
  "company_name": "公司名",
  "company_type": "德国有限责任公司",
  "channel_role": "经销商",
  "business_scope": "主营范围",
  "company_size_hint": "规模线索",
  "website": "官网",
  "phone": "电话",
  "credibility": "高|中|低",
  "credibility_reason_cn": "为什么给这个可信度",
  "risk_flags_cn": ["风险点1"],
  "summary_cn": "2~3 句中文概述",
  "evidence": ["你依据的原文片段"],
  "source": "ai|rule",
  "model": "模型名",
  "is_free_mail": false
}
```

## 推断信号（按可靠性排序）

### 1. 邮件签名块（最可信）

`_extract_signature(body)` 抓正文末尾 12 行非空行。这里通常有姓名、职位、
公司名、电话、地址、官网 —— 信息密度最高。

### 2. 邮箱域名

- **ccTLD 国家后缀**：`.de`→德国、`.fr`→法国、`.br`→巴西……
  查 `CCTLD_COUNTRY` 表（覆盖外贸高频的 50+ 个国家/地区）
- **企业域名 vs 免费邮箱**：`@mueller-handel.de` 比 `@gmail.com` 可信得多。
  查 `FREE_MAIL_DOMAINS`，命中即 `is_free_mail=True`，可信度降一档

### 3. 电话国际区号

查 `PHONE_CC`，**按区号长度倒序匹配**（否则 `+1` 会吃掉 `+1242` 巴哈马）。

### 4. 公司法律形式后缀

查 `COMPANY_TYPE_SIGNALS`：

| 后缀 | 推断 |
|---|---|
| GmbH / AG / KG / e.K. | 德国企业 |
| SARL / SAS / EURL | 法国企业 |
| SpA / Srl | 意大利企业 |
| B.V. / N.V. | 荷兰企业 |
| Pty Ltd | 澳大利亚企业 |
| Ltd / PLC / LLP | 英国/英联邦企业 |
| Inc / Corp / LLC | 美国企业 |
| Ltda / S.A. de C.V. | 拉美企业 |
| Oy / AB / AS / ApS | 北欧企业 |

### 5. 渠道身份

查 `CHANNEL_SIGNALS`：distributor→经销商、wholesaler→批发商、importer→进口商、
retailer→零售商、trader→贸易商、agent→代理商、OEM/ODM、manufacturer→制造商、
end user→终端用户。

**这个字段业务价值最高** —— 经销商/进口商是直接买家，终端用户要另换话术。

### 6. 货币与语言

`CURRENCY_SIGNALS`：EUR→欧元区、GBP→英国、AED→阿联酋……
可作为国家推断的补充信号（低置信度）。

## 降级策略

`ai_profile()` 主入口：

```
有 Key  → 模型推断（PROFILE_SYSTEM 提示词）
无 Key  → rule_profile()：纯规则，只做域名/区号/后缀/关键词匹配
          credibility 直接给「偏低（免费邮箱）/ 中」，标注 source='rule'
```

模型返回的 JSON 解析失败 → 自动回落到 `rule_profile()`，
并在 `summary_cn` 末尾追加「（模型不可用，已降级）」，让用户看得见。

## 防重复烧 token

`pipeline._make_profile()` 有短路逻辑：

```python
if existing_src == "ai":     return       # 已是 AI 档 → 不重复做
if existing_src == "rule" and not ai_client_ready():  return   # 规则档且没 Key → 不做
```

即：**规则档 + 后来配好了 Key → 升级一次**；已经是 AI 档 → 永不再跑。
背调是针对「客户」而非「邮件」的，同一客户只做一次就够。

## 提示词要点（PROFILE_SYSTEM）

- 明确告知模型**没有联网能力**，结论必须基于给定文本
- 证据不足就直说、降低置信度；**「未知」是合法且常常正确的答案**
- 每个字段都要能指出依据（`evidence` 数组）
- 所有面向人的文本字段必须**简体中文**，公司名/人名/型号保留原文
- 提示模型警惕泛泛的群发模板，遇到就标记为风险

## 何时触发

- **自动**：`pipeline._make_profile()`，首次接触新客户时
- **手动**：客户详情页「重新分析」→ `POST /contacts/{cid}/profile`
  取该客户**最近一封来信**作为素材，覆盖旧档案

## 界面呈现

客户详情页顶部「客户背调」卡片，四宫格展示：
来源国家/地区（含置信度）、公司主体、渠道身份、可信度（含免费邮箱标记），
下面依次是中文概述、官网/电话/规模、风险提示标签、**判断依据列表**。

「判断依据」必须展示 —— 让用户看到结论是从哪句话推出来的，
才能自己判断可信度，而不是把推断当事实用。
