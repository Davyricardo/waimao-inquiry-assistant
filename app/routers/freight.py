"""货运代理与运费比价路由。"""
import time
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


@router.get("/freight", response_class=HTMLResponse)
def freight_page(request: Request, tab: str = "calc",
                 dest: str = "", channel: str = "",
                 weight: str = "", vol: str = "",
                 fid: str = "", q: str = "",
                 ok: str = "", err: str = ""):
    """货运代理、报价库与成本对比测算综合页面。"""
    if tab not in ("calc", "quotes", "forwarders"):
        tab = "calc"

    forwarders = queries.list_forwarders()
    total_quotes = len(queries.list_freight_quotes())

    try:
        w_val = float(weight) if weight and weight.strip() else 0.0
    except ValueError:
        w_val = 0.0
    try:
        v_val = float(vol) if vol and vol.strip() else 0.0
    except ValueError:
        v_val = 0.0

    fid_val = int(fid) if fid and fid.isdigit() else None
    calc_res, quotes_list, forwarders_list = {}, [], forwarders

    if tab == "calc":
        calc_res = queries.compare_shipping_costs(
            destination=dest, channel_type=channel,
            weight_kg=w_val, volume_cbm=v_val)
    elif tab == "quotes":
        quotes_list = queries.list_freight_quotes(
            forwarder_id=fid_val, channel_type=channel,
            destination=dest, q=q)
    elif tab == "forwarders":
        if q:
            forwarders_list = queries.list_forwarders(q=q)

    return HTMLResponse(render(
        "freight.html",
        active_tab=tab, calc=calc_res, quotes=quotes_list,
        forwarders=forwarders_list, total_forwarders=len(forwarders),
        total_quotes=total_quotes,
        f={"dest": dest, "channel": channel, "weight": weight, "vol": vol,
           "fid": fid_val, "q": q},
        ok=ok, err=err,
        csrf=auth.csrf_token(request.cookies.get(COOKIE, ""))
    ))


@router.post("/freight/forwarder/save")
def freight_forwarder_save(request: Request, id: str = Form(""),
                           name: str = Form(""), contact_person: str = Form(""),
                           phone: str = Form(""), email: str = Form(""),
                           wechat_or_im: str = Form(""), advantages: str = Form(""),
                           rating: str = Form("5"), address: str = Form(""),
                           note: str = Form(""), csrf_tok: str = Form("")):
    """新增或更新货运代理信息。"""
    if not check_csrf(request, csrf_tok):
        return RedirectResponse("/freight?tab=forwarders&err=" + quote("请求校验失败，请重试"), status_code=303)

    ok, msg, fid = queries.save_forwarder({
        "id": id, "name": name, "contact_person": contact_person,
        "phone": phone, "email": email, "wechat_or_im": wechat_or_im,
        "advantages": advantages, "rating": rating, "address": address, "note": note,
    })
    if not ok:
        return RedirectResponse("/freight?tab=forwarders&err=" + quote(msg), status_code=303)

    queries.log(config.ADMIN_USER, "forwarder_save", "forwarder", fid, detail=name.strip())
    return RedirectResponse("/freight?tab=forwarders&ok=" + quote(msg), status_code=303)


@router.post("/freight/forwarder/{fid}/delete")
def freight_forwarder_delete(request: Request, fid: int, csrf_tok: str = Form("")):
    if not check_csrf(request, csrf_tok):
        return RedirectResponse("/freight?tab=forwarders&err=" + quote("请求校验失败"), status_code=303)
    queries.delete_forwarder(fid)
    queries.log(config.ADMIN_USER, "forwarder_delete", "forwarder", fid)
    return RedirectResponse("/freight?tab=forwarders&ok=" + quote("已删除货代及其全部报价"), status_code=303)


@router.post("/freight/quote/save")
def freight_quote_save(request: Request, id: str = Form(""),
                       forwarder_id: str = Form(""), title: str = Form(""),
                       channel_type: str = Form("sea"), origin: str = Form("深圳"),
                       destination: str = Form(""), unit_price: str = Form("0"),
                       price_unit: str = Form("kg"), currency: str = Form("CNY"),
                       min_charge: str = Form(""), transit_time_text: str = Form(""),
                       valid_until: str = Form(""), extra_fees: str = Form(""),
                       remarks: str = Form(""), csrf_tok: str = Form("")):
    if not check_csrf(request, csrf_tok):
        return RedirectResponse("/freight?tab=quotes&err=" + quote("请求校验失败，请重试"), status_code=303)

    ok, msg, qid = queries.save_freight_quote({
        "id": id, "forwarder_id": forwarder_id, "title": title,
        "channel_type": channel_type, "origin": origin, "destination": destination,
        "unit_price": unit_price, "price_unit": price_unit, "currency": currency,
        "min_charge": min_charge, "transit_time_text": transit_time_text,
        "valid_until": valid_until, "extra_fees": extra_fees, "remarks": remarks,
    })
    if not ok:
        return RedirectResponse("/freight?tab=quotes&err=" + quote(msg), status_code=303)

    queries.log(config.ADMIN_USER, "freight_quote_save", "quote", qid,
                detail=f"{title} ({destination})")
    return RedirectResponse("/freight?tab=quotes&ok=" + quote(msg), status_code=303)


@router.post("/freight/quote/{qid}/delete")
def freight_quote_delete(request: Request, qid: int, csrf_tok: str = Form("")):
    if not check_csrf(request, csrf_tok):
        return RedirectResponse("/freight?tab=quotes&err=" + quote("请求校验失败"), status_code=303)
    queries.delete_freight_quote(qid)
    queries.log(config.ADMIN_USER, "freight_quote_delete", "quote", qid)
    return RedirectResponse("/freight?tab=quotes&ok=" + quote("已删除该报价方案"), status_code=303)


@router.get("/freight/export")
def freight_export(request: Request):
    content = queries.export_freight_quotes_csv()
    filename = f"外贸询盘助手_freight_quotes_{time.strftime('%Y%m%d_%H%M%S')}.csv"
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8-sig",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-cache",
        }
    )


@router.get("/api/freight/forwarder/{fid}")
def api_freight_forwarder(fid: int):
    fwd = queries.get_forwarder(fid)
    if not fwd:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(fwd)


@router.get("/api/freight/quote/{qid}")
def api_freight_quote(qid: int):
    q = queries.get_freight_quote(qid)
    if not q:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(q)
