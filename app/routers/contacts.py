"""客户管理路由。"""
import time
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .. import ai_profile, auth, config, db, queries
from ..templating import render

router = APIRouter()

COOKIE = "mb_sess"


def check_csrf(request: Request, token: str) -> bool:
    sess = request.cookies.get(COOKIE, "")
    return bool(token) and token == auth.csrf_token(sess)


@router.get("/contacts", response_class=HTMLResponse)
def contacts_page(request: Request, page: int = 1, q: str = "",
                  stage: str = "", min_score: str = "", ok: str = "", err: str = ""):
    ms = None
    try:
        ms = int(min_score) if min_score not in ("", None) else None
    except ValueError:
        ms = None
    data = queries.list_contacts(page=page, q=q, stage=stage, min_score=ms)
    return HTMLResponse(render(
        "contacts.html", data=data, ok=ok, err=err,
        f={"q": q, "stage": stage, "min_score": min_score},
        csrf=auth.csrf_token(request.cookies.get(COOKIE, ""))))


@router.get("/contacts/export")
def contacts_export(request: Request):
    """导出客户名录为 CSV 文件。"""
    content = queries.export_contacts_csv()
    filename = f"外贸询盘助手_contacts_{time.strftime('%Y%m%d_%H%M%S')}.csv"
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8-sig",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-cache",
        }
    )


@router.post("/contacts/save")
def contact_save(request: Request, contact_id: str = Form(""),
                 email: str = Form(""), name: str = Form(""),
                 company: str = Form(""), country: str = Form(""),
                 stage: str = Form("new"), company_type: str = Form(""),
                 channel_role: str = Form(""), credibility: str = Form(""),
                 note: str = Form(""), csrf_tok: str = Form("")):
    """新增或保存客户信息。"""
    if not check_csrf(request, csrf_tok):
        return RedirectResponse("/contacts?err=" + quote("请求校验失败，请重试"), status_code=303)

    ok, msg, cid = queries.save_contact({
        "id": contact_id, "email": email, "name": name, "company": company,
        "country": country, "stage": stage, "company_type": company_type,
        "channel_role": channel_role, "credibility": credibility, "note": note,
    })

    if not ok:
        target = f"/contacts/{contact_id}?err=" if contact_id else "/contacts?err="
        return RedirectResponse(target + quote(msg), status_code=303)

    queries.log(config.ADMIN_USER, "contact_save", "contact", cid, detail=f"email={email.strip()}")
    if contact_id:
        return RedirectResponse(f"/contacts/{cid}?ok=" + quote(msg), status_code=303)
    return RedirectResponse("/contacts?ok=" + quote(msg), status_code=303)


@router.post("/contacts/{cid}/stage")
def contact_stage_update(request: Request, cid: int, stage: str = Form(""),
                         csrf_tok: str = Form("")):
    """快捷修改客户阶段。"""
    if not check_csrf(request, csrf_tok):
        return RedirectResponse(f"/contacts/{cid}?err=" + quote("请求校验失败"), status_code=303)
    if queries.update_contact_stage(cid, stage):
        queries.log(config.ADMIN_USER, "contact_stage", "contact", cid, detail=f"stage={stage}")
        return RedirectResponse(f"/contacts/{cid}?ok=" + quote(f"已更新阶段为：{stage}"), status_code=303)
    return RedirectResponse(f"/contacts/{cid}?err=" + quote("无效的客户阶段"), status_code=303)


@router.post("/contacts/{cid}/delete")
def contact_delete_one(request: Request, cid: int, csrf_tok: str = Form("")):
    """删除指定客户及关联会话与邮件。"""
    if not check_csrf(request, csrf_tok):
        return RedirectResponse("/contacts?err=" + quote("请求校验失败"), status_code=303)
    with db.ro() as conn:
        c = conn.execute("SELECT email FROM contacts WHERE id=?", (cid,)).fetchone()
    email_label = c["email"] if c else str(cid)
    queries.delete_contact(cid)
    queries.log(config.ADMIN_USER, "contact_delete", "contact", cid, detail=email_label)
    return RedirectResponse("/contacts?ok=" + quote(f"已彻底删除客户 {email_label}"), status_code=303)


@router.post("/contacts/clear")
def contacts_clear_all(request: Request, confirm: str = Form(""),
                       csrf_tok: str = Form("")):
    """清空全部客户与关联数据。"""
    if not check_csrf(request, csrf_tok):
        return RedirectResponse("/contacts?err=" + quote("请求校验失败"), status_code=303)
    if confirm.strip() != "CLEAR":
        return RedirectResponse("/contacts?err=" + quote("请输入正确的确认文本 CLEAR 后再提交"), status_code=303)
    res = queries.clear_all_contacts()
    count = res.get("contacts_cleared", 0)
    queries.log(config.ADMIN_USER, "contacts_clear_all", detail=f"cleared={count}")
    return RedirectResponse("/contacts?ok=" + quote(f"已清空全部客户数据（共清理 {count} 条客户记录及相关会话）"), status_code=303)


@router.get("/contacts/{cid}", response_class=HTMLResponse)
def contact_detail(request: Request, cid: int, ok: str = "", err: str = ""):
    c = queries.get_contact(cid)
    if not c:
        return HTMLResponse(render("error.html", msg="客户不存在"), status_code=404)
    return HTMLResponse(render(
        "contact_detail.html", page="contacts",
        c=c, ok=ok, err=err, high=config.HIGH_INTENT,
        csrf=auth.csrf_token(request.cookies.get(COOKIE, ""))))


@router.post("/contacts/{cid}/profile")
def contact_profile(request: Request, cid: int, csrf_tok: str = Form("")):
    if not check_csrf(request, csrf_tok):
        return RedirectResponse(f"/contacts/{cid}?err=1", status_code=303)
    with db.ro() as conn:
        c = conn.execute("SELECT * FROM contacts WHERE id=?", (cid,)).fetchone()
        m = conn.execute(
            "SELECT * FROM messages WHERE contact_id=? AND direction='in' "
            "ORDER BY sent_ts DESC LIMIT 1", (cid,)).fetchone()
    if not c:
        return RedirectResponse("/contacts?err=" + quote("客户不存在"), status_code=303)
    if not m:
        return RedirectResponse(f"/contacts/{cid}?err=" + quote("该客户还没有来信，无法分析"), status_code=303)
    try:
        res = ai_profile.ai_profile(
            from_addr=c["email"] or "", name=c["name"] or "",
            company=c["company"] or "", country_hint=c["country"] or "",
            body=m["body_text"] or "", subject=m["subject"] or "")
        country, background = ai_profile.format_profile_for_db(res)
        with db.tx() as conn:
            conn.execute(
                "UPDATE contacts SET country=COALESCE(NULLIF(?,''),country),"
                "background=?,company_type=?,channel_role=?,credibility=?,"
                "profiled_ts=? WHERE id=?",
                (country, background, res.get("company_type"),
                 res.get("channel_role"), res.get("credibility"),
                 db.now_ts(), cid))
    except Exception as e:
        return RedirectResponse(f"/contacts/{cid}?err=" + quote(
            f"分析失败：{type(e).__name__}"), status_code=303)
    queries.log(config.ADMIN_USER, "contact_profile", "contact", cid,
                detail=res.get("source", ""))
    src = "AI 分析" if res.get("source") == "ai" else "规则推断"
    return RedirectResponse(f"/contacts/{cid}?ok=" + quote(f"已重新分析（{src}）"),
                            status_code=303)
