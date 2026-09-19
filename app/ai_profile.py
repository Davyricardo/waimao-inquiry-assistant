"""客户背调：从邮件线索推断来源国家 + 公司背景 + 可信度。

设计原则
--------
**只用邮件本身的信息，不联网抓取。** 原因：
1. 服务器在国内，抓海外企业信息（LinkedIn / 海关数据 / 各国工商库）既慢又常被墙；
2. 擅自抓取第三方数据有合规风险，不该替用户做这个决定；
3. 邮件签名、域名、正文自述里其实已有大量可用信号，模型足以给出有依据的判断。

所以本模块的定位是「**基于邮件证据的推断**」，而不是「情报核实」。
输出里必须带上 evidence 字段，让用户看到结论是从哪句话推出来的，
便于他自己判断可信度 —— 这一步很关键，避免用户把推断当事实用。

推断来源（按可靠性排序）
------------------------
1. 邮件签名块（姓名/职位/公司/电话/地址/官网）—— 最可信
2. 邮箱域名（企业域名 vs 免费邮箱；ccTLD 国家后缀）
3. 正文自述（"we are a distributor in Germany"）
4. 电话国际区号、货币符号、语言习惯

降级：无 Key 时给纯规则的国家/域名推断，标注为「低置信度」。
"""
import re

from . import ai_client, db

# ============ 规则兜底用的信号表 ============

# 域名后缀 → 国家/地区（ISO 3166-1 alpha-2 常见子集 + 外贸高频）
CCTLD_COUNTRY = {
    "de": "德国", "fr": "法国", "it": "意大利", "es": "西班牙",
    "nl": "荷兰", "be": "比利时", "at": "奥地利", "ch": "瑞士",
    "se": "瑞典", "no": "挪威", "dk": "丹麦", "fi": "芬兰",
    "pl": "波兰", "cz": "捷克", "pt": "葡萄牙", "gr": "希腊",
    "ie": "爱尔兰", "ru": "俄罗斯", "ua": "乌克兰", "tr": "土耳其",
    "uk": "英国", "gb": "英国", "us": "美国", "ca": "加拿大",
    "mx": "墨西哥", "br": "巴西", "ar": "阿根廷", "cl": "智利",
    "co": "哥伦比亚", "pe": "秘鲁", "au": "澳大利亚", "nz": "新西兰",
    "jp": "日本", "kr": "韩国", "cn": "中国", "hk": "中国香港",
    "tw": "中国台湾", "sg": "新加坡", "my": "马来西亚", "th": "泰国",
    "vn": "越南", "id": "印度尼西亚", "ph": "菲律宾", "in": "印度",
    "pk": "巴基斯坦", "bd": "孟加拉国", "ae": "阿联酋",
    "sa": "沙特阿拉伯", "il": "以色列", "eg": "埃及",
    "za": "南非", "ng": "尼日利亚", "ke": "肯尼亚", "ma": "摩洛哥",
}

# 免费邮箱域名：出现即判定「非企业主体」
FREE_MAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "yahoo.co.uk", "hotmail.com", "outlook.com",
    "live.com", "aol.com", "icloud.com", "mail.com", "gmx.com", "gmx.de",
    "web.de", "yandex.ru", "mail.ru", "protonmail.com", "proton.me",
    "qq.com", "163.com", "126.com", "foxmail.com", "sina.com",
    "139.com", "sohu.com", "aliyun.com", "outlook.com.br",
}

