"""邮件摘要生成：接入真实 AI 模型，**强制输出中文**。

为什么单独一个模块
------------------
1. 摘要与「意向打分」是两个独立关注点：打分要客观，摘要要给人读。
   分开后可以各自调整提示词，互不干扰。
2. 摘要必须中文 —— 外贸客户来信多是英文/小语种，Davy 要的是能直接扫的中文要点。
   提示词里把「中文输出」写成硬约束，并做后置校验（检出非中文占比过高时重试一次）。

降级链路
--------
有 Key  → 模型摘要（中文）
无 Key  → 规则摘要（从正文里抽要点句 + 中文模板拼接）
两者产出都写库，界面上标明来源，用户能一眼看出这条是不是 AI 生成的。
"""
import re

from . import ai_client, db

# 面向用户的固定标签（用于无 Key 时的规则摘要）
CATEGORY_CN = {
    "inquiry": "询盘", "quote": "报价", "after_sale": "售后",
    "complaint": "投诉", "spam": "垃圾邮件", "other": "其他",
}

# ============ 提示词 ============

SUMMARY_SYSTEM = """你是一名外贸公司的邮件助理。你的任务是把客户来信概括成**中文**要点。

硬性要求：
1. **必须用简体中文输出**。即使原文是英文、德文、西班牙文等，也要翻译成中文再概括。
   专有名词（公司名、型号、人名）保留原文，不要翻译。
2. 只输出 JSON，不要任何其他文字、不要 markdown 代码围栏。
3. 不要编造原文没有的信息。原文没提到的字段填 null。

输出 JSON 结构：
{
  "summary_cn": "一句话说清客户想要什么（不超过 60 字）",
  "key_points_cn": ["要点1", "要点2", "要点3"],
  "intent_cn": "客户的核心诉求，一句话",
  "urgency_cn": "紧急程度：高 / 中 / 低，并说明理由",
  "translated_body_cn": "把客户来信正文翻译成通顺的中文，保留段落结构"
}

字段说明：
- key_points_cn：提炼 2~5 条，覆盖「要什么产品」「数量/规格」「价格/贸易条款」「交期」「其他要求」
- urgency_cn：出现 asap/urgent/尽快/紧急 或明确交期压力 → 高
- translated_body_cn：完整翻译，不要省略、不要总结，供人工核对时对照

只输出 JSON。"""


def _looks_chinese(text: str, threshold: float = 0.25) -> bool:
    """粗略判断文本是否足够「中文」。用于后置校验。

    统计中日韩统一表意文字占非空白字符的比例。
    英文文本这个比例接近 0，中文文本通常 > 0.5。
    """
    if not text:
        return False
    body = re.sub(r"\s", "", text)
    if not body:
        return False
    cjk = len(re.findall(r"[\u4e00-\u9fff]", body))
    return (cjk / len(body)) >= threshold


def _contains_cjk(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text or ""))


# ============ 规则降级摘要 ============

def rule_summary(subject: str, body: str, category: str = "",
                 score: int | None = None,
                 quantity: str = "", models: str = "") -> dict:
    """无模型时的中文摘要：从正文里挑要点句 + 中文模板拼接。

    要点句挑选策略：按「含数字 / 含贸易术语 / 含疑问」加权排序，取前 3 条。
    """
    text = (body or "").strip()
    # 按句切分，保留原始语言
    sents = [s.strip() for s in re.split(r"(?<=[.!?。！？])\s+|\n{2,}", text)
             if s.strip()]
    sents = [s for s in sents if len(s) > 15][:40]

    def weight(s: str) -> int:
        w = 0
        if re.search(r"\d", s):
            w += 3
        if re.search(r"\b(fob|cif|exw|moq|price|quote|sample|order|"
                     r"container|pcs|units)\b", s, re.I):
            w += 3
        if "?" in s:
            w += 1
        return w

    picked = sorted(sents, key=weight, reverse=True)[:3]
    picked = [p for p in picked if weight(p) > 0] or sents[:2]

    cat_cn = CATEGORY_CN.get(category, category or "未分类")
    head = f"[规则摘要] 类别：{cat_cn}"
    if score is not None:
        head += f"，意向分 {score}"
    if quantity:
        head += f"，数量：{quantity}"
    if models:
        head += f"，型号：{models}"

    points = []
    if quantity:
        points.append(f"采购数量：{quantity}")
    if models:
        points.append(f"涉及型号：{models}")
    if picked:
        points.append("原文要点（未翻译）：" + " / ".join(
            p[:90] for p in picked[:2]))

    return {
        "summary_cn": head,
        "key_points_cn": points,
        "intent_cn": f"客户就{cat_cn}事宜发来邮件",
        "urgency_cn": "中（规则模式无法判断紧急度）",
        "translated_body_cn": "",
        "source": "rule",
        "model": "rule-based",
    }


