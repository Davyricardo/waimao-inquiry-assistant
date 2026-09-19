"""设置、规则、模板、审核台路由。"""
import json
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import ai_client, auth, config, crypto_util, db, pipeline, queries, sender
from ..templating import render

router = APIRouter()

COOKIE = "mb_sess"

# 邮箱预设（集中在这里，减少 web.py 中的内容）
MAIL_PRESETS = {
    "qq": {"label": "QQ 邮箱", "imap_host": "imap.qq.com", "imap_port": 993,
           "smtp_host": "smtp.qq.com", "smtp_port": 465,
           "help": "设置 → 账户 → 开启 IMAP/SMTP 服务 → 生成 16 位授权码"},
    "163": {"label": "网易 163 邮箱", "imap_host": "imap.163.com", "imap_port": 993,
            "smtp_host": "smtp.163.com", "smtp_port": 465,
            "help": "设置 → POP3/SMTP/IMAP → 开启 IMAP/SMTP → 获取授权码"},
    "outlook": {"label": "Outlook / Microsoft 365",
                "imap_host": "outlook.office365.com", "imap_port": 993,
                "smtp_host": "smtp.office365.com", "smtp_port": 587,
                "help": "需先开启双重验证 → 生成应用密码（App Password）"},
    "exmail": {"label": "腾讯企业邮箱", "imap_host": "imap.exmail.qq.com",
               "imap_port": 993, "smtp_host": "smtp.exmail.qq.com", "smtp_port": 465,
               "help": "邮箱设置 → 客户端设置 → 开启 IMAP/SMTP → 生成专用密码"},
    "aliyun": {"label": "阿里云企业邮箱", "imap_host": "imap.qiye.aliyun.com",
               "imap_port": 993, "smtp_host": "smtp.qiye.aliyun.com", "smtp_port": 465,
               "help": "设置 → 客户端设置 → 生成专用密码"},
    "custom": {"label": "自定义", "imap_host": "imap.qq.com", "imap_port": 993,
               "smtp_host": "smtp.qq.com", "smtp_port": 465,
               "help": "手动填写服务器地址"},
}


def check_csrf(request: Request, token: str) -> bool:
    sess = request.cookies.get(COOKIE, "")
    return bool(token) and token == auth.csrf_token(sess)


def _ai_context() -> dict:
    """设置页 AI 区块需要的上下文。"""
    base = ai_client.get_base_url()
    preset = "custom"
    for k, p in ai_client.AI_PRESETS.items():
        if p["base_url"] and p["base_url"] == base:
            preset = k
            break
    cur = ai_client.AI_PRESETS.get(preset, {})
    key = ai_client.get_api_key()
    enc = ai_client.get_api_key_enc()
    key_ok = bool(key)
    return {
        "ai_status": ai_client.status_text(),
        "ai_presets": ai_client.AI_PRESETS,
        "ai_presets_json": json.dumps(ai_client.AI_PRESETS, ensure_ascii=False),
        "ai_conf": {
            "configured": key_ok, "enabled": ai_client.is_enabled(),
            "base_url": base, "model": ai_client.get_model(),
            "preset": preset,
            "preset_label": cur.get("label", "自定义"),
            "portal_name": cur.get("portal_name", "官方开放平台"),
            "key_masked": crypto_util.mask_secret(key),
            "key_plain": key, "key_ok": key_ok,
            "key_unreadable": bool(enc) and not key_ok,
            "key_from_env": (not enc) and bool(key),
            "key_len": len(key),
            "key_url": cur.get("key_url", ""),
            "hint": cur.get("hint", ""),
        },
    }


def _probe_account(acct: dict) -> str:
    ok1, msg1 = sender.test_imap(acct)
    ok2, msg2 = sender.test_connection(acct)
    from .. import mail_engine as me
    if ok1:
        try:
            mails = me.fetch_new_mails(acct, limit=1)
            extra = f" | 拉取测试：读到 {len(mails)} 封新邮件"
        except Exception as e:
            extra = f" | 拉取测试失败：{type(e).__name__}"
    else:
        extra = ""
    return (f"[{acct.get('email','')}] "
            f"IMAP: {'✓' if ok1 else '✗'} {msg1}　|　"
            f"SMTP: {'✓' if ok2 else '✗'} {msg2}{extra}")


# ============ 设置页 ============