# 电话国际区号 → 国家/地区（按长度倒序匹配，避免 +1 吃掉 +1242）
PHONE_CC = {
    "86": "中国", "852": "中国香港", "853": "中国澳门", "886": "中国台湾",
    "81": "日本", "82": "韩国", "65": "新加坡", "60": "马来西亚",
    "66": "泰国", "84": "越南", "62": "印度尼西亚", "63": "菲律宾",
    "91": "印度", "92": "巴基斯坦", "880": "孟加拉国",
    "971": "阿联酋", "966": "沙特阿拉伯", "972": "以色列",
    "20": "埃及", "27": "南非", "234": "尼日利亚", "254": "肯尼亚",
    "212": "摩洛哥", "90": "土耳其",
    "44": "英国", "49": "德国", "33": "法国", "39": "意大利",
    "34": "西班牙", "31": "荷兰", "32": "比利时", "43": "奥地利",
    "41": "瑞士", "46": "瑞典", "47": "挪威", "45": "丹麦",
    "358": "芬兰", "48": "波兰", "420": "捷克", "351": "葡萄牙",
    "30": "希腊", "353": "爱尔兰", "7": "俄罗斯", "380": "乌克兰",
    "1": "美国/加拿大", "52": "墨西哥", "55": "巴西", "54": "阿根廷",
    "56": "智利", "57": "哥伦比亚", "51": "秘鲁",
    "61": "澳大利亚", "64": "新西兰",
}

# 公司类型关键词（多语言）
COMPANY_TYPE_SIGNALS = [
    (r"\b(gmbh|ag|kg|ohg|e\.?k\.?)\b", "德国企业"),
    (r"\b(sarl|sa|sas|eurl)\b", "法国企业"),
    (r"\b(s\.?p\.?a\.?|srl|s\.?r\.?l\.?)\b", "意大利企业"),
    (r"\b(b\.?v\.?|n\.?v\.?)\b", "荷兰企业"),
    (r"\b(pty\.?\s*ltd|pty)\b", "澳大利亚企业"),
    (r"\b(ltd\.?|limited|plc|llp)\b", "英国/英联邦企业"),
    (r"\b(inc\.?|corp\.?|corporation|llc|l\.?l\.?c\.?)\b", "美国企业"),
    (r"\b(ltda|s\.?a\.?\s*de\s*c\.?v\.?)\b", "拉美企业"),
    (r"\b(oy|ab|as|aps)\b", "北欧企业"),
]

# 渠道身份
CHANNEL_SIGNALS = [
    (r"\b(distributor|distributorship)\b", "经销商"),
    (r"\b(wholesaler|wholesale)\b", "批发商"),
    (r"\b(importer|import)\b", "进口商"),
    (r"\b(retail(er)?|retail chain|supermarket|hypermarket)\b", "零售商"),
    (r"\b(trader|trading company)\b", "贸易商"),
    (r"\b(agent|agency|representative)\b", "代理商"),
    (r"\b(oem|odm)\b", "OEM/ODM"),
    (r"\b(manufacturer|factory|producer)\b", "制造商"),
    (r"\b(end user|end-user|contractor)\b", "终端用户"),
]

# 货币符号 → 地区提示
CURRENCY_SIGNALS = [
    (r"\b(eur|€)\b", "欧元区"),
    (r"\b(usd|\$)\b", "美元区"),
    (r"\b(gbp|£)\b", "英国"),
    (r"\b(rmb|cny|¥|元)\b", "中国"),
    (r"\b(jpy|¥)\b", "日本"),
    (r"\b(inr|₹)\b", "印度"),
    (r"\b(aed|د\.إ)\b", "阿联酋"),
]


# ============ 规则兜底 ============

def _extract_signature(body: str) -> str:
    """抓取邮件末尾的签名块（通常最后 10 行里的非空行）。"""
    lines = [l.strip() for l in (body or "").splitlines()]
    lines = [l for l in lines if l]
    return "\n".join(lines[-12:]) if lines else ""


