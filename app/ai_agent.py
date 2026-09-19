"""AI 分析层：意图分类 + 实体抽取 + 意向打分 + 回复起草。

两条路径：
1. 有 API 密钥 → 直连上游 /chat/completions（不走 OpenClaw 网关，省两个数量级 token）
2. 无密钥 → 规则降级（关键词 + 启发式），保证链路先跑通，方便实验

密钥解析与底层调用统一收敛到 ai_client（DB 设置优先，环境变量兜底）。

设计约束：
- 任何 AI 输出都必须能解析失败降级，绝不让一封信卡死整条流水线
- 涉及价格/交期的内容 AI 不得编造，只留占位符待人工填
- 所有面向用户的文本（摘要/依据）一律输出中文
"""
import re

from . import ai_client, config, db

CATEGORIES = ["inquiry", "quote", "after_sale", "complaint", "spam", "other"]

CATEGORY_CN = {
    "inquiry": "询盘", "quote": "报价", "after_sale": "售后",
    "complaint": "投诉", "spam": "垃圾邮件", "other": "其他",
}

# ============ 规则降级用的信号词 ============

SIGNALS = {
    "quote": [
        r"\b(price|quotation|quote|quotation|fob|cif|exw|unit price|"
        r"cost|discount|payment term|moq)\b",
        r"(价格|报价|单价|账期|折扣|起订量)",
    ],
    "inquiry": [
        r"\b(inquiry|enquiry|interested in|we are looking for|"
        r"please send.*(catalog|catalogue|specification)|"
        r"do you (supply|manufacture|produce)|sample)\b",
        r"(询价|求购|目录|样品|规格)",
    ],
    "after_sale": [
        r"\b(not working|defective|broken|damaged|return|refund|"
        r"warranty|repair|quality issue|missing)\b",
        r"(售后|退货|退款|破损|质量问题|维修|保修)",
    ],
    "complaint": [
        r"\b(complain|unacceptable|disappointed|terrible|"
        r"worst|angry|legal action|lawyer)\b",
        r"(投诉|失望|不可接受|律师|起诉)",
    ],
    "spam": [
        r"\b(seo service|guaranteed traffic|bitcoin|crypto|"
        r"viagra|casino|loan offer|work from home)\b",
        r"(推广服务|刷单|代运营)",
    ],
}

# 高价值信号：出现即加分
HIGH_VALUE_PATTERNS = [
    (r"\b(\d[\d,]{2,})\s*(pcs|pieces|units|sets|containers?|ctns?|cartons?)\b", 22,
     "明确采购数量"),
    (r"\b(fob|cif|exw|ddp|cfr)\b", 12, "提及贸易术语"),
    (r"\b(annual|yearly|monthly|per month|container|20ft|40ft|40hq)\b", 14,
     "提及长期/整柜采购"),
    (r"\b(moq|minimum order)\b", 8, "关注起订量"),
    (r"\b(asap|urgent|urgently|immediately)\b", 10, "需求紧急"),
    (r"\b(target price|best price|competitor|cheaper)\b", 10, "比价意图"),
    (r"\b(sample|samples|trial order)\b", 12, "索样/试单"),
    (r"\b(distributor|wholesale|importer|retail chain|supermarket)\b", 12,
     "渠道身份"),
    (r"\b(company|gmbh|inc\.|ltd\.?|llc|s\.a\.|pty|bv|sarl|co\.,)\b", 8,
     "企业主体"),
    (r"\b(website|www\.|https?://)\b", 6, "提供官网"),
    (r"\b(certificate|ce|rohs|fda|iso|ul|reach)\b", 8, "关注认证"),
    (r"\b(payment term|credit|lc|t/t|30 days|60 days)\b", 10, "谈账期"),
]

# 低价值/风险信号：出现即减分
LOW_VALUE_PATTERNS = [
    (r"\b(just (want|need) (a )?(catalog|catalogue|price list))\b", -14,
     "仅索要目录"),
    (r"\b(no budget|not buying|just browsing|only looking)\b", -18, "明确无预算"),
    (r"\b(free sample|send.*free.*sample)\b", -10, "索要免费样品"),
    (r"\b(god bless|dear sir/madam)\b", -6, "泛发模板痕迹"),
]


