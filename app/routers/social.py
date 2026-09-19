"""社媒外贸获客开发路由。"""
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .. import auth, config, queries
from ..templating import render

router = APIRouter()

COOKIE = "mb_sess"


def check_csrf(request: Request, token: str) -> bool:
    sess = request.cookies.get(COOKIE, "")
    return bool(token) and token == auth.csrf_token(sess)


@router.get("/social", response_class=HTMLResponse)
def social_leads_page(request: Request, view: str = "kanban",
                      platform: str = "", stage: str = "",
                      followup_status: str = "", search: str = "",
                      ok: str = "", err: str = ""):
    """社媒外贸潜客线索库首页（支持看板与列表视图）。"""
    summary = queries.get_social_leads_funnel_summary()
    leads = queries.list_social_leads(
        platform=platform or None, stage=stage or None,
        followup_status=followup_status or None, search=search or None,
    )
    kanban_columns = {}
    for s_key, s_info in queries.LEAD_STAGES.items():
        col_leads = [l for l in leads if l.get("stage") == s_key]
        kanban_columns[s_key] = {
            "stage_info": s_info, "count": len(col_leads), "leads": col_leads,
        }
    return HTMLResponse(render(
        "social_leads.html", page="social",
        view_mode=view, summary=summary, leads=leads,
        kanban_columns=kanban_columns, current_platform=platform,
        current_stage=stage, current_followup=followup_status,
        search_kw=search, today_str=queries._today(), ok=ok, err=err,
        csrf=auth.csrf_token(request.cookies.get(COOKIE, "")),
    ))


@router.get("/social/export")
def social_leads_export(request: Request, platform: str = "", stage: str = ""):
    leads = queries.list_social_leads(platform=platform or None, stage=stage or None)
    csv_text = queries.export_social_leads_csv(leads)
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=social_leads.csv"}
    )


@router.get("/social/{lid}", response_class=HTMLResponse)
def social_lead_detail_page(request: Request, lid: int, ok: str = "", err: str = ""):
    """潜客详情与触达中心。"""
    lead = queries.get_social_lead(lid)
    if not lead:
        return RedirectResponse("/social?err=" + quote("潜客线索不存在"), status_code=303)
    products = queries.list_products(is_active=1)
    return HTMLResponse(render(
        "social_lead_detail.html", page="social",
        lead=lead, products=products,
        today_str=queries._today(), ok=ok, err=err,
        csrf=auth.csrf_token(request.cookies.get(COOKIE, "")),
    ))


@router.post("/social/save")
def social_lead_save(request: Request, lead_id: str = Form(""),
                     name: str = Form(""), company: str = Form(""),
                     country: str = Form(""), position: str = Form(""),
                     source_platform: str = Form("linkedin"),
                     whatsapp: str = Form(""), linkedin_url: str = Form(""),
                     social_handle: str = Form(""), email: str = Form(""),
                     website: str = Form(""), industry_or_niche: str = Form(""),
                     stage: str = Form("discovered"), rating: str = Form("3"),
                     next_followup_date: str = Form(""), notes: str = Form(""),
                     csrf_tok: str = Form("")):
    """新建或编辑社媒潜客。"""
    if not check_csrf(request, csrf_tok):
        return RedirectResponse("/social?err=" + quote("请求校验失败"), status_code=303)
    lid = int(lead_id) if lead_id.strip() and lead_id.isdigit() else None
    data = {
        "name": name, "company": company, "country": country, "position": position,
        "source_platform": source_platform, "whatsapp": whatsapp,
        "linkedin_url": linkedin_url, "social_handle": social_handle,
        "email": email, "website": website, "industry_or_niche": industry_or_niche,
        "stage": stage, "rating": rating, "next_followup_date": next_followup_date,
        "notes": notes,
    }
    ok, msg, saved_id = queries.save_social_lead(data, lead_id=lid)
    if not ok:
        redirect_url = f"/social/{lid}?err=" if lid else "/social?err="
        return RedirectResponse(redirect_url + quote(msg), status_code=303)
    queries.log(config.ADMIN_USER, "social_lead_save", "social_lead",
                saved_id, detail=name)
    return RedirectResponse(f"/social/{saved_id}?ok=" + quote(msg), status_code=303)