@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, ok: str = "", err: str = "", test: str = ""):
    with db.ro() as conn:
        rows = conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()
        counts = {r["account_id"]: r["n"] for r in conn.execute(
            "SELECT account_id, COUNT(*) AS n FROM messages "
            "GROUP BY account_id").fetchall()}
    accounts = []
    for r in rows:
        a = dict(r)
        plain = crypto_util.decrypt_secret_safe(a.get("secret_enc", ""))
        a["secret_masked"] = crypto_util.mask_secret(plain)
        a["secret_ok"] = bool(plain)
        a["secret_plain"] = plain
        a["msg_count"] = counts.get(a["id"], 0)
        accounts.append(a)
    return HTMLResponse(render(
        "settings.html",
        account=accounts[0] if accounts else None,
        accounts=accounts,
        settings=queries.list_settings(),
        seller_profile=queries.get_seller_profile(),
        admin_user=auth.get_admin_user(),
        has_security_question=auth.has_security_question(),
        security_question=auth.get_security_question(),
        preset_security_questions=auth.PRESET_SECURITY_QUESTIONS,
        ok=ok, err=err, test=test,
        csrf=auth.csrf_token(request.cookies.get(COOKIE, "")),
        presets=MAIL_PRESETS, presets_json=json.dumps(MAIL_PRESETS, ensure_ascii=False),
        **_ai_context()))


@router.post("/settings/admin/credentials")
def settings_admin_credentials(
    request: Request,
    current_password: str = Form(""),
    security_answer: str = Form(""),
    new_username: str = Form(""),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
    new_security_question: str = Form(""),
    new_security_question_custom: str = Form(""),
    new_security_answer: str = Form(""),
    csrf_tok: str = Form(""),
):
    if not check_csrf(request, csrf_tok):
        return RedirectResponse("/settings?err=" + quote("请求校验失败"), status_code=303)

    if new_password and new_password != confirm_password:
        return RedirectResponse("/settings?err=" + quote("两次输入的新密码不一致，请重新输入"), status_code=303)

    target_question = (new_security_question or "").strip()
    if target_question == "自定义密保问题" and new_security_question_custom.strip():
        target_question = new_security_question_custom.strip()

    ok, msg, updated_user = auth.update_admin_credentials(
        current_password=current_password,
        security_answer=security_answer,
        new_username=new_username,
        new_password=new_password,
        new_security_question=target_question,
        new_security_answer=new_security_answer,
    )
    if not ok:
        queries.log(auth.get_admin_user(), "admin_credentials_fail", detail=msg)
        return RedirectResponse("/settings?err=" + quote(msg), status_code=303)

    queries.log(updated_user, "admin_credentials_update", detail=f"username={updated_user}")
    resp = RedirectResponse("/settings?ok=" + quote(msg), status_code=303)
    resp.set_cookie(
        COOKIE,
        auth.make_session(updated_user),
        httponly=True,
        samesite="lax",
        max_age=config.SESSION_TTL,
        path="/",
    )
    return resp


@router.post("/settings/ai")
def settings_ai(request: Request, api_key: str = Form(""),
                base_url: str = Form(""), model: str = Form(""),
                enabled: str = Form("0"), csrf_tok: str = Form("")):
    if not check_csrf(request, csrf_tok):
        return RedirectResponse("/settings?err=" + quote("请求校验失败"), status_code=303)
    if not base_url.strip():
        return RedirectResponse("/settings?err=" + quote("API 地址不能为空"), status_code=303)
    ai_client.save_credentials(
        api_key if api_key.strip() else None, base_url, model, enabled == "1")
    queries.log(auth.get_admin_user(), "settings_ai", detail=f"model={model}")
    return RedirectResponse("/settings?ok=" + quote("AI 配置已保存"), status_code=303)


@router.post("/settings/ai/test")
def settings_ai_test(request: Request, csrf_tok: str = Form("")):
    if not check_csrf(request, csrf_tok):
        return RedirectResponse("/settings?err=1", status_code=303)
    ok, msg = ai_client.test_connection()
    queries.log(config.ADMIN_USER, "settings_ai_test", detail=msg)
    key = "ok" if ok else "err"
    return RedirectResponse(f"/settings?{key}=" + quote(f"模型测试：{msg}"), status_code=303)