# ============ 模型调用（转发到 ai_client，保持向后兼容） ============

def _call_model(messages: list, max_tokens: int = 800) -> str | None:
    """直连上游模型 API。失败返回 None，由调用方降级。"""
    return ai_client.chat(messages, max_tokens=max_tokens)


def _extract_json(text: str) -> dict | None:
    return ai_client.extract_json(text)


# ============ 规则打分 ============

def rule_analyze(subject: str, body: str, from_addr: str) -> dict:
    """无模型时的启发式分析。用于先把链路跑通，也给模型结果做交叉校验。"""
    text = f"{subject}\n{body}".lower()
    category, cat_conf = "other", 0.3
    best = 0
    for cat, pats in SIGNALS.items():
        hits = sum(1 for p in pats if re.search(p, text, re.I))
        if hits > best:
            best, category, cat_conf = hits, cat, min(0.5 + hits * 0.15, 0.9)

    score, evidence = 25, []
    for pat, weight, label in HIGH_VALUE_PATTERNS:
        if re.search(pat, text, re.I):
            score += weight
            evidence.append(f"+{weight} {label}")
    for pat, weight, label in LOW_VALUE_PATTERNS:
        if re.search(pat, text, re.I):
            score += weight
            evidence.append(f"{weight} {label}")

    # 邮箱域名：免费邮箱略减，企业域名加分
    domain = from_addr.split("@")[-1] if "@" in from_addr else ""
    free_mail = {"qq.com", "163.com", "126.com", "gmail.com", "outlook.com",
                 "hotmail.com", "yahoo.com", "foxmail.com", "sina.com"}
    if domain and domain not in free_mail:
        score += 8
        evidence.append("+8 企业域名")
    elif domain in free_mail:
        score -= 4
        evidence.append("-4 免费邮箱")

    # 正文长度：太短通常是泛发
    if len(body.strip()) < 40:
        score -= 10
        evidence.append("-10 正文过短")

    score = max(0, min(100, score))

    entities = {}
    m = re.search(r"(\d[\d,]{2,})\s*(pcs|pieces|units|sets|containers?|ctns?|"
                  r"cartons?)", text, re.I)
    if m:
        entities["quantity"] = f"{m.group(1)} {m.group(2)}"
    for term in ["fob", "cif", "exw", "ddp", "cfr"]:
        if re.search(rf"\b{term}\b", text, re.I):
            entities.setdefault("trade_terms", []).append(term.upper())
    m = re.search(r"\b([a-z]{1,4}[- ]?\d{2,6}[a-z]?)\b", text)
    if m and any(c.isdigit() for c in m.group(1)):
        entities.setdefault("models", []).append(m.group(1).upper())

    return {
        "category": category,
        "confidence": cat_conf,
        "score": score,
        "entities": entities,
        "summary": f"[规则分析] 归类为{CATEGORY_CN.get(category, category)}，"
                   f"意向分 {score}",
        "rationale": "; ".join(evidence) or "无显著信号",
        "model": "rule-based",
    }


# ============ 模型分析 ============

ANALYZE_SYSTEM = """You are an email analyst for a Chinese export trading company.
Analyze the customer email and return STRICT JSON only, no other text.

Schema:
{
  "category": "inquiry|quote|after_sale|complaint|spam|other",
  "score": 0-100,
  "entities": {
    "quantity": "string or null",
    "models": ["product model codes"],
    "trade_terms": ["FOB","CIF"...],
    "target_price": "string or null",
    "company": "string or null",
    "country": "string or null"
  },
  "summary": "one sentence in Chinese",
  "rationale": "scoring reasoning in Chinese, list the signals you found"
}

Scoring guide (0-100 = purchase intent):
- Explicit quantity / container volume / annual demand: strong positive
- Asks FOB/CIF price, target price, MOQ, payment terms: positive
- Company domain, business identity, certification questions: positive
- Requests sample or trial order: positive
- Only asks for catalog / no specifics / free sample begging: negative
- Vague mass-mail template: negative

category guide:
- inquiry: asking about products, specs, availability, catalog
- quote: explicitly asking for price / quotation
- after_sale: product problems, returns, warranty on EXISTING order
- complaint: expressing serious dissatisfaction
- spam: unsolicited marketing, phishing, scam

Return JSON only."""