@router.post("/social/{lid}/stage")
def social_lead_advance_stage(request: Request, lid: int, stage: str = Form(""),
                               note: str = Form(""), csrf_tok: str = Form("")):
    if not check_csrf(request, csrf_tok):
        return RedirectResponse(f"/social/{lid}?err=" + quote("请求校验失败"), status_code=303)
    ok, msg = queries.advance_social_lead_stage(lid, stage, note=note)
    if not ok:
        return RedirectResponse(f"/social/{lid}?err=" + quote(msg), status_code=303)
    queries.log(config.ADMIN_USER, "social_lead_stage", "social_lead", lid, detail=stage)
    return RedirectResponse(f"/social/{lid}?ok=" + quote(msg), status_code=303)


@router.post("/social/{lid}/touchpoint")
def social_lead_add_touchpoint(request: Request, lid: int,
                                channel: str = Form("whatsapp"),
                                touch_type: str = Form("first_touch"),
                                content: str = Form(""), feedback: str = Form(""),
                                next_action: str = Form(""),
                                csrf_tok: str = Form("")):
    if not check_csrf(request, csrf_tok):
        return RedirectResponse(f"/social/{lid}?err=" + quote("请求校验失败"), status_code=303)
    ok, msg, tpid = queries.save_social_touchpoint(
        lid, channel, touch_type, content, feedback=feedback, next_action=next_action)
    if not ok:
        return RedirectResponse(f"/social/{lid}?err=" + quote(msg), status_code=303)
    queries.log(config.ADMIN_USER, "social_touchpoint_add", "social_touchpoint",
                tpid, detail=touch_type)
    return RedirectResponse(f"/social/{lid}?ok=" + quote(msg), status_code=303)


@router.post("/social/{lid}/convert")
def social_lead_convert(request: Request, lid: int, email: str = Form(""),
                        csrf_tok: str = Form("")):
    """将社媒潜客一键转化为 CRM 正式客户。"""
    if not check_csrf(request, csrf_tok):
        return RedirectResponse(f"/social/{lid}?err=" + quote("请求校验失败"), status_code=303)
    ok, msg, cid = queries.convert_lead_to_contact(lid, email=email)
    if not ok:
        return RedirectResponse(f"/social/{lid}?err=" + quote(msg), status_code=303)
    queries.log(config.ADMIN_USER, "social_lead_convert", "contact", cid, detail=email)
    return RedirectResponse(f"/social/{lid}?ok=" + quote(msg), status_code=303)


@router.post("/social/{lid}/delete")
def social_lead_delete(request: Request, lid: int, csrf_tok: str = Form("")):
    if not check_csrf(request, csrf_tok):
        return RedirectResponse("/social?err=" + quote("请求校验失败"), status_code=303)
    queries.delete_social_lead(lid)
    queries.log(config.ADMIN_USER, "social_lead_delete", "social_lead", lid)
    return RedirectResponse("/social?ok=" + quote("已删除潜客线索"), status_code=303)


@router.post("/api/social/generate-scripts")
async def api_generate_outreach_scripts(request: Request):
    """异步生成多场景多语言破冰话术。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    lid = body.get("lead_id")
    lead = queries.get_social_lead(lid) if lid else None
    if not lead:
        return JSONResponse({"ok": False, "error": "潜客不存在"}, status_code=404)
    prod_id = body.get("product_id")
    product = queries.get_product(prod_id) if prod_id else None
    lang = body.get("lang") or "en"
    custom_pitch = body.get("custom_pitch") or ""
    scripts = queries.generate_outreach_scripts(
        lead, product=product, lang=lang, custom_pitch=custom_pitch)
    return JSONResponse({"ok": True, "scripts": scripts})


@router.get("/api/social/{lid}")
def api_social_lead_detail(lid: int):
    lead = queries.get_social_lead(lid)
    if not lead:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(lead)