@router.post("/settings/ai/reprocess")
def settings_ai_reprocess(request: Request, csrf_tok: str = Form("")):
    if not check_csrf(request, csrf_tok):
        return RedirectResponse("/settings?err=1", status_code=303)
    if not ai_client.is_configured():
        return RedirectResponse(
            "/settings?err=" + quote("尚未配置 API Key，无法批量补跑"), status_code=303)
    stat = pipeline.reprocess_all(limit=50)
    queries.log(config.ADMIN_USER, "settings_ai_reprocess",
                detail=json.dumps(stat, ensure_ascii=False))
    return RedirectResponse(
        "/settings?ok=" + quote(f"已补跑 {stat['done']}/{stat['total']} 封"
                                f"（失败 {stat['failed']}）"),
        status_code=303)


@router.post("/settings/global")
def settings_global(request: Request, auto_send: str = Form("0"),
                    high_intent: int = Form(70), reply_signature: str = Form(""),
                    csrf_tok: str = Form("")):
    if not check_csrf(request, csrf_tok):
        return RedirectResponse("/settings?err=1", status_code=303)
    with db.tx() as conn:
        for k, v in [("auto_send", "1" if auto_send == "1" else "0"),
                     ("high_intent", str(high_intent)),
                     ("reply_signature", reply_signature)]:
            conn.execute(
                "INSERT INTO settings (key,value,updated_ts) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
                "updated_ts=excluded.updated_ts", (k, v, db.now_ts()))
    queries.log(config.ADMIN_USER, "settings_global", detail=f"auto_send={auto_send}")
    return RedirectResponse("/settings?ok=" + quote("全局设置已保存"), status_code=303)


@router.post("/settings/seller-profile")
def settings_seller_profile(request: Request,
                             company_name_en: str = Form(""),
                             company_name_cn: str = Form(""),
                             company_address_en: str = Form(""),
                             company_tel: str = Form(""),
                             company_email: str = Form(""),
                             company_website: str = Form(""),
                             bank_beneficiary_name: str = Form(""),
                             bank_name: str = Form(""),
                             bank_address: str = Form(""),
                             bank_account_no: str = Form(""),
                             bank_swift_code: str = Form(""),
                             csrf_tok: str = Form("")):
    if not check_csrf(request, csrf_tok):
        return RedirectResponse("/settings?err=" + quote("请求校验失败"), status_code=303)
    data = {
        "company_name_en": company_name_en, "company_name_cn": company_name_cn,
        "company_address_en": company_address_en, "company_tel": company_tel,
        "company_email": company_email, "company_website": company_website,
        "bank_beneficiary_name": bank_beneficiary_name, "bank_name": bank_name,
        "bank_address": bank_address, "bank_account_no": bank_account_no,
        "bank_swift_code": bank_swift_code,
    }
    queries.save_seller_profile(data)
    queries.log(config.ADMIN_USER, "seller_profile_save")
    return RedirectResponse("/settings?ok=" + quote("外贸单证抬头与收款银行账户已成功保存"), status_code=303)


@router.post("/settings/account")
def account_save(request: Request, email: str = Form(""),
                 display_name: str = Form(""), secret: str = Form(""),
                 preset: str = Form("qq"), watch_folder: str = Form("INBOX"),
                 lookback_days: int = Form(7), account_id: str = Form(""),
                 csrf_tok: str = Form("")):
    if not check_csrf(request, csrf_tok):
        return RedirectResponse("/settings?err=" + quote("请求校验失败"), status_code=303)
    addr = email.strip().lower()
    if not addr or "@" not in addr:
        return RedirectResponse("/settings?err=" + quote("请填写有效的邮箱地址"), status_code=303)

    p = MAIL_PRESETS.get(preset, MAIL_PRESETS["qq"])
    imap_host = request.headers.get("x-noop") or p["imap_host"]
    aid = int(account_id) if account_id.strip().isdigit() else None
    with db.ro() as conn:
        target = (conn.execute("SELECT * FROM accounts WHERE id=?", (aid,)).fetchone()
                  if aid else None)
        if aid and not target:
            return RedirectResponse("/settings?err=" + quote("账户不存在"), status_code=303)
        dup = conn.execute("SELECT id FROM accounts WHERE email=? AND id<>?",
                           (addr, aid or -1)).fetchone()
        if dup:
            return RedirectResponse(
                "/settings?err=" + quote("该邮箱已存在，请直接编辑那一条"), status_code=303)

    if secret.strip():
        enc = crypto_util.encrypt_secret(secret.strip())
    elif target:
        enc = target["secret_enc"]
    else:
        return RedirectResponse("/settings?err=" + quote("新增账户必须填写授权码"), status_code=303)

    since = db.now_ts() - max(1, lookback_days) * 86400
    with db.tx() as conn:
        if target:
            conn.execute(
                "UPDATE accounts SET email=?,display_name=?,secret_enc=?,"
                "imap_host=?,imap_port=?,smtp_host=?,smtp_port=?,watch_folder=?,"
                "since_ts=?,is_active=1,last_error=NULL WHERE id=?",
                (addr, display_name, enc, imap_host, p["imap_port"],
                 p["smtp_host"], p["smtp_port"], watch_folder, since, target["id"]))
            msg = "账户已更新"
        else:
            conn.execute(
                "INSERT INTO accounts (email,display_name,secret_enc,imap_host,"
                "imap_port,smtp_host,smtp_port,watch_folder,since_ts,is_active,"
                "created_ts) VALUES (?,?,?,?,?,?,?,?,?,1,?)",
                (addr, display_name, enc, imap_host, p["imap_port"],
                 p["smtp_host"], p["smtp_port"], watch_folder, since, db.now_ts()))
            msg = "账户已添加"
    queries.log(config.ADMIN_USER, "account_save", "account", detail=addr)
    return RedirectResponse("/settings?ok=" + quote(msg), status_code=303)


