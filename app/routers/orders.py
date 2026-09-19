"""外贸订单全生命周期路由。"""
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import auth, config, queries
from ..templating import render

router = APIRouter()

COOKIE = "mb_sess"


def check_csrf(request: Request, token: str) -> bool:
    sess = request.cookies.get(COOKIE, "")
    return bool(token) and token == auth.csrf_token(sess)


@router.get("/orders", response_class=HTMLResponse)
def orders_page(request: Request, view: str = "kanban",
                stage: str = "", cid: str = "", q: str = "",
                ok: str = "", err: str = ""):
    if view not in ("kanban", "list"):
        view = "kanban"
    cid_val = int(cid) if cid and cid.isdigit() else None
    kanban_data, orders_list = {}, []
    if view == "kanban":
        kanban_data = queries.get_orders_kanban()
    else:
        orders_list = queries.list_orders(stage=stage, contact_id=cid_val, q=q)
    all_contacts = queries.list_contacts(size=1000).get("items", [])
    all_quotes = queries.list_freight_quotes()
    return HTMLResponse(render(
        "orders.html", view=view, kanban=kanban_data, orders=orders_list,
        contacts=all_contacts, quotes=all_quotes,
        f={"stage": stage, "cid": cid_val, "q": q},
        ok=ok, err=err,
        csrf=auth.csrf_token(request.cookies.get(COOKIE, ""))
    ))


@router.get("/orders/{oid}", response_class=HTMLResponse)
def order_detail_page(request: Request, oid: int, ok: str = "", err: str = ""):
    od = queries.get_order(oid)
    if not od:
        return HTMLResponse(render("error.html", msg="订单不存在"), status_code=404)
    stages_list = list(queries.ORDER_STAGES.values())
    all_quotes = queries.list_freight_quotes()
    return HTMLResponse(render(
        "order_detail.html", page="orders",
        order=od, stages_list=stages_list, quotes=all_quotes,
        ok=ok, err=err,
        csrf=auth.csrf_token(request.cookies.get(COOKIE, ""))
    ))


@router.post("/orders/save")
def order_save(request: Request, id: str = Form(""),
               contact_id: str = Form(""), order_no: str = Form(""),
               title: str = Form(""), stage: str = Form("draft"),
               trade_term: str = Form("FOB"), payment_term: str = Form(""),
               currency: str = Form("USD"), total_amount: str = Form("0"),
               deposit_amount: str = Form("0"), balance_amount: str = Form("0"),
               settlement_fx_rate: str = Form("7.20"), factory_cost: str = Form("0"),
               shipping_cost: str = Form("0"), other_cost: str = Form("0"),
               est_delivery_date: str = Form(""), actual_delivery_date: str = Form(""),
               tracking_or_bl_no: str = Form(""), destination_port: str = Form(""),
               forwarder_quote_id: str = Form(""), note: str = Form(""),
               csrf_tok: str = Form("")):
    if not check_csrf(request, csrf_tok):
        target = f"/orders/{id}?err=" if id else "/orders?err="
        return RedirectResponse(target + quote("请求校验失败，请重试"), status_code=303)

    ok, msg, oid = queries.save_order({
        "id": id, "contact_id": contact_id, "order_no": order_no, "title": title,
        "stage": stage, "trade_term": trade_term, "payment_term": payment_term,
        "currency": currency, "total_amount": total_amount,
        "deposit_amount": deposit_amount, "balance_amount": balance_amount,
        "settlement_fx_rate": settlement_fx_rate, "factory_cost": factory_cost,
        "shipping_cost": shipping_cost, "other_cost": other_cost,
        "est_delivery_date": est_delivery_date,
        "actual_delivery_date": actual_delivery_date,
        "tracking_or_bl_no": tracking_or_bl_no, "destination_port": destination_port,
        "forwarder_quote_id": forwarder_quote_id, "note": note,
    }, operator=config.ADMIN_USER)

    if not ok:
        target = f"/orders/{id}?err=" if id else "/orders?err="
        return RedirectResponse(target + quote(msg), status_code=303)

    queries.log(config.ADMIN_USER, "order_save", "order", oid,
                detail=f"{order_no or oid} {title}")
    return RedirectResponse(f"/orders/{oid}?ok=" + quote(msg), status_code=303)


@router.post("/orders/{oid}/stage")
def order_stage_advance(request: Request, oid: int, stage: str = Form(""),
                        note: str = Form(""), csrf_tok: str = Form("")):
    if not check_csrf(request, csrf_tok):
        return RedirectResponse(f"/orders/{oid}?err=" + quote("请求校验失败"), status_code=303)
    ok, msg = queries.advance_order_stage(oid, stage, note=note, operator=config.ADMIN_USER)
    if not ok:
        return RedirectResponse(f"/orders/{oid}?err=" + quote(msg), status_code=303)
    queries.log(config.ADMIN_USER, "order_stage", "order", oid,
                detail=f"stage={stage} note={note}")
    referer = request.headers.get("referer") or f"/orders/{oid}"
    clean_referer = referer.split("?ok=")[0].split("&ok=")[0]
    delimiter = "&" if "?" in clean_referer else "?"
    return RedirectResponse(f"{clean_referer}{delimiter}ok=" + quote(msg), status_code=303)


