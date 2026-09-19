"""外贸单证与凭证管理路由 (Vouchers Router)

支持单证台账多维列表、新建录入、本地 OCR 智能识别提取、批量删除/导出/状态归档。
"""
import io
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .. import auth, db, queries
from ..ocr_engine import ocr_image_to_text, parse_voucher_info
from ..templating import render

router = APIRouter()

COOKIE = "mb_sess"


def check_csrf(request: Request, token: str) -> bool:
    sess = request.cookies.get(COOKIE, "")
    return bool(token) and token == auth.csrf_token(sess)


@router.get("/vouchers", response_class=HTMLResponse)
def vouchers_list(
    request: Request,
    q: str = "",
    voucher_type: str = "",
    status: str = "",
    page: int = 1,
    ok: str = "",
    err: str = ""
):
    """凭证与外贸单证列表首页。"""
    data = queries.list_vouchers(q=q, voucher_type=voucher_type, status=status, page=page, page_size=15)
    stats = queries.voucher_stats()

    # 取最近 100 位客户和最近 100 个订单供新建关联选择
    with db.ro() as conn:
        contacts = [dict(r) for r in conn.execute(
            "SELECT id, name, company FROM contacts ORDER BY score DESC, id DESC LIMIT 100"
        ).fetchall()]
        orders = [dict(r) for r in conn.execute(
            "SELECT id, order_no, title FROM orders ORDER BY id DESC LIMIT 100"
        ).fetchall()]

    csrf = auth.csrf_token(request.cookies.get(COOKIE, ""))
    return HTMLResponse(render(
        "vouchers.html",
        page="vouchers",
        data=data,
        stats=stats,
        f={"q": q, "voucher_type": voucher_type, "status": status},
        contacts=contacts,
        orders=orders,
        type_names=queries.VOUCHER_TYPE_NAMES,
        status_names=queries.VOUCHER_STATUS_NAMES,
        ok=ok,
        err=err,
        csrf=csrf
    ))


@router.post("/vouchers/ocr")
async def vouchers_ocr(
    request: Request,
    file: Optional[UploadFile] = File(None),
    raw_text: Optional[str] = Form("")
):
    """本地 OCR 智能识别单证并提取结构化外贸字段 (0 Token 消耗)。"""
    text = (raw_text or "").strip()
    conf = 0.95

    if file and file.filename:
        content = await file.read()
        if content:
            extracted_text, score = ocr_image_to_text(content)
            if extracted_text:
                text = extracted_text
                conf = score

    if not text:
        return JSONResponse({
            "ok": False,
            "msg": "未识别到有效文字，请上传清晰的单证图片或直接粘贴单据文本"
        }, status_code=400)

    parsed = parse_voucher_info(text)
    return JSONResponse({
        "ok": True,
        "data": parsed,
        "text": text,
        "confidence": round(conf, 2),
        "msg": "识别成功"
    })


@router.post("/vouchers")
def vouchers_create(
    request: Request,
    voucher_no: str = Form(""),
    voucher_type: str = Form("commercial_invoice"),
    title: str = Form(""),
    trade_date: str = Form(""),
    currency: str = Form("USD"),
    amount: float = Form(0.0),
    contact_id: Optional[int] = Form(None),
    order_id: Optional[int] = Form(None),
    shipper: str = Form(""),
    consignee: str = Form(""),
    product_desc: str = Form(""),
    ocr_status: str = Form("manual"),
    ocr_raw_text: str = Form(""),
    status: str = Form("confirmed"),
    notes: str = Form(""),
    csrf_tok: str = Form("")
):
    """新建或保存单证凭证记录。"""
    if not check_csrf(request, csrf_tok):
        return RedirectResponse("/vouchers?err=" + quote("安全校验失败，请重试"), status_code=303)

    if not voucher_no.strip():
        return RedirectResponse("/vouchers?err=" + quote("单证编号不可为空"), status_code=303)

    data = {
        "voucher_no": voucher_no,
        "voucher_type": voucher_type,
        "title": title,
        "trade_date": trade_date,
        "currency": currency,
        "amount": amount,
        "contact_id": contact_id,
        "order_id": order_id,
        "shipper": shipper,
        "consignee": consignee,
        "product_desc": product_desc,
        "ocr_status": ocr_status,
        "ocr_raw_text": ocr_raw_text,
        "status": status,
        "notes": notes,
    }
    vid = queries.create_voucher(data)
    queries.log("user", "voucher_create", "voucher", vid, f"录入单证 {voucher_no} ({title})")
    return RedirectResponse(f"/vouchers?ok=" + quote(f"单证凭证 {voucher_no} 录入成功 (ID: {vid})"), status_code=303)