@router.post("/settings/account/{aid}/toggle")
def account_toggle(aid: int, request: Request, csrf_tok: str = Form("")):
    if not check_csrf(request, csrf_tok):
        return RedirectResponse("/settings?err=" + quote("请求校验失败"), status_code=303)
    with db.tx() as conn:
        row = conn.execute("SELECT is_active,email FROM accounts WHERE id=?",
                           (aid,)).fetchone()
        if not row:
            return RedirectResponse("/settings?err=" + quote("账户不存在"), status_code=303)
        new = 0 if row["is_active"] else 1
        conn.execute("UPDATE accounts SET is_active=? WHERE id=?", (new, aid))
    queries.log(config.ADMIN_USER, "account_toggle", "account", aid)
    return RedirectResponse(
        "/settings?ok=" + quote(("已启用 " if new else "已停用 ") + row["email"]),
        status_code=303)


@router.post("/settings/account/{aid}/delete")
def account_delete(aid: int, request: Request, csrf_tok: str = Form("")):
    if not check_csrf(request, csrf_tok):
        return RedirectResponse("/settings?err=" + quote("请求校验失败"), status_code=303)
    with db.ro() as conn:
        row = conn.execute("SELECT email FROM accounts WHERE id=?", (aid,)).fetchone()
        if not row:
            return RedirectResponse("/settings?err=" + quote("账户不存在"), status_code=303)
        n = conn.execute("SELECT COUNT(*) FROM messages WHERE account_id=?",
                         (aid,)).fetchone()[0]
    with db.tx() as conn:
        conn.execute("DELETE FROM accounts WHERE id=?", (aid,))
    queries.log(config.ADMIN_USER, "account_delete", "account", aid,
                detail=f"{row['email']} 连同 {n} 封邮件")
    return RedirectResponse(
        "/settings?ok=" + quote(f"已删除 {row['email']}（含 {n} 封邮件）"), status_code=303)


@router.post("/settings/account/{aid}/test")
def account_test_one(aid: int, request: Request, csrf_tok: str = Form("")):
    if not check_csrf(request, csrf_tok):
        return RedirectResponse("/settings?err=" + quote("请求校验失败"), status_code=303)
    with db.ro() as conn:
        acct = conn.execute("SELECT * FROM accounts WHERE id=?", (aid,)).fetchone()
    if not acct:
        return RedirectResponse("/settings?err=" + quote("账户不存在"), status_code=303)
    res = _probe_account(dict(acct))
    return RedirectResponse("/settings?test=" + quote(res), status_code=303)