def rule_profile(from_addr: str, name: str = "", company: str = "",
                 body: str = "") -> dict:
    """无模型时的规则背调。返回结构化线索，标注低置信度。"""
    addr = (from_addr or "").lower()
    domain = addr.split("@")[-1] if "@" in addr else ""
    sig = _extract_signature(body)
    hay = f"{sig}\n{body or ''}"
    low = hay.lower()

    # --- 国家推断：优先级 ccTLD > 电话区号 > 文中提及 ---
    country, country_conf, country_ev = "", "低", []
    if "." in domain:
        tld = domain.rsplit(".", 1)[-1]
        if tld in CCTLD_COUNTRY:
            country = CCTLD_COUNTRY[tld]
            country_conf = "中"
            country_ev.append(f"邮箱域名 .{tld}")

    if not country:
        for cc in sorted(PHONE_CC, key=len, reverse=True):
            if re.search(rf"\+\s*{cc}\b", hay):
                country = PHONE_CC[cc]
                country_conf = "中"
                country_ev.append(f"电话区号 +{cc}")
                break

    if not country:
        for tld, cn in CCTLD_COUNTRY.items():
            if re.search(rf"\b\w+\.{tld}\b", low):
                country = cn
                country_conf = "低"
                country_ev.append(f"文中出现 .{tld} 网址")
                break

    # --- 免费邮箱判定 ---
    is_free = domain in FREE_MAIL_DOMAINS
    if is_free:
        country_ev.append("使用免费邮箱，主体性偏弱")

    # --- 公司类型 ---
    ctype, ctype_ev = "", []
    for pat, label in COMPANY_TYPE_SIGNALS:
        if re.search(pat, low, re.I):
            ctype = label
            ctype_ev.append(label)
            break

    # --- 渠道身份 ---
    channel, channel_ev = [], []
    for pat, label in CHANNEL_SIGNALS:
        if re.search(pat, low, re.I):
            channel.append(label)
            channel_ev.append(label)

    # --- 币种 ---
    currency = []
    for pat, label in CURRENCY_SIGNALS:
        if re.search(pat, low, re.I):
            currency.append(label)
    if currency and not country:
        country = currency[0].replace("区", "")
        country_conf = "低"
        country_ev.append(f"使用{currency[0]}报价")

    # --- 官网 ---
    website = ""
    m = re.search(r"\b((?:https?://|www\.)[^\s<>\"')]+)", hay, re.I)
    if m:
        website = m.group(1)[:200]

    return {
        "country": country or "",
        "country_conf": country_conf if country else "",
        "company_name": company or "",
        "company_type": ctype,
        "channel_role": "、".join(channel),
        "is_free_mail": is_free,
        "website": website,
        "currency_hint": "、".join(currency),
        "phone": _find_phone(hay),
        "credibility": ("偏低" if is_free else "中"),
        "summary_cn": (f"[规则背调] 域名 {domain}"
                       + (f"，推断国家 {country}" if country else "，国家未识别")
                       + (f"，{ctype}" if ctype else "")
                       + (f"，身份：{'、'.join(channel)}" if channel else "")),
        "evidence": country_ev + ctype_ev + channel_ev,
        "source": "rule",
        "model": "rule-based",
    }


def _find_phone(text: str) -> str:
    m = re.search(r"(\+\d[\d\s\-()]{6,20}\d)", text or "")
    return m.group(1).strip()[:40] if m else ""


# ============ 模型背调 ============

