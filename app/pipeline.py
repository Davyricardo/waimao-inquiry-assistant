"""流水线编排：拉邮件 → 分析 → 命中规则 → 起草 → 入库。

这是 worker 循环每轮调用的主流程。
"""
import json
import traceback

from . import (ai_agent, ai_client, ai_profile, ai_summary, config, db,
               mail_engine, rules_engine, sender)


def get_accounts() -> list:
    with db.ro() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM accounts WHERE is_active=1").fetchall()]


def process_new_mail(account) -> dict:
    """拉取并处理一轮新邮件。返回本轮统计。"""
    stat = {"fetched": 0, "new": 0, "analyzed": 0, "drafted": 0,
            "errors": 0, "error_detail": []}

    try:
        mails = mail_engine.fetch_new_mails(account)
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        stat["errors"] += 1
        stat["error_detail"].append(f"IMAP 拉取失败: {msg}")
        _mark_account_error(account["id"], msg)
        return stat
    stat["fetched"] = len(mails)

    for m in mails:
        try:
            mid = mail_engine.ingest(m, account["id"])
            if mid is None:
                continue
            stat["new"] += 1
            _analyze_and_draft(mid)
            stat["analyzed"] += 1
        except Exception as e:
            stat["errors"] += 1
            stat["error_detail"].append(
                f"处理 {m.get('subject', '')[:40]} 失败: {type(e).__name__}: {e}")
            traceback.print_exc()

    if stat["errors"] == 0:
        _mark_account_ok(account["id"])
    return stat


def _mark_account_ok(account_id: int) -> None:
    with db.tx() as conn:
        conn.execute("UPDATE accounts SET last_sync_ts=?, last_error=NULL WHERE id=?",
                     (db.now_ts(), account_id))


def _mark_account_error(account_id: int, msg: str) -> None:
    with db.tx() as conn:
        conn.execute("UPDATE accounts SET last_sync_ts=?, last_error=? WHERE id=?",
                     (db.now_ts(), msg[:500], account_id))


def _analyze_and_draft(message_id: int) -> None:
    """对一封邮件做分析、中文摘要、客户背调与起草。

    所有分支都必须收尾，不留 status='new'。
    摘要/背调失败不影响主流程（各自内部已降级）。
    """
    with db.ro() as conn:
        msg = conn.execute("SELECT * FROM messages WHERE id=?",
                           (message_id,)).fetchone()
        if not msg:
            return
        msg = dict(msg)
        contact = conn.execute("SELECT * FROM contacts WHERE id=?",
                               (msg["contact_id"],)).fetchone()
        contact = dict(contact) if contact else {}

    # --- 分析 ---
    analysis = ai_agent.ai_analyze(
        msg["subject"] or "", msg["body_text"] or "",
        msg["from_addr"] or "", contact.get("name") or "")

    decision = rules_engine.decide(analysis)
    action = decision["action"]

    # --- 落库分析结果 ---
    entities_json = json.dumps(analysis.get("entities") or {}, ensure_ascii=False)
    status = {
        "drop": "skipped", "notify": "analyzed",
        "draft_only": "drafted", "auto_reply": "drafted",
    }.get(action, "analyzed")

    with db.tx() as conn:
        conn.execute(
            "UPDATE messages SET category=?,score=?,entities=?,ai_summary=?,"
            "ai_rationale=?,ai_model=?,analyzed_ts=?,status=? WHERE id=?",
            (analysis["category"], analysis["score"], entities_json,
             analysis.get("summary"), analysis.get("rationale"),
             analysis.get("model"), db.now_ts(), status, message_id))
        # 客户意向分取历史最高，阶段推进
        conn.execute(
            "UPDATE contacts SET score=MAX(score,?), last_seen_ts=MAX("
            "COALESCE(last_seen_ts,0),?) WHERE id=?",
            (analysis["score"], msg["sent_ts"], msg["contact_id"]))
        conn.execute(
            "UPDATE threads SET score=MAX(score,?) WHERE id=?",
            (analysis["score"], msg["thread_id"]))

        # 如果邮件被识别为垃圾邮件 (spam)，将客户阶段打上 spam 标记，防止污染正常客户池
        if analysis.get("category") == "spam":
            conn.execute(
                "UPDATE contacts SET stage='spam' WHERE id=? AND stage NOT IN ('won', 'negotiating')",
                (msg["contact_id"],))

    # --- 中文摘要（仅对目标邮件调用 AI 生成并保存，减少 token 浪费）---
    # 目标邮件定义：询盘 (inquiry)、报价 (quote)、售后 (after_sale / after_sales)、投诉 (complaint)
    target_categories = {"inquiry", "quote", "after_sale", "after_sales", "complaint"}
    if analysis.get("category") in target_categories:
        _make_summary(msg, contact, analysis)

    # --- 客户背调（仅对有效目标邮件且客户还没有档案时做，避免对垃圾发件人浪费 token）---
    if analysis.get("category") in target_categories:
        _make_profile(msg, contact)

    if action == "drop":
        return

    # --- 投诉类只提醒，不起草 ---
    if action == "notify":
        return

    # --- 起草回复 ---
    template = _load_template(decision["rule"].get("template_key"))
    signature = _get_setting("reply_signature",
                             "Best regards,\n{sender_name}")
    signature = signature.replace("{sender_name}",
                                  contact.get("name") or "Sales Team")

    ctx = {
        "subject": msg["subject"] or "",
        "name": contact.get("name") or "there",
        "email": msg["from_addr"] or "",
        "signature": signature,
    }

    drafted = ai_agent.draft_reply(
        msg["subject"] or "", msg["body_text"] or "",
        contact.get("name") or "", template, signature, analysis)

    if drafted:
        subj, body = drafted
        model = analysis.get("model")
    else:
        subj, body = ai_agent.render_template(template, ctx)
        model = "template"

    with db.tx() as conn:
        conn.execute(
            "INSERT INTO drafts (message_id,contact_id,subject,body_text,"
            "template_key,ai_model,status,created_ts) "
            "VALUES (?,?,?,?,?,?,'pending',?)",
            (message_id, msg["contact_id"], subj, body,
             decision["rule"].get("template_key"), model, db.now_ts()))

    # --- 全自动发送（仅当规则与全局开关都放行） ---
    if action == "auto_reply" and config.AUTO_SEND:
        _auto_send(message_id)