@router.post("/settings/account/test")
def account_test(request: Request, csrf_tok: str = Form(""),
                 account_id: str = Form("")):
    if not check_csrf(request, csrf_tok):
        return RedirectResponse("/settings?err=" + quote("请求校验失败"), status_code=303)
    with db.ro() as conn:
        if account_id.strip().isdigit():
            acct = conn.execute("SELECT * FROM accounts WHERE id=?",
                                (int(account_id),)).fetchone()
        else:
            acct = conn.execute(
                "SELECT * FROM accounts WHERE is_active=1 ORDER BY id LIMIT 1"
            ).fetchone()
    if not acct:
        return RedirectResponse("/settings?test=" + quote("尚未配置账户"), status_code=303)
    res = _probe_account(dict(acct))
    return RedirectResponse("/settings?test=" + quote(res), status_code=303)


@router.get("/review", response_class=HTMLResponse)
def review_page(request: Request, ok: str = "", err: str = ""):
    queries.clean_invalid_pending_drafts()
    drafts = queries.pending_drafts()
    return HTMLResponse(render(
        "review.html", drafts=drafts, ok=ok, err=err, high=config.HIGH_INTENT,
        csrf=auth.csrf_token(request.cookies.get(COOKIE, ""))))


@router.post("/review/{did}")
def review_action(request: Request, did: int, action: str = Form(""),
                  final_text: str = Form(""), csrf_tok: str = Form("")):
    referer = request.headers.get("referer", "")
    with db.ro() as conn:
        d = conn.execute("SELECT * FROM drafts WHERE id=?", (did,)).fetchone()
        m = conn.execute("SELECT * FROM messages WHERE id=?",
                         (d["message_id"],)).fetchone() if d else None
        acct = None
        if m and m["account_id"]:
            acct = conn.execute("SELECT * FROM accounts WHERE id=?", (m["account_id"],)).fetchone()
        if not acct:
            acct = conn.execute("SELECT * FROM accounts WHERE is_active=1 ORDER BY id LIMIT 1").fetchone()
        if not acct:
            acct = conn.execute("SELECT * FROM accounts ORDER BY id LIMIT 1").fetchone()

    back_base = f"/messages/{d['message_id']}" if (d and "/messages/" in referer) else "/review"

    if not check_csrf(request, csrf_tok):
        return RedirectResponse(f"{back_base}?err=" + quote("请求校验失败，请重试"), status_code=303)

    if not d:
        return RedirectResponse(f"{back_base}?err=" + quote("草稿不存在"), status_code=303)
    if not m:
        return RedirectResponse(f"{back_base}?err=" + quote("关联的原邮件记录不存在"), status_code=303)

    body = (final_text or "").strip() or d["body_text"]

    if action == "reject":
        with db.tx() as conn:
            conn.execute("UPDATE drafts SET status='rejected',reviewed_ts=?,"
                         "reviewed_by=? WHERE id=?",
                         (db.now_ts(), config.ADMIN_USER, did))
            conn.execute("UPDATE messages SET status='rejected' WHERE id=?",
                         (d["message_id"],))
        queries.log(config.ADMIN_USER, "draft_reject", "draft", did)
        return RedirectResponse(f"{back_base}?ok=" + quote("已否决该草稿"), status_code=303)

    if action == "approve":
        with db.tx() as conn:
            conn.execute("UPDATE drafts SET status='approved',final_text=?,"
                         "reviewed_ts=?,reviewed_by=? WHERE id=?",
                         (body, db.now_ts(), config.ADMIN_USER, did))
            conn.execute("UPDATE messages SET status='approved' WHERE id=?",
                         (d["message_id"],))
        queries.log(config.ADMIN_USER, "draft_approve", "draft", did)
        return RedirectResponse(f"{back_base}?ok=" + quote("已通过，待发送"), status_code=303)

    if action == "send":
        if not acct:
            return RedirectResponse(f"{back_base}?err=" + quote("未配置邮箱账户或没有已启用的发件邮箱"), status_code=303)
        if not m["from_addr"]:
            return RedirectResponse(f"{back_base}?err=" + quote("原邮件发件人地址为空，无法发信"), status_code=303)

        try:
            ok, info = sender.send_mail(
                dict(acct), m["from_addr"], d["subject"], body,
                reply_to_msgid=m["msgid"], references=m["refs"])
            if ok:
                from .. import mail_engine
                mail_engine.record_outgoing(acct["id"], m["contact_id"],
                                            m["thread_id"], m["from_addr"],
                                            d["subject"], body, info)
                with db.tx() as conn:
                    conn.execute("UPDATE drafts SET status='sent',final_text=?,"
                                 "reviewed_ts=?,reviewed_by=?,sent_ts=? WHERE id=?",
                                 (body, db.now_ts(), config.ADMIN_USER, db.now_ts(), did))
                    conn.execute("UPDATE messages SET status='auto_sent' WHERE id=?",
                                 (d["message_id"],))
                queries.log(config.ADMIN_USER, "draft_send", "draft", did,
                            detail=f"to={m['from_addr']}")
                return RedirectResponse(f"{back_base}?ok=" + quote("邮件已成功发送"), status_code=303)
            with db.tx() as conn:
                conn.execute("UPDATE drafts SET send_error=? WHERE id=?", (info, did))
            queries.log(config.ADMIN_USER, "draft_send_fail", "draft", did, detail=info)
            return RedirectResponse(f"{back_base}?err=" + quote(f"发送失败: {info}"), status_code=303)
        except Exception as exc:
            err_msg = f"发送邮件异常: {type(exc).__name__}: {exc}"
            with db.tx() as conn:
                conn.execute("UPDATE drafts SET send_error=? WHERE id=?", (err_msg, did))
            queries.log(config.ADMIN_USER, "draft_send_exception", "draft", did, detail=err_msg)
            return RedirectResponse(f"{back_base}?err=" + quote(err_msg), status_code=303)

    return RedirectResponse(f"{back_base}?err=" + quote("未知操作"), status_code=303)


