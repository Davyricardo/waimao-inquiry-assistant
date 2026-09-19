"""外贸基础产品库、订单明细品项与外贸单证生成器自动化测试套件。涵盖产品CRUD、CBM自动核算、订单品项增删改、件重尺汇总与订单反向同步、PI/CI/PL单证数据聚合与专业排版渲染。"""
import sys

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from app import db, queries
from app.templating import render


def run_tests():
    print("=" * 66)
    print(" 外贸基础产品库与单证生成器（Phase 2）自动化测试")
    print("=" * 66)

    passed = 0
    failed = 0

    def check(name: str, cond: bool, extra: str = ""):
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  [ PASS ] {name}")
        else:
            failed += 1
            msg = f"  [ FAIL ] {name}"
            if extra:
                msg += f" ({extra})"
            print(msg)

    # 初始化数据库
    db.init_db()

    # 0. 预先清理可能残留的测试数据
    with db.tx() as conn:
        conn.execute("DELETE FROM products WHERE sku LIKE 'TEST_%'")
        conn.execute("DELETE FROM orders WHERE title LIKE 'TEST_%' OR order_no LIKE 'TEST_%'")
        conn.execute("DELETE FROM contacts WHERE email='test_doc_buyer@berlin-tech.de'")

    # ======================= 1. 产品库 CRUD 与 CBM 自动核算 =======================
    # 新增产品 1 (长宽高分别为 50cm, 40cm, 30cm -> CBM 应为 0.0600)
    ok_p1, _, pid1 = queries.save_product({
        "sku": "TEST_MTR-A100",
        "name_en": "High Speed Brushless DC Motor 24V",
        "name_cn": "高速无刷直流电机 24V",
        "hs_code": "8501310000",
        "category": "电机",
        "specs": "24V 3000RPM, 200W, Aluminum Alloy, IP65",
        "unit": "PCS",
        "price_usd": 45.0,
        "cost_cny": 160.0,
        "moq": 50,
        "carton_qty": 10,
        "carton_length_cm": 50.0,
        "carton_width_cm": 40.0,
        "carton_height_cm": 30.0,
        "carton_gw_kg": 18.5,
        "carton_nw_kg": 16.0,
        "note": "出口标配珍珠棉防震包装",
        "is_active": 1,
    })
    check("创建标准产品 1 成功", ok_p1 and pid1 > 0)

    p1 = queries.get_product(pid1)
    check("获取产品 1 详情", p1 is not None)
    check("产品 1 SKU 正确", p1["sku"] == "TEST_MTR-A100")
    check("产品 1 英文品名正确", p1["name_en"] == "High Speed Brushless DC Motor 24V")
    check("产品 1 HS Code 正确", p1["hs_code"] == "8501310000")
    # 50 * 40 * 30 / 1,000,000 = 0.06
    check("外箱 CBM 自动核算正确 (0.0600 CBM)", abs(p1["carton_cbm"] - 0.06) < 1e-4)
    check("产品单箱毛重正确 (18.5kg)", abs(p1["carton_gw_kg"] - 18.5) < 1e-4)

    # 重复 SKU 校验拦截
    dup_ok, dup_msg, _ = queries.save_product({
        "sku": "TEST_MTR-A100",
        "name_en": "Duplicate Motor",
    })
    check("重复 SKU 唯一性校验拦截", not dup_ok and "已存在" in dup_msg)

    # 新增产品 2
    ok_p2, _, pid2 = queries.save_product({
        "sku": "TEST_DRV-D200",
        "name_en": "Intelligent Motor Driver Controller",
        "name_cn": "智能电机驱动器",
        "hs_code": "8537109090",
        "category": "控制器",
        "unit": "PCS",
        "price_usd": 25.0,
        "cost_cny": 80.0,
        "carton_qty": 20,
        "carton_length_cm": 40.0,
        "carton_width_cm": 30.0,
        "carton_height_cm": 20.0,
        "carton_gw_kg": 12.0,
        "carton_nw_kg": 10.0,
    })
    check("创建产品 2 成功", ok_p2 and pid2 > 0)

    # 列表与检索
    all_prods = queries.list_products(q="TEST_MTR")
    check("按 SKU 前缀检索命中", len(all_prods) == 1 and all_prods[0]["sku"] == "TEST_MTR-A100")

    hs_prods = queries.list_products(q="853710")
    check("按 HS Code 检索命中", len(hs_prods) == 1 and hs_prods[0]["sku"] == "TEST_DRV-D200")

    # 产品 CSV 导出
    csv_str = queries.export_products_csv()
    check("产品 CSV 导出不为空", bool(csv_str))
    check("产品 CSV 包含表头", "SKU型号" in csv_str and "英文品名" in csv_str and "单箱体积(CBM)" in csv_str)
    check("产品 CSV 包含 TEST_MTR-A100", "TEST_MTR-A100" in csv_str)

    # ======================= 2. 订单创建与明细品项联动 =======================
    ok_c, _, cid = queries.save_contact({
        "email": "test_doc_buyer@berlin-tech.de",
        "name": "Hans Becker",
        "company": "Berlin Automation AG",
        "country": "德国",
        "stage": "negotiating",
    })
    check("创建测试客户成功", ok_c and cid > 0)

    ok_o, _, oid = queries.save_order({
        "order_no": "TEST_PI_2026_001",
        "title": "TEST_德国工业电机驱动成套采购订单",
        "contact_id": cid,
        "currency": "USD",
        "trade_term": "FOB",
        "payment_term": "30% T/T Deposit, 70% Balance against B/L copy",
        "loading_port": "Shenzhen, China",
        "destination_port": "Hamburg, Germany",
        "carrier": "COSCO SHIPPING / V.088E",
        "shipping_marks": "PO: TEST-2026 / MADE IN CHINA",
        "total_amount": 0.0,
        "factory_cost": 0.0,
    })
    check("创建测试订单成功", ok_o and oid > 0)

    # 添加订单品项 1: 500 PCS 电机 (每箱10台 -> 50箱, 毛重 50*18.5=925kg, CBM 50*0.06=3.00)
    ok_it1, _, iid1 = queries.save_order_item(oid, {
        "product_id": pid1,
        "item_no": 1,
        "sku": "TEST_MTR-A100",
        "name_en": "High Speed Brushless DC Motor 24V",
        "name_cn": "高速无刷直流电机 24V",
        "hs_code": "8501310000",
        "specs": "24V 3000RPM, Aluminum casing, IP65",
        "unit": "PCS",
        "quantity": 500,
        "unit_price": 45.0,  # 总额 22,500 USD
        "unit_cost_cny": 160.0,  # 成本 80,000 CNY
        "cartons": 50,
        "net_weight_kg": 800.0,
        "gross_weight_kg": 925.0,
        "cbm": 3.00,
    })
    check("添加订单品项 1 成功", ok_it1 and iid1 > 0)

    # 添加订单品项 2: 500 PCS 驱动器 (每箱20台 -> 25箱, 单价 25 USD -> 12,500 USD, 成本 40,000 CNY, CBM 0.6)
    ok_it2, _, iid2 = queries.save_order_item(oid, {
        "product_id": pid2,
        "item_no": 2,
        "sku": "TEST_DRV-D200",
        "name_en": "Intelligent Motor Driver Controller",
        "name_cn": "智能电机驱动器",
        "hs_code": "8537109090",
        "unit": "PCS",
        "quantity": 500,
        "unit_price": 25.0,  # 总额 12,500 USD
        "unit_cost_cny": 80.0,  # 成本 40,000 CNY
        "cartons": 25,
        "net_weight_kg": 250.0,
        "gross_weight_kg": 300.0,
        "cbm": 0.60,
    })
    check("添加订单品项 2 成功", ok_it2 and iid2 > 0)

    # 查询品项列表
    items = queries.list_order_items(oid)
    check("订单品项列表数量为 2", len(items) == 2)
    check("品项 1 小计金额正确 (22,500 USD)", items[0]["amount"] == 22500.0)
    check("品项 2 小计金额正确 (12,500 USD)", items[1]["amount"] == 12500.0)

    # 订单品项汇总指标核算
    summary = queries.get_order_items_summary(oid)
    # 总金额: 22,500 + 12,500 = 35,000 USD
    check("品项总金额核算正确 (35,000 USD)", summary["total_amount"] == 35000.0)
    # 总件数: 500 + 500 = 1000 PCS
    check("订购总件数核算正确 (1000 PCS)", summary["total_qty"] == 1000)
    # 总箱数: 50 + 25 = 75 箱
    check("总箱数核算正确 (75 CTNS)", summary["total_cartons"] == 75)
    # 总毛重: 925 + 300 = 1225 kg
    check("总毛重核算正确 (1225.00 KG)", summary["total_gw"] == 1225.0)
    # 总体积: 3.00 + 0.60 = 3.60 CBM
    check("总体积核算正确 (3.6000 CBM)", abs(summary["total_cbm"] - 3.60) < 1e-4)
    # 总工厂成本: 80,000 + 40,000 = 120,000 CNY
    check("工厂出厂采购总额核算正确 (120,000 CNY)", summary["total_cost_cny"] == 120000.0)

    # 反向一键同步回写订单总额与出厂成本
    ok_sync, sync_msg = queries.sync_order_items_totals(oid)
    check("一键反向同步执行成功", ok_sync)

    order_updated = queries.get_order(oid)
    check("订单总额已自动更新为 35,000 USD", order_updated["total_amount"] == 35000.0)
    check("订单工厂出厂成本已自动更新为 120,000 CNY", order_updated["factory_cost"] == 120000.0)
    check("订单详情挂载 items 数量正确", len(order_updated["items"]) == 2)
    check("订单详情挂载 items_summary 正确", order_updated["items_summary"]["total_cartons"] == 75)

    # ======================= 3. 英文金额大写转换与外贸单证聚合 =======================
    words_35000 = queries.amount_to_english_words(35000.0, "USD")
    check("35,000 美元英文大写正确", words_35000 == "SAY US DOLLARS THIRTY-FIVE THOUSAND ONLY")

    words_cents = queries.amount_to_english_words(25800.75, "USD")
    check("带美分小数金额英文大写正确", words_cents == "SAY US DOLLARS TWENTY-FIVE THOUSAND EIGHT HUNDRED AND CENTS SEVENTY-FIVE ONLY")

    # 公司抬头与收款银行账户存取测试
    test_prof = {
        "company_name_en": "TEST GLOBAL TRADING LIMITED",
        "company_name_cn": "测试环球贸易有限公司",
        "bank_beneficiary_name": "TEST GLOBAL TRADING LIMITED",
        "bank_name": "HSBC HONG KONG",
        "bank_account_no": "888-123456-001",
        "bank_swift_code": "HSBCHKHHHKH",
    }
    queries.save_seller_profile(test_prof)
    cur_prof = queries.get_seller_profile()
    check("卖方公司英文名保存与读取", cur_prof["company_name_en"] == "TEST GLOBAL TRADING LIMITED")
    check("外汇银行 SWIFT Code 正确", cur_prof["bank_swift_code"] == "HSBCHKHHHKH")

    # PI (形式发票) 单证数据聚合
    pi_data = queries.get_order_document_data(oid, "pi")
    check("PI 单证数据聚合成功", pi_data is not None)
    check("PI 标题为 PROFORMA INVOICE", pi_data["doc_title"] == "PROFORMA INVOICE")
    check("PI 单号与订单号一致", pi_data["doc_no"] == "TEST_PI_2026_001")
    check("PI 买家公司名称正确", pi_data["contact"]["company"] == "Berlin Automation AG")
    check("PI 包含 2 项品项明细", len(pi_data["items"]) == 2)
    check("PI 包含英文大写金额", "THIRTY-FIVE THOUSAND" in pi_data["amount_in_words"])
    check("PI 包含境外收款银行账号", pi_data["seller"]["bank_account_no"] == "888-123456-001")

    # CI (商业发票) 单证数据聚合
    ci_data = queries.get_order_document_data(oid, "ci")
    check("CI 标题为 COMMERCIAL INVOICE", ci_data["doc_title"] == "COMMERCIAL INVOICE")
    check("CI 发票号前缀正确", ci_data["doc_no"] == "INV-TEST_PI_2026_001")
    check("CI 包含包装唛头", ci_data["order"]["shipping_marks"] == "PO: TEST-2026 / MADE IN CHINA")

    # PL (装箱单) 单证数据聚合
    pl_data = queries.get_order_document_data(oid, "pl")
    check("PL 标题为 PACKING LIST", pl_data["doc_title"] == "PACKING LIST")
    check("PL 单号前缀正确", pl_data["doc_no"] == "PL-TEST_PI_2026_001")
    check("PL 汇总总箱数为 75", pl_data["summary"]["total_cartons"] == 75)
    check("PL 汇总总毛重为 1225kg", pl_data["summary"]["total_gw"] == 1225.0)
    check("PL 汇总总体积为 3.6 CBM", abs(pl_data["summary"]["total_cbm"] - 3.60) < 1e-4)

    # ======================= 4. 模板页面渲染验证 =======================
    # 渲染产品库页面
    prod_html = render("products.html", products=[p1], categories=["电机", "控制器"], f={}, ok="")
    check("产品库页面渲染成功", prod_html is not None)
    check("产品库页面包含标题", "外贸产品库" in prod_html)
    check("产品库页面包含产品型号", "TEST_MTR-A100" in prod_html)
    check("产品库页面包含 CBM 算费说明", "CBM" in prod_html)

    # 渲染订单详情页 (包含品项表与单证按钮)
    stages_list = list(queries.ORDER_STAGES.values())
    detail_html = render("order_detail.html", order=order_updated, stages_list=stages_list, quotes=[], csrf="test_csrf")
    check("订单详情页渲染成功", detail_html is not None)
    check("包含形式发票 PI 快捷入口", "形式发票 (PI)" in detail_html)
    check("包含商业发票 CI 快捷入口", "商业发票 (CI)" in detail_html)
    check("包含装箱单 PL 快捷入口", "装箱单 (PL)" in detail_html)
    check("包含订单产品明细品项表格", "订单产品明细品项" in detail_html)
    check("包含明细项 1 SKU", "TEST_MTR-A100" in detail_html)
    check("包含明细项 2 SKU", "TEST_DRV-D200" in detail_html)
    check("包含总箱数 75 箱统计", "75 箱" in detail_html)

    # 渲染 A4 标准单证页面
    pi_html = render("document.html", **pi_data, csrf="test_csrf")
    check("PI 单证页面渲染成功", pi_html is not None)
    check("PI 页面包含大写金额", "THIRTY-FIVE THOUSAND" in pi_html)
    check("PI 页面包含开户行 SWIFT Code", "HSBCHKHHHKH" in pi_html)
    check("PI 页面包含打印快捷按钮", "打印 / 另存为 PDF" in pi_html)

    ci_html = render("document.html", **ci_data, csrf="test_csrf")
    check("CI 单证页面渲染成功", ci_html is not None)
    check("CI 页面包含发票号", "INV-TEST_PI_2026_001" in ci_html)
    check("CI 页面包含原产国声明", "COUNTRY OF ORIGIN" in ci_html)

    pl_html = render("document.html", **pl_data, csrf="test_csrf")
    check("PL 单证页面渲染成功", pl_html is not None)
    check("PL 页面包含总方数", "3.6000 CBM" in pl_html or "3.6 CBM" in pl_html)
    check("PL 页面包含总毛重", "1,225.00" in pl_html or "1225" in pl_html)

    # ======================= 5. 数据安全清理与级联删除验证 =======================
    # 删除品项 1
    del_it1 = queries.delete_order_item(oid, iid1)
    check("单独删除品项 1 成功", del_it1)
    items_after_del = queries.list_order_items(oid)
    check("删除后订单品项只剩 1 个", len(items_after_del) == 1)

    # 删除整个订单，验证级联删除 order_items 与时间轴
    del_o = queries.delete_order(oid)
    check("删除订单成功", del_o)
    items_cascade = queries.list_order_items(oid)
    check("关联订单品项已随级联删除干净", len(items_cascade) == 0)

    # 删除产品 1 与 产品 2
    queries.delete_product(pid1)
    queries.delete_product(pid2)
    check("删除产品 1 成功", queries.get_product(pid1) is None)
    check("删除产品 2 成功", queries.get_product(pid2) is None)

    # 清理客户
    with db.tx() as conn:
        conn.execute("DELETE FROM contacts WHERE id=?", (cid,))

    # 恢复默认 seller profile
    queries.save_seller_profile(queries.DEFAULT_SELLER_PROFILE)

    # 终极零残留核验
    with db.ro() as conn:
        p_res = conn.execute("SELECT COUNT(*) FROM products WHERE sku LIKE 'TEST_%'").fetchone()[0]
        o_res = conn.execute("SELECT COUNT(*) FROM orders WHERE order_no LIKE 'TEST_%'").fetchone()[0]
        it_res = conn.execute("SELECT COUNT(*) FROM order_items WHERE sku LIKE 'TEST_%'").fetchone()[0]
    check("数据库完全清理干净 (测试产品数为 0)", p_res == 0)
    check("数据库完全清理干净 (测试订单数为 0)", o_res == 0)
    check("数据库完全清理干净 (测试品项数为 0)", it_res == 0)

    print()
    print("=" * 66)
    print(f"Phase 2 专项测试结果：通过 {passed} 项 · 失败 {failed} 项")
    print("=" * 66)
    if failed == 0:
        print("外贸基础产品库与单证生成器专项测试全部 100% 通过！\n")
    else:
        print("存在失败项，请检查上方日志！\n")
        sys.exit(1)


if __name__ == "__main__":
    run_tests()