def ai_analyze(subject: str, body: str, from_addr: str,
               contact_name: str = "") -> dict:
    """主分析入口。有密钥走模型，无密钥或失败则降级到规则。"""
    fallback = rule_analyze(subject, body, from_addr)
    if not ai_client.is_enabled():
        return fallback

    prompt = (f"From: {from_addr} {contact_name}\n"
              f"Subject: {subject}\n\n"
              f"Body:\n{body[:6000]}")
    content = _call_model([
        {"role": "system", "content": ANALYZE_SYSTEM},
        {"role": "user", "content": prompt},
    ], max_tokens=900)
    parsed = _extract_json(content) if content else None

    if not parsed:
        fallback["rationale"] += "（模型不可用，已降级规则分析）"
        return fallback

    category = str(parsed.get("category", "")).lower().strip()
    if category not in CATEGORIES:
        category = fallback["category"]

    try:
        score = int(parsed.get("score", fallback["score"]))
    except (TypeError, ValueError):
        score = fallback["score"]
    score = max(0, min(100, score))

    return {
        "category": category,
        "confidence": 0.85,
        "score": score,
        "entities": parsed.get("entities") or {},
        "summary": str(parsed.get("summary") or fallback["summary"])[:500],
        "rationale": str(parsed.get("rationale") or fallback["rationale"])[:1000],
        "model": ai_client.get_model(),
    }


# ============ 回复起草 ============

DRAFT_SYSTEM = """You are a professional sales assistant for a Chinese export company.
Write a reply to the customer email. Requirements:
- Match the customer's language (if they wrote English, reply in English)
- Be professional, warm, and concise. No fluff.
- NEVER invent prices, lead times, MOQ numbers, or certifications.
  If those are needed, insert a placeholder like [PRICE TO BE FILLED] on its own line.
- If the customer asked for a catalog, say you will send it and ask for
  specific models and quantities.
- Sign off with the given signature.
- Return ONLY the email body. No subject line, no markdown fences."""


def draft_reply(subject: str, body: str, contact_name: str,
                template: dict, signature: str,
                analysis: dict) -> tuple[str, str] | None:
    """生成回复草稿。返回 (主题, 正文)；无模型时返回 None 由模板兜底。"""
    if not ai_client.is_enabled():
        return None

    hint = (template or {}).get("ai_hint") or ""
    lang = (template or {}).get("lang") or "en"
    user = (
        f"Customer name: {contact_name or 'there'}\n"
        f"Customer email subject: {subject}\n\n"
        f"Customer message:\n{body[:4000]}\n\n"
        f"Detected category: {analysis.get('category')}\n"
        f"Template instruction: {hint}\n"
        f"Preferred language: {lang}\n"
        f"Signature to use:\n{signature}"
    )
    content = _call_model([
        {"role": "system", "content": DRAFT_SYSTEM},
        {"role": "user", "content": user},
    ], max_tokens=1200)
    if not content:
        return None

    # 去掉模型可能带出来的 markdown 代码围栏
    content = re.sub(r"^```[a-z]*\n?|```$", "", content.strip(),
                     flags=re.MULTILINE).strip()
    subj = subject if subject.lower().startswith("re:") else "Re: " + subject
    return subj, content


def render_template(template: dict, ctx: dict) -> tuple[str, str]:
    """模板兜底渲染。缺变量时保留原样，不炸。"""
    class SafeDict(dict):
        def __missing__(self, key):
            return "{" + key + "}"

    subj_t = (template or {}).get("subject") or "Re: {subject}"
    body_t = (template or {}).get("body") or ""
    subj = subj_t.format_map(SafeDict(ctx))
    body = body_t.format_map(SafeDict(ctx))
    return subj, body
