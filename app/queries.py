"""数据查询层：看板统计、列表分页、详情。全部参数化查询，杜绝拼接。"""
import csv
import io
import json
import re
import time
import urllib.parse
from typing import Optional

from . import config, db, geo_time


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(
        time.time() + config.TZ_OFFSET_HOURS * 3600))


TARGET_CATEGORIES = ("inquiry", "quote", "after_sale", "after_sales", "complaint")
TARGET_CATEGORIES_SQL = "('inquiry', 'quote', 'after_sale', 'after_sales', 'complaint')"


def pending_review_count() -> int:
    """待人工审核的目标邮件草稿数。
    与审核台 /review 列表保持 100% 绝对一致：
    严格仅统计目标邮件（询盘、报价、售后、投诉），排除垃圾邮件 (spam) 与其他无效邮件 (other)。
    """
    with db.ro() as conn:
        r = conn.execute(
            f"SELECT COUNT(*) FROM drafts d "
            f"JOIN messages m ON m.id = d.message_id "
            f"WHERE d.status='pending' AND m.category IN {TARGET_CATEGORIES_SQL}"
        ).fetchone()
        return r[0] if r and r[0] is not None else 0


def clean_invalid_pending_drafts() -> int:
    """清理关联邮件为垃圾邮件或无效分类的历史残留 pending 草稿。"""
    with db.tx() as conn:
        cursor = conn.execute(
            f"UPDATE drafts SET status='cancelled' "
            f"WHERE status='pending' AND message_id IN ("
            f"  SELECT id FROM messages WHERE category NOT IN {TARGET_CATEGORIES_SQL} OR category IS NULL"
            f")"
        )
        return cursor.rowcount


def dashboard() -> dict:
    """看板顶部指标卡。统计口径严格以有效目标邮件为准。"""
    offset = config.TZ_OFFSET_HOURS * 3600
    today = _today()
    yesterday = time.strftime("%Y-%m-%d", time.gmtime(
        time.time() + offset - 86400))
    with db.ro() as conn:
        def one(sql, args=()):
            r = conn.execute(sql, args).fetchone()
            return (r[0] if r and r[0] is not None else 0)

        # 1. 今日有效目标询盘（剔除垃圾邮件与其他）
        today_in = one(f"SELECT COUNT(*) FROM messages WHERE direction='in' "
                       f"AND (category IN {TARGET_CATEGORIES_SQL} OR (category IS NULL AND status='new')) "
                       f"AND date(sent_ts+?,'unixepoch')=?", (offset, today))
        yday_in = one(f"SELECT COUNT(*) FROM messages WHERE direction='in' "
                      f"AND (category IN {TARGET_CATEGORIES_SQL} OR (category IS NULL AND status='new')) "
                      f"AND date(sent_ts+?,'unixepoch')=?", (offset, yesterday))

        # 2. 高意向询盘：必须为目标分类且意向分达到阈值
        high = one(f"SELECT COUNT(*) FROM messages WHERE direction='in' AND score>=? "
                   f"AND category IN {TARGET_CATEGORIES_SQL} "
                   f"AND date(sent_ts+?,'unixepoch')=?",
                   (config.HIGH_INTENT, offset, today))

        # 3. 待人工审核：严格与审核台 /review 列表逻辑完全一致
        pending = one(f"SELECT COUNT(*) FROM drafts d "
                      f"JOIN messages m ON m.id = d.message_id "
                      f"WHERE d.status='pending' AND m.category IN {TARGET_CATEGORIES_SQL}")

        # 4. 今日对外发信
        sent_today = one("SELECT COUNT(*) FROM messages WHERE direction='out' "
                         "AND date(sent_ts+?,'unixepoch')=?", (offset, today))
        auto_today = one("SELECT COUNT(*) FROM messages WHERE direction='out' "
                         "AND status='auto_sent' "
                         "AND date(sent_ts+?,'unixepoch')=?", (offset, today))

        # 5. 累计客户：仅统计高意向客户 (score >= 70) 以及跟进中/已成交客户，剔除垃圾与流失
        total_contacts = one(
            "SELECT COUNT(*) FROM contacts WHERE stage NOT IN ('spam', 'lost') "
            "AND (score >= ? OR stage IN ('engaging', 'quoted', 'negotiating', 'won') OR reply_count > 0)",
            (config.HIGH_INTENT,)
        )

        # 6. 未读有效邮件：剔除垃圾邮件和其他
        unread = one(f"SELECT COUNT(*) FROM messages WHERE direction='in' "
                     f"AND is_read=0 AND category IN {TARGET_CATEGORIES_SQL}")

        acct = conn.execute(
            "SELECT email,last_sync_ts,last_error,is_active FROM accounts "
            "WHERE is_active=1 ORDER BY id LIMIT 1").fetchone()
        any_acct = conn.execute("SELECT COUNT(*) c FROM accounts").fetchone()["c"]

    delta = None
    if yday_in:
        delta = round((today_in - yday_in) / yday_in * 100)
    return {
        "today_in": today_in, "yday_in": yday_in, "delta": delta,
        "high_intent": high, "pending_review": pending,
        "sent_today": sent_today, "auto_today": auto_today,
        "total_contacts": total_contacts, "unread": unread,
        "auto_rate": round(auto_today / sent_today * 100) if sent_today else 0,
        "account": dict(acct) if acct else None,
        "demo_mode": bool(any_acct) and acct is None,
    }


def trend(days: int = 7) -> dict:
    """近 N 日趋势。仅统计有效目标邮件，剔除垃圾与其他数据。"""
    offset = config.TZ_OFFSET_HOURS * 3600
    labels, ins, highs, autos = [], [], [], []
    with db.ro() as conn:
        for i in range(days - 1, -1, -1):
            d = time.strftime("%Y-%m-%d", time.gmtime(
                time.time() + offset - i * 86400))
            labels.append(d[5:])
            r = conn.execute("SELECT in_count,high_intent,auto_count "
                             "FROM daily_stats WHERE day=?", (d,)).fetchone()
            if r:
                ins.append(r["in_count"])
                highs.append(r["high_intent"])
                autos.append(r["auto_count"])
            else:
                ins.append(conn.execute(
                    f"SELECT COUNT(*) c FROM messages WHERE direction='in' "
                    f"AND (category IN {TARGET_CATEGORIES_SQL} OR (category IS NULL AND status='new')) "
                    f"AND date(sent_ts+?,'unixepoch')=?",
                    (offset, d)).fetchone()["c"])
                highs.append(conn.execute(
                    f"SELECT COUNT(*) c FROM messages WHERE direction='in' "
                    f"AND score>=? AND category IN {TARGET_CATEGORIES_SQL} "
                    f"AND date(sent_ts+?,'unixepoch')=?",
                    (config.HIGH_INTENT, offset, d)).fetchone()["c"])
                autos.append(0)
    return {"labels": labels, "in_count": ins, "high_intent": highs,
            "auto_count": autos}


def category_breakdown(days: int = 30) -> list:
    """近 30 日目标邮件分类分布。严格排除 spam 垃圾邮件与 other 其他无效邮件。"""
    since = int(time.time()) - days * 86400
    category_labels = {
        "inquiry": "询盘问价",
        "quote": "索要报价",
        "after_sale": "售后支持",
        "after_sales": "售后支持",
        "complaint": "客户投诉",
    }
    with db.ro() as conn:
        rows = conn.execute(
            f"SELECT category cat, COUNT(*) c, "
            f"ROUND(AVG(score)) avg_score FROM messages WHERE direction='in' "
            f"AND category IN {TARGET_CATEGORIES_SQL} "
            f"AND sent_ts>=? GROUP BY category ORDER BY c DESC", (since,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["cat_cn"] = category_labels.get(d["cat"], d["cat"])
        out.append(d)
    return out


def top_leads(limit: int = 8) -> list:
    """高意向询盘列表。仅展示属于目标分类（询盘/报价/售后/投诉）的有效客户邮件。"""
    with db.ro() as conn:
        rows = conn.execute(
            f"SELECT m.id,m.subject,m.score,m.category,m.sent_ts,m.status,"
            f"m.ai_summary,c.id contact_id,c.email,c.name,c.company,c.country,"
            f"c.stage FROM messages m JOIN contacts c ON c.id=m.contact_id "
            f"WHERE m.direction='in' AND m.score IS NOT NULL "
            f"AND m.category IN {TARGET_CATEGORIES_SQL} "
            f"ORDER BY m.score DESC, m.sent_ts DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def list_messages(page: int = 1, size: int = 20, category: str = "",
                  min_score: int = None, status: str = "", q: str = "",
                  direction: str = "in") -> dict:
    where, args = ["1=1"], []
    if direction:
        where.append("m.direction=?")
        args.append(direction)
    if category:
        if category in ("all", "any"):
            pass  # 查看全量（包含垃圾邮件）
        elif category in ("valid", ""):
            where.append("(m.category != 'spam' OR m.category IS NULL)")
        elif category in ("after_sale", "after_sales"):
            where.append("m.category IN ('after_sale', 'after_sales')")
        else:
            where.append("m.category=?")
            args.append(category)
    else:
        # 默认不选分类或空分类时，自动排除垃圾邮件，优先保证外贸有效询盘体验
        where.append("(m.category != 'spam' OR m.category IS NULL)")
    if min_score is not None:
        where.append("m.score>=?")
        args.append(min_score)
    if status:
        where.append("m.status=?")
        args.append(status)
    if q:
        where.append("(m.subject LIKE ? OR m.body_text LIKE ? OR m.from_addr LIKE ?)")
        like = f"%{q}%"
        args.extend([like, like, like])
    wsql = " AND ".join(where)

    with db.ro() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) c FROM messages m WHERE {wsql}", args).fetchone()["c"]
        rows = conn.execute(
            f"SELECT m.*, c.name contact_name, c.company contact_company, "
            f"c.country contact_country, t.subject thread_subject "
            f"FROM messages m LEFT JOIN contacts c ON c.id=m.contact_id "
            f"LEFT JOIN threads t ON t.id=m.thread_id "
            f"WHERE {wsql} ORDER BY m.sent_ts DESC LIMIT ? OFFSET ?",
            args + [size, (page - 1) * size]).fetchall()
    pages = max(1, (total + size - 1) // size)
    return {"items": [dict(r) for r in rows], "total": total,
            "page": page, "size": size, "pages": pages,
            "prev_page": max(1, page - 1), "next_page": min(pages, page + 1)}


def get_message(mid: int) -> dict | None:
    with db.ro() as conn:
        m = conn.execute(
            "SELECT m.*, c.name contact_name, c.company, c.country, c.stage, "
            "c.score contact_score, c.id contact_id "
            "FROM messages m LEFT JOIN contacts c ON c.id=m.contact_id "
            "WHERE m.id=?", (mid,)).fetchone()
        if not m:
            return None
        out = dict(m)
        out["thread"] = [dict(r) for r in conn.execute(
            "SELECT id,direction,subject,body_text,sent_ts,from_addr,to_addr,status "
            "FROM messages WHERE thread_id=? ORDER BY sent_ts ASC",
            (m["thread_id"],)).fetchall()]
        out["drafts"] = [dict(r) for r in conn.execute(
            "SELECT * FROM drafts WHERE message_id=? ORDER BY id DESC",
            (mid,)).fetchall()]
        try:
            out["entities_parsed"] = json.loads(m["entities"] or "{}")
        except json.JSONDecodeError:
            out["entities_parsed"] = {}
        out["key_points"] = _json_list(m["key_points_cn"])
        return out


def _json_list(raw, limit: int = 12) -> list:
    """把 TEXT 列里存的 JSON 数组还原成 list。任何异常都返回空列表。"""
    if not raw:
        return []
    try:
        v = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list):
        return []
    return [str(x) for x in v if str(x).strip()][:limit]


def list_contacts(page: int = 1, size: int = 20, q: str = "",
                  stage: str = "", min_score: int = None) -> dict:
    where, args = ["1=1"], []
    if q:
        where.append("(email LIKE ? OR name LIKE ? OR company LIKE ?)")
        like = f"%{q}%"
        args.extend([like, like, like])
    if stage:
        where.append("stage=?")
        args.append(stage)
    else:
        # 默认不选阶段时，自动过滤掉垃圾客户与无效数据污染，保障客户池纯净
        where.append("stage NOT IN ('spam', 'invalid')")
    if min_score is not None:
        where.append("score>=?")
        args.append(min_score)
    wsql = " AND ".join(where)
    with db.ro() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) c FROM contacts WHERE {wsql}", args).fetchone()["c"]
        rows = conn.execute(
            f"SELECT * FROM contacts WHERE {wsql} "
            f"ORDER BY score DESC, last_seen_ts DESC LIMIT ? OFFSET ?",
            args + [size, (page - 1) * size]).fetchall()
    items = []
    for r in rows:
        item = dict(r)
        item["local_time"] = geo_time.get_contact_time_info(item, base_tz_offset=config.TZ_OFFSET_HOURS)
        items.append(item)
    pages = max(1, (total + size - 1) // size)
    return {"items": items, "total": total, "page": page,
            "size": size, "pages": pages,
            "prev_page": max(1, page - 1), "next_page": min(pages, page + 1)}


def clean_spam_contacts() -> int:
    """清理垃圾邮件与无效数据污染的客户档案。
    如果一个客户名下的所有邮件均为垃圾邮件 (spam)，则将其 stage 自动更新为 'spam'；
    如果一个客户名下的邮件全为 'other' 且最高分低于 30 且没有任何我方回信记录，则将其 stage 标记为 'invalid'。
    """
    with db.tx() as conn:
        # 1. 所有来信均为 spam 的客户
        c1 = conn.execute(
            """UPDATE contacts SET stage='spam'
               WHERE stage NOT IN ('spam', 'lost', 'won') AND id IN (
                   SELECT contact_id FROM messages
                   WHERE contact_id IS NOT NULL
                   GROUP BY contact_id
                   HAVING COUNT(*) > 0 AND SUM(CASE WHEN category = 'spam' THEN 1 ELSE 0 END) = COUNT(*)
               )"""
        ).rowcount
        # 2. 所有来信均为 other、意向分极低 (<30) 且无客户回信的无效客户
        c2 = conn.execute(
            """UPDATE contacts SET stage='invalid'
               WHERE stage NOT IN ('spam', 'invalid', 'won') AND score < 30 AND reply_count = 0 AND id IN (
                   SELECT contact_id FROM messages
                   WHERE contact_id IS NOT NULL
                   GROUP BY contact_id
                   HAVING COUNT(*) > 0 AND SUM(CASE WHEN category = 'other' THEN 1 ELSE 0 END) = COUNT(*)
               )"""
        ).rowcount
        return c1 + c2


def get_contact(cid: int) -> dict | None:
    with db.ro() as conn:
        c = conn.execute("SELECT * FROM contacts WHERE id=?", (cid,)).fetchone()
        if not c:
            return None
        out = dict(c)
        out["threads"] = [dict(r) for r in conn.execute(
            "SELECT t.*, (SELECT COUNT(*) FROM messages m WHERE m.thread_id=t.id) c "
            "FROM threads t WHERE contact_id=? ORDER BY last_ts DESC LIMIT 20",
            (cid,)).fetchall()]
        out["recent"] = [dict(r) for r in conn.execute(
            "SELECT id,subject,direction,sent_ts,score,category,status "
            "FROM messages WHERE contact_id=? ORDER BY sent_ts DESC LIMIT 20",
            (cid,)).fetchall()]
        out["tags"] = [dict(r) for r in conn.execute(
            "SELECT t.id,t.name,t.color FROM tags t JOIN contact_tags ct "
            "ON ct.tag_id=t.id WHERE ct.contact_id=?", (cid,)).fetchall()]
        # 客户背调档案（JSON）：模板里直接用 bg.xxx 取字段
        out["bg"] = _json_obj(c["background"])
        # 计算客户当地时间与沟通时机
        out["local_time"] = geo_time.get_contact_time_info(out, base_tz_offset=config.TZ_OFFSET_HOURS)
        out["local_time_card"] = geo_time.render_time_card(out["local_time"])
        return out


def _json_obj(raw) -> dict:
    """把 TEXT 列里存的 JSON 对象还原成 dict。任何异常都返回空 dict。"""
    if not raw:
        return {}
    try:
        v = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return v if isinstance(v, dict) else {}


def save_contact(data: dict) -> tuple[bool, str, int | None]:
    """新增或更新客户信息。返回 (是否成功, 说明信息, contact_id)。"""
    email_addr = (data.get("email") or "").strip().lower()
    if not email_addr or "@" not in email_addr:
        return False, "请提供有效的客户邮箱", None

    cid = data.get("id")
    cid = int(cid) if cid and str(cid).isdigit() else None
    name = (data.get("name") or "").strip()
    company = (data.get("company") or "").strip()
    country = (data.get("country") or "").strip()
    if not country:
        inferred = geo_time.infer_country_from_contact({"email": email_addr})
        if inferred:
            reg = geo_time.resolve_country(inferred)
            if reg:
                country = reg["name"]

    stage = (data.get("stage") or "new").strip()
    company_type = (data.get("company_type") or "").strip()
    channel_role = (data.get("channel_role") or "").strip()
    credibility = (data.get("credibility") or "").strip()
    note = (data.get("note") or "").strip()

    ts = db.now_ts()

    with db.ro() as conn:
        dup = conn.execute(
            "SELECT id FROM contacts WHERE email=? AND id<>?",
            (email_addr, cid or -1)
        ).fetchone()
        if dup:
            return False, f"邮箱 {email_addr} 已存在于客户库中", None

    with db.tx() as conn:
        if cid:
            conn.execute(
                "UPDATE contacts SET email=?, name=?, company=?, country=?, stage=?,"
                "company_type=?, channel_role=?, credibility=?, note=? WHERE id=?",
                (email_addr, name, company, country, stage,
                 company_type, channel_role, credibility, note, cid)
            )
            return True, "客户信息已更新", cid
        else:
            cur = conn.execute(
                "INSERT INTO contacts (email, name, company, country, stage,"
                "company_type, channel_role, credibility, note, source,"
                "first_seen_ts, last_seen_ts, msg_count, reply_count, score) "
                "VALUES (?,?,?,?,?,?,?,?,?,'manual',?,?,0,0,0)",
                (email_addr, name, company, country, stage,
                 company_type, channel_role, credibility, note, ts, ts)
            )
            return True, "客户已新建", cur.lastrowid


def delete_contact(cid: int) -> bool:
    """删除指定客户及其关联的所有邮件、线索与草稿。"""
    with db.tx() as conn:
        conn.execute("DELETE FROM drafts WHERE contact_id=?", (cid,))
        conn.execute("DELETE FROM messages WHERE contact_id=?", (cid,))
        conn.execute("DELETE FROM threads WHERE contact_id=?", (cid,))
        conn.execute("DELETE FROM contact_tags WHERE contact_id=?", (cid,))
        conn.execute("DELETE FROM contacts WHERE id=?", (cid,))
    return True


def update_contact_stage(cid: int, stage: str) -> bool:
    """更新客户所处的销售跟进阶段。"""
    valid_stages = {"new", "engaging", "quoted", "negotiating", "won", "lost", "spam"}
    stage = stage.strip().lower()
    if stage not in valid_stages:
        return False
    with db.tx() as conn:
        conn.execute("UPDATE contacts SET stage=? WHERE id=?", (stage, cid))
    return True


def clear_all_contacts() -> dict:
    """彻底清空所有客户数据及关联的会话、邮件与草稿，重置统计。"""
    with db.tx() as conn:
        n_c = conn.execute("DELETE FROM contacts").rowcount
        conn.execute("DELETE FROM threads")
        conn.execute("DELETE FROM messages")
        conn.execute("DELETE FROM drafts")
        conn.execute("DELETE FROM contact_tags")
        conn.execute("DELETE FROM daily_stats")
        try:
            conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('contacts','threads','messages','drafts')")
        except Exception:
            pass
    return {"contacts_cleared": n_c}


def export_contacts_csv() -> str:
    """导出全部客户数据为 CSV 格式字符串（包含 UTF-8 BOM）。"""
    with db.ro() as conn:
        rows = conn.execute(
            "SELECT * FROM contacts ORDER BY score DESC, id DESC"
        ).fetchall()

    buf = io.StringIO()
    # 写入 UTF-8 BOM 头，确保 Windows Excel 打开时不乱码
    buf.write('\ufeff')
    writer = csv.writer(buf)
    writer.writerow([
        "客户ID", "邮箱", "联系人姓名", "公司名称", "国家/地区",
        "当地时间", "时区时差", "商务作息与建议",
        "意向分", "跟进阶段", "公司类型", "渠道身份", "可信度",
        "收到邮件数", "回复邮件数", "首次联系时间", "最近联系时间", "备注"
    ])

    offset = config.TZ_OFFSET_HOURS * 3600
    for r in rows:
        def fmt_ts(ts):
            if not ts:
                return ""
            return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts + offset))

        r_dict = dict(r)
        tz_info = geo_time.get_contact_time_info(r_dict, base_tz_offset=config.TZ_OFFSET_HOURS)
        local_time_str = f"{tz_info['weekday_cn']} {tz_info['time_str']}" if tz_info else ""
        tz_diff_str = f"{tz_info['utc_str']} ({tz_info['diff_desc']})" if tz_info else ""
        advice_str = f"[{tz_info['status_text']}] {tz_info['advice']}" if tz_info else ""

        writer.writerow([
            r["id"],
            r["email"] or "",
            r["name"] or "",
            r["company"] or "",
            r["country"] or (tz_info["country"] if tz_info else ""),
            local_time_str,
            tz_diff_str,
            advice_str,
            r["score"] if r["score"] is not None else 0,
            r["stage"] or "new",
            r["company_type"] or "",
            r["channel_role"] or "",
            r["credibility"] or "",
            r["msg_count"] or 0,
            r["reply_count"] or 0,
            fmt_ts(r["first_seen_ts"]),
            fmt_ts(r["last_seen_ts"]),
            r["note"] or "",
        ])
    return buf.getvalue()