# ============ 模型摘要 ============

def ai_summary(subject: str, body: str, from_addr: str = "",
               contact_name: str = "", company: str = "",
               category: str = "", score: int | None = None,
               retry_on_non_chinese: bool = True) -> dict:
    """生成中文摘要。有 Key 走模型，否则规则降级。

    返回 dict：summary_cn / key_points_cn / intent_cn / urgency_cn /
              translated_body_cn / source / model
    """
    if not ai_client.is_enabled():
        return rule_summary(subject, body, category, score)

    ctx = [f"寄件人：{from_addr}"]
    if contact_name:
        ctx.append(f"联系人：{contact_name}")
    if company:
        ctx.append(f"公司：{company}")
    ctx.append(f"主题：{subject}")
    header = "\n".join(ctx)

    user = f"""{header}

邮件正文：
{(body or '')[:6000]}

请按上述 JSON 结构输出中文摘要。"""

    messages = [
        {"role": "system", "content": SUMMARY_SYSTEM},
        {"role": "user", "content": user},
    ]
    raw = ai_client.chat(messages, max_tokens=2000)
    parsed = ai_client.extract_json(raw) if raw else None

    # 后置校验：模型没吐 JSON，或者吐出来的不是中文 → 重试一次并加重语气
    if retry_on_non_chinese:
        summary_text = ""
        if isinstance(parsed, dict):
            summary_text = str(parsed.get("summary_cn") or "")
        bad = (not isinstance(parsed, dict)) or (
            summary_text and not _contains_cjk(summary_text))
        if bad:
            messages.append({"role": "assistant", "content": raw or ""})
            messages.append({
                "role": "user",
                "content": "以上输出不合格。请严格只输出 JSON，"
                           "且 summary_cn、key_points_cn、intent_cn、"
                           "urgency_cn 必须是简体中文。",
            })
            raw2 = ai_client.chat(messages, max_tokens=2000)
            parsed2 = ai_client.extract_json(raw2) if raw2 else None
            if isinstance(parsed2, dict):
                parsed = parsed2

    if not isinstance(parsed, dict):
        out = rule_summary(subject, body, category, score)
        out["summary_cn"] += "（模型不可用，已降级）"
        return out

    # 逐字段兜底：模型可能漏字段
    summary_cn = str(parsed.get("summary_cn") or "").strip()
    if not summary_cn:
        summary_cn = rule_summary(subject, body, category, score)["summary_cn"]

    kp = parsed.get("key_points_cn") or []
    if isinstance(kp, str):
        kp = [kp]
    kp = [str(x).strip() for x in kp if str(x).strip()][:6]

    return {
        "summary_cn": summary_cn[:800],
        "key_points_cn": kp,
        "intent_cn": str(parsed.get("intent_cn") or "").strip()[:300],
        "urgency_cn": str(parsed.get("urgency_cn") or "").strip()[:120],
        "translated_body_cn": str(
            parsed.get("translated_body_cn") or "").strip()[:8000],
        "source": "ai",
        "model": ai_client.get_model(),
    }


def format_summary_for_db(res: dict) -> str:
    """把摘要结果压成一段可存进 messages.ai_summary 的文本。"""
    parts = [res.get("summary_cn") or ""]
    kp = res.get("key_points_cn") or []
    if kp:
        parts.append("；".join(kp))
    return " ｜ ".join(p for p in parts if p)[:2000]