PROFILE_SYSTEM = """You are a trade compliance analyst for a Chinese export company.
Infer the customer's country and company background **from the email content only**.

CRITICAL RULES:
1. You have NO internet access. Base every conclusion strictly on evidence
   found in the provided email text. Never invent facts.
2. If evidence is insufficient, say so explicitly and lower the confidence.
   "Unknown" is an acceptable and often correct answer.
3. For every field you fill, you must cite the exact snippet you based it on.
4. All human-readable text fields MUST be in Simplified Chinese (简体中文).
   Keep company names, person names and product models in their original form.

Return STRICT JSON only, no markdown fences:
{
  "country": "country/region in Chinese, or empty string if unknown",
  "country_confidence": "高|中|低",
  "country_evidence": "the exact snippet that supports the country",
  "company_name": "company name, or empty string",
  "company_type": "in Chinese, e.g. 德国有限责任公司 / 美国股份公司",
  "channel_role": "in Chinese, e.g. 经销商 / 批发商 / 进口商 / 终端用户",
  "business_scope": "in Chinese, what they appear to sell or do",
  "company_size_hint": "in Chinese, size hint if inferable, else empty",
  "website": "website if mentioned, else empty",
  "phone": "phone if mentioned, else empty",
  "credibility": "高|中|低",
  "credibility_reason_cn": "in Chinese, why you rated the credibility this way",
  "risk_flags_cn": ["in Chinese, any warning signs; empty array if none"],
  "summary_cn": "in Chinese, a 2-3 sentence profile summary",
  "evidence": ["in Chinese-labelled list of the exact snippets you relied on"]
}

Inference guidance:
- Corporate email domain (e.g. @mueller-handel.de) → stronger signal than free mail
- ccTLD (.de/.fr/.br...) and phone country code are the most reliable country clues
- Legal-form suffixes (GmbH/SARL/SpA/Pty Ltd/Inc/LLC) indicate the country's legal system
- Free mail domains (gmail/hotmail/yahoo) → weaker identity, lower credibility
- Explicit self-description ("we are a distributor in Germany") is strong evidence
- Be skeptical of vague mass-mail templates; flag them

Return JSON only."""


def ai_profile(from_addr: str, name: str = "", company: str = "",
               country_hint: str = "", body: str = "",
               subject: str = "") -> dict:
    """客户背调主入口。有 Key 走模型，否则规则降级。"""
    if not ai_client.is_enabled():
        return rule_profile(from_addr, name, company, body)

    sig = _extract_signature(body)
    user = f"""Email address: {from_addr}
Contact name: {name or '(unknown)'}
Company field: {company or '(unknown)'}
Country field: {country_hint or '(unknown)'}
Subject: {subject or '(none)'}

--- Email body ---
{(body or '')[:6000]}

--- Signature block (last lines of body) ---
{sig}

Infer the customer profile from the above only."""

    raw = ai_client.chat([
        {"role": "system", "content": PROFILE_SYSTEM},
        {"role": "user", "content": user},
    ], max_tokens=1600)
    parsed = ai_client.extract_json(raw) if raw else None

    if not isinstance(parsed, dict):
        out = rule_profile(from_addr, name, company, body)
        out["summary_cn"] += "（模型不可用，已降级）"
        return out

    def s(key: str, limit: int = 400) -> str:
        return str(parsed.get(key) or "").strip()[:limit]

    ev = parsed.get("evidence") or []
    if isinstance(ev, str):
        ev = [ev]
    ev = [str(x).strip() for x in ev if str(x).strip()][:10]

    flags = parsed.get("risk_flags_cn") or []
    if isinstance(flags, str):
        flags = [flags]
    flags = [str(x).strip() for x in flags if str(x).strip()][:6]

    domain = (from_addr or "").split("@")[-1].lower()
    return {
        "country": s("country", 60),
        "country_conf": s("country_confidence", 10),
        "company_name": s("company_name", 200) or company or "",
        "company_type": s("company_type", 120),
        "channel_role": s("channel_role", 120),
        "business_scope": s("business_scope", 400),
        "company_size_hint": s("company_size_hint", 120),
        "website": s("website", 200),
        "phone": s("phone", 40),
        "credibility": s("credibility", 10) or "中",
        "credibility_reason_cn": s("credibility_reason_cn", 500),
        "risk_flags_cn": flags,
        "summary_cn": s("summary_cn", 800),
        "country_evidence": s("country_evidence", 300),
        "evidence": ev,
        "is_free_mail": domain in FREE_MAIL_DOMAINS,
        "source": "ai",
        "model": ai_client.get_model(),
    }


def format_profile_for_db(res: dict) -> tuple[str, str]:
    """返回 (存进 contacts.country 的值, 存进 contacts.background 的 JSON 文本)。"""
    import json
    country = res.get("country") or ""
    payload = {k: v for k, v in res.items()
               if k not in ("country",)}
    return country, json.dumps(payload, ensure_ascii=False)