def pending_drafts(limit: int = 50) -> list:
    with db.ro() as conn:
        rows = conn.execute(
            f"SELECT d.*, m.subject in_subject, m.from_addr, m.body_text in_body, "
            f"m.sent_ts in_ts, m.score, m.category, m.summary_cn, m.key_points_cn, "
            f"c.name contact_name, c.company FROM drafts d "
            f"JOIN messages m ON m.id=d.message_id "
            f"LEFT JOIN contacts c ON c.id=d.contact_id "
            f"WHERE d.status='pending' AND m.category IN {TARGET_CATEGORIES_SQL} "
            f"ORDER BY m.score DESC, d.created_ts DESC "
            f"LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def list_templates() -> list:
    with db.ro() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM templates ORDER BY category,key").fetchall()]


def list_rules() -> list:
    with db.ro() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM rules ORDER BY priority").fetchall()]


def list_settings() -> dict:
    with db.ro() as conn:
        return {r["key"]: r["value"] for r in conn.execute(
            "SELECT key,value FROM settings").fetchall()}


def audit_tail(limit: int = 50) -> list:
    with db.ro() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM audit_log ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()]


def log(actor: str, action: str, target_type: str = None,
        target_id: int = None, detail: str = "") -> None:
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO audit_log (ts,actor,action,target_type,target_id,detail) "
            "VALUES (?,?,?,?,?,?)",
            (db.now_ts(), actor, action, target_type, target_id, detail[:1000]))


# ============ 货运代理与报价比价 ============

CHANNEL_NAMES = {
    "sea": "海运",
    "air": "空运",
    "express": "国际快递",
    "rail": "铁运",
    "truck": "卡航",
}

CHANNEL_ICONS = {
    "sea": "🌊",
    "air": "✈️",
    "express": "🚀",
    "rail": "🚆",
    "truck": "🚛",
}

UNIT_NAMES = {
    "kg": "元/KG",
    "cbm": "元/CBM",
    "20gp": "元/20GP(小柜)",
    "40hq": "元/40HQ(高柜)",
    "flat": "元/票(一口价)",
}

FX_RATES = {
    "CNY": 1.0,
    "USD": 7.20,
    "EUR": 7.85,
}


def list_forwarders(q: str = "") -> list[dict]:
    """获取所有货代列表，附带报价数量统计。"""
    where = ["1=1"]
    args = []
    if q and q.strip():
        kw = f"%{q.strip()}%"
        where.append("(f.name LIKE ? OR f.contact_person LIKE ? OR f.phone LIKE ? OR f.advantages LIKE ?)")
        args.extend([kw, kw, kw, kw])
    sql = f"""
        SELECT f.*,
               (SELECT COUNT(*) FROM freight_quotes q WHERE q.forwarder_id = f.id) AS quote_count
        FROM forwarders f
        WHERE {" AND ".join(where)}
        ORDER BY f.rating DESC, f.updated_ts DESC
    """
    with db.ro() as conn:
        rows = conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]


def get_forwarder(fid: int) -> dict | None:
    """获取单个货代详情及其所有渠道报价。"""
    with db.ro() as conn:
        f = conn.execute("SELECT * FROM forwarders WHERE id=?", (fid,)).fetchone()
        if not f:
            return None
        out = dict(f)
        quotes = conn.execute(
            "SELECT * FROM freight_quotes WHERE forwarder_id=? ORDER BY channel_type, unit_price ASC",
            (fid,)
        ).fetchall()
        out["quotes"] = [_format_quote_row(dict(q), f["name"]) for q in quotes]
        return out


def save_forwarder(data: dict) -> tuple[bool, str, int | None]:
    """新增或更新货代信息。"""
    name = (data.get("name") or "").strip()
    if not name:
        return False, "货代公司/代理名称不能为空", None

    fid = data.get("id")
    fid = int(fid) if fid and str(fid).isdigit() else None
    contact_person = (data.get("contact_person") or "").strip()
    phone = (data.get("phone") or "").strip()
    email = (data.get("email") or "").strip()
    wechat_or_im = (data.get("wechat_or_im") or "").strip()
    advantages = (data.get("advantages") or "").strip()
    try:
        rating = max(1, min(5, int(data.get("rating") or 5)))
    except ValueError:
        rating = 5
    address = (data.get("address") or "").strip()
    note = (data.get("note") or "").strip()
    ts = db.now_ts()

    with db.tx() as conn:
        if fid:
            conn.execute(
                """UPDATE forwarders
                   SET name=?, contact_person=?, phone=?, email=?, wechat_or_im=?,
                       advantages=?, rating=?, address=?, note=?, updated_ts=?
                   WHERE id=?""",
                (name, contact_person, phone, email, wechat_or_im, advantages,
                 rating, address, note, ts, fid)
            )
            return True, "货代信息已更新", fid
        else:
            cur = conn.execute(
                """INSERT INTO forwarders
                   (name, contact_person, phone, email, wechat_or_im, advantages,
                    rating, address, note, created_ts, updated_ts)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (name, contact_person, phone, email, wechat_or_im, advantages,
                 rating, address, note, ts, ts)
            )
            return True, "已添加新货代", cur.lastrowid


def delete_forwarder(fid: int) -> bool:
    """删除货代（级联删除关联报价）。"""
    with db.tx() as conn:
        conn.execute("DELETE FROM forwarders WHERE id=?", (fid,))
    return True


def _format_quote_row(q: dict, forwarder_name: str = "") -> dict:
    """格式化报价行显示数据。"""
    channel = q.get("channel_type", "sea")
    q["channel_name"] = CHANNEL_NAMES.get(channel, channel)
    q["channel_icon"] = CHANNEL_ICONS.get(channel, "📦")
    q["forwarder_name"] = forwarder_name or q.get("forwarder_name", "")
    p_unit = q.get("price_unit", "kg")
    curr = q.get("currency", "CNY")
    curr_sym = "¥" if curr == "CNY" else ("$" if curr == "USD" else "€")
    q["currency_symbol"] = curr_sym
    q["unit_display"] = f"{curr_sym}{q['unit_price']:.2f} / {p_unit.upper()}"

    # 有效期状态
    valid_until = (q.get("valid_until") or "").strip()
    today_str = _today()
    if valid_until:
        if valid_until < today_str:
            q["valid_status"] = "expired"
            q["valid_badge"] = '<span class="status-pill status-expired">已过期</span>'
        else:
            q["valid_status"] = "valid"
            q["valid_badge"] = f'<span class="status-pill status-active">有效期至 {valid_until}</span>'
    else:
        q["valid_status"] = "valid"
        q["valid_badge"] = '<span class="status-pill status-active">长期有效</span>'
    return q


def list_freight_quotes(forwarder_id: int = None, channel_type: str = "",
                        destination: str = "", q: str = "") -> list[dict]:
    """查询报价列表，可按货代、渠道、目的地或关键词筛选。"""
    where = ["1=1"]
    args = []
    if forwarder_id:
        where.append("q.forwarder_id = ?")
        args.append(forwarder_id)
    if channel_type:
        where.append("q.channel_type = ?")
        args.append(channel_type)
    if destination:
        where.append("q.destination LIKE ?")
        args.append(f"%{destination.strip()}%")
    if q and q.strip():
        kw = f"%{q.strip()}%"
        where.append("(q.title LIKE ? OR q.destination LIKE ? OR q.origin LIKE ? OR f.name LIKE ?)")
        args.extend([kw, kw, kw, kw])

    sql = f"""
        SELECT q.*, f.name AS forwarder_name, f.contact_person AS forwarder_contact,
               f.phone AS forwarder_phone, f.rating AS forwarder_rating, f.wechat_or_im AS forwarder_im
        FROM freight_quotes q
        JOIN forwarders f ON q.forwarder_id = f.id
        WHERE {" AND ".join(where)}
        ORDER BY q.channel_type, q.unit_price ASC
    """
    with db.ro() as conn:
        rows = conn.execute(sql, args).fetchall()
        return [_format_quote_row(dict(r)) for r in rows]


def get_freight_quote(qid: int) -> dict | None:
    """获取单个报价详情。"""
    sql = """
        SELECT q.*, f.name AS forwarder_name, f.contact_person AS forwarder_contact,
               f.phone AS forwarder_phone, f.rating AS forwarder_rating
        FROM freight_quotes q
        JOIN forwarders f ON q.forwarder_id = f.id
        WHERE q.id = ?
    """
    with db.ro() as conn:
        r = conn.execute(sql, (qid,)).fetchone()
        if not r:
            return None
        return _format_quote_row(dict(r))


def save_freight_quote(data: dict) -> tuple[bool, str, int | None]:
    """新增或更新货代报价。"""
    fid = data.get("forwarder_id")
    if not fid or not str(fid).isdigit():
        return False, "必须关联有效的货运代理", None
    fid = int(fid)

    title = (data.get("title") or "").strip()
    if not title:
        return False, "渠道方案名称不能为空（如：美森快船超大件、DHL特惠）", None

    channel_type = (data.get("channel_type") or "sea").strip().lower()
    if channel_type not in CHANNEL_NAMES:
        channel_type = "sea"

    origin = (data.get("origin") or "深圳").strip()
    destination = (data.get("destination") or "").strip()
    if not destination:
        return False, "目的地/目的港不能为空", None

    try:
        unit_price = float(data.get("unit_price") or 0)
        if unit_price <= 0:
            return False, "基准单价必须大于 0", None
    except ValueError:
        return False, "单价格式不正确", None

    price_unit = (data.get("price_unit") or "kg").strip().lower()
    if price_unit not in UNIT_NAMES:
        price_unit = "kg"

    currency = (data.get("currency") or "CNY").strip().upper()
    if currency not in FX_RATES:
        currency = "CNY"

    min_charge = (data.get("min_charge") or "").strip()
    transit_time_text = (data.get("transit_time_text") or "").strip()
    valid_until = (data.get("valid_until") or "").strip()
    extra_fees = (data.get("extra_fees") or "").strip()
    remarks = (data.get("remarks") or "").strip()
    ts = db.now_ts()

    qid = data.get("id")
    qid = int(qid) if qid and str(qid).isdigit() else None

    with db.tx() as conn:
        if qid:
            conn.execute(
                """UPDATE freight_quotes
                   SET forwarder_id=?, title=?, channel_type=?, origin=?, destination=?,
                       unit_price=?, price_unit=?, currency=?, min_charge=?,
                       transit_time_text=?, valid_until=?, extra_fees=?, remarks=?, updated_ts=?
                   WHERE id=?""",
                (fid, title, channel_type, origin, destination, unit_price, price_unit,
                 currency, min_charge, transit_time_text, valid_until, extra_fees, remarks, ts, qid)
            )
            return True, "报价方案已更新", qid
        else:
            cur = conn.execute(
                """INSERT INTO freight_quotes
                   (forwarder_id, title, channel_type, origin, destination,
                    unit_price, price_unit, currency, min_charge,
                    transit_time_text, valid_until, extra_fees, remarks, created_ts, updated_ts)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (fid, title, channel_type, origin, destination, unit_price, price_unit,
                 currency, min_charge, transit_time_text, valid_until, extra_fees, remarks, ts, ts)
            )
            return True, "已录入新报价方案", cur.lastrowid


def delete_freight_quote(qid: int) -> bool:
    """删除单条报价。"""
    with db.tx() as conn:
        conn.execute("DELETE FROM freight_quotes WHERE id=?", (qid,))
    return True


def compare_shipping_costs(destination: str = "", channel_type: str = "",
                           weight_kg: float = 0.0, volume_cbm: float = 0.0) -> dict:
    """核心运费对比与测算引擎。

    输入：目的地/国家、运输渠道、货物毛重(KG)、货物体积(CBM)
    计算：
      - 智能计费重量/体积判定（空运/快递体积重换算）
      - 各家货代渠道预估总运费（折算汇率对比）
      - 评选「最优惠」与「最快时效」
      - 生成一键复制给客户的报价沟通文案
    """
    quotes = list_freight_quotes(channel_type=channel_type, destination=destination)

    calc_results = []
    for q in quotes:
        unit_p = q["unit_price"]
        p_unit = q["price_unit"]
        curr = q["currency"]
        rate = FX_RATES.get(curr, 1.0)

        # 计费测算
        estimated_cost = 0.0
        charge_basis = ""
        if p_unit == "kg":
            # 快递计费除以 5000（1CBM=200KG），空运计费除以 6000（1CBM=167KG）
            vol_ratio = 200.0 if q["channel_type"] == "express" else 167.0
            vol_weight = volume_cbm * vol_ratio if volume_cbm > 0 else 0.0
            chargeable_weight = max(weight_kg, vol_weight) if (weight_kg > 0 or volume_cbm > 0) else 1.0
            estimated_cost = unit_p * chargeable_weight
            charge_basis = f"{chargeable_weight:.1f} KG"
        elif p_unit == "cbm":
            effective_cbm = volume_cbm if volume_cbm > 0 else (weight_kg / 500.0 if weight_kg > 0 else 1.0)
            estimated_cost = unit_p * effective_cbm
            charge_basis = f"{effective_cbm:.2f} CBM"
        elif p_unit in ("20gp", "40hq", "flat"):
            estimated_cost = unit_p
            charge_basis = "整柜/整票"

        cost_cny = estimated_cost * rate
        # 提取天数估值用于时效排序
        days_est = 999
        t_text = q.get("transit_time_text") or ""
        import re
        nums = [int(n) for n in re.findall(r"\d+", t_text)]
        if nums:
            days_est = nums[0]

        item = dict(q)
        item["estimated_cost"] = round(estimated_cost, 2)
        item["cost_cny"] = round(cost_cny, 2)
        item["charge_basis"] = charge_basis
        item["days_est"] = days_est
        item["is_cheapest"] = False
        item["is_fastest"] = False
        calc_results.append(item)

    # 排序与标记
    if calc_results:
        # 按总成本折合人民币由低到高排序
        calc_results.sort(key=lambda x: x["cost_cny"])
        calc_results[0]["is_cheapest"] = True

        # 找出最快
        fastest = min(calc_results, key=lambda x: x["days_est"])
        if fastest["days_est"] < 999:
            fastest["is_fastest"] = True

    # 生成一键复制给客户的报价文本
    summary_lines = []
    dest_display = destination.strip() if destination else "全球/指定目的地"
    summary_lines.append(f"【运费测算与对比参考】")
    summary_lines.append(f"• 目的地：{dest_display}")
    if weight_kg > 0 or volume_cbm > 0:
        summary_lines.append(f"• 货物规格：{weight_kg:.1f} KG / {volume_cbm:.2f} CBM")
    summary_lines.append("----------------------------------------")
    for idx, r in enumerate(calc_results[:5], 1):
        tag = ""
        if r.get("is_cheapest") and r.get("is_fastest"):
            tag = " 🏆[性价比之王·最快且最低]"
        elif r.get("is_cheapest"):
            tag = " 🥇[最低成本]"
        elif r.get("is_fastest"):
            tag = " ⚡[最快时效]"

        transit = f" | 时效: {r['transit_time_text']}" if r.get('transit_time_text') else ""
        summary_lines.append(
            f"{idx}. {r['forwarder_name']} - {r['title']} ({r['channel_name']}){tag}\n"
            f"   预估运费: {r['currency_symbol']}{r['estimated_cost']:.2f} (约 ¥{r['cost_cny']:.0f}){transit}\n"
            f"   单价标准: {r['unit_display']}"
        )
        if r.get("extra_fees"):
            summary_lines.append(f"   杂费/条款: {r['extra_fees']}")
    summary_lines.append("----------------------------------------")
    summary_lines.append("注：以上费用为系统根据货代实时报价估算，最终以装柜/出运实际磅码数据及货代账单为准。")

    return {
        "quotes": calc_results,
        "destination": destination,
        "channel_type": channel_type,
        "weight_kg": weight_kg,
        "volume_cbm": volume_cbm,
        "summary_text": "\n".join(summary_lines),
        "total_matches": len(calc_results),
    }


