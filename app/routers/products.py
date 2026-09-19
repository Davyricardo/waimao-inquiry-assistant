"""产品库路由。"""
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


@router.get("/products", response_class=HTMLResponse)
def products_page(request: Request, q: str = "", category: str = "",
                  is_active: str = "", ok: str = "", err: str = ""):
    active_val = int(is_active) if is_active in ("0", "1") else None
    prods = queries.list_products(q=q, category=category, is_active=active_val)
    categories = sorted(list({p["category"] for p in queries.list_products()
                               if p.get("category")}))
    if "default" not in categories:
        categories.insert(0, "default")
    return HTMLResponse(render(
        "products.html", products=prods, categories=categories,
        f={"q": q, "category": category, "is_active": is_active},
        ok=ok, err=err,
        csrf=auth.csrf_token(request.cookies.get(COOKIE, ""))
    ))


@router.post("/products/save")
def product_save(request: Request, id: str = Form(""),
                 sku: str = Form(""), name_en: str = Form(""),
                 name_cn: str = Form(""), hs_code: str = Form(""),
                 category: str = Form("default"), specs: str = Form(""),
                 unit: str = Form("PCS"), price_usd: str = Form("0"),
                 cost_cny: str = Form("0"), moq: str = Form("1"),
                 carton_qty: str = Form("1"), carton_length_cm: str = Form("0"),
                 carton_width_cm: str = Form("0"), carton_height_cm: str = Form("0"),
                 carton_cbm: str = Form("0"), carton_gw_kg: str = Form("0"),
                 carton_nw_kg: str = Form("0"), image_url: str = Form(""),
                 note: str = Form(""), is_active: str = Form("1"),
                 csrf_tok: str = Form("")):
    if not check_csrf(request, csrf_tok):
        return RedirectResponse("/products?err=" + quote("请求校验失败"), status_code=303)
    data = {
        "id": id, "sku": sku, "name_en": name_en, "name_cn": name_cn,
        "hs_code": hs_code, "category": category, "specs": specs,
        "unit": unit, "price_usd": price_usd, "cost_cny": cost_cny,
        "moq": moq, "carton_qty": carton_qty, "carton_length_cm": carton_length_cm,
        "carton_width_cm": carton_width_cm, "carton_height_cm": carton_height_cm,
        "carton_cbm": carton_cbm, "carton_gw_kg": carton_gw_kg,
        "carton_nw_kg": carton_nw_kg, "image_url": image_url,
        "note": note, "is_active": is_active
    }
    ok, msg, pid = queries.save_product(data)
    if not ok:
        return RedirectResponse("/products?err=" + quote(msg), status_code=303)
    queries.log(config.ADMIN_USER, "product_save", "product", pid, detail=sku)
    return RedirectResponse("/products?ok=" + quote(msg), status_code=303)


@router.post("/products/{pid}/delete")
def product_delete(request: Request, pid: int, csrf_tok: str = Form("")):
    if not check_csrf(request, csrf_tok):
        return RedirectResponse("/products?err=" + quote("请求校验失败"), status_code=303)
    queries.delete_product(pid)
    queries.log(config.ADMIN_USER, "product_delete", "product", pid)
    return RedirectResponse("/products?ok=" + quote("已删除产品"), status_code=303)


@router.get("/products/export")
def products_export(request: Request):
    csv_text = queries.export_products_csv()
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=products_catalog.csv"}
    )


@router.get("/api/products/{pid}")
def api_product_detail(pid: int):
    p = queries.get_product(pid)
    if not p:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(p)


@router.get("/api/products")
def api_products_list(q: str = ""):
    """返回在售产品列表，供订单品项下拉选择。"""
    prods = queries.list_products(q=q, is_active=1)
    return JSONResponse(prods)