def _auto_send(message_id: int) -> None:
    with db.ro() as conn:
        d = conn.execute(
            "SELECT * FROM drafts WHERE message_id=? AND status='pending' "
            "ORDER BY id DESC LIMIT 1", (message_id,)).fetchone()
        msg = conn.execute("SELECT * FROM messages WHERE id=?",
                           (message_id,)).fetchone()
        acct = conn.execute("SELECT * FROM accounts LIMIT 1").fetchone()
    if not d or not msg or not acct:
        return
    ok, info = sender.send_mail(
        dict(acct), msg["from_addr"], d["subject"], d["body_text"],
        reply_to_msgid=msg["msgid"], references=msg["refs"])
    with db.tx() as conn:
        conn.execute(
            "UPDATE drafts SET status=?, sent_ts=?, send_error=? WHERE id=?",
            ("sent" if ok else "pending", db.now_ts() if ok else None,
             None if ok else info, d["id"]))
        if ok:
            conn.execute("UPDATE messages SET status='auto_sent' WHERE id=?",
                         (message_id,))
    if ok:
        mail_engine.record_outgoing(acct["id"], msg["contact_id"],
                                    msg["thread_id"], msg["from_addr"],
                                    d["subject"], d["body_text"], info)


def _make_summary(msg: dict, contact: dict, analysis: dict) -> None:
    """生成中文摘要并入库。失败静默（不影响主流程）。"""
    try:
        res = ai_summary.ai_summary(
            subject=msg.get("subject") or "",
            body=msg.get("body_text") or "",
            from_addr=msg.get("from_addr") or "",
            contact_name=contact.get("name") or "",
            company=contact.get("company") or "",
            category=analysis.get("category") or "",
            score=analysis.get("score"),
        )
    except Exception:
        traceback.print_exc()
        return

    kp = json.dumps(res.get("key_points_cn") or [], ensure_ascii=False)
    with db.tx() as conn:
        conn.execute(
            "UPDATE messages SET summary_cn=?,key_points_cn=?,urgency_cn=?,"
            "translated_cn=?,summary_source=?,summarized_ts=? WHERE id=?",
            (res.get("summary_cn"), kp, res.get("urgency_cn"),
             res.get("translated_body_cn"), res.get("source"),
             db.now_ts(), msg["id"]))


def _make_profile(msg: dict, contact: dict) -> None:
    """客户背调。已有档案则跳过（除非是规则档而当前已具备 AI 能力）。"""
    if not contact:
        return
    existing_src = ""
    if contact.get("background"):
        try:
            existing_src = (json.loads(contact["background"])
                            .get("source") or "")
        except (json.JSONDecodeError, TypeError):
            existing_src = ""

    # 已用 AI 做过 → 不重复；仅规则做过且现在有 Key → 升级一次
    if existing_src == "ai":
        return
    if existing_src == "rule" and not ai_client_ready():
        return

    try:
        res = ai_profile.ai_profile(
            from_addr=msg.get("from_addr") or "",
            name=contact.get("name") or "",
            company=contact.get("company") or "",
            country_hint=contact.get("country") or "",
            body=msg.get("body_text") or "",
            subject=msg.get("subject") or "",
        )
    except Exception:
        traceback.print_exc()
        return

    country, background = ai_profile.format_profile_for_db(res)
    with db.tx() as conn:
        conn.execute(
            "UPDATE contacts SET country=COALESCE(NULLIF(?,''),country),"
            "background=?,company_type=?,channel_role=?,credibility=?,"
            "profiled_ts=? WHERE id=?",
            (country, background, res.get("company_type"),
             res.get("channel_role"), res.get("credibility"),
             db.now_ts(), contact["id"]))