def export_freight_quotes_csv() -> str:
    """导出所有货代报价为 CSV 文本。"""
    quotes = list_freight_quotes()
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow([
        "货代公司", "联系人", "联系电话", "渠道方案", "运输方式",
        "起运地", "目的地", "基准单价", "计价单位", "币种",
        "起运限制", "预估时效", "报价有效期", "状态", "杂费说明", "备注"
    ])
    for q in quotes:
        writer.writerow([
            q.get("forwarder_name", ""),
            q.get("forwarder_contact", ""),
            q.get("forwarder_phone", ""),
            q.get("title", ""),
            q.get("channel_name", ""),
            q.get("origin", ""),
            q.get("destination", ""),
            q.get("unit_price", ""),
            q.get("price_unit", ""),
            q.get("currency", ""),
            q.get("min_charge", ""),
            q.get("transit_time_text", ""),
            q.get("valid_until", ""),
            "有效" if q.get("valid_status") == "valid" else "已过期",
            q.get("extra_fees", ""),
            q.get("remarks", ""),
        ])
    return out.getvalue()


# ============ 外贸订单全生命周期流转 ============

ORDER_STAGES = {
    "draft": {
        "key": "draft",
        "name": "洽谈起草",
        "step": 1,
        "icon": "📝",
        "color": "#64748b",
        "bg": "#f1f5f9",
        "next": "pi_confirmed",
        "action": "确认PI并发送买家",
    },
    "pi_confirmed": {
        "key": "pi_confirmed",
        "name": "PI已确认",
        "step": 2,
        "icon": "📄",
        "color": "#0284c7",
        "bg": "#e0f2fe",
        "next": "deposit_received",
        "action": "收到定金水单",
    },
    "deposit_received": {
        "key": "deposit_received",
        "name": "定金到账",
        "step": 3,
        "icon": "💰",
        "color": "#059669",
        "bg": "#d1fae5",
        "next": "in_production",
        "action": "通知工厂排产",
    },
    "in_production": {
        "key": "in_production",
        "name": "工厂排产中",
        "step": 4,
        "icon": "🏭",
        "color": "#d97706",
        "bg": "#fef3c7",
        "next": "qc_passed",
        "action": "验货合格通过",
    },
    "qc_passed": {
        "key": "qc_passed",
        "name": "验货完成",
        "step": 5,
        "icon": "✅",
        "color": "#16a34a",
        "bg": "#dcfce7",
        "next": "balance_received",
        "action": "收到尾款水单",
    },
    "balance_received": {
        "key": "balance_received",
        "name": "尾款已收齐",
        "step": 6,
        "icon": "💳",
        "color": "#2563eb",
        "bg": "#eff6ff",
        "next": "shipping",
        "action": "安排订舱出运",
    },
    "shipping": {
        "key": "shipping",
        "name": "订舱出运中",
        "step": 7,
        "icon": "🚢",
        "color": "#7c3aed",
        "bg": "#ede9fe",
        "next": "completed",
        "action": "客户已提货归档",
    },
    "completed": {
        "key": "completed",
        "name": "履约完成",
        "step": 8,
        "icon": "🏆",
        "color": "#0f766e",
        "bg": "#ccfbf1",
        "next": None,
        "action": None,
    },
}

CURRENCY_SYMBOLS = {
    "USD": "$",
    "EUR": "€",
    "CNY": "¥",
    "GBP": "£",
}


def _format_order_row(r: dict) -> dict:
    """格式化订单行：计算利润、汇率折合、交期倒计时与阶段徽章。"""
    stage = r.get("stage") or "draft"
    st_info = ORDER_STAGES.get(stage, ORDER_STAGES["draft"])
    r["stage_name"] = st_info["name"]
    r["stage_icon"] = st_info["icon"]
    r["stage_step"] = st_info["step"]
    r["stage_color"] = st_info["color"]
    r["stage_bg"] = st_info["bg"]
    r["next_stage"] = st_info["next"]
    r["next_action"] = st_info["action"]
    r["stage_badge"] = f'<span class="order-stage-badge" style="background:{st_info["bg"]};color:{st_info["color"]};border:1px solid {st_info["color"]}33;">{st_info["icon"]} {st_info["name"]}</span>'

    curr = r.get("currency") or "USD"
    curr_sym = CURRENCY_SYMBOLS.get(curr, "$")
    r["currency_symbol"] = curr_sym

    total_amount = float(r.get("total_amount") or 0.0)
    deposit_amount = float(r.get("deposit_amount") or 0.0)
    balance_amount = float(r.get("balance_amount") or 0.0)
    received_total = deposit_amount + balance_amount
    received_pct = round(received_total / total_amount * 100) if total_amount > 0 else 0
    r["total_amount_formatted"] = f"{curr_sym}{total_amount:,.2f}"
    r["received_total"] = round(received_total, 2)
    r["received_total_formatted"] = f"{curr_sym}{received_total:,.2f}"
    r["received_pct"] = received_pct

    fx_rate = float(r.get("settlement_fx_rate") or FX_RATES.get(curr, 7.20))
    r["settlement_fx_rate"] = fx_rate
    revenue_cny = round(total_amount * fx_rate, 2)
    r["revenue_cny"] = revenue_cny

    factory_cost = float(r.get("factory_cost") or 0.0)
    shipping_cost = float(r.get("shipping_cost") or 0.0)
    other_cost = float(r.get("other_cost") or 0.0)
    total_cost_cny = round(factory_cost + shipping_cost + other_cost, 2)
    r["total_cost_cny"] = total_cost_cny

    net_profit_cny = round(revenue_cny - total_cost_cny, 2)
    profit_margin = round(net_profit_cny / revenue_cny * 100, 1) if revenue_cny > 0 else 0.0
    r["net_profit_cny"] = net_profit_cny
    r["profit_margin"] = profit_margin

    # 交期倒计时与告警
    etd = (r.get("est_delivery_date") or "").strip()
    r["etd_display"] = etd or "未设置"
    if etd and stage != "completed":
        today_str = _today()
        import datetime
        try:
            d_etd = datetime.date.fromisoformat(etd)
            d_today = datetime.date.fromisoformat(today_str)
            diff = (d_etd - d_today).days
            if diff < 0:
                r["etd_status"] = "overdue"
                r["etd_tag"] = f'<span class="etd-badge etd-danger">⚠️ 逾期 {-diff} 天</span>'
            elif diff <= 7:
                r["etd_status"] = "warning"
                r["etd_tag"] = f'<span class="etd-badge etd-warn">⏰ 剩 {diff} 天交期</span>'
            else:
                r["etd_status"] = "normal"
                r["etd_tag"] = f'<span class="etd-badge etd-ok">剩 {diff} 天</span>'
        except Exception:
            r["etd_status"] = "none"
            r["etd_tag"] = f'<span class="etd-badge">{etd}</span>'
    elif stage == "completed":
        r["etd_status"] = "completed"
        r["etd_tag"] = '<span class="etd-badge etd-ok">已完成交付</span>'
    else:
        r["etd_status"] = "none"
        r["etd_tag"] = '<span class="etd-badge etd-none">未排交期</span>'

    return r


def list_orders(stage: str = "", contact_id: int = None, q: str = "") -> list[dict]:
    """查询订单列表，支持阶段、客户与关键词筛选。"""
    where = ["1=1"]
    args = []
    if stage:
        where.append("o.stage = ?")
        args.append(stage)
    if contact_id:
        where.append("o.contact_id = ?")
        args.append(contact_id)
    if q and q.strip():
        kw = f"%{q.strip()}%"
        where.append("(o.order_no LIKE ? OR o.title LIKE ? OR c.name LIKE ? OR c.company LIKE ? OR o.destination_port LIKE ?)")
        args.extend([kw, kw, kw, kw, kw])

    sql = f"""
        SELECT o.*,
               c.name AS customer_name, c.company AS customer_company,
               c.email AS customer_email, c.country AS customer_country,
               fq.title AS forwarder_quote_title, fq.channel_type AS forwarder_channel
        FROM orders o
        JOIN contacts c ON o.contact_id = c.id
        LEFT JOIN freight_quotes fq ON o.forwarder_quote_id = fq.id
        WHERE {" AND ".join(where)}
        ORDER BY CASE o.stage WHEN 'completed' THEN 2 ELSE 1 END,
                 CASE WHEN o.est_delivery_date IS NULL OR o.est_delivery_date = '' THEN '9999-99-99' ELSE o.est_delivery_date END ASC,
                 o.updated_ts DESC
    """
    with db.ro() as conn:
        rows = conn.execute(sql, args).fetchall()
        return [_format_order_row(dict(r)) for r in rows]


def get_orders_kanban() -> dict:
    """获取 8 大流转阶段的订单泳道数据，含每阶段金额统计。"""
    orders = list_orders()
    cols = {st_key: {"stage": st_key, "info": st_info, "orders": [], "count": 0, "amount_sum": 0.0}
            for st_key, st_info in ORDER_STAGES.items()}

    total_count = len(orders)
    total_revenue_cny = 0.0
    total_profit_cny = 0.0

    for od in orders:
        st = od.get("stage", "draft")
        if st in cols:
            cols[st]["orders"].append(od)
            cols[st]["count"] += 1
            cols[st]["amount_sum"] += od.get("total_amount", 0.0)
        total_revenue_cny += od.get("revenue_cny", 0.0)
        total_profit_cny += od.get("net_profit_cny", 0.0)

    for col in cols.values():
        col["amount_sum_formatted"] = f"{col['amount_sum']:,.2f}"

    return {
        "columns": list(cols.values()),
        "total_orders": total_count,
        "total_revenue_cny": round(total_revenue_cny, 2),
        "total_profit_cny": round(total_profit_cny, 2),
    }


def get_order(oid: int) -> dict | None:
    """获取订单详情，包含关联客户、货代报价及全过程时间轴。"""
    sql = """
        SELECT o.*,
               c.name AS customer_name, c.company AS customer_company,
               c.email AS customer_email, c.country AS customer_country,
               fq.title AS forwarder_quote_title, fq.channel_type AS forwarder_channel,
               fq.transit_time_text AS forwarder_transit, fq.extra_fees AS forwarder_fees,
               f.name AS forwarder_name, f.phone AS forwarder_phone
        FROM orders o
        JOIN contacts c ON o.contact_id = c.id
        LEFT JOIN freight_quotes fq ON o.forwarder_quote_id = fq.id
        LEFT JOIN forwarders f ON fq.forwarder_id = f.id
        WHERE o.id = ?
    """
    with db.ro() as conn:
        r = conn.execute(sql, (oid,)).fetchone()
        if not r:
            return None
        out = _format_order_row(dict(r))

        # 时间轴流水
        t_rows = conn.execute(
            "SELECT * FROM order_timeline WHERE order_id = ? ORDER BY created_ts DESC",
            (oid,)
        ).fetchall()
        timeline = []
        for tr in t_rows:
            t_item = dict(tr)
            t_item["date_str"] = time.strftime("%Y-%m-%d %H:%M", time.gmtime(t_item["created_ts"] + 8 * 3600))
            to_st = ORDER_STAGES.get(t_item["to_stage"], {})
            t_item["to_stage_name"] = to_st.get("name", t_item["to_stage"])
            t_item["to_stage_icon"] = to_st.get("icon", "📌")
            timeline.append(t_item)
        out["timeline"] = timeline

        # 挂载订单明细项与汇总
        items = list_order_items(oid)
        out["items"] = items
        out["items_summary"] = get_order_items_summary(oid)
        return out


def save_order(data: dict, operator: str = "admin") -> tuple[bool, str, int | None]:
    """新增或更新订单信息。"""
    contact_id = data.get("contact_id")
    if not contact_id or not str(contact_id).isdigit():
        return False, "必须关联有效的成交客户", None
    contact_id = int(contact_id)

    title = (data.get("title") or "").strip()
    if not title:
        return False, "订单项目名称不能为空", None

    order_no = (data.get("order_no") or "").strip()
    ts = db.now_ts()

    oid = data.get("id")
    oid = int(oid) if oid and str(oid).isdigit() else None

    # 自动生成订单号
    if not order_no:
        date_prefix = time.strftime("%Y%m%d", time.gmtime(ts + 8 * 3600))
        with db.ro() as conn:
            cnt = conn.execute("SELECT COUNT(*) FROM orders WHERE order_no LIKE ?", (f"PI-{date_prefix}%",)).fetchone()[0]
        order_no = f"PI-{date_prefix}-{cnt + 1:03d}"

    stage = (data.get("stage") or "draft").strip().lower()
    if stage not in ORDER_STAGES:
        stage = "draft"

    trade_term = (data.get("trade_term") or "FOB").strip().upper()
    payment_term = (data.get("payment_term") or "30% T/T Deposit, 70% Balance before shipment").strip()
    currency = (data.get("currency") or "USD").strip().upper()

    try:
        total_amount = float(data.get("total_amount") or 0.0)
    except ValueError:
        total_amount = 0.0

    try:
        deposit_amount = float(data.get("deposit_amount") or 0.0)
    except ValueError:
        deposit_amount = 0.0

    try:
        balance_amount = float(data.get("balance_amount") or 0.0)
    except ValueError:
        balance_amount = 0.0

    try:
        settlement_fx_rate = float(data.get("settlement_fx_rate") or FX_RATES.get(currency, 7.20))
    except ValueError:
        settlement_fx_rate = 7.20

    try:
        factory_cost = float(data.get("factory_cost") or 0.0)
    except ValueError:
        factory_cost = 0.0

    try:
        shipping_cost = float(data.get("shipping_cost") or 0.0)
    except ValueError:
        shipping_cost = 0.0

    try:
        other_cost = float(data.get("other_cost") or 0.0)
    except ValueError:
        other_cost = 0.0

    fwd_quote_id = data.get("forwarder_quote_id")
    fwd_quote_id = int(fwd_quote_id) if fwd_quote_id and str(fwd_quote_id).isdigit() else None

    est_delivery_date = (data.get("est_delivery_date") or "").strip()
    actual_delivery_date = (data.get("actual_delivery_date") or "").strip()
    tracking_or_bl_no = (data.get("tracking_or_bl_no") or "").strip()
    destination_port = (data.get("destination_port") or "").strip()
    loading_port = (data.get("loading_port") or "Shenzhen, China").strip()
    carrier = (data.get("carrier") or "").strip()
    shipping_marks = (data.get("shipping_marks") or "N/M").strip()
    pi_date = (data.get("pi_date") or "").strip()
    ci_date = (data.get("ci_date") or "").strip()
    note = (data.get("note") or "").strip()

    with db.tx() as conn:
        if oid:
            # 校验 order_no 唯一性
            exist = conn.execute("SELECT id FROM orders WHERE order_no = ? AND id != ?", (order_no, oid)).fetchone()
            if exist:
                return False, f"订单编号【{order_no}】已存在，请换一个编号", None

            conn.execute(
                """UPDATE orders
                   SET order_no=?, title=?, contact_id=?, forwarder_quote_id=?, stage=?,
                       trade_term=?, payment_term=?, currency=?, total_amount=?,
                       deposit_amount=?, balance_amount=?, settlement_fx_rate=?,
                       factory_cost=?, shipping_cost=?, other_cost=?,
                       est_delivery_date=?, actual_delivery_date=?, tracking_or_bl_no=?,
                       destination_port=?, loading_port=?, carrier=?, shipping_marks=?,
                       pi_date=?, ci_date=?, note=?, updated_ts=?
                   WHERE id=?""",
                (order_no, title, contact_id, fwd_quote_id, stage,
                 trade_term, payment_term, currency, total_amount,
                 deposit_amount, balance_amount, settlement_fx_rate,
                 factory_cost, shipping_cost, other_cost,
                 est_delivery_date, actual_delivery_date, tracking_or_bl_no,
                 destination_port, loading_port, carrier, shipping_marks,
                 pi_date, ci_date, note, ts, oid)
            )
            return True, "订单资料已成功更新", oid
        else:
            exist = conn.execute("SELECT id FROM orders WHERE order_no = ?", (order_no,)).fetchone()
            if exist:
                return False, f"订单编号【{order_no}】已存在，请重新输入", None

            cur = conn.execute(
                """INSERT INTO orders
                   (order_no, title, contact_id, forwarder_quote_id, stage,
                    trade_term, payment_term, currency, total_amount,
                    deposit_amount, balance_amount, settlement_fx_rate,
                    factory_cost, shipping_cost, other_cost,
                    est_delivery_date, actual_delivery_date, tracking_or_bl_no,
                    destination_port, loading_port, carrier, shipping_marks,
                    pi_date, ci_date, note, created_ts, updated_ts)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (order_no, title, contact_id, fwd_quote_id, stage,
                 trade_term, payment_term, currency, total_amount,
                 deposit_amount, balance_amount, settlement_fx_rate,
                 factory_cost, shipping_cost, other_cost,
                 est_delivery_date, actual_delivery_date, tracking_or_bl_no,
                 destination_port, loading_port, carrier, shipping_marks,
                 pi_date, ci_date, note, ts, ts)
            )
            new_oid = cur.lastrowid

            # 自动插入立项初始流水
            conn.execute(
                """INSERT INTO order_timeline
                   (order_id, from_stage, to_stage, action_name, operator, detail, created_ts)
                   VALUES (?, NULL, ?, '订单立项起草', ?, ?, ?)""",
                (new_oid, stage, operator, f"形式发票号: {order_no} · 总金额: {currency} {total_amount:,.2f}", ts)
            )
            return True, "新订单已建立", new_oid


def advance_order_stage(oid: int, target_stage: str, note: str = "",
                        operator: str = "admin") -> tuple[bool, str]:
    """推进或变更订单阶段，并自动向时间轴写入节点记录。"""
    if target_stage not in ORDER_STAGES:
        return False, "无效的订单流转阶段"

    st_info = ORDER_STAGES[target_stage]
    ts = db.now_ts()

    with db.tx() as conn:
        curr_order = conn.execute("SELECT stage, contact_id, order_no FROM orders WHERE id=?", (oid,)).fetchone()
        if not curr_order:
            return False, "订单不存在"

        old_stage = curr_order["stage"]
        if old_stage == target_stage:
            return True, f"订单当前已在【{st_info['name']}】阶段"

        conn.execute(
            "UPDATE orders SET stage=?, updated_ts=? WHERE id=?",
            (target_stage, ts, oid)
        )

        action_name = f"推进至【{st_info['name']}】"
        detail_msg = (note or "").strip() or f"阶段状态由【{ORDER_STAGES.get(old_stage, {}).get('name', old_stage)}】变更为【{st_info['name']}】"
        conn.execute(
            """INSERT INTO order_timeline
               (order_id, from_stage, to_stage, action_name, operator, detail, created_ts)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (oid, old_stage, target_stage, action_name, operator, detail_msg, ts)
        )

        # 若订单履约完成，自动将该客户的跟进阶段升级为 'won' (已成交)，并提高意向分
        if target_stage == "completed":
            conn.execute(
                "UPDATE contacts SET stage='won', score=MAX(score, 90) WHERE id=?",
                (curr_order["contact_id"],)
            )

    return True, f"订单已成功推进至【{st_info['name']}】"


