"""外贸公司管理路由模块。

负责维护外贸公司中英文企业抬头档案与境外美金电汇收款银行账户（用于自动注入 PI / CI / PL 单证）。
"""
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import auth, config, queries
from ..templating import render

COOKIE = "mb_sess"
router = APIRouter()


def check_csrf(request: Request, token: str) -> bool:
    sess = request.cookies.get(COOKIE, "")
    return bool(token) and token == auth.csrf_token(sess)


@router.get("/company", response_class=HTMLResponse)
def company_page(request: Request, ok: str = "", err: str = ""):
    """外贸公司主体档案与收款银行设置页面。"""
    profile = queries.get_seller_profile()
    orders_count = len(queries.list_orders())
    return HTMLResponse(render(
        "company.html",
        page="company",
        seller_profile=profile,
        orders_count=orders_count,
        ok=ok,
        err=err,
        csrf=auth.csrf_token(request.cookies.get(COOKIE, "")),
    ))


@router.post("/company/save")
def company_save(request: Request,
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
    """保存外贸公司主体档案与外汇银行收款账户。"""
    if not check_csrf(request, csrf_tok):
        return RedirectResponse("/company?err=" + quote("请求校验失败，请重试"), status_code=303)

    if not company_name_en.strip():
        return RedirectResponse("/company?err=" + quote("请填写卖方公司英文名称"), status_code=303)

    data = {
        "company_name_en": company_name_en.strip(),
        "company_name_cn": company_name_cn.strip(),
        "company_address_en": company_address_en.strip(),
        "company_tel": company_tel.strip(),
        "company_email": company_email.strip(),
        "company_website": company_website.strip(),
        "bank_beneficiary_name": bank_beneficiary_name.strip(),
        "bank_name": bank_name.strip(),
        "bank_address": bank_address.strip(),
        "bank_account_no": bank_account_no.strip(),
        "bank_swift_code": bank_swift_code.strip(),
    }
    queries.save_seller_profile(data)
    queries.log(config.ADMIN_USER, "company_profile_save")
    return RedirectResponse("/company?ok=" + quote("外贸公司主体与境外电汇收款账户已成功保存"), status_code=303)
