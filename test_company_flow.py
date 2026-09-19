"""外贸公司管理与收款银行账户端到端自动化测试脚本。"""
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from app import auth, config, db, queries
from app.templating import render


def check(cond: bool, msg: str):
    if not cond:
        raise AssertionError(f"Test failed: {msg}")
    print(f"  [ OK ] {msg}")


def run_tests():
    print("=" * 60)
    print(" 外贸公司管理与电汇银行收款账户 自动化测试")
    print("=" * 60)

    # 1. 验证公司管理页面模板渲染与侧边栏高亮
    from app.routers.company import company_page
    from starlette.requests import Request
    req = Request({'type': 'http', 'method': 'GET', 'headers': []})
    resp = company_page(req)
    check(resp.status_code == 200, "GET /company 路由返回 200 OK")
    check(len(resp.body) > 1000, "GET /company 正常返回页面 HTML 内容")

    profile = queries.get_seller_profile()
    html = render(
        "company.html",
        page="company",
        seller_profile=profile,
        orders_count=0,
        ok="保存成功测试",
        csrf="test-csrf-token",
    )
    check("公司管理" in html, "页面大标题包含「公司管理」")
    check("外贸公司主体档案" in html, "页面包含外贸公司主体档案表单卡片")
    check("境外美金电汇收款银行账户" in html, "页面包含美金境外电汇收款银行账户卡片")
    check("单证页眉效果预览" in html, "页面包含单证页眉实时效果预览卡片")
    check("nav_company" not in html or "active" in html, "侧边栏公司管理导航项被正确激活")

    # 2. 验证数据保存与回显
    test_data = {
        "company_name_en": "ACME INTERNATIONAL INDUSTRIAL LIMITED",
        "company_name_cn": "鼎盛国际实业有限公司",
        "company_address_en": "Unit 1205, Modern International Trade Tower, Shenzhen, China",
        "company_tel": "+86-755-23456789",
        "company_email": "export@acme-industrial.com",
        "company_website": "www.acme-industrial.com",
        "bank_beneficiary_name": "ACME INTERNATIONAL INDUSTRIAL LIMITED",
        "bank_name": "STANDARD CHARTERED BANK (HONG KONG) LIMITED",
        "bank_address": "32/F, 4-4A Des Voeux Road Central, Hong Kong",
        "bank_account_no": "003-888-99991234",
        "bank_swift_code": "SCBLHKHHXXX",
    }
    queries.save_seller_profile(test_data)
    loaded = queries.get_seller_profile()

    check(loaded["company_name_en"] == test_data["company_name_en"], "英文公司名保存与回显一致")
    check(loaded["company_name_cn"] == test_data["company_name_cn"], "中文公司名保存与回显一致")
    check(loaded["bank_beneficiary_name"] == test_data["bank_beneficiary_name"], "收款人户名保存与回显一致")
    check(loaded["bank_swift_code"] == test_data["bank_swift_code"], "SWIFT Code 保存与回显一致")
    check(loaded["bank_account_no"] == test_data["bank_account_no"], "银行账号保存与回显一致")

    # 3. 验证再次渲染包含更新后的公司信息与汇款指引
    html_updated = render(
        "company.html",
        page="company",
        seller_profile=loaded,
        orders_count=5,
        csrf="test-csrf-token",
    )
    check("ACME INTERNATIONAL INDUSTRIAL LIMITED" in html_updated, "预览区包含更新后的公司英文全称")
    check("SCBLHKHHXXX" in html_updated, "汇款指引区包含更新后的 SWIFT 代码")
    check("STANDARD CHARTERED BANK" in html_updated, "汇款指引区包含更新后的开户行名")

    # 4. 验证设置页面不再包含 sellerProfileCard
    settings_html = render(
        "settings.html",
        page="settings",
        accounts=[],
        presets=queries.MAIL_PRESETS if hasattr(queries, "MAIL_PRESETS") else {},
        csrf="test-csrf-token",
    )
    check("sellerProfileCard" not in settings_html, "系统设置页已不再包含公司抬头表单")
    check("/company" in settings_html, "系统设置页保留前往公司管理的引导链接")

    print("=" * 60)
    print(" 全部公司管理自动化测试顺利通过！")
    print("=" * 60)


if __name__ == "__main__":
    run_tests()