def update_order_finance(oid: int, data: dict, operator: str = "admin") -> tuple[bool, str]:
    """更新订单财务款项与物流运费。"""
    ts = db.now_ts()
    with db.tx() as conn:
        curr = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        if not curr:
            return False, "订单不存在"

        try:
            deposit = float(data.get("deposit_amount", curr["deposit_amount"]))
            balance = float(data.get("balance_amount", curr["balance_amount"]))
            f_cost = float(data.get("factory_cost", curr["factory_cost"]))
            s_cost = float(data.get("shipping_cost", curr["shipping_cost"]))
            o_cost = float(data.get("other_cost", curr["other_cost"]))
            fx = float(data.get("settlement_fx_rate", curr["settlement_fx_rate"]))
        except ValueError:
            return False, "金额或汇率数值格式不正确"

        tracking = (data.get("tracking_or_bl_no") or curr["tracking_or_bl_no"] or "").strip()

        conn.execute(
            """UPDATE orders
               SET deposit_amount=?, balance_amount=?, factory_cost=?,
                   shipping_cost=?, other_cost=?, settlement_fx_rate=?,
                   tracking_or_bl_no=?, updated_ts=?
               WHERE id=?""",
            (deposit, balance, f_cost, s_cost, o_cost, fx, tracking, ts, oid)
        )

        # 记录流水日志
        detail = (data.get("note") or "").strip() or f"更新款项：已收定金 {curr['currency']} {deposit:,.2f}，已收尾款 {curr['currency']} {balance:,.2f}"
        conn.execute(
            """INSERT INTO order_timeline
               (order_id, from_stage, to_stage, action_name, operator, detail, created_ts)
               VALUES (?, ?, ?, '更新收汇与成本流水', ?, ?, ?)""",
            (oid, curr["stage"], curr["stage"], operator, detail, ts)
        )
    return True, "财务款项记录已更新"


def delete_order(oid: int) -> bool:
    """删除订单（级联删除时间轴）。"""
    with db.tx() as conn:
        conn.execute("DELETE FROM orders WHERE id=?", (oid,))
    return True


# ==============================================================================
# Phase 2: 外贸基础产品库、订单明细品项与外贸单证生成器
# ==============================================================================