def ai_client_ready() -> bool:
    """当前是否具备模型能力（供背调升级判断用）。"""
    return ai_client.is_enabled()


def reprocess_message(message_id: int) -> tuple[bool, str]:
    """对单封邮件重跑 分析 + 摘要 + 背调。设置页/详情页的手动触发用。"""
    with db.ro() as conn:
        exists = conn.execute("SELECT 1 FROM messages WHERE id=?",
                              (message_id,)).fetchone()
    if not exists:
        return False, "邮件不存在"
    try:
        _analyze_and_draft(message_id)
        return True, "已重新分析"
    except Exception as e:
        traceback.print_exc()
        return False, f"{type(e).__name__}: {e}"


def reprocess_all(limit: int = 50) -> dict:
    """批量重跑最近的邮件（用于刚配好 API Key 后补摘要/背调）。"""
    with db.ro() as conn:
        rows = conn.execute(
            "SELECT id FROM messages WHERE direction='in' "
            "ORDER BY sent_ts DESC LIMIT ?", (limit,)).fetchall()
    done, failed = 0, 0
    for r in rows:
        ok, _ = reprocess_message(r["id"])
        if ok:
            done += 1
        else:
            failed += 1
    return {"total": len(rows), "done": done, "failed": failed}


def _load_template(key) -> dict:
    if not key:
        return {}
    with db.ro() as conn:
        r = conn.execute("SELECT * FROM templates WHERE key=?",
                         (key,)).fetchone()
    return dict(r) if r else {}


def _get_setting(key: str, default: str = "") -> str:
    with db.ro() as conn:
        r = conn.execute("SELECT value FROM settings WHERE key=?",
                         (key,)).fetchone()
    return r["value"] if r and r["value"] is not None else default


def refresh_daily_stats() -> None:
    """刷新今日统计快照，供看板趋势图直接读取。仅统计有效目标邮件。"""
    import time as _t
    from . import queries
    queries.clean_invalid_pending_drafts()
    queries.clean_spam_contacts()
    offset = config.TZ_OFFSET_HOURS * 3600
    today = _t.strftime("%Y-%m-%d", _t.gmtime(_t.time() + offset))
    cats_sql = queries.TARGET_CATEGORIES_SQL
    with db.tx() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) c FROM messages WHERE direction='in' "
            f"AND (category IN {cats_sql} OR (category IS NULL AND status='new')) "
            f"AND date(sent_ts + ?, 'unixepoch')=?", (offset, today)).fetchone()
        in_count = row["c"] if row else 0
        row = conn.execute(
            "SELECT COUNT(*) c FROM messages WHERE direction='out' "
            "AND date(sent_ts + ?, 'unixepoch')=?", (offset, today)).fetchone()
        out_count = row["c"] if row else 0
        row = conn.execute(
            "SELECT COUNT(*) c FROM messages WHERE direction='out' "
            "AND status='auto_sent' AND date(sent_ts + ?, 'unixepoch')=?",
            (offset, today)).fetchone()
        auto_count = row["c"] if row else 0
        row = conn.execute(
            f"SELECT COUNT(*) c FROM messages WHERE direction='in' AND score>=? "
            f"AND category IN {cats_sql} "
            f"AND date(sent_ts + ?, 'unixepoch')=?",
            (config.HIGH_INTENT, offset, today)).fetchone()
        high = row["c"] if row else 0
        row = conn.execute(
            "SELECT COUNT(*) c FROM contacts WHERE (score>=? OR stage IN ('engaging', 'quoted', 'negotiating', 'won') OR reply_count>0) "
            "AND date(first_seen_ts + ?, 'unixepoch')=?", (config.HIGH_INTENT, offset, today)).fetchone()
        newc = row["c"] if row else 0
        row = conn.execute(
            f"SELECT AVG(score) a FROM messages WHERE direction='in' AND score IS NOT NULL "
            f"AND category IN {cats_sql} "
            f"AND date(sent_ts + ?, 'unixepoch')=?", (offset, today)).fetchone()
        avg = row["a"] if row and row["a"] is not None else 0

        conn.execute(
            "INSERT INTO daily_stats (day,in_count,out_count,auto_count,"
            "high_intent,new_contacts,avg_score,updated_ts) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(day) DO UPDATE SET in_count=excluded.in_count,"
            "out_count=excluded.out_count,auto_count=excluded.auto_count,"
            "high_intent=excluded.high_intent,new_contacts=excluded.new_contacts,"
            "avg_score=excluded.avg_score,updated_ts=excluded.updated_ts",
            (today, in_count, out_count, auto_count, high, newc, avg, db.now_ts()))
