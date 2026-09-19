"""规则引擎：根据 AI 分析结果决定动作（自动回 / 起草待审 / 仅提醒 / 丢弃）。

实验期默认策略：所有邮件都走人工审核队列，不自动发送。
只有显式打开 auto_send 且规则命中 auto_reply 时才会自动发。
"""
import json

from . import db


def load_rules() -> list:
    with db.ro() as conn:
        rows = conn.execute(
            "SELECT * FROM rules WHERE is_active=1 ORDER BY priority ASC"
        ).fetchall()
    out = []
    for r in rows:
        try:
            cond = json.loads(r["cond_json"] or "{}")
        except json.JSONDecodeError:
            cond = {}
        out.append({
            "id": r["id"], "name": r["name"], "cond": cond,
            "action": r["action"], "template_key": r["template_key"],
            "priority": r["priority"],
        })
    return out


def match(analysis: dict, rules: list = None) -> dict:
    """返回命中的规则。按 priority 升序，第一条命中的即生效。"""
    rules = rules if rules is not None else load_rules()
    cat = analysis.get("category") or "other"
    score = analysis.get("score") or 0

    for r in rules:
        cond = r["cond"]
        cats = cond.get("categories")
        if cats and cat not in cats:
            continue
        if cond.get("min_score") is not None and score < cond["min_score"]:
            continue
        if cond.get("max_score") is not None and score > cond["max_score"]:
            continue
        return r

    # 没有任何规则命中时的默认兜底：
    # 若属于无效邮件（spam 垃圾邮件或 other 其他未分类邮件），默认忽略丢弃，绝不自动起草回复
    if cat in ("spam", "other"):
        return {
            "id": None, "name": "无效/其他邮件默认丢弃", "cond": {},
            "action": "drop", "template_key": None,
            "priority": 999,
        }

    return {
        "id": None, "name": "默认兜底", "cond": {},
        "action": "draft_only", "template_key": "inquiry_ack",
        "priority": 999,
    }


def decide(analysis: dict) -> dict:
    """综合规则与全局开关，得出最终动作。"""
    rule = match(analysis)
    action = rule["action"]

    with db.ro() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key='auto_send'").fetchone()
    auto_enabled = (row["value"] if row else "0") == "1"

    # 安全阀：全局未开启自动发送时，auto_reply 降级为 draft_only
    if action == "auto_reply" and not auto_enabled:
        action = "draft_only"
        rule = dict(rule, name=rule["name"] + "（已因全局静默降级）")

    return {"action": action, "rule": rule}