def amount_to_english_words(amount: float, currency: str = "USD") -> str:
    """将金额转换为标准外贸单证英文大写。
    例如: 25000.50, 'USD' -> 'SAY US DOLLARS TWENTY-FIVE THOUSAND AND CENTS FIFTY ONLY'
    """
    ones = ['', 'ONE', 'TWO', 'THREE', 'FOUR', 'FIVE', 'SIX', 'SEVEN', 'EIGHT', 'NINE',
            'TEN', 'ELEVEN', 'TWELVE', 'THIRTEEN', 'FOURTEEN', 'FIFTEEN', 'SIXTEEN',
            'SEVENTEEN', 'EIGHTEEN', 'NINETEEN']
    tens = ['', '', 'TWENTY', 'THIRTY', 'FORTY', 'FIFTY', 'SIXTY', 'SEVENTY', 'EIGHTY', 'NINETY']

    def helper(num: int) -> str:
        if num == 0:
            return ''
        elif num < 20:
            return ones[num]
        elif num < 100:
            return tens[num // 10] + (('-' + ones[num % 10]) if (num % 10 != 0) else '')
        elif num < 1000:
            return ones[num // 100] + ' HUNDRED' + ((' AND ' + helper(num % 100)) if (num % 100 != 0) else '')
        elif num < 1000000:
            return helper(num // 1000) + ' THOUSAND' + ((' ' + helper(num % 1000)) if (num % 1000 != 0) else '')
        elif num < 1000000000:
            return helper(num // 1000000) + ' MILLION' + ((' ' + helper(num % 1000000)) if (num % 1000000 != 0) else '')
        else:
            return helper(num // 1000000000) + ' BILLION' + ((' ' + helper(num % 1000000000)) if (num % 1000000000) != 0 else '')

    curr_names = {
        'USD': ('US DOLLARS', 'CENTS'),
        'EUR': ('EUROS', 'CENTS'),
        'GBP': ('POUNDS STERLING', 'PENCE'),
        'CNY': ('CHINESE YUAN', 'FEN'),
    }
    major, minor = curr_names.get(currency.upper(), (currency.upper(), 'CENTS'))
    amt = round(float(amount or 0.0), 2)
    int_part = int(amt)
    dec_part = int(round((amt - int_part) * 100))

    int_words = helper(int_part) if int_part > 0 else "ZERO"
    if dec_part > 0:
        dec_words = helper(dec_part)
        return f"SAY {major} {int_words} AND {minor} {dec_words} ONLY"
    return f"SAY {major} {int_words} ONLY"


DEFAULT_SELLER_PROFILE = {
    "company_name_en": "SHENZHEN GLOBAL TRADE CO., LIMITED",
    "company_name_cn": "深圳市全球行国际贸易有限公司",
    "company_address_en": "Room 808, Building B, High-Tech Industrial Park, Nanshan District, Shenzhen, Guangdong, China",
    "company_tel": "+86-755-88889999",
    "company_email": "export@globaltrade-cn.com",
    "company_website": "www.globaltrade-cn.com",
    "bank_beneficiary_name": "SHENZHEN GLOBAL TRADE CO., LIMITED",
    "bank_name": "BANK OF CHINA, SHENZHEN BRANCH",
    "bank_address": "No. 1088 Shennan Road, Shenzhen, Guangdong, China",
    "bank_account_no": "9556 8888 1234 5678",
    "bank_swift_code": "BKCHCNBJ400",
}


def get_seller_profile() -> dict:
    """获取外贸单证抬头与收款银行配置。"""
    with db.ro() as conn:
        r = conn.execute("SELECT value FROM settings WHERE key = 'seller_profile'").fetchone()
        if r and r["value"]:
            try:
                saved = json.loads(r["value"])
                res = dict(DEFAULT_SELLER_PROFILE)
                res.update(saved)
                return res
            except Exception:
                pass
    return dict(DEFAULT_SELLER_PROFILE)


def save_seller_profile(data: dict) -> tuple[bool, str]:
    """保存外贸单证抬头与收款银行配置。"""
    prof = get_seller_profile()
    for k in prof.keys():
        if k in data:
            prof[k] = str(data.get(k) or "").strip()
    with db.tx() as conn:
        conn.execute(
            """INSERT INTO settings (key, value, updated_ts)
               VALUES ('seller_profile', ?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_ts=excluded.updated_ts""",
            (json.dumps(prof, ensure_ascii=False), db.now_ts())
        )
    return True, "外贸单证抬头与银行账户已成功保存"


# --- 产品库管理 ---

def _format_product_row(p: dict) -> dict:
    p["price_usd_formatted"] = f"{p.get('price_usd', 0.0):,.2f}"
    p["cost_cny_formatted"] = f"{p.get('cost_cny', 0.0):,.2f}"
    p["cbm_formatted"] = f"{p.get('carton_cbm', 0.0):.4f}"
    p["gw_formatted"] = f"{p.get('carton_gw_kg', 0.0):.2f}"
    p["nw_formatted"] = f"{p.get('carton_nw_kg', 0.0):.2f}"
    p["status_text"] = "在售" if p.get("is_active") else "已归档"
    p["status_badge"] = (
        '<span class="badge on">在售</span>' if p.get("is_active")
        else '<span class="badge off">已归档</span>'
    )
    return p


def list_products(q: str = "", category: str = "", is_active: int = None) -> list[dict]:
    """查询产品目录列表。"""
    where = ["1=1"]
    args = []
    if is_active is not None:
        where.append("is_active = ?")
        args.append(is_active)
    if category and category != "all":
        where.append("category = ?")
        args.append(category)
    if q and q.strip():
        kw = f"%{q.strip()}%"
        where.append("(sku LIKE ? OR name_en LIKE ? OR name_cn LIKE ? OR hs_code LIKE ?)")
        args.extend([kw, kw, kw, kw])

    sql = f"""
        SELECT * FROM products
        WHERE {" AND ".join(where)}
        ORDER BY updated_ts DESC, id DESC
    """
    with db.ro() as conn:
        rows = conn.execute(sql, args).fetchall()
        return [_format_product_row(dict(r)) for r in rows]


def get_product(pid: int) -> dict | None:
    """获取单个产品详情。"""
    with db.ro() as conn:
        r = conn.execute("SELECT * FROM products WHERE id = ?", (pid,)).fetchone()
        return _format_product_row(dict(r)) if r else None


def save_product(data: dict) -> tuple[bool, str, int | None]:
    """新增或更新产品信息。"""
    sku = (data.get("sku") or "").strip()
    if not sku:
        return False, "产品型号/SKU 不能为空", None
    name_en = (data.get("name_en") or "").strip()
    if not name_en:
        return False, "英文品名不能为空", None

    name_cn = (data.get("name_cn") or "").strip()
    hs_code = (data.get("hs_code") or "").strip()
    category = (data.get("category") or "default").strip()
    specs = (data.get("specs") or "").strip()
    unit = (data.get("unit") or "PCS").strip().upper()
    image_url = (data.get("image_url") or "").strip()
    note = (data.get("note") or "").strip()

    try:
        price_usd = float(data.get("price_usd") or 0.0)
    except ValueError:
        price_usd = 0.0

    try:
        cost_cny = float(data.get("cost_cny") or 0.0)
    except ValueError:
        cost_cny = 0.0

    try:
        moq = int(data.get("moq") or 1)
    except ValueError:
        moq = 1

    try:
        carton_qty = int(data.get("carton_qty") or 1)
        if carton_qty <= 0:
            carton_qty = 1
    except ValueError:
        carton_qty = 1

    try:
        l = float(data.get("carton_length_cm") or 0.0)
        w = float(data.get("carton_width_cm") or 0.0)
        h = float(data.get("carton_height_cm") or 0.0)
    except ValueError:
        l, w, h = 0.0, 0.0, 0.0

    # 自动计算 CBM
    try:
        manual_cbm = float(data.get("carton_cbm") or 0.0)
    except ValueError:
        manual_cbm = 0.0

    if l > 0 and w > 0 and h > 0:
        carton_cbm = round(l * w * h / 1000000.0, 4)
    else:
        carton_cbm = manual_cbm

    try:
        carton_gw = float(data.get("carton_gw_kg") or 0.0)
        carton_nw = float(data.get("carton_nw_kg") or 0.0)
    except ValueError:
        carton_gw, carton_nw = 0.0, 0.0

    is_active = 1 if str(data.get("is_active", "1")) in ("1", "true", "True") else 0
    pid = data.get("id")
    pid = int(pid) if pid and str(pid).isdigit() else None
    ts = db.now_ts()

    with db.tx() as conn:
        if pid:
            exist = conn.execute("SELECT id FROM products WHERE sku = ? AND id != ?", (sku, pid)).fetchone()
            if exist:
                return False, f"产品 SKU【{sku}】已存在，请换一个型号编码", None
            conn.execute(
                """UPDATE products
                   SET sku=?, name_en=?, name_cn=?, hs_code=?, category=?,
                       specs=?, unit=?, price_usd=?, cost_cny=?, moq=?,
                       carton_qty=?, carton_length_cm=?, carton_width_cm=?,
                       carton_height_cm=?, carton_cbm=?, carton_gw_kg=?,
                       carton_nw_kg=?, image_url=?, note=?, is_active=?, updated_ts=?
                   WHERE id=?""",
                (sku, name_en, name_cn, hs_code, category,
                 specs, unit, price_usd, cost_cny, moq,
                 carton_qty, l, w, h, carton_cbm, carton_gw,
                 carton_nw, image_url, note, is_active, ts, pid)
            )
            return True, "产品资料已更新", pid
        else:
            exist = conn.execute("SELECT id FROM products WHERE sku = ?", (sku,)).fetchone()
            if exist:
                return False, f"产品 SKU【{sku}】已存在，请换一个型号编码", None
            cur = conn.execute(
                """INSERT INTO products
                   (sku, name_en, name_cn, hs_code, category,
                    specs, unit, price_usd, cost_cny, moq,
                    carton_qty, carton_length_cm, carton_width_cm,
                    carton_height_cm, carton_cbm, carton_gw_kg,
                    carton_nw_kg, image_url, note, is_active, created_ts, updated_ts)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (sku, name_en, name_cn, hs_code, category,
                 specs, unit, price_usd, cost_cny, moq,
                 carton_qty, l, w, h, carton_cbm, carton_gw,
                 carton_nw, image_url, note, is_active, ts, ts)
            )
            return True, "新产品已添加到目录库", cur.lastrowid


def delete_product(pid: int) -> bool:
    """删除产品。"""
    with db.tx() as conn:
        conn.execute("DELETE FROM products WHERE id=?", (pid,))
    return True


def export_products_csv() -> str:
    """导出全部产品目录为 CSV。"""
    prods = list_products()
    output = io.StringIO()
    output.write("\ufeff")  # UTF-8 BOM
    writer = csv.writer(output)
    writer.writerow([
        "ID", "SKU型号", "英文品名", "中文品名", "HS编码", "分类",
        "规格描述", "单位", "参考单价(USD)", "出厂成本(CNY)", "起订量(MOQ)",
        "装箱数(PCS/CTN)", "外箱长(cm)", "外箱宽(cm)", "外箱高(cm)",
        "单箱体积(CBM)", "单箱毛重(KG)", "单箱净重(KG)", "状态"
    ])
    for p in prods:
        writer.writerow([
            p["id"], p["sku"], p["name_en"], p["name_cn"], p["hs_code"], p["category"],
            p["specs"], p["unit"], p["price_usd"], p["cost_cny"], p["moq"],
            p["carton_qty"], p["carton_length_cm"], p["carton_width_cm"], p["carton_height_cm"],
            p["carton_cbm"], p["carton_gw_kg"], p["carton_nw_kg"], p["status_text"]
        ])
    return output.getvalue()


# --- 订单品项管理 ---

def _format_order_item(item: dict) -> dict:
    item["unit_price_formatted"] = f"{item.get('unit_price', 0.0):,.2f}"
    item["amount_formatted"] = f"{item.get('amount', 0.0):,.2f}"
    item["cbm_formatted"] = f"{item.get('cbm', 0.0):.4f}"
    item["gw_formatted"] = f"{item.get('gross_weight_kg', 0.0):.2f}"
    item["nw_formatted"] = f"{item.get('net_weight_kg', 0.0):.2f}"
    item["total_cost_cny"] = round(float(item.get("quantity", 0.0)) * float(item.get("unit_cost_cny", 0.0)), 2)
    return item


def list_order_items(order_id: int) -> list[dict]:
    """查询订单的所有品项明细。"""
    with db.ro() as conn:
        rows = conn.execute(
            """SELECT * FROM order_items
               WHERE order_id = ?
               ORDER BY item_no ASC, id ASC""",
            (order_id,)
        ).fetchall()
        return [_format_order_item(dict(r)) for r in rows]


def get_order_item(item_id: int) -> dict | None:
    """获取单条订单品项。"""
    with db.ro() as conn:
        r = conn.execute("SELECT * FROM order_items WHERE id = ?", (item_id,)).fetchone()
        return _format_order_item(dict(r)) if r else None


def get_order_items_summary(order_id: int) -> dict:
    """计算订单品项的汇总统计指标（件数、总价、箱数、总净毛重、总CBM）。"""
    items = list_order_items(order_id)
    total_qty = sum(float(it.get("quantity") or 0.0) for it in items)
    total_amount = sum(float(it.get("amount") or 0.0) for it in items)
    total_cartons = sum(int(it.get("cartons") or 0) for it in items)
    total_gw = sum(float(it.get("gross_weight_kg") or 0.0) for it in items)
    total_nw = sum(float(it.get("net_weight_kg") or 0.0) for it in items)
    total_cbm = sum(float(it.get("cbm") or 0.0) for it in items)
    total_cost_cny = sum(float(it.get("total_cost_cny") or 0.0) for it in items)

    return {
        "item_count": len(items),
        "total_qty": total_qty,
        "total_amount": round(total_amount, 2),
        "total_amount_formatted": f"{total_amount:,.2f}",
        "total_cartons": total_cartons,
        "total_gw": round(total_gw, 2),
        "total_gw_formatted": f"{total_gw:,.2f}",
        "total_nw": round(total_nw, 2),
        "total_nw_formatted": f"{total_nw:,.2f}",
        "total_cbm": round(total_cbm, 4),
        "total_cbm_formatted": f"{total_cbm:.4f}",
        "total_cost_cny": round(total_cost_cny, 2),
        "total_cost_cny_formatted": f"{total_cost_cny:,.2f}",
    }


def save_order_item(order_id: int, data: dict) -> tuple[bool, str, int | None]:
    """新增或更新订单品项。"""
    name_en = (data.get("name_en") or "").strip()
    if not name_en:
        return False, "品项英文名称不能为空", None

    sku = (data.get("sku") or "").strip()
    name_cn = (data.get("name_cn") or "").strip()
    hs_code = (data.get("hs_code") or "").strip()
    specs = (data.get("specs") or "").strip()
    unit = (data.get("unit") or "PCS").strip().upper()

    try:
        qty = float(data.get("quantity") or 1.0)
        if qty <= 0:
            qty = 1.0
    except ValueError:
        qty = 1.0

    try:
        unit_price = float(data.get("unit_price") or 0.0)
    except ValueError:
        unit_price = 0.0

    amount = round(qty * unit_price, 2)

    try:
        unit_cost_cny = float(data.get("unit_cost_cny") or 0.0)
    except ValueError:
        unit_cost_cny = 0.0

    # 包装与重量
    prod_id = data.get("product_id")
    prod_id = int(prod_id) if prod_id and str(prod_id).isdigit() else None

    try:
        cartons = int(data.get("cartons") or 0)
    except ValueError:
        cartons = 0

    try:
        gw = float(data.get("gross_weight_kg") or 0.0)
        nw = float(data.get("net_weight_kg") or 0.0)
        cbm = float(data.get("cbm") or 0.0)
    except ValueError:
        gw, nw, cbm = 0.0, 0.0, 0.0

    try:
        item_no = int(data.get("item_no") or 0)
    except ValueError:
        item_no = 0

    iid = data.get("id")
    iid = int(iid) if iid and str(iid).isdigit() else None
    ts = db.now_ts()

    with db.tx() as conn:
        if not item_no:
            cnt = conn.execute("SELECT COUNT(*) FROM order_items WHERE order_id = ?", (order_id,)).fetchone()[0]
            item_no = cnt + 1

        if iid:
            conn.execute(
                """UPDATE order_items
                   SET product_id=?, item_no=?, sku=?, name_en=?, name_cn=?,
                       hs_code=?, specs=?, unit=?, quantity=?, unit_price=?,
                       amount=?, unit_cost_cny=?, cartons=?, net_weight_kg=?,
                       gross_weight_kg=?, cbm=?
                   WHERE id=? AND order_id=?""",
                (prod_id, item_no, sku, name_en, name_cn,
                 hs_code, specs, unit, qty, unit_price,
                 amount, unit_cost_cny, cartons, nw, gw, cbm, iid, order_id)
            )
            return True, "品项已更新", iid
        else:
            cur = conn.execute(
                """INSERT INTO order_items
                   (order_id, product_id, item_no, sku, name_en, name_cn,
                    hs_code, specs, unit, quantity, unit_price,
                    amount, unit_cost_cny, cartons, net_weight_kg,
                    gross_weight_kg, cbm, created_ts)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (order_id, prod_id, item_no, sku, name_en, name_cn,
                 hs_code, specs, unit, qty, unit_price,
                 amount, unit_cost_cny, cartons, nw, gw, cbm, ts)
            )
            return True, "品项已添加至订单", cur.lastrowid


def delete_order_item(order_id: int, item_id: int) -> bool:
    """删除订单品项。"""
    with db.tx() as conn:
        conn.execute("DELETE FROM order_items WHERE id=? AND order_id=?", (item_id, order_id))
    return True


def sync_order_items_totals(order_id: int) -> tuple[bool, str]:
    """根据订单明细项重新反向汇总并更新订单的合同总额与工厂成本。"""
    summary = get_order_items_summary(order_id)
    if summary["item_count"] == 0:
        return False, "当前订单暂无品项明细，无法同步"

    ts = db.now_ts()
    with db.tx() as conn:
        conn.execute(
            """UPDATE orders
               SET total_amount=?, factory_cost=?, updated_ts=?
               WHERE id=?""",
            (summary["total_amount"], summary["total_cost_cny"], ts, order_id)
        )
    return True, f"已同步：订单总额更新为 {summary['total_amount']:,.2f}，出厂成本更新为 ¥{summary['total_cost_cny']:,.2f}"


# --- 外贸单证数据聚合 ---

def get_order_document_data(order_id: int, doc_type: str = "pi") -> dict | None:
    """聚合单证所需的全量数据（含订单、客户、卖方抬头、明细行、汇总统计与金额英文大写）。"""
    order = get_order(order_id)
    if not order:
        return None

    doc_type = doc_type.lower().strip()
    if doc_type not in ("pi", "ci", "pl"):
        doc_type = "pi"

    contact = None
    with db.ro() as conn:
        r = conn.execute("SELECT * FROM contacts WHERE id=?", (order["contact_id"],)).fetchone()
        if r:
            contact = dict(r)

    seller = get_seller_profile()
    items = list_order_items(order_id)
    summary = get_order_items_summary(order_id)

    # 英文大写金额
    amount_in_words = amount_to_english_words(order["total_amount"], order["currency"])

    titles = {
        "pi": "PROFORMA INVOICE",
        "ci": "COMMERCIAL INVOICE",
        "pl": "PACKING LIST",
    }
    subtitles = {
        "pi": "形式发票 · 签约确认与外汇定金预付款依据",
        "ci": "商业发票 · 出口报关与目的港清关核心凭据",
        "pl": "装箱单 · 货物装运出运与件重尺明细表",
    }

    # 单证编号与日期
    if doc_type == "pi":
        doc_no = order["order_no"]
        doc_date = order.get("pi_date") or time.strftime("%Y-%m-%d", time.gmtime(order["created_ts"] + 8 * 3600))
    elif doc_type == "ci":
        doc_no = f"INV-{order['order_no']}"
        doc_date = order.get("ci_date") or _today()
    else:
        doc_no = f"PL-{order['order_no']}"
        doc_date = order.get("ci_date") or _today()

    return {
        "order": order,
        "contact": contact or {},
        "seller": seller,
        "items": items,
        "summary": summary,
        "amount_in_words": amount_in_words,
        "doc_type": doc_type,
        "doc_title": titles[doc_type],
        "doc_subtitle": subtitles[doc_type],
        "doc_no": doc_no,
        "doc_date": doc_date,
        "today_str": _today(),
    }


# ==============================================================================
# Phase 3: 外贸社媒多渠道获客开发与智能触达系统
# ==============================================================================

SOCIAL_PLATFORMS = {
    "linkedin": {
        "id": "linkedin",
        "name": "LinkedIn",
        "icon": "💼",
        "badge_class": "badge-linkedin",
        "url_prefix": "https://www.linkedin.com/in/",
        "color": "#0A66C2",
    },
    "whatsapp": {
        "id": "whatsapp",
        "name": "WhatsApp",
        "icon": "💬",
        "badge_class": "badge-whatsapp",
        "url_prefix": "https://wa.me/",
        "color": "#25D366",
    },
    "facebook": {
        "id": "facebook",
        "name": "Facebook",
        "icon": "👥",
        "badge_class": "badge-facebook",
        "url_prefix": "https://www.facebook.com/",
        "color": "#1877F2",
    },
    "instagram": {
        "id": "instagram",
        "name": "Instagram",
        "icon": "📸",
        "badge_class": "badge-instagram",
        "url_prefix": "https://www.instagram.com/",
        "color": "#E4405F",
    },
    "tiktok": {
        "id": "tiktok",
        "name": "TikTok",
        "icon": "🎵",
        "badge_class": "badge-tiktok",
        "url_prefix": "https://www.tiktok.com/@",
        "color": "#111827",
    },
    "google": {
        "id": "google",
        "name": "Google/官网",
        "icon": "🌐",
        "badge_class": "badge-google",
        "url_prefix": "",
        "color": "#EA4335",
    },
    "exhibition": {
        "id": "exhibition",
        "name": "展会名片",
        "icon": "🏷️",
        "badge_class": "badge-exhibition",
        "url_prefix": "",
        "color": "#F59E0B",
    },
    "other": {
        "id": "other",
        "name": "其他渠道",
        "icon": "📌",
        "badge_class": "badge-secondary",
        "url_prefix": "",
        "color": "#64748B",
    },
}

LEAD_STAGES = {
    "discovered": {"id": "discovered", "name": "新发掘/待破冰", "color": "#64748b", "bg": "#f1f5f9", "step": 1},
    "connected": {"id": "connected", "name": "已建联/已加好友", "color": "#0284c7", "bg": "#e0f2fe", "step": 2},
    "chatted": {"id": "chatted", "name": "沟通中/已破冰", "color": "#059669", "bg": "#d1fae5", "step": 3},
    "catalog_sent": {"id": "catalog_sent", "name": "已推介图册/方案", "color": "#d97706", "bg": "#fef3c7", "step": 4},
    "converted": {"id": "converted", "name": "已转为正式客户", "color": "#16a34a", "bg": "#dcfce7", "step": 5},
    "unqualified": {"id": "unqualified", "name": "无效/暂无意向", "color": "#94a3b8", "bg": "#f8fafc", "step": 6},
}

TOUCH_TYPES = {
    "first_touch": {"id": "first_touch", "name": "首次破冰打招呼", "icon": "👋"},
    "catalog_pitch": {"id": "catalog_pitch", "name": "推介产品图册/参数", "icon": "📑"},
    "sample_followup": {"id": "sample_followup", "name": "索样进展跟进", "icon": "📦"},
    "quote_followup": {"id": "quote_followup", "name": "报价单追踪", "icon": "💰"},
    "holiday_greeting": {"id": "holiday_greeting", "name": "节日问候/关怀", "icon": "🎉"},
    "call": {"id": "call", "name": "电话/音视频沟通", "icon": "📞"},
    "note": {"id": "note", "name": "内部跟进备忘", "icon": "📝"},
}

TOUCH_FEEDBACKS = {
    "no_reply": "已发未回",
    "replied": "已积极探讨",
    "asked_catalog": "索要产品目录",
    "asked_quote": "索要报价单",
    "rejected": "暂无需求/委婉拒绝",
}


def format_whatsapp_number(raw_phone: str | None) -> str:
    """清洗国际电话号码为纯数字，便于生成 wa.me 免存直连链接。"""
    if not raw_phone:
        return ""
    return re.sub(r"[^\d]", "", str(raw_phone))


def generate_whatsapp_click_url(phone: str | None, text: str = "") -> str:
    """生成 WhatsApp 免存直连点击直跳链接。支持预填发送文案。"""
    digits = format_whatsapp_number(phone)
    if not digits:
        return ""
    base = f"https://wa.me/{digits}"
    if text:
        return f"{base}?text={urllib.parse.quote(text)}"
    return base


def generate_outreach_scripts(
    lead: dict,
    product: dict | None = None,
    lang: str = "en",
    custom_pitch: str = "",
) -> dict:
    """多场景多语言社媒破冰话术与开发信生成器。
    涵盖：
    1. linkedin_note: LinkedIn 300 字符好友邀请留言 (严格 <= 300 字符)
    2. linkedin_inmail: LinkedIn InMail / 站内私信开发信
    3. whatsapp_first: WhatsApp 首次破冰问候
    4. whatsapp_product: WhatsApp 产品/图册推介 (智能嵌入所选产品 SKU 与规格)
    5. exhibition_followup: 广交会/线下展会会后跟进
    6. reengage_greeting: 潜客激活与问候
    """
    raw_name = (lead.get("name") or "there").strip()
    first_name = raw_name.split()[0] if raw_name else "there"
    company = (lead.get("company") or "your company").strip()
    position = (lead.get("position") or "purchasing").strip()
    industry = (lead.get("industry_or_niche") or "the industry").strip()

    seller = get_seller_profile()
    seller_name = seller.get("company_name_en") or "our factory"

    # 产品参数注入
    if product:
        prod_name = product.get("name_en") or product.get("name_cn") or "our featured product"
        prod_sku = f" (Model: {product['sku']})" if product.get("sku") else ""
        moq_str = f"MOQ {product['moq']} {product.get('unit') or 'PCS'}" if product.get("moq") else "flexible MOQ"
        prod_specs = f" {product['specs']}" if product.get("specs") else ""
        prod_desc = f"{prod_name}{prod_sku}, {moq_str}.{prod_specs}"
    else:
        prod_name = "our top-grade product line"
        prod_sku = ""
        moq_str = "flexible MOQ"
        prod_desc = f"our featured solutions for {industry}."

    lang = (lang or "en").lower().strip()

    if lang == "es":
        # 西班牙语
        scripts = {
            "linkedin_note": (
                f"Hola {first_name}, vi tu perfil como {position} en {company}. "
                f"Como fabricante directo en {industry}, me encantaría conectar y explorar "
                f"sinergias comerciales. Saludos cordiales, {seller_name}"
            ),
            "linkedin_inmail": (
                f"Asunto: Oportunidad de Colaboración Directa de Fábrica para {company}\n\n"
                f"Estimado/a {first_name},\n\n"
                f"Espero que este mensaje te encuentre muy bien. Me pongo en contacto tras conocer "
                f"el excelente trabajo que realiza {company} en el sector de {industry}.\n\n"
                f"Somos fabricantes certificados en China ({seller_name}), especializados en "
                f"{prod_name}{prod_sku}. Ofrecemos precios directos de fábrica, soporte OEM/ODM y "
                f"estándares de calidad internacional.\n\n"
                f"¿Estarías disponible para una breve llamada o te gustaría que te envíe nuestro catálogo 2026?\n\n"
                f"Saludos cordiales,\n{seller_name}"
            ),
            "whatsapp_first": (
                f"¡Hola {first_name}! Saludos de {seller_name}. Espero que tengas un excelente día. "
                f"Nos especializamos en soluciones para {industry}. "
                f"¿Sería oportuno compartirte nuestro catálogo digital 2026 para tu referencia? Muchas gracias."
            ),
            "whatsapp_product": (
                f"Hola {first_name}, para {company} te recomiendo especialmente nuestro producto "
                f"estrella: {prod_desc} Ofrecemos precios directos de fábrica y {moq_str}. "
                f"¿Deseas que te envíe la ficha técnica y cotización FOB?"
            ),
            "exhibition_followup": (
                f"Hola {first_name}, ¡un gran gusto haber conversado en la feria comercial! "
                f"Dando seguimiento a nuestra charla sobre {industry}, te adjunto la información prometida de {seller_name}. "
                f"Quedo atento a tus comentarios."
            ),
            "reengage_greeting": (
                f"¡Hola {first_name}! Saludos cordiales desde {seller_name}. "
                f"Quería saber cómo van los proyectos en {company} este trimestre. "
                f"Tenemos nuevos lanzamientos en {industry}, quedo a tu disposición."
            ),
        }
    elif lang == "de":
        # 德语
        scripts = {
            "linkedin_note": (
                f"Guten Tag {first_name}, ich habe Ihr Profil als {position} bei {company} gesehen. "
                f"Als direkter Hersteller im Bereich {industry} würde ich mich freuen, mich mit Ihnen "
                f"zu vernetzen. Beste Grüße, {seller_name}"
            ),
            "linkedin_inmail": (
                f"Betreff: Direkte Hersteller-Partnerschaft für {company}\n\n"
                f"Sehr geehrte(r) Herr/Frau {raw_name},\n\n"
                f"wir schätzen die Marktpräsenz von {company} im Bereich {industry} sehr. "
                f"Als ISO-zertifizierter Hersteller ({seller_name}) bieten wir hochwertige Produkte "
                f"wie {prod_name}{prod_sku} zu wettbewerbsfähigen Direktpreisen.\n\n"
                f"Dürfte ich Ihnen unseren aktuellen Produktkatalog 2026 unverbindlich zusenden?\n\n"
                f"Mit freundlichen Grüßen,\n{seller_name}"
            ),
            "whatsapp_first": (
                f"Hallo {first_name}, beste Grüße von {seller_name}. "
                f"Wir sind Hersteller für {industry}. Dürfte ich Ihnen unseren Produktkatalog 2026 "
                f"kurz zur Ansicht weiterleiten? Vielen Dank und beste Grüße!"
            ),
            "whatsapp_product": (
                f"Hallo {first_name}, zu {industry} möchte ich Ihnen unser Spitzenmodell vorstellen: "
                f"{prod_desc} Wir bieten verlässliche Qualität und direkte Werkskonditionen. "
                f"Darf ich Ihnen das Datenblatt zusenden?"
            ),
            "exhibition_followup": (
                f"Hallo {first_name}, vielen Dank für das freundliche Gespräch auf der Messe! "
                f"Wie vereinbart sende ich Ihnen die Unterlagen von {seller_name}. "
                f"Ich freue mich auf Ihr Feedback."
            ),
            "reengage_greeting": (
                f"Guten Tag {first_name}, viele Grüße von {seller_name}. "
                f"Ich wollte mich kurz erkundigen, wie der aktuelle Beschaffungsbedarf bei {company} aussieht. "
                f"Beste Grüße!"
            ),
        }
    elif lang == "fr":
        # 法语
        scripts = {
            "linkedin_note": (
                f"Bonjour {first_name}, j'ai remarqué votre rôle de {position} chez {company}. "
                f"Fabricant direct dans le secteur {industry}, je serais ravi d'échanger avec vous "
                f"et de partager nos perspectives. Cordialement, {seller_name}"
            ),
            "linkedin_inmail": (
                f"Objet: Opportunité de Partenariat Industriel & Sourcing pour {company}\n\n"
                f"Bonjour {first_name},\n\n"
                f"Nous suivons avec intérêt les développements de {company} dans le secteur {industry}.\n"
                f"En tant que fabricant direct ({seller_name}), nous fournissons {prod_name}{prod_sku} "
                f"avec un strict contrôle qualité et des tarifs d'usine très compétitifs.\n\n"
                f"Seriez-vous ouvert(e) à recevoir notre catalogue 2026 par e-mail ?\n\n"
                f"Bien cordialement,\n{seller_name}"
            ),
            "whatsapp_first": (
                f"Bonjour {first_name}, salutations de {seller_name}. "
                f"Nous sommes fabricant spécialisé dans {industry}. "
                f"Puis-je vous faire parvenir notre catalogue 2026 pour consultation ? Merci !"
            ),
            "whatsapp_product": (
                f"Bonjour {first_name}, concernant {industry}, notre produit phare {prod_desc} "
                f"rencontre un franc succès. Souhaitez-vous recevoir la fiche technique et nos tarifs FOB ?"
            ),
            "exhibition_followup": (
                f"Bonjour {first_name}, ravi de vous avoir rencontré lors du salon professionnel ! "
                f"Suite à notre discussion, voici les informations de {seller_name}. À très bientôt !"
            ),
            "reengage_greeting": (
                f"Bonjour {first_name}, j'espère que vous allez bien. Je prenais des nouvelles de vos "
                f"projets chez {company}. N'hésitez pas si vous avez des besoins en {industry}."
            ),
        }
    elif lang == "pt":
        # 葡萄牙语
        scripts = {
            "linkedin_note": (
                f"Olá {first_name}, vi seu perfil como {position} na {company}. "
                f"Como fabricantes diretos no setor de {industry}, gostaríamos de nos conectar e "
                f"compartilhar soluções. Atenciosamente, {seller_name}"
            ),
            "linkedin_inmail": (
                f"Assunto: Oportunidade de Parceria Direta de Fábrica para {company}\n\n"
                f"Olá {first_name},\n\n"
                f"Acompanhamos a destacada atuação da {company} no mercado de {industry}.\n"
                f"A {seller_name} é fabricante especializada em {prod_name}{prod_sku}. "
                f"Oferecemos preços diretos de fábrica e suporte técnico completo.\n\n"
                f"Podemos enviar nosso catálogo completo para sua análise?\n\n"
                f"Atenciosamente,\n{seller_name}"
            ),
            "whatsapp_first": (
                f"Olá {first_name}! Tudo bem? Saudações da {seller_name}. "
                f"Somos fabricantes especializados no segmento de {industry}. "
                f"Posso te enviar nosso catálogo 2026 para referência? Obrigado!"
            ),
            "whatsapp_product": (
                f"Olá {first_name}, para a {company} recomendamos nosso item principal: {prod_desc} "
                f"Condições direto da fábrica e suporte OEM. Posso te enviar a cotação?"
            ),
            "exhibition_followup": (
                f"Olá {first_name}, foi um prazer conhecê-lo na feira! "
                f"Conforme combinamos, envio os materiais da {seller_name}. Um abraço!"
            ),
            "reengage_greeting": (
                f"Olá {first_name}, tudo bem? Passando para saber como estão os projetos na {company}. "
                f"Estamos à disposição para qualquer demanda em {industry}!"
            ),
        }
    elif lang == "ar":
        # 阿拉伯语
        scripts = {
            "linkedin_note": (
                f"مرحباً {first_name}، يسعدني التواصل معكم كـ {position} في {company}. "
                f"نحن مصنع متخصص في {industry} ونتطلع لبناء شراكة تجارية مثمرة معكم. "
                f"مع أطيب التحيات، {seller_name}"
            ),
            "linkedin_inmail": (
                f"الموضوع: فرصة توريد مباشر من المصنع لشركة {company}\n\n"
                f"عزيزي {first_name}،\n\n"
                f"نقدر نشاطكم المتميز في مجال {industry}. بصفتنا مصنعاً معتمداً ({seller_name})، "
                f"يسرنا تزويدكم بمنتجاتنا عالية الجودة {prod_name}{prod_sku} بأسعار المصنع المباشرة.\n\n"
                f"هل يناسبكم إرسال كتالوج 2026 لمراجعته؟\n\n"
                f"مع أطيب التحيات،\n{seller_name}"
            ),
            "whatsapp_first": (
                f"مرحباً {first_name}! تحياتنا من {seller_name}. "
                f"نحن مصنع متخصص في {industry}. هل يمكنني إرسال الكتالوج الجديد لعام 2026؟ شكراً جزيلاً."
            ),
            "whatsapp_product": (
                f"مرحباً {first_name}، نود أن نقترح لشركة {company} منتجنا المميز: {prod_desc} "
                f"أسعار منافسة وجودة عالية. هل ترغب في الاطلاع على المواصفات الفنية؟"
            ),
            "exhibition_followup": (
                f"مرحباً {first_name}، سعدنا جداً بلقائكم في المعرض التجاري! "
                f"متابعةً لنقاشنا، يسعدنا مشاركة التفاصيل من {seller_name}. مع التحية."
            ),
            "reengage_greeting": (
                f"مرحباً {first_name}، نأمل أن تكونوا بأفضل حال. "
                f"نتشرف بالاطمئنان على مشاريعكم في {company} ومستعدون لأي استفسار في {industry}."
            ),
        }
    elif lang == "ru":
        # 俄语
        scripts = {
            "linkedin_note": (
                f"Здравствуйте, {first_name}! Буду рад установить контакт с Вами как с {position} "
                f"в {company}. Мы являемся прямым производителем в сфере {industry}. "
                f"С уважением, {seller_name}"
            ),
            "linkedin_inmail": (
                f"Тема: Прямые поставки от завода-производителя для {company}\n\n"
                f"Уважаемый(ая) {first_name},\n\n"
                f"Мы высоко ценим деятельность {company} на рынке {industry}.\n"
                f"Компания {seller_name} — прямой производитель продукции {prod_name}{prod_sku}. "
                f"Предлагаем выгодные цены от завода, гибкие условия и надежный контроль качества.\n\n"
                f"Будем рады направить наш каталог 2026 для ознакомления.\n\n"
                f"С уважением,\n{seller_name}"
            ),
            "whatsapp_first": (
                f"Здравствуйте, {first_name}! Приветствуем от имени {seller_name}. "
                f"Мы производим продукцию для сферы {industry}. "
                f"Можем ли мы направить Вам наш каталог 2026 для ознакомления? Спасибо!"
            ),
            "whatsapp_product": (
                f"Здравствуйте, {first_name}! Для {company} рекомендуем наш ключевой продукт: {prod_desc} "
                f"Прямые цены от завода и гарантия качества. Отправить спецификацию и расчет цен?"
            ),
            "exhibition_followup": (
                f"Здравствуйте, {first_name}! Были очень рады встрече на выставке. "
                f"В продолжение нашего диалога направляю материалы от {seller_name}. На связи!"
            ),
            "reengage_greeting": (
                f"Здравствуйте, {first_name}! Надеюсь, у Вас все отлично. "
                f"Будем рады узнать о текущих потребностях {company} в сфере {industry}."
            ),
        }
    else:
        # 默认英语 (English)
        scripts = {
            "linkedin_note": (
                f"Hi {first_name}, noticed your role as {position} at {company}. "
                f"As a direct manufacturer in {industry}, I would love to connect and explore "
                f"potential supply chain synergies. Best regards, {seller_name}"
            ),
            "linkedin_inmail": (
                f"Subject: Strategic Sourcing & Factory-Direct Solutions for {company}\n\n"
                f"Dear {first_name},\n\n"
                f"Hope this message finds you well. We have been closely following {company}'s remarkable "
                f"footprint in the {industry} market.\n\n"
                f"As a certified manufacturing facility ({seller_name}), we specialize in high-performance "
                f"{prod_name}{prod_sku}. We support global importers with flexible OEM/ODM solutions, "
                f"rigorous QC inspection, and competitive factory-direct pricing.\n\n"
                f"Would it be feasible to share our 2026 e-catalog and price list for your review?\n\n"
                f"Best regards,\n{seller_name}"
            ),
            "whatsapp_first": (
                f"Hi {first_name}, this is from {seller_name}. Hope you are having a productive week! "
                f"We specialize in manufacturing solutions for {industry}. "
                f"Would it be convenient to share our 2026 e-catalog for your reference? Thank you!"
            ),
            "whatsapp_product": (
                f"Hi {first_name}, regarding {industry}, our featured item {prod_desc} "
                f"is performing exceptionally well with overseas partners. Reliable quality & direct FOB rate. "
                f"May I send you the spec sheet and quote?"
            ),
            "exhibition_followup": (
                f"Hi {first_name}, it was a real pleasure meeting you at the trade show! "
                f"Following up on our conversation regarding {industry}, here is the product overview from {seller_name}. "
                f"Looking forward to hearing your thoughts."
            ),
            "reengage_greeting": (
                f"Hi {first_name}, warm greetings from {seller_name}! "
                f"Checking in to see how projects are developing at {company} this quarter. "
                f"We have updated solutions for {industry}, happy to assist anytime."
            ),
        }

    # 如果有业务员自定义诉求，拼接在正文后
    if custom_pitch:
        for k in ["linkedin_inmail", "whatsapp_product"]:
            scripts[k] += f"\n\nPS: {custom_pitch}"

    # 严格保证 LinkedIn Connection Note 不超过 300 字符
    if len(scripts["linkedin_note"]) > 300:
        scripts["linkedin_note"] = scripts["linkedin_note"][:297] + "..."

    return scripts


def _format_social_lead_row(r: dict) -> dict:
    """格式化潜客行数据，补齐社媒平台徽章、阶段样式、WhatsApp 直跳链接、当地时钟等。"""
    lead = dict(r)
    platform_key = lead.get("source_platform") or "other"
    lead["platform_info"] = SOCIAL_PLATFORMS.get(platform_key, SOCIAL_PLATFORMS["other"])

    stage_key = lead.get("stage") or "discovered"
    lead["stage_info"] = LEAD_STAGES.get(stage_key, LEAD_STAGES["discovered"])

    lead["whatsapp_formatted"] = format_whatsapp_number(lead.get("whatsapp"))
    lead["wa_click_url"] = generate_whatsapp_click_url(lead.get("whatsapp"))

    # 星级打分展示
    rating = int(lead.get("rating") or 3)
    lead["rating_stars"] = "★" * rating + "☆" * (5 - rating)

    # 下次跟进状态判定
    followup_date = lead.get("next_followup_date")
    today = _today()
    if followup_date:
        if followup_date < today and stage_key not in ("converted", "unqualified"):
            lead["followup_status"] = "overdue"
            lead["followup_badge"] = "逾期未跟进"
            lead["followup_color"] = "danger"
        elif followup_date == today and stage_key not in ("converted", "unqualified"):
            lead["followup_status"] = "due_today"
            lead["followup_badge"] = "今日待跟进"
            lead["followup_color"] = "warning"
        else:
            lead["followup_status"] = "upcoming"
            lead["followup_badge"] = f"{followup_date} 跟进"
            lead["followup_color"] = "info"
    else:
        lead["followup_status"] = "none"
        lead["followup_badge"] = "未设定日程"
        lead["followup_color"] = "secondary"

    # 当地时区与时间计算
    country_str = lead.get("country") or ""
    time_info = geo_time.get_country_time_info(country_str) if country_str else None
    lead["local_time"] = time_info
    lead["time_badge_html"] = geo_time.render_time_badge(time_info) if time_info else ""
    lead["time_card_html"] = geo_time.render_time_card(time_info) if time_info else ""

    # 创建与更新时间
    if lead.get("created_ts"):
        lead["created_at_str"] = time.strftime("%Y-%m-%d", time.gmtime(lead["created_ts"] + 8 * 3600))
    else:
        lead["created_at_str"] = ""

    return lead


def list_social_leads(
    platform: str | None = None,
    stage: str | None = None,
    followup_status: str | None = None,
    search: str | None = None,
    rating: int | None = None,
    limit: int = 200,
) -> list[dict]:
    """查询社媒潜客线索列表。"""
    sql = """
        SELECT l.*, c.name as contact_name, c.email as contact_email
        FROM social_leads l
        LEFT JOIN contacts c ON l.contact_id = c.id
        WHERE 1=1
    """
    args = []
    if platform:
        sql += " AND l.source_platform = ?"
        args.append(platform)
    if stage:
        sql += " AND l.stage = ?"
        args.append(stage)
    if rating:
        sql += " AND l.rating >= ?"
        args.append(rating)
    if search:
        kw = f"%{search.strip()}%"
        sql += " AND (l.name LIKE ? OR l.company LIKE ? OR l.country LIKE ? OR l.whatsapp LIKE ? OR l.notes LIKE ?)"
        args.extend([kw, kw, kw, kw, kw])

    today = _today()
    if followup_status == "due_today":
        sql += " AND l.next_followup_date = ? AND l.stage NOT IN ('converted', 'unqualified')"
        args.append(today)
    elif followup_status == "overdue":
        sql += " AND l.next_followup_date < ? AND l.stage NOT IN ('converted', 'unqualified')"
        args.append(today)
    elif followup_status == "upcoming":
        sql += " AND l.next_followup_date > ? AND l.stage NOT IN ('converted', 'unqualified')"
        args.append(today)

    sql += " ORDER BY l.id DESC LIMIT ?"
    args.append(limit)

    with db.ro() as conn:
        rows = [dict(r) for r in conn.execute(sql, args).fetchall()]

    return [_format_social_lead_row(r) for r in rows]


def get_social_lead(lead_id: int) -> dict | None:
    """获取单个社媒潜客详情，并挂载触达记录。"""
    with db.ro() as conn:
        r = conn.execute(
            """SELECT l.*, c.name as contact_name, c.email as contact_email
               FROM social_leads l
               LEFT JOIN contacts c ON l.contact_id = c.id
               WHERE l.id = ?""",
            (lead_id,)
        ).fetchone()
        if not r:
            return None
        lead = _format_social_lead_row(dict(r))

        # 挂载该线索的触达记录
        tp_rows = conn.execute(
            "SELECT * FROM social_touchpoints WHERE lead_id = ? ORDER BY id DESC",
            (lead_id,)
        ).fetchall()
        tps = []
        for tp in tp_rows:
            item = dict(tp)
            channel_info = SOCIAL_PLATFORMS.get(item.get("channel"), SOCIAL_PLATFORMS["other"])
            touch_type_info = TOUCH_TYPES.get(item.get("touch_type"), {"name": item.get("touch_type"), "icon": "📌"})
            item["channel_info"] = channel_info
            item["touch_type_info"] = touch_type_info
            item["feedback_label"] = TOUCH_FEEDBACKS.get(item.get("feedback"), item.get("feedback") or "记录")
            item["created_at_str"] = time.strftime("%Y-%m-%d %H:%M", time.gmtime(item["created_ts"] + 8 * 3600))
            tps.append(item)
        lead["touchpoints"] = tps
        return lead


def save_social_lead(data: dict, lead_id: int | None = None) -> tuple[bool, str, int | None]:
    """新建或更新社媒潜客线索。"""
    name = str(data.get("name") or "").strip()
    if not name:
        return False, "潜客联系人姓名不能为空", None

    company = str(data.get("company") or "").strip()
    country = str(data.get("country") or "").strip()
    position = str(data.get("position") or "").strip()
    source_platform = str(data.get("source_platform") or "linkedin").strip()
    if source_platform not in SOCIAL_PLATFORMS:
        source_platform = "other"

    raw_whatsapp = str(data.get("whatsapp") or "").strip()
    whatsapp = format_whatsapp_number(raw_whatsapp)
    linkedin_url = str(data.get("linkedin_url") or "").strip()
    social_handle = str(data.get("social_handle") or "").strip()
    email = str(data.get("email") or "").strip()
    website = str(data.get("website") or "").strip()
    industry_or_niche = str(data.get("industry_or_niche") or "").strip()
    stage = str(data.get("stage") or "discovered").strip()
    if stage not in LEAD_STAGES:
        stage = "discovered"

    try:
        rating = int(data.get("rating") or 3)
    except Exception:
        rating = 3

    next_followup_date = str(data.get("next_followup_date") or "").strip()
    notes = str(data.get("notes") or "").strip()

    now = db.now_ts()

    if lead_id:
        with db.tx() as conn:
            conn.execute(
                """UPDATE social_leads SET
                   name=?, company=?, country=?, position=?, source_platform=?,
                   whatsapp=?, linkedin_url=?, social_handle=?, email=?, website=?,
                   industry_or_niche=?, stage=?, rating=?, next_followup_date=?, notes=?,
                   updated_ts=?
                   WHERE id=?""",
                (name, company, country, position, source_platform,
                 whatsapp, linkedin_url, social_handle, email, website,
                 industry_or_niche, stage, rating, next_followup_date, notes,
                 now, lead_id)
            )
        return True, "社媒潜客信息已更新", lead_id
    else:
        with db.tx() as conn:
            cur = conn.execute(
                """INSERT INTO social_leads (
                   name, company, country, position, source_platform,
                   whatsapp, linkedin_url, social_handle, email, website,
                   industry_or_niche, stage, rating, next_followup_date, notes,
                   created_ts, updated_ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (name, company, country, position, source_platform,
                 whatsapp, linkedin_url, social_handle, email, website,
                 industry_or_niche, stage, rating, next_followup_date, notes,
                 now, now)
            )
            new_id = cur.lastrowid
            # 自动记录一条线索新建的流水
            conn.execute(
                """INSERT INTO social_touchpoints (
                   lead_id, channel, touch_type, content, feedback, operator, created_ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (new_id, source_platform, "first_touch",
                 f"通过 [{SOCIAL_PLATFORMS[source_platform]['name']}] 发掘新线索",
                 "replied" if stage != "discovered" else "no_reply",
                 "admin", now)
            )
        return True, "社媒潜客录入成功", new_id


def advance_social_lead_stage(lead_id: int, new_stage: str, note: str = "", operator: str = "admin") -> tuple[bool, str]:
    """推进潜客开发流转阶段并追加触达记录。"""
    if new_stage not in LEAD_STAGES:
        return False, "无效的阶段名称"
    lead = get_social_lead(lead_id)
    if not lead:
        return False, "线索不存在"

    old_stage_name = lead["stage_info"]["name"]
    new_stage_name = LEAD_STAGES[new_stage]["name"]

    now = db.now_ts()
    with db.tx() as conn:
        conn.execute(
            "UPDATE social_leads SET stage = ?, updated_ts = ? WHERE id = ?",
            (new_stage, now, lead_id)
        )
        # 记录流水
        content = f"阶段流转：从【{old_stage_name}】推进至【{new_stage_name}】"
        if note:
            content += f" (备注: {note})"
        conn.execute(
            """INSERT INTO social_touchpoints (
               lead_id, channel, touch_type, content, operator, created_ts
            ) VALUES (?, ?, ?, ?, ?, ?)""",
            (lead_id, lead.get("source_platform") or "other", "note", content, operator, now)
        )
    return True, f"线索已成功推进至【{new_stage_name}】"


def delete_social_lead(lead_id: int) -> tuple[bool, str]:
    """安全删除社媒潜客及其所有关联触达记录。"""
    with db.tx() as conn:
        conn.execute("DELETE FROM social_touchpoints WHERE lead_id = ?", (lead_id,))
        conn.execute("DELETE FROM social_leads WHERE id = ?", (lead_id,))
    return True, "社媒潜客已成功删除"


def get_social_leads_funnel_summary() -> dict:
    """获取社媒获客看板核心汇总指标与 6 阶段泳道分布。"""
    today = _today()
    with db.ro() as conn:
        def count_sql(sql, args=()):
            r = conn.execute(sql, args).fetchone()
            return r[0] if r and r[0] else 0

        total_leads = count_sql("SELECT COUNT(*) FROM social_leads")
        due_today = count_sql(
            "SELECT COUNT(*) FROM social_leads WHERE next_followup_date = ? AND stage NOT IN ('converted', 'unqualified')",
            (today,)
        )
        overdue = count_sql(
            "SELECT COUNT(*) FROM social_leads WHERE next_followup_date < ? AND stage NOT IN ('converted', 'unqualified')",
            (today,)
        )
        converted = count_sql("SELECT COUNT(*) FROM social_leads WHERE stage = 'converted'")

        # 漏斗转化率
        conversion_rate = round((converted / total_leads * 100), 1) if total_leads > 0 else 0.0

        # 各阶段分布统计
        stage_counts = {}
        for s_key in LEAD_STAGES:
            cnt = count_sql("SELECT COUNT(*) FROM social_leads WHERE stage = ?", (s_key,))
            stage_counts[s_key] = cnt

        # 各平台分布统计
        platform_counts = {}
        for p_key in SOCIAL_PLATFORMS:
            cnt = count_sql("SELECT COUNT(*) FROM social_leads WHERE source_platform = ?", (p_key,))
            platform_counts[p_key] = cnt

    return {
        "total_leads": total_leads,
        "due_today": due_today,
        "overdue": overdue,
        "converted": converted,
        "conversion_rate": conversion_rate,
        "stage_counts": stage_counts,
        "platform_counts": platform_counts,
        "stages": LEAD_STAGES,
        "platforms": SOCIAL_PLATFORMS,
    }


def list_social_touchpoints(lead_id: int) -> list[dict]:
    """查询指定潜客的所有沟通触达记录。"""
    with db.ro() as conn:
        rows = conn.execute(
            "SELECT * FROM social_touchpoints WHERE lead_id = ? ORDER BY id DESC",
            (lead_id,)
        ).fetchall()
        res = []
        for r in rows:
            item = dict(r)
            item["channel_info"] = SOCIAL_PLATFORMS.get(item.get("channel"), SOCIAL_PLATFORMS["other"])
            item["touch_type_info"] = TOUCH_TYPES.get(item.get("touch_type"), {"name": item.get("touch_type"), "icon": "📌"})
            item["feedback_label"] = TOUCH_FEEDBACKS.get(item.get("feedback"), item.get("feedback") or "记录")
            item["created_at_str"] = time.strftime("%Y-%m-%d %H:%M", time.gmtime(item["created_ts"] + 8 * 3600))
            res.append(item)
        return res


def save_social_touchpoint(
    lead_id: int,
    channel: str,
    touch_type: str,
    content: str,
    feedback: str | None = None,
    next_action: str | None = None,
    operator: str = "admin",
) -> tuple[bool, str, int | None]:
    """录入一次新的社媒触达记录。"""
    content = str(content or "").strip()
    if not content:
        return False, "沟通内容不能为空", None

    if channel not in SOCIAL_PLATFORMS:
        channel = "whatsapp"
    if touch_type not in TOUCH_TYPES:
        touch_type = "first_touch"

    now = db.now_ts()
    with db.tx() as conn:
        cur = conn.execute(
            """INSERT INTO social_touchpoints (
               lead_id, channel, touch_type, content, feedback, next_action, operator, created_ts
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (lead_id, channel, touch_type, content, feedback or "", next_action or "", operator, now)
        )
        conn.execute("UPDATE social_leads SET updated_ts = ? WHERE id = ?", (now, lead_id))
        return True, "触达记录保存成功", cur.lastrowid


def delete_social_touchpoint(tpid: int) -> tuple[bool, str]:
    """删除单条触达流水。"""
    with db.tx() as conn:
        conn.execute("DELETE FROM social_touchpoints WHERE id = ?", (tpid,))
    return True, "触达记录已删除"


def convert_lead_to_contact(lead_id: int, email: str, operator: str = "admin") -> tuple[bool, str, int | None]:
    """将社媒潜客一键转化为 CRM 正式客户，打通订单管理与单证生成。"""
    email = str(email or "").strip().lower()
    if not email or "@" not in email:
        return False, "请填写有效的客户电子邮箱以便转入客户档案库", None

    lead = get_social_lead(lead_id)
    if not lead:
        return False, "社媒线索不存在", None

    now = db.now_ts()
    with db.tx() as conn:
        # 检查是否已存在同邮箱客户
        existing = conn.execute("SELECT id, name FROM contacts WHERE email = ?", (email,)).fetchone()
        if existing:
            contact_id = existing["id"]
            # 关联现有客户并补充社媒信息
            conn.execute(
                """UPDATE contacts SET
                   whatsapp = COALESCE(NULLIF(whatsapp, ''), ?),
                   linkedin_url = COALESCE(NULLIF(linkedin_url, ''), ?),
                   social_lead_id = ?
                   WHERE id = ?""",
                (lead.get("whatsapp"), lead.get("linkedin_url"), lead_id, contact_id)
            )
            msg = f"已成功关联合并至现有客户「{existing['name'] or email}」"
        else:
            # 创建新客户
            source_label = f"social_{lead.get('source_platform') or 'leads'}"
            notes_addon = f"来自社媒开发 ({lead['platform_info']['name']})。职位: {lead.get('position') or '未填'}。备忘: {lead.get('notes') or ''}"
            cur = conn.execute(
                """INSERT INTO contacts (
                   email, name, company, country, source, first_seen_ts,
                   last_seen_ts, score, stage, note, whatsapp, linkedin_url, social_lead_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (email, lead.get("name"), lead.get("company"), lead.get("country"),
                 source_label, now, now, 80, "engaging", notes_addon,
                 lead.get("whatsapp"), lead.get("linkedin_url"), lead_id)
            )
            contact_id = cur.lastrowid
            msg = "已成功转化为正式 CRM 客户"

        # 更新社媒线索状态为 converted 并绑定 contact_id
        conn.execute(
            """UPDATE social_leads SET
               stage = 'converted', contact_id = ?, email = ?, updated_ts = ?
               WHERE id = ?""",
            (contact_id, email, now, lead_id)
        )

        # 写入触达流水
        conn.execute(
            """INSERT INTO social_touchpoints (
               lead_id, channel, touch_type, content, feedback, operator, created_ts
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (lead_id, "other", "note", f"{msg} (邮箱: {email}, ID: {contact_id})", "replied", operator, now)
        )

    return True, msg, contact_id


def export_social_leads_csv(leads: list[dict]) -> str:
    """将社媒潜客列表导出为 UTF-8 BOM CSV 文件，Excel 打开不乱码。"""
    buf = io.StringIO()
    buf.write("\ufeff")
    writer = csv.writer(buf)
    writer.writerow([
        "序号", "联系人姓名", "公司名称", "国家/地区", "职位", "来源平台",
        "WhatsApp/电话", "LinkedIn主页", "社媒账号", "电子邮箱", "公司官网",
        "主营行业", "流转阶段", "意向星级", "下次跟进日期", "备忘记录", "创建时间"
    ])
    for idx, l in enumerate(leads, 1):
        writer.writerow([
            idx,
            l.get("name", ""),
            l.get("company", ""),
            l.get("country", ""),
            l.get("position", ""),
            l.get("platform_info", {}).get("name", l.get("source_platform", "")),
            l.get("whatsapp", ""),
            l.get("linkedin_url", ""),
            l.get("social_handle", ""),
            l.get("email", ""),
            l.get("website", ""),
            l.get("industry_or_niche", ""),
            l.get("stage_info", {}).get("name", l.get("stage", "")),
            f"{l.get('rating', 3)}星",
            l.get("next_followup_date", ""),
            l.get("notes", ""),
            l.get("created_at_str", ""),
        ])
    return buf.getvalue()


# ==============================================================================
# 外贸单证与凭证管理查询 (Vouchers)
# ==============================================================================

VOUCHER_TYPE_NAMES = {
    "commercial_invoice": "商业发票 (CI)",
    "proforma_invoice": "形式发票 (PI)",
    "packing_list": "装箱单 (PL)",
    "bill_of_lading": "海运提单 (B/L)",
    "customs_declaration": "报关单",
    "bank_slip": "付汇/结汇水单",
    "contract": "外贸合同",
    "other": "其他凭证",
}

VOUCHER_STATUS_NAMES = {
    "confirmed": "已确认",
    "pending": "待核对",
    "archived": "已归档",
}


def create_voucher(data: dict) -> int:
    """创建外贸单证/凭证记录。"""
    now = db.now_ts()
    with db.tx() as conn:
        cur = conn.execute(
            """INSERT INTO vouchers (
                voucher_no, voucher_type, title, trade_date, currency, amount,
                contact_id, order_id, shipper, consignee, product_desc,
                file_path, ocr_status, ocr_raw_text, status, notes,
                created_ts, updated_ts
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                data.get("voucher_no", "").strip(),
                data.get("voucher_type", "commercial_invoice").strip(),
                data.get("title", "").strip() or None,
                data.get("trade_date", "").strip() or None,
                data.get("currency", "USD").strip().upper(),
                float(data.get("amount", 0.0) or 0.0),
                data.get("contact_id") or None,
                data.get("order_id") or None,
                data.get("shipper", "").strip() or None,
                data.get("consignee", "").strip() or None,
                data.get("product_desc", "").strip() or None,
                data.get("file_path", "").strip() or None,
                data.get("ocr_status", "pending").strip(),
                data.get("ocr_raw_text", "") or None,
                data.get("status", "confirmed").strip(),
                data.get("notes", "").strip() or None,
                now, now
            )
        )
        vid = cur.lastrowid
    return vid


def update_voucher(vid: int, data: dict) -> bool:
    """更新外贸凭证信息。"""
    now = db.now_ts()
    with db.tx() as conn:
        res = conn.execute(
            """UPDATE vouchers SET
                voucher_no = COALESCE(?, voucher_no),
                voucher_type = COALESCE(?, voucher_type),
                title = COALESCE(?, title),
                trade_date = COALESCE(?, trade_date),
                currency = COALESCE(?, currency),
                amount = COALESCE(?, amount),
                contact_id = ?,
                order_id = ?,
                shipper = COALESCE(?, shipper),
                consignee = COALESCE(?, consignee),
                product_desc = COALESCE(?, product_desc),
                file_path = COALESCE(?, file_path),
                ocr_status = COALESCE(?, ocr_status),
                ocr_raw_text = COALESCE(?, ocr_raw_text),
                status = COALESCE(?, status),
                notes = COALESCE(?, notes),
                updated_ts = ?
            WHERE id = ?""",
            (
                data.get("voucher_no"),
                data.get("voucher_type"),
                data.get("title"),
                data.get("trade_date"),
                data.get("currency"),
                float(data["amount"]) if "amount" in data and data["amount"] is not None else None,
                data.get("contact_id") if "contact_id" in data else None,
                data.get("order_id") if "order_id" in data else None,
                data.get("shipper"),
                data.get("consignee"),
                data.get("product_desc"),
                data.get("file_path"),
                data.get("ocr_status"),
                data.get("ocr_raw_text"),
                data.get("status"),
                data.get("notes"),
                now, vid
            )
        )
        return res.rowcount > 0


def delete_voucher(vid: int) -> bool:
    """删除单张凭证。"""
    with db.tx() as conn:
        res = conn.execute("DELETE FROM vouchers WHERE id = ?", (vid,))
        return res.rowcount > 0


def batch_delete_vouchers(vids: list[int]) -> int:
    """批量删除凭证。"""
    if not vids:
        return 0
    placeholders = ",".join("?" for _ in vids)
    with db.tx() as conn:
        res = conn.execute(f"DELETE FROM vouchers WHERE id IN ({placeholders})", vids)
        return res.rowcount


def batch_update_voucher_status(vids: list[int], status: str) -> int:
    """批量更新凭证状态（confirmed / pending / archived）。"""
    if not vids:
        return 0
    now = db.now_ts()
    placeholders = ",".join("?" for _ in vids)
    params = [status, now] + list(vids)
    with db.tx() as conn:
        res = conn.execute(
            f"UPDATE vouchers SET status = ?, updated_ts = ? WHERE id IN ({placeholders})",
            params
        )
        return res.rowcount


def get_voucher(vid: int) -> Optional[dict]:
    """查询单张凭证详情及关联客户/订单。"""
    with db.ro() as conn:
        row = conn.execute(
            """SELECT v.*, c.name AS contact_name, c.company AS contact_company,
                      o.order_no AS order_no_rel, o.title AS order_title
               FROM vouchers v
               LEFT JOIN contacts c ON c.id = v.contact_id
               LEFT JOIN orders o ON o.id = v.order_id
               WHERE v.id = ?""",
            (vid,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["voucher_type_cn"] = VOUCHER_TYPE_NAMES.get(d["voucher_type"], d["voucher_type"])
        d["status_cn"] = VOUCHER_STATUS_NAMES.get(d["status"], d["status"])
        return d


def list_vouchers(
    q: str = "",
    voucher_type: str = "",
    status: str = "",
    contact_id: Optional[int] = None,
    order_id: Optional[int] = None,
    page: int = 1,
    page_size: int = 20
) -> dict:
    """多维分页检索外贸凭证列表。"""
    where_clauses = ["1=1"]
    params: list = []

    if q:
        like_q = f"%{q.strip()}%"
        where_clauses.append(
            "(v.voucher_no LIKE ? OR v.title LIKE ? OR v.shipper LIKE ? OR v.consignee LIKE ? OR v.product_desc LIKE ? OR c.name LIKE ? OR c.company LIKE ?)"
        )
        params.extend([like_q, like_q, like_q, like_q, like_q, like_q, like_q])

    if voucher_type:
        where_clauses.append("v.voucher_type = ?")
        params.append(voucher_type.strip())

    if status:
        where_clauses.append("v.status = ?")
        params.append(status.strip())

    if contact_id:
        where_clauses.append("v.contact_id = ?")
        params.append(contact_id)

    if order_id:
        where_clauses.append("v.order_id = ?")
        params.append(order_id)

    where_sql = " AND ".join(where_clauses)
    offset = (page - 1) * page_size

    with db.ro() as conn:
        total = conn.execute(
            f"""SELECT COUNT(*) FROM vouchers v
                LEFT JOIN contacts c ON c.id = v.contact_id
                WHERE {where_sql}""",
            params
        ).fetchone()[0]

        rows = conn.execute(
            f"""SELECT v.*, c.name AS contact_name, c.company AS contact_company,
                       o.order_no AS order_no_rel
                FROM vouchers v
                LEFT JOIN contacts c ON c.id = v.contact_id
                LEFT JOIN orders o ON o.id = v.order_id
                WHERE {where_sql}
                ORDER BY v.trade_date DESC, v.created_ts DESC
                LIMIT ? OFFSET ?""",
            params + [page_size, offset]
        ).fetchall()

    items = []
    for r in rows:
        d = dict(r)
        d["voucher_type_cn"] = VOUCHER_TYPE_NAMES.get(d["voucher_type"], d["voucher_type"])
        d["status_cn"] = VOUCHER_STATUS_NAMES.get(d["status"], d["status"])
        items.append(d)

    total_pages = (total + page_size - 1) // page_size if total > 0 else 1
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "has_prev": page > 1,
        "has_next": page < total_pages,
    }


def voucher_stats() -> dict:
    """获取凭证仪表盘关键统计指标。"""
    with db.ro() as conn:
        total = conn.execute("SELECT COUNT(*) FROM vouchers").fetchone()[0]
        confirmed = conn.execute("SELECT COUNT(*) FROM vouchers WHERE status='confirmed'").fetchone()[0]
        pending = conn.execute("SELECT COUNT(*) FROM vouchers WHERE status='pending'").fetchone()[0]
        archived = conn.execute("SELECT COUNT(*) FROM vouchers WHERE status='archived'").fetchone()[0]
        ocr_done = conn.execute("SELECT COUNT(*) FROM vouchers WHERE ocr_status='success'").fetchone()[0]

        # 按币种汇总总金额
        amounts = {}
        for r in conn.execute("SELECT currency, SUM(amount) FROM vouchers WHERE status!='archived' GROUP BY currency").fetchall():
            amounts[r[0]] = round(r[1] or 0.0, 2)

    return {
        "total": total,
        "confirmed": confirmed,
        "pending": pending,
        "archived": archived,
        "ocr_done": ocr_done,
        "amounts": amounts,
    }


def export_vouchers_csv(items: list[dict]) -> str:
    """导出凭证列表为 UTF-8 BOM CSV。"""
    buf = io.StringIO()
    buf.write("\ufeff")
    writer = csv.writer(buf)
    writer.writerow([
        "序号", "单证编号", "单证类型", "标题/简述", "业务日期", "币种", "金额",
        "关联客户", "关联订单", "发货人/卖方", "收货人/买方", "品名货品描述",
        "OCR识别状态", "单据状态", "备注"
    ])
    for idx, v in enumerate(items, 1):
        writer.writerow([
            idx,
            v.get("voucher_no", ""),
            v.get("voucher_type_cn", v.get("voucher_type", "")),
            v.get("title", ""),
            v.get("trade_date", ""),
            v.get("currency", "USD"),
            f"{v.get('amount', 0.0):.2f}",
            v.get("contact_name") or (f"ID:{v['contact_id']}" if v.get("contact_id") else ""),
            v.get("order_no_rel") or (f"ID:{v['order_id']}" if v.get("order_id") else ""),
            v.get("shipper", ""),
            v.get("consignee", ""),
            v.get("product_desc", ""),
            v.get("ocr_status", ""),
            v.get("status_cn", v.get("status", "")),
            v.get("notes", ""),
        ])
    return buf.getvalue()


# ============================================================
# 日志模块：每日工作检阅、Token测算、24H邮件时点分析
# ============================================================

def get_daily_work_stats(day_str: str = "") -> dict:
    """获取指定日期的每日工作进度与动作统计。
    
    默认当天 (基于 config.TZ_OFFSET_HOURS，如 YYYY-MM-DD)。
    统计指标：
    - 邮件外发数 (已发送/自动发送/人工审核)
    - 审批通过/起草的回复草稿数
    - 新增或跟进客户数
    - 录入或OCR识别单证数
    - 社媒触达与线索记录数
    - 审计操作总流水数与动作分布
    - 当日 AI 模型调用次数与 Token 消耗
    - 自动生成的下班工作复盘日报（Markdown / 纯文本，方便一键复制）
    """
    offset = config.TZ_OFFSET_HOURS * 3600
    target_day = (day_str or "").strip() or _today()

    with db.ro() as conn:
        def one(sql, args=()):
            r = conn.execute(sql, args).fetchone()
            return r[0] if r and r[0] is not None else 0

        # 1. 邮件外发数
        sent_emails = one(
            "SELECT COUNT(*) FROM messages WHERE direction='out' "
            "AND date(sent_ts+?,'unixepoch')=?", (offset, target_day)
        )
        auto_sent = one(
            "SELECT COUNT(*) FROM messages WHERE direction='out' AND status='auto_sent' "
            "AND date(sent_ts+?,'unixepoch')=?", (offset, target_day)
        )
        received_emails = one(
            "SELECT COUNT(*) FROM messages WHERE direction='in' "
            "AND date(sent_ts+?,'unixepoch')=?", (offset, target_day)
        )
        high_inquiries = one(
            "SELECT COUNT(*) FROM messages WHERE direction='in' AND score>=? "
            "AND date(sent_ts+?,'unixepoch')=?", (config.HIGH_INTENT, offset, target_day)
        )

        # 2. 草稿处理情况
        drafts_approved = one(
            "SELECT COUNT(*) FROM drafts WHERE status IN ('approved', 'sent') "
            "AND (date(reviewed_ts+?,'unixepoch')=? OR date(sent_ts+?,'unixepoch')=?)",
            (offset, target_day, offset, target_day)
        )
        drafts_pending = one("SELECT COUNT(*) FROM drafts WHERE status='pending'")

        # 3. 客户跟进与新增
        new_contacts = one(
            "SELECT COUNT(*) FROM contacts WHERE date(first_seen_ts+?,'unixepoch')=?",
            (offset, target_day)
        )
        followup_actions = one(
            "SELECT COUNT(*) FROM audit_log WHERE target_type='contact' "
            "AND date(ts+?,'unixepoch')=?", (offset, target_day)
        )

        # 4. 单证录入与 OCR 识别
        new_vouchers = one(
            "SELECT COUNT(*) FROM vouchers WHERE date(created_ts+?,'unixepoch')=?",
            (offset, target_day)
        )
        ocr_vouchers = one(
            "SELECT COUNT(*) FROM vouchers WHERE ocr_status='success' "
            "AND date(created_ts+?,'unixepoch')=?", (offset, target_day)
        )

        # 5. 社媒触达与线索
        social_outreach = one(
            "SELECT COUNT(*) FROM audit_log WHERE (target_type='social_lead' OR action LIKE '%social%') "
            "AND date(ts+?,'unixepoch')=?", (offset, target_day)
        )

        # 6. 操作流水总数及主要动作分布
        total_actions = one(
            "SELECT COUNT(*) FROM audit_log WHERE date(ts+?,'unixepoch')=?",
            (offset, target_day)
        )
        action_breakdown_rows = conn.execute(
            "SELECT action, COUNT(*) as cnt FROM audit_log "
            "WHERE date(ts+?,'unixepoch')=? GROUP BY action ORDER BY cnt DESC LIMIT 8",
            (offset, target_day)
        ).fetchall()
        action_breakdown = [{"action": r["action"], "count": r["cnt"]} for r in action_breakdown_rows]

        # 7. AI 模型调用与 Token 测算
        ai_stat = conn.execute(
            "SELECT COUNT(*) as calls, "
            "COALESCE(SUM(total_tokens), 0) as total_tokens, "
            "COALESCE(SUM(prompt_tokens), 0) as prompt_tokens, "
            "COALESCE(SUM(completion_tokens), 0) as completion_tokens, "
            "COALESCE(SUM(cost_usd), 0.0) as cost_usd, "
            "COALESCE(SUM(cost_rmb), 0.0) as cost_rmb "
            "FROM ai_logs WHERE date(ts+?,'unixepoch')=?",
            (offset, target_day)
        ).fetchone()

        ai_calls = ai_stat["calls"] if ai_stat else 0
        ai_tokens = ai_stat["total_tokens"] if ai_stat else 0
        ai_prompt_tokens = ai_stat["prompt_tokens"] if ai_stat else 0
        ai_completion_tokens = ai_stat["completion_tokens"] if ai_stat else 0
        ai_cost_usd = round(ai_stat["cost_usd"] if ai_stat else 0.0, 4)
        ai_cost_rmb = round(ai_stat["cost_rmb"] if ai_stat else 0.0, 4)

        # 8. 最近的关键工作记录（用于复盘）
        recent_logs = conn.execute(
            "SELECT ts, actor, action, target_type, target_id, detail "
            "FROM audit_log WHERE date(ts+?,'unixepoch')=? "
            "ORDER BY ts DESC LIMIT 15",
            (offset, target_day)
        ).fetchall()
        recent_log_list = [dict(r) for r in recent_logs]

    # 生成规范化下班工作汇报文本 (Markdown)
    report_lines = [
        f"📅 【外贸业务工作日报 · {target_day}】",
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"✉️ 一、邮件与询盘处理",
        f"  • 收到买家来信：{received_emails} 封（其中高意向询盘 {high_inquiries} 封）",
        f"  • 邮件对外发出：{sent_emails} 封（系统自动发送 {auto_sent} 封，人工审批发信 {drafts_approved} 封）",
        f"  • 当前待审草稿：{drafts_pending} 封",
        f"",
        f"👥 二、客户跟进与拓展",
        f"  • 今日新增客户：{new_contacts} 位",
        f"  • 客户互动跟进：{followup_actions} 次",
        f"  • 社媒开发触达：{social_outreach} 条",
        f"",
        f"📑 三、外贸单证与业务管理",
        f"  • 新建/处理单证：{new_vouchers} 份（本地智能 OCR 识别 {ocr_vouchers} 份）",
        f"  • 业务审计总操作：{total_actions} 次",
        f"",
        f"🤖 四、AI 提效与 Token 成本",
        f"  • AI 调用次数：{ai_calls} 次",
        f"  • 消耗 Token 总计：{ai_tokens:,}（输入 {ai_prompt_tokens:,} / 输出 {ai_completion_tokens:,}）",
        f"  • 当日折合费用：${ai_cost_usd:.4f} USD（约 ¥{ai_cost_rmb:.2f} 元）",
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"💡 下班小结：今日工作已全面归档，各项跟进无遗漏。",
    ]
    daily_report_text = "\n".join(report_lines)

    return {
        "day": target_day,
        "received_emails": received_emails,
        "high_inquiries": high_inquiries,
        "sent_emails": sent_emails,
        "auto_sent": auto_sent,
        "drafts_approved": drafts_approved,
        "drafts_pending": drafts_pending,
        "new_contacts": new_contacts,
        "followup_actions": followup_actions,
        "new_vouchers": new_vouchers,
        "ocr_vouchers": ocr_vouchers,
        "social_outreach": social_outreach,
        "total_actions": total_actions,
        "action_breakdown": action_breakdown,
        "ai_calls": ai_calls,
        "ai_tokens": ai_tokens,
        "ai_prompt_tokens": ai_prompt_tokens,
        "ai_completion_tokens": ai_completion_tokens,
        "ai_cost_usd": ai_cost_usd,
        "ai_cost_rmb": ai_cost_rmb,
        "recent_logs": recent_log_list,
        "daily_report_text": daily_report_text,
    }


def list_audit_logs(page: int = 1, size: int = 30, day_str: str = "", action_type: str = "") -> dict:
    """分页查询业务操作审计流水。"""
    page = max(1, page)
    size = max(10, min(100, size))
    offset_page = (page - 1) * size
    tz_offset = config.TZ_OFFSET_HOURS * 3600

    where = ["1=1"]
    args = []
    if day_str:
        where.append("date(ts+?,'unixepoch')=?")
        args.extend([tz_offset, day_str.strip()])
    if action_type:
        where.append("action LIKE ?")
        args.append(f"%{action_type.strip()}%")

    where_sql = " AND ".join(where)
    with db.ro() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM audit_log WHERE {where_sql}", args).fetchone()[0]
        rows = conn.execute(
            f"SELECT id, ts, actor, action, target_type, target_id, detail "
            f"FROM audit_log WHERE {where_sql} ORDER BY ts DESC LIMIT ? OFFSET ?",
            args + [size, offset_page]
        ).fetchall()
        items = [dict(r) for r in rows]

    total_pages = max(1, (total + size - 1) // size)
    return {
        "items": items,
        "total": total,
        "page": page,
        "size": size,
        "total_pages": total_pages,
        "has_prev": page > 1,
        "has_next": page < total_pages,
    }


def get_ai_usage_summary(days: int = 30) -> dict:
    """获取大模型 Token 消耗与计费测算汇总。"""
    tz_offset = config.TZ_OFFSET_HOURS * 3600
    with db.ro() as conn:
        # 1. 累计总体情况
        total_row = conn.execute(
            "SELECT COUNT(*) as calls, "
            "COALESCE(SUM(total_tokens), 0) as total_tokens, "
            "COALESCE(SUM(prompt_tokens), 0) as prompt_tokens, "
            "COALESCE(SUM(completion_tokens), 0) as completion_tokens, "
            "COALESCE(SUM(cost_usd), 0.0) as cost_usd, "
            "COALESCE(SUM(cost_rmb), 0.0) as cost_rmb, "
            "COALESCE(AVG(latency_ms), 0) as avg_latency, "
            "COALESCE(SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END), 0) as ok_calls "
            "FROM ai_logs"
        ).fetchone()

        calls = total_row["calls"] or 0
        total_tokens = total_row["total_tokens"] or 0
        prompt_tokens = total_row["prompt_tokens"] or 0
        completion_tokens = total_row["completion_tokens"] or 0
        cost_usd = round(total_row["cost_usd"] or 0.0, 4)
        cost_rmb = round(total_row["cost_rmb"] or 0.0, 4)
        avg_latency = round(total_row["avg_latency"] or 0)
        ok_calls = total_row["ok_calls"] or 0
        success_rate = round(ok_calls / calls * 100, 1) if calls > 0 else 100.0

        # 2. 按用途分布 (purpose)
        purpose_rows = conn.execute(
            "SELECT purpose, COUNT(*) as calls, "
            "COALESCE(SUM(total_tokens), 0) as tokens, "
            "COALESCE(SUM(cost_rmb), 0.0) as cost_rmb "
            "FROM ai_logs GROUP BY purpose ORDER BY tokens DESC"
        ).fetchall()

        purpose_names = {
            "analysis": "询盘意向分类与打分",
            "summary": "邮件中文智能摘要",
            "draft": "业务回复起草",
            "profile": "买家深度背调画像",
            "test": "API连通性测试",
            "general": "通用调用",
        }
        purposes = []
        for r in purpose_rows:
            p = r["purpose"]
            tok = r["tokens"]
            pct = round(tok / total_tokens * 100, 1) if total_tokens > 0 else 0
            purposes.append({
                "purpose": p,
                "label": purpose_names.get(p, p),
                "calls": r["calls"],
                "tokens": tok,
                "cost_rmb": round(r["cost_rmb"] or 0.0, 4),
                "pct": pct,
            })

        # 3. 日度趋势 (过去指定天数，默认 14 或 30 天)
        trend_rows = conn.execute(
            "SELECT date(ts+?,'unixepoch') as day, "
            "COUNT(*) as calls, "
            "COALESCE(SUM(total_tokens), 0) as tokens, "
            "COALESCE(SUM(cost_rmb), 0.0) as cost_rmb "
            "FROM ai_logs WHERE ts >= ? "
            "GROUP BY day ORDER BY day ASC",
            (tz_offset, db.now_ts() - days * 86400)
        ).fetchall()
        daily_trends = [
            {
                "day": r["day"],
                "calls": r["calls"],
                "tokens": r["tokens"],
                "cost_rmb": round(r["cost_rmb"] or 0.0, 4),
            }
            for r in trend_rows
        ]

    return {
        "calls": calls,
        "total_tokens": total_tokens,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cost_usd": cost_usd,
        "cost_rmb": cost_rmb,
        "avg_latency": avg_latency,
        "success_rate": success_rate,
        "purposes": purposes,
        "daily_trends": daily_trends,
    }


def list_ai_logs(page: int = 1, size: int = 30, purpose: str = "", status: str = "") -> dict:
    """分页查询大模型调用与 Token 流水。"""
    page = max(1, page)
    size = max(10, min(100, size))
    offset_page = (page - 1) * size

    where = ["1=1"]
    args = []
    if purpose:
        where.append("purpose=?")
        args.append(purpose.strip())
    if status:
        where.append("status=?")
        args.append(status.strip())

    where_sql = " AND ".join(where)
    with db.ro() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM ai_logs WHERE {where_sql}", args).fetchone()[0]
        rows = conn.execute(
            f"SELECT id, ts, model, purpose, prompt_tokens, completion_tokens, total_tokens, "
            f"cost_usd, cost_rmb, latency_ms, message_id, contact_id, status, error_msg "
            f"FROM ai_logs WHERE {where_sql} ORDER BY ts DESC LIMIT ? OFFSET ?",
            args + [size, offset_page]
        ).fetchall()
        items = [dict(r) for r in rows]

    total_pages = max(1, (total + size - 1) // size)
    return {
        "items": items,
        "total": total,
        "page": page,
        "size": size,
        "total_pages": total_pages,
        "has_prev": page > 1,
        "has_next": page < total_pages,
    }


def get_email_24h_timing_analysis() -> dict:
    """统计全天 24 小时（00:00 - 23:00）邮件收发分布，并结合主要贸易伙伴时区给出黄金沟通时段。"""
    tz_offset = config.TZ_OFFSET_HOURS * 3600

    # 初始化 24 小时桶
    hours_data = [
        {"hour": h, "hour_label": f"{h:02d}:00", "in_count": 0, "out_count": 0, "high_count": 0}
        for h in range(24)
    ]

    with db.ro() as conn:
        # 收信分布 (买家发送/我们接收)
        in_rows = conn.execute(
            "SELECT CAST(strftime('%H', sent_ts+?, 'unixepoch') AS INTEGER) as hr, "
            "COUNT(*) as cnt, "
            "SUM(CASE WHEN score >= ? THEN 1 ELSE 0 END) as high_cnt "
            "FROM messages WHERE direction='in' "
            "GROUP BY hr",
            (tz_offset, config.HIGH_INTENT)
        ).fetchall()
        for r in in_rows:
            hr = r["hr"]
            if hr is not None and 0 <= hr < 24:
                hours_data[hr]["in_count"] = r["cnt"] or 0
                hours_data[hr]["high_count"] = r["high_cnt"] or 0

        # 发信分布 (我方回复/发出)
        out_rows = conn.execute(
            "SELECT CAST(strftime('%H', sent_ts+?, 'unixepoch') AS INTEGER) as hr, "
            "COUNT(*) as cnt "
            "FROM messages WHERE direction='out' "
            "GROUP BY hr",
            (tz_offset,)
        ).fetchall()
        for r in out_rows:
            hr = r["hr"]
            if hr is not None and 0 <= hr < 24:
                hours_data[hr]["out_count"] = r["cnt"] or 0

    # 找出峰值
    max_in = max((d["in_count"] for d in hours_data), default=0)
    max_out = max((d["out_count"] for d in hours_data), default=0)
    peak_in_hour = max(hours_data, key=lambda d: d["in_count"])["hour"] if max_in > 0 else 15
    peak_out_hour = max(hours_data, key=lambda d: d["out_count"])["hour"] if max_out > 0 else 10

    # 全球主要外贸市场与北京时间对照的沟通时机建议
    regions = [
        {
            "region": "欧洲 (中欧/英国)",
            "countries": "德国、法国、意大利、英国、西班牙、荷兰",
            "tz_desc": "UTC+1 / UTC+0 (较北京晚 6-7 小时)",
            "golden_bj_window": "15:00 - 18:00 (当地 09:00 - 12:00)",
            "work_bj_window": "14:00 - 23:00",
            "status_badge": "欧洲黄金窗口",
            "advice": "欧洲买家习惯上班第一件事处理新邮件。北京时间 15:00-18:00 发送可正处于其收件箱顶端，回复率最高；18:00-20:00 适合进一步澄清技术参数。",
        },
        {
            "region": "北美洲 (美东/加东)",
            "countries": "美国 (纽约/波士顿)、加拿大 (多伦多)",
            "tz_desc": "UTC-5 (较北京晚 12-13 小时)",
            "golden_bj_window": "21:00 - 24:00 (当地 09:00 - 12:00)",
            "work_bj_window": "20:00 - 05:00",
            "status_badge": "美东黄金窗口",
            "advice": "北美商务节奏极快。北京时间 20:30-22:30 定时送达，刚好切入美东早间开工时间；若需即时在线答复，可在此区间保持通知畅通。",
        },
        {
            "region": "北美洲 (美西太平洋)",
            "countries": "美国 (加州/西雅图/洛杉矶)、加拿大 (温哥华)",
            "tz_desc": "UTC-8 (较北京晚 15-16 小时)",
            "golden_bj_window": "00:00 - 03:00 (当地 09:00 - 12:00)",
            "work_bj_window": "23:00 - 08:00",
            "status_badge": "美西窗口",
            "advice": "建议利用系统的定时发送或在凌晨前备妥草稿，让邮件在美西客户 08:30-09:30 到达。",
        },
        {
            "region": "中东与海湾地区",
            "countries": "阿联酋 (迪拜)、沙特阿拉伯、土耳其",
            "tz_desc": "UTC+3 / UTC+4 (较北京晚 4-5 小时)",
            "golden_bj_window": "13:30 - 17:00 (当地 09:30 - 13:00)",
            "work_bj_window": "13:00 - 21:00",
            "status_badge": "中东黄金窗口",
            "advice": "注意宗教作息：沙特等国周五、周六为公休或祈祷日，周日至周四为常规工作日；北京时间下午为最佳交流期。",
        },
        {
            "region": "亚太地区 (东南亚/日韩/澳新)",
            "countries": "越南、泰国、新加坡、日本、澳大利亚",
            "tz_desc": "UTC+7 至 UTC+11 (时差在 ±3 小时内)",
            "golden_bj_window": "09:30 - 11:30 & 14:00 - 16:30",
            "work_bj_window": "08:30 - 17:30",
            "status_badge": "即时同频窗口",
            "advice": "时区高度同频，支持即时交流。建议在收到询盘后 1 小时内快速初次响应以抢占商机。",
        },
    ]

    return {
        "hours_data": hours_data,
        "max_in": max_in,
        "max_out": max_out,
        "peak_in_hour": peak_in_hour,
        "peak_out_hour": peak_out_hour,
        "regions": regions,
    }