@router.post("/vouchers/batch")
def vouchers_batch(
    request: Request,
    batch_action: str = Form(""),
    vids: list[int] = Form([]),
    target_status: str = Form("confirmed"),
    csrf_tok: str = Form("")
):
    """批量操作：批量删除、批量修改状态、批量导出。"""
    if not check_csrf(request, csrf_tok):
        return RedirectResponse("/vouchers?err=" + quote("安全校验失败，请重试"), status_code=303)

    if not vids:
        return RedirectResponse("/vouchers?err=" + quote("请先勾选需要操作的单证"), status_code=303)

    if batch_action == "delete":
        cnt = queries.batch_delete_vouchers(vids)
        queries.log("user", "voucher_batch_delete", "voucher", None, f"批量删除 {cnt} 条单证")
        return RedirectResponse("/vouchers?ok=" + quote(f"已批量删除 {cnt} 条单证凭证"), status_code=303)

    if batch_action == "status":
        cnt = queries.batch_update_voucher_status(vids, target_status)
        st_cn = queries.VOUCHER_STATUS_NAMES.get(target_status, target_status)
        queries.log("user", "voucher_batch_status", "voucher", None, f"批量更新 {cnt} 条单证状态为 {st_cn}")
        return RedirectResponse("/vouchers?ok=" + quote(f"已将 {cnt} 条单证状态更新为「{st_cn}」"), status_code=303)

    if batch_action == "export":
        # 导出选中的凭证
        placeholders = ",".join("?" for _ in vids)
        with db.ro() as conn:
            rows = conn.execute(
                f"""SELECT v.*, c.name AS contact_name, o.order_no AS order_no_rel
                    FROM vouchers v
                    LEFT JOIN contacts c ON c.id = v.contact_id
                    LEFT JOIN orders o ON o.id = v.order_id
                    WHERE v.id IN ({placeholders})
                    ORDER BY v.trade_date DESC""",
                vids
            ).fetchall()
        items = []
        for r in rows:
            d = dict(r)
            d["voucher_type_cn"] = queries.VOUCHER_TYPE_NAMES.get(d["voucher_type"], d["voucher_type"])
            d["status_cn"] = queries.VOUCHER_STATUS_NAMES.get(d["status"], d["status"])
            items.append(d)
        csv_str = queries.export_vouchers_csv(items)
        return Response(
            content=csv_str,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=vouchers_export.csv"}
        )

    return RedirectResponse("/vouchers?err=" + quote("未知批量操作"), status_code=303)


@router.get("/vouchers/{vid}")
def vouchers_detail_json(request: Request, vid: int):
    """获取凭证详情 JSON。"""
    v = queries.get_voucher(vid)
    if not v:
        return JSONResponse({"ok": False, "msg": "凭证不存在"}, status_code=404)
    return JSONResponse({"ok": True, "data": v})


@router.post("/vouchers/{vid}")
def vouchers_update(
    request: Request,
    vid: int,
    voucher_no: str = Form(""),
    voucher_type: str = Form("commercial_invoice"),
    title: str = Form(""),
    trade_date: str = Form(""),
    currency: str = Form("USD"),
    amount: float = Form(0.0),
    contact_id: Optional[int] = Form(None),
    order_id: Optional[int] = Form(None),
    shipper: str = Form(""),
    consignee: str = Form(""),
    product_desc: str = Form(""),
    status: str = Form("confirmed"),
    notes: str = Form(""),
    csrf_tok: str = Form("")
):
    """更新凭证记录。"""
    if not check_csrf(request, csrf_tok):
        return RedirectResponse("/vouchers?err=" + quote("安全校验失败，请重试"), status_code=303)

    data = {
        "voucher_no": voucher_no,
        "voucher_type": voucher_type,
        "title": title,
        "trade_date": trade_date,
        "currency": currency,
        "amount": amount,
        "contact_id": contact_id,
        "order_id": order_id,
        "shipper": shipper,
        "consignee": consignee,
        "product_desc": product_desc,
        "status": status,
        "notes": notes,
    }
    ok = queries.update_voucher(vid, data)
    if ok:
        queries.log("user", "voucher_update", "voucher", vid, f"更新单证 {voucher_no}")
    msg = "凭证已更新" if ok else "更新失败或记录不存在"
    key = "ok" if ok else "err"
    return RedirectResponse(f"/vouchers?{key}=" + quote(msg), status_code=303)


@router.post("/vouchers/{vid}/delete")
def vouchers_delete(request: Request, vid: int, csrf_tok: str = Form("")):
    """删除单张凭证。"""
    if not check_csrf(request, csrf_tok):
        return RedirectResponse("/vouchers?err=" + quote("安全校验失败，请重试"), status_code=303)
    ok = queries.delete_voucher(vid)
    if ok:
        queries.log("user", "voucher_delete", "voucher", vid, f"删除单证 ID {vid}")
    msg = "凭证已删除" if ok else "删除失败"
    key = "ok" if ok else "err"
    return RedirectResponse(f"/vouchers?{key}=" + quote(msg), status_code=303)