@router.post("/orders/{oid}/finance")
def order_finance_update(request: Request, oid: int,
                         deposit_amount: str = Form("0"),
                         balance_amount: str = Form("0"),
                         factory_cost: str = Form("0"),
                         shipping_cost: str = Form("0"),
                         other_cost: str = Form("0"),
                         settlement_fx_rate: str = Form("7.20"),
                         tracking_or_bl_no: str = Form(""),
                         note: str = Form(""),
                         csrf_tok: str = Form("")):
    if not check_csrf(request, csrf_tok):
        return RedirectResponse(f"/orders/{oid}?err=" + quote("请求校验失败"), status_code=303)
    ok, msg = queries.update_order_finance(oid, {
        "deposit_amount": deposit_amount, "balance_amount": balance_amount,
        "factory_cost": factory_cost, "shipping_cost": shipping_cost,
        "other_cost": other_cost, "settlement_fx_rate": settlement_fx_rate,
        "tracking_or_bl_no": tracking_or_bl_no, "note": note,
    }, operator=config.ADMIN_USER)
    if not ok:
        return RedirectResponse(f"/orders/{oid}?err=" + quote(msg), status_code=303)
    queries.log(config.ADMIN_USER, "order_finance", "order", oid, detail=note)
    return RedirectResponse(f"/orders/{oid}?ok=" + quote(msg), status_code=303)


@router.post("/orders/{oid}/delete")
def order_delete(request: Request, oid: int, csrf_tok: str = Form("")):
    if not check_csrf(request, csrf_tok):
        return RedirectResponse("/orders?err=" + quote("请求校验失败"), status_code=303)
    queries.delete_order(oid)
    queries.log(config.ADMIN_USER, "order_delete", "order", oid)
    return RedirectResponse("/orders?ok=" + quote("已删除订单"), status_code=303)


@router.get("/api/orders/{oid}")
def api_order_detail(oid: int):
    od = queries.get_order(oid)
    if not od:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(od)


# ============ 订单明细品项 ============

@router.post("/orders/{oid}/items/save")
def order_item_save(request: Request, oid: int, id: str = Form(""),
                    product_id: str = Form(""), item_no: str = Form(""),
                    sku: str = Form(""), name_en: str = Form(""),
                    name_cn: str = Form(""), hs_code: str = Form(""),
                    specs: str = Form(""), unit: str = Form("PCS"),
                    quantity: str = Form("1"), unit_price: str = Form("0"),
                    unit_cost_cny: str = Form("0"), cartons: str = Form("0"),
                    net_weight_kg: str = Form("0"), gross_weight_kg: str = Form("0"),
                    cbm: str = Form("0"), auto_sync: str = Form("0"),
                    csrf_tok: str = Form("")):
    if not check_csrf(request, csrf_tok):
        return RedirectResponse(f"/orders/{oid}?err=" + quote("请求校验失败"), status_code=303)
    data = {
        "id": id, "product_id": product_id, "item_no": item_no,
        "sku": sku, "name_en": name_en, "name_cn": name_cn,
        "hs_code": hs_code, "specs": specs, "unit": unit,
        "quantity": quantity, "unit_price": unit_price,
        "unit_cost_cny": unit_cost_cny, "cartons": cartons,
        "net_weight_kg": net_weight_kg, "gross_weight_kg": gross_weight_kg, "cbm": cbm
    }
    ok, msg, iid = queries.save_order_item(oid, data)
    if not ok:
        return RedirectResponse(f"/orders/{oid}?err=" + quote(msg), status_code=303)
    if auto_sync == "1":
        queries.sync_order_items_totals(oid)
    queries.log(config.ADMIN_USER, "order_item_save", "order", oid,
                detail=f"item={name_en}")
    return RedirectResponse(f"/orders/{oid}?ok=" + quote(msg), status_code=303)


@router.post("/orders/{oid}/items/{iid}/delete")
def order_item_delete(request: Request, oid: int, iid: int, csrf_tok: str = Form("")):
    if not check_csrf(request, csrf_tok):
        return RedirectResponse(f"/orders/{oid}?err=" + quote("请求校验失败"), status_code=303)
    queries.delete_order_item(oid, iid)
    queries.log(config.ADMIN_USER, "order_item_delete", "order", oid,
                detail=f"item_id={iid}")
    return RedirectResponse(f"/orders/{oid}?ok=" + quote("已删除品项"), status_code=303)


@router.post("/orders/{oid}/items/sync")
def order_items_sync(request: Request, oid: int, csrf_tok: str = Form("")):
    if not check_csrf(request, csrf_tok):
        return RedirectResponse(f"/orders/{oid}?err=" + quote("请求校验失败"), status_code=303)
    ok, msg = queries.sync_order_items_totals(oid)
    if not ok:
        return RedirectResponse(f"/orders/{oid}?err=" + quote(msg), status_code=303)
    queries.log(config.ADMIN_USER, "order_items_sync", "order", oid)
    return RedirectResponse(f"/orders/{oid}?ok=" + quote(msg), status_code=303)


# ============ 外贸三大单证 ============

@router.get("/orders/{oid}/docs/{doc_type}", response_class=HTMLResponse)
def order_document_view(request: Request, oid: int, doc_type: str = "pi"):
    """外贸单证（PI / CI / PL）专属排版与 A4 打印预览。"""
    data = queries.get_order_document_data(oid, doc_type)
    if not data:
        return HTMLResponse(render("error.html", msg="订单不存在或数据无法生成单证"), status_code=404)
    return HTMLResponse(render(
        "document.html", page="orders", **data,
        csrf=auth.csrf_token(request.cookies.get(COOKIE, ""))
    ))
