"""货运代理与运费成本对比模块自动化测试。涵盖货代/报价 CRUD、多渠道计费测算、汇率折算、模板与导航渲染。"""
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
    print(" 货运代理与运费对比模块自动化测试")
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

    # 初始化数据库（确保新表建好）
    db.init_db()

    # 0. 清理可能残留的测试数据
    with db.tx() as conn:
        conn.execute("DELETE FROM forwarders WHERE name LIKE 'TEST_%'")

    # 1. 新建货代测试
    ok, msg, fid1 = queries.save_forwarder({
        "name": "TEST_快捷达国际物流",
        "contact_person": "张经理",
        "phone": "13800138000",
        "email": "zhang@kuajieda.com",
        "wechat_or_im": "wx_zhang_fwd",
        "advantages": "欧美超大件海卡、欧洲双清包税",
        "rating": 5,
        "address": "深圳市宝安区福永街道物流园",
        "note": "每周三、六装柜，支持预申报",
    })
    check("新建货代成功", ok and fid1 is not None and fid1 > 0, msg)

    ok2, msg2, fid2 = queries.save_forwarder({
        "name": "TEST_顺捷速递代理",
        "contact_person": "李总",
        "phone": "13900139000",
        "email": "li@shunjie-express.com",
        "wechat_or_im": "shunjie_vip",
        "advantages": "香港DHL/FedEx一级庄，美线空派",
        "rating": 4,
        "address": "广州白云机场物流中心",
        "note": "旺季仓位保障",
    })
    check("新建第二家货代成功", ok2 and fid2 is not None and fid2 > 0, msg2)

    # 2. 查询与编辑货代
    fwd1 = queries.get_forwarder(fid1)
    check("获取单个货代详情", fwd1 is not None and fwd1["name"] == "TEST_快捷达国际物流")
    check("货代联系人正确", fwd1["contact_person"] == "张经理")
    check("货代评级正确", fwd1["rating"] == 5)

    ok_up, msg_up, _ = queries.save_forwarder({
        "id": fid1,
        "name": "TEST_快捷达国际物流(集团)",
        "contact_person": "张总",
        "phone": "13800138888",
        "email": "zhang@kuajieda.com",
        "wechat_or_im": "wx_zhang_fwd",
        "advantages": "欧美超大件海卡、欧洲双清包税、中东专线",
        "rating": 5,
        "address": "深圳市宝安区福永街道物流园",
        "note": "账期支持月结",
    })
    check("更新货代资料成功", ok_up, msg_up)
    fwd1_up = queries.get_forwarder(fid1)
    check("更新后名称验证", fwd1_up["name"] == "TEST_快捷达国际物流(集团)")
    check("更新后联系人验证", fwd1_up["contact_person"] == "张总")

    # 3. 录入多渠道报价
    # 报价 1: 快捷达 - 美森快船 (海运, 按 KG)
    ok_q1, _, qid1 = queries.save_freight_quote({
        "forwarder_id": fid1,
        "title": "美森快船超大件限时达",
        "channel_type": "sea",
        "origin": "深圳/盐田",
        "destination": "美国洛杉矶",
        "unit_price": 18.5,
        "price_unit": "kg",
        "currency": "CNY",
        "min_charge": "21KG起",
        "transit_time_text": "12-15天",
        "valid_until": "2026-12-31",
        "extra_fees": "双清包税，报关费350元/票",
        "remarks": "超大件需打托盘",
    })
    check("录入海运报价 1 成功", ok_q1 and qid1 is not None and qid1 > 0)

    # 报价 2: 顺捷速递 - 香港DHL特惠 (国际快递, 按 KG)
    ok_q2, _, qid2 = queries.save_freight_quote({
        "forwarder_id": fid2,
        "title": "香港DHL大陆特惠线",
        "channel_type": "express",
        "origin": "深圳/香港",
        "destination": "美国洛杉矶",
        "unit_price": 52.0,
        "price_unit": "kg",
        "currency": "CNY",
        "min_charge": "0.5KG起",
        "transit_time_text": "3-5工作日",
        "valid_until": "2026-12-31",
        "extra_fees": "燃油附加费已含，不含目的地关税",
        "remarks": "排仓时效稳定",
    })
    check("录入快递报价 2 成功", ok_q2 and qid2 is not None and qid2 > 0)

    # 报价 3: 快捷达 - 欧洲海运拼箱 (海运, 按 CBM, 美元计价)
    ok_q3, _, qid3 = queries.save_freight_quote({
        "forwarder_id": fid1,
        "title": "欧洲汉堡拼箱专线",
        "channel_type": "sea",
        "origin": "宁波",
        "destination": "德国汉堡",
        "unit_price": 60.0,
        "price_unit": "cbm",
        "currency": "USD",
        "min_charge": "1CBM起",
        "transit_time_text": "28-32天",
        "valid_until": "2026-11-30",
        "extra_fees": "港杂费另计",
        "remarks": "直拼无需中转",
    })
    check("录入拼箱美元报价 3 成功", ok_q3 and qid3 is not None and qid3 > 0)

    # 4. 报价查询与格式化校验
    q1 = queries.get_freight_quote(qid1)
    check("单条报价查询", q1 is not None and q1["title"] == "美森快船超大件限时达")
    check("报价关联货代名正确", q1["forwarder_name"] == "TEST_快捷达国际物流(集团)")
    check("渠道图标与名称正确", q1["channel_icon"] == "🌊" and q1["channel_name"] == "海运")
    check("计价单位展示格式化", "¥18.50 / KG" in q1["unit_display"])
    check("报价有效性判定为有效", q1["valid_status"] == "valid")

    # 验证货代关联的报价计数
    fwd_list = queries.list_forwarders(q="TEST_")
    f1_item = next((f for f in fwd_list if f["id"] == fid1), None)
    f2_item = next((f for f in fwd_list if f["id"] == fid2), None)
    check("货代 1 报价统计为 2 个", f1_item is not None and f1_item["quote_count"] == 2)
    check("货代 2 报价统计为 1 个", f2_item is not None and f2_item["quote_count"] == 1)

    # 5. 核心运输成本测算与比价逻辑测试
    # 场景 A：目的地美国洛杉矶，500 KG，2.0 CBM
    comp_us = queries.compare_shipping_costs(
        destination="洛杉矶", channel_type="",
        weight_kg=500.0, volume_cbm=2.0
    )
    check("洛杉矶比价匹配到 2 个方案", comp_us["total_matches"] == 2)
    quotes_us = comp_us["quotes"]
    check("比价结果按总运费升序排序", quotes_us[0]["cost_cny"] <= quotes_us[1]["cost_cny"])

    # 海运 500 KG * 18.5 = 9250 元
    # 快递 500 KG * 52.0 = 26000 元
    cheapest = quotes_us[0]
    fastest = next((q for q in quotes_us if q["is_fastest"]), None)
    check("最便宜方案为海运", cheapest["channel_type"] == "sea" and cheapest["is_cheapest"])
    check("海运算费金额正确 (9250元)", cheapest["estimated_cost"] == 9250.0)
    check("最快方案为快递 (3-5天)", fastest is not None and fastest["channel_type"] == "express")

    # 校验生成的客户沟通文案
    summary = comp_us["summary_text"]
    check("客户文案包含目的地", "洛杉矶" in summary)
    check("客户文案包含最优标签", "最低成本" in summary)
    check("客户文案包含最快标签", "最快时效" in summary)
    check("客户文案包含预估金额", "9250" in summary)

    # 场景 B：体积重膨胀测算（国际快递 1 CBM = 200 KG）
    # 实际毛重 10 KG，体积 0.5 CBM（材积重 0.5 * 200 = 100 KG）
    comp_vol = queries.compare_shipping_costs(
        destination="洛杉矶", channel_type="express",
        weight_kg=10.0, volume_cbm=0.5
    )
    exp_quote = comp_vol["quotes"][0]
    check("快递体积重 100KG 触发 (100 * 52 = 5200元)", exp_quote["estimated_cost"] == 5200.0)

    # 场景 C：美元换算汇率测算（欧洲德国汉堡）
    comp_de = queries.compare_shipping_costs(
        destination="汉堡", channel_type="",
        weight_kg=0.0, volume_cbm=5.0
    )
    de_quote = comp_de["quotes"][0]
    # 5 CBM * $60 = $300, 折合人民币 300 * 7.2 = 2160 元
    check("拼箱美元计算正确 ($300)", de_quote["estimated_cost"] == 300.0)
    check("美元折合人民币换算正确 (2160元)", de_quote["cost_cny"] == 2160.0)

    # 6. CSV 导出校验
    csv_text = queries.export_freight_quotes_csv()
    check("CSV 导出不为空", len(csv_text) > 100)
    check("CSV 包含表头", "货代公司" in csv_text and "基准单价" in csv_text and "预估时效" in csv_text)
    check("CSV 包含美森快船方案", "美森快船超大件限时达" in csv_text)
    check("CSV 包含 DHL 方案", "香港DHL大陆特惠线" in csv_text)

    # 7. 页面模板渲染测试
    # 测算比价页渲染
    html_calc = render(
        "freight.html",
        page="freight",
        active_tab="calc",
        calc=comp_us,
        quotes=quotes_us,
        forwarders=fwd_list,
        total_forwarders=len(fwd_list),
        total_quotes=3,
        f={"dest": "洛杉矶", "channel": "", "weight": "500", "vol": "2.0", "fid": None, "q": ""},
        ok="比价完成",
        err="",
        csrf="test_csrf_token_123"
    )
    check("比价页面渲染成功", len(html_calc) > 2000)
    check("包含网页标题「货运代理 · 外贸询盘助手」", "货运代理 · 外贸询盘助手" in html_calc)
    check("货运代理导航项处于激活状态", 'class="nav-item active" href="/freight"' in html_calc)
    check("包含比价亮点", "最低成本" in html_calc and "最快时效" in html_calc)
    check("包含复制对比文案按钮", "一键复制对比文案" in html_calc)
    check("包含 CSRF Token", "test_csrf_token_123" in html_calc)

    # 报价库页面渲染
    html_quotes = render(
        "freight.html",
        page="freight",
        active_tab="quotes",
        calc={},
        quotes=queries.list_freight_quotes(),
        forwarders=fwd_list,
        total_forwarders=len(fwd_list),
        total_quotes=3,
        f={"dest": "", "channel": "", "weight": "", "vol": "", "fid": None, "q": ""},
        ok="",
        err="",
        csrf="test_csrf_token_123"
    )
    check("报价库页面渲染成功", "渠道报价库" in html_quotes and "欧洲汉堡拼箱专线" in html_quotes)

    # 货代名录页面渲染
    html_fwds = render(
        "freight.html",
        page="freight",
        active_tab="forwarders",
        calc={},
        quotes=[],
        forwarders=fwd_list,
        total_forwarders=len(fwd_list),
        total_quotes=3,
        f={"dest": "", "channel": "", "weight": "", "vol": "", "fid": None, "q": ""},
        ok="",
        err="",
        csrf="test_csrf_token_123"
    )
    check("货代名录卡片网格渲染成功", "TEST_快捷达国际物流" in html_fwds and "★★★★★" in html_fwds)

    # 8. 删除与级联清理测试
    # 单独删除报价 3
    del_q_ok = queries.delete_freight_quote(qid3)
    check("单独删除报价成功", del_q_ok)
    check("已删除报价无法查出", queries.get_freight_quote(qid3) is None)

    # 删除货代 1（测试级联删除报价 1）
    del_f_ok = queries.delete_forwarder(fid1)
    check("删除货代 1 成功", del_f_ok)
    check("货代 1 无法查出", queries.get_forwarder(fid1) is None)
    check("关联的报价 1 已随级联删除", queries.get_freight_quote(qid1) is None)

    # 删除货代 2（级联删除报价 2）
    queries.delete_forwarder(fid2)
    check("货代 2 无法查出", queries.get_forwarder(fid2) is None)
    check("关联的报价 2 已随级联删除", queries.get_freight_quote(qid2) is None)

    # 确认测试数据无残留
    with db.ro() as conn:
        remain_fwd = conn.execute("SELECT COUNT(*) FROM forwarders WHERE name LIKE 'TEST_%'").fetchone()[0]
        remain_q = conn.execute("SELECT COUNT(*) FROM freight_quotes WHERE title LIKE '%TEST%'").fetchone()[0]
    check("数据库完全清理干净 (测试货代数为 0)", remain_fwd == 0)
    check("数据库完全清理干净 (测试报价数为 0)", remain_q == 0)

    print()
    print("=" * 66)
    print(f"货运代理专项测试结果：通过 {passed} 项 · 失败 {failed} 项")
    print("=" * 66)
    if failed > 0:
        sys.exit(1)
    print("货运代理专项测试全部 100% 通过！")


if __name__ == "__main__":
    run_tests()