# ============ 模板 ============

@router.get("/templates", response_class=HTMLResponse)
def templates_page(request: Request, ok: str = ""):
    return HTMLResponse(render(
        "templates.html", items=queries.list_templates(), ok=ok,
        csrf=auth.csrf_token(request.cookies.get(COOKIE, ""))))


@router.post("/templates/{tid}")
def template_save(request: Request, tid: int, name: str = Form(""),
                  subject: str = Form(""), body: str = Form(""),
                  ai_hint: str = Form(""), is_active: str = Form("0"),
                  csrf_tok: str = Form("")):
    if not check_csrf(request, csrf_tok):
        return RedirectResponse("/templates?err=1", status_code=303)
    with db.tx() as conn:
        conn.execute("UPDATE templates SET name=?,subject=?,body=?,ai_hint=?,"
                     "is_active=? WHERE id=?",
                     (name, subject, body, ai_hint,
                      1 if is_active == "1" else 0, tid))
    queries.log(config.ADMIN_USER, "template_save", "template", tid)
    return RedirectResponse("/templates?ok=" + quote("已保存"), status_code=303)


# ============ 规则 ============

@router.get("/rules", response_class=HTMLResponse)
def rules_page(request: Request, ok: str = ""):
    rs = queries.list_rules()
    for r in rs:
        try:
            r["cond_pretty"] = json.dumps(json.loads(r["cond_json"] or "{}"),
                                          ensure_ascii=False, indent=2)
        except json.JSONDecodeError:
            r["cond_pretty"] = r["cond_json"]
    return HTMLResponse(render(
        "rules.html", items=rs, ok=ok, settings=queries.list_settings(),
        csrf=auth.csrf_token(request.cookies.get(COOKIE, ""))))


@router.post("/rules/{rid}/toggle")
def rule_toggle(request: Request, rid: int, csrf_tok: str = Form("")):
    if not check_csrf(request, csrf_tok):
        return RedirectResponse("/rules?err=1", status_code=303)
    with db.tx() as conn:
        conn.execute("UPDATE rules SET is_active=1-is_active WHERE id=?", (rid,))
    queries.log(config.ADMIN_USER, "rule_toggle", "rule", rid)
    return RedirectResponse("/rules?ok=" + quote("已切换"), status_code=303)


@router.post("/rules/{rid}/cond")
def rule_cond(request: Request, rid: int, cond_json: str = Form("{}"),
              csrf_tok: str = Form("")):
    if not check_csrf(request, csrf_tok):
        return RedirectResponse("/rules?err=1", status_code=303)
    try:
        json.loads(cond_json)
    except json.JSONDecodeError as e:
        return RedirectResponse("/rules?err=" + quote(f"JSON 格式错误: {e}"), status_code=303)
    with db.tx() as conn:
        conn.execute("UPDATE rules SET cond_json=? WHERE id=?",
                     (cond_json.strip(), rid))
    queries.log(config.ADMIN_USER, "rule_cond", "rule", rid)
    return RedirectResponse("/rules?ok=" + quote("条件已更新"), status_code=303)
