"""外贸订单流转全生命周期管理自动化测试套件。涵盖订单创建、8大流转阶段推进、全过程时间轴流水、利润成本核算、交期预警、客户赢单联动与看板渲染。"""
import datetime
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
    print(" 外贸订单流转全生命周期管理自动化测试")
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
        conn.execute("DELETE FROM orders WHERE title LIKE 'TEST_%'")
        conn.execute("DELETE FROM contacts WHERE email='test_order_buyer@munich-tech.de'")
        conn.execute("DELETE FROM forwarders WHERE name LIKE 'TEST_ORDER_%'")

    # 1. 创建测试买家客户与货代报价
    ok_c, _, cid = queries.save_contact({
        "email": "test_order_buyer@munich-tech.de",
        "name": "Wolfgang Schneider",
        "company": "Schneider Industrial GmbH",
        "country": "德国",
        "stage": "engaging",
    })
    check("创建测试客户成功", ok_c and cid > 0)

    ok_f, _, fid = queries.save_forwarder({
        "name": "TEST_ORDER_快捷达物流",
        "contact_person": "张经理",
        "phone": "13800001111",
    })
    check("创建测试货代成功", ok_f and fid > 0)

    ok_q, _, qid = queries.save_freight_quote({
        "forwarder_id": fid,
        "title": "欧洲汉堡海运整箱专线",
        "channel_type": "sea",
        "origin": "深圳",
        "destination": "德国汉堡",
        "unit_price": 2200.0,
        "price_unit": "40hq",
        "currency": "USD",
    })
    check("创建关联货代报价成功", ok_q and qid > 0)

    # 2. 新建订单 A：自动生成 PI 编号
    today_str = datetime.date.today().isoformat()
    etd_warning = (datetime.date.today() + datetime.timedelta(days=5)).isoformat()

    ok_o1, msg_o1, oid1 = queries.save_order({
        "contact_id": cid,
        "title": "TEST_德国工业自动化电机 500 台大货订单",
        "order_no": "",  # 留空自动生成
        "stage": "draft",
        "trade_term": "FOB",
        "payment_term": "30% T/T Deposit, 70% Balance before shipment",
        "currency": "USD",
        "total_amount": 25000.0,
        "deposit_amount": 7500.0,
        "balance_amount": 0.0,
        "settlement_fx_rate": 7.20,
        "factory_cost": 110000.0,
        "shipping_cost": 15000.0,
        "other_cost": 3000.0,
        "est_delivery_date": etd_warning,
        "destination_port": "德国汉堡港",
        "forwarder_quote_id": qid,
        "note": "电压 380V，需打出口熏蒸托盘",
    }, operator="admin")
    check("新建订单 1 (自动 PI 编号) 成功", ok_o1 and oid1 > 0, msg_o1)

    od1 = queries.get_order(oid1)
    check("订单自动生成 PI 编号", od1["order_no"].startswith("PI-"))
    check("订单项目标题正确", "德国工业自动化电机" in od1["title"])
    check("关联客户名称正确", od1["customer_name"] == "Wolfgang Schneider")
    check("关联客户公司正确", od1["customer_company"] == "Schneider Industrial GmbH")
    check("关联货代渠道名称正确", od1["forwarder_name"] == "TEST_ORDER_快捷达物流")

    # 3. 财务与利润即时核算测试
    # 销售额 = $25,000 * 7.2 = 180,000 元
    # 总成本 = 110,000(工厂) + 15,000(运费) + 3,000(杂费) = 128,000 元
    # 净利润 = 180,000 - 128,000 = 52,000 元
    # 净利率 = 52,000 / 180,000 = 28.9%
    check("折合人民币销售额计算正确 (180,000元)", od1["revenue_cny"] == 180000.0)
    check("总支出成本核算正确 (128,000元)", od1["total_cost_cny"] == 128000.0)
    check("预估净利润核算正确 (52,000元)", od1["net_profit_cny"] == 52000.0)
    check("净毛利率计算正确 (28.9%)", od1["profit_margin"] == 28.9)
    check("已收货款占比正确 (30%)", od1["received_pct"] == 30)

    # 4. 交期预警测试
    check("交期临近 (5天) 标记为 warning", od1["etd_status"] == "warning")
    check("包含临期提示徽标", "剩 5 天" in od1["etd_tag"])

    # 5. 时间轴初始流水测试
    check("时间轴包含立项记录", len(od1["timeline"]) >= 1)
    first_evt = od1["timeline"][-1]
    check("立项动作名称正确", first_evt["action_name"] == "订单立项起草")

    # 6. 8 大生命周期流转推进测试
    # 步骤 2: PI已确认
    ok_s2, msg_s2 = queries.advance_order_stage(oid1, "pi_confirmed", "买家邮件回传签字盖章版 PI")
    check("推进至【PI已确认】", ok_s2, msg_s2)

    # 步骤 3: 定金到账
    ok_s3, msg_s3 = queries.advance_order_stage(oid1, "deposit_received", "收到中行水单 $7,500")
    check("推进至【定金到账】", ok_s3, msg_s3)

    # 步骤 4: 工厂排产
    ok_s4, msg_s4 = queries.advance_order_stage(oid1, "in_production", "工厂已下达生产工单，交期 20 天")
    check("推进至【工厂排产中】", ok_s4, msg_s4)

    # 步骤 5: 验货完成
    ok_s5, msg_s5 = queries.advance_order_stage(oid1, "qc_passed", "品控抽检合格，大货包装良好")
    check("推进至【验货完成】", ok_s5, msg_s5)

    # 步骤 6: 尾款到账（更新财务）
    queries.update_order_finance(oid1, {
        "deposit_amount": 7500.0,
        "balance_amount": 17500.0,
        "factory_cost": 110000.0,
        "shipping_cost": 15000.0,
        "other_cost": 3000.0,
        "settlement_fx_rate": 7.20,
        "tracking_or_bl_no": "MSCU-88997766",
        "note": "客户已结清 70% 尾款 $17,500，准备放单",
    })
    ok_s6, msg_s6 = queries.advance_order_stage(oid1, "balance_received", "尾款已全部核销入账")
    check("推进至【尾款已收齐】", ok_s6, msg_s6)

    od1_paid = queries.get_order(oid1)
    check("货款全额收齐 (100%)", od1_paid["received_pct"] == 100)
    check("提单号已记录", od1_paid["tracking_or_bl_no"] == "MSCU-88997766")

    # 步骤 7: 订舱出运
    ok_s7, msg_s7 = queries.advance_order_stage(oid1, "shipping", "已装柜盐田开船，提单已电放给买家")
    check("推进至【订舱出运中】", ok_s7, msg_s7)

    # 步骤 8: 履约完成 ➔ 验证联动将客户升级为 'won'
    ok_s8, msg_s8 = queries.advance_order_stage(oid1, "completed", "客户汉堡工厂已成功提货入库，反馈良好")
    check("推进至【履约完成】", ok_s8, msg_s8)

    # 验证客户状态联动升级
    c_after = queries.get_contact(cid)
    check("客户跟进阶段自动联动升级为 won (已成交)", c_after["stage"] == "won")
    check("客户意向分自动提高至 >= 90", c_after["score"] >= 90)

    # 验证时间轴完整流水数
    od1_done = queries.get_order(oid1)
    check("时间轴包含 8 步完整流水", len(od1_done["timeline"]) >= 8)

    # 7. 看板数据聚合测试
    kanban = queries.get_orders_kanban()
    check("看板总订单数统计正确", kanban["total_orders"] >= 1)
    check("看板包含 8 个泳道列", len(kanban["columns"]) == 8)
    completed_col = next((col for col in kanban["columns"] if col["stage"] == "completed"), None)
    check("已完成泳道包含该订单", completed_col is not None and completed_col["count"] >= 1)

    # 8. 列表与筛选测试
    list_res = queries.list_orders(stage="completed")
    check("按 completed 阶段筛选有数据", len(list_res) >= 1)
    list_search = queries.list_orders(q="Schneider")
    check("按客户公司关键词检索命中", len(list_search) >= 1)

    # 9. 模板渲染测试
    # 渲染看板
    html_kanban = render(
        "orders.html",
        page="orders",
        view="kanban",
        kanban=kanban,
        orders=[],
        contacts=[c_after],
        quotes=[],
        f={"stage": "", "cid": None, "q": ""},
        ok="操作成功",
        err="",
        csrf="test_csrf_token"
    )
    check("订单看板页面渲染成功", len(html_kanban) > 2000)
    check("看板包含订单管理标题", "订单管理 · 外贸询盘助手" in html_kanban)
    check("导航栏订单项处于激活状态", 'class="nav-item active" href="/orders"' in html_kanban)
    check("看板包含订单卡片编号", od1["order_no"] in html_kanban)
    check("看板包含客户名称", "Wolfgang Schneider" in html_kanban)

    # 渲染列表页
    html_list = render(
        "orders.html",
        page="orders",
        view="list",
        kanban={},
        orders=[od1_done],
        contacts=[c_after],
        quotes=[],
        f={"stage": "", "cid": None, "q": ""},
        ok="",
        err="",
        csrf="test_csrf_token"
    )
    check("订单列表页渲染成功", "贸易条款" in html_list and "FOB" in html_list)

    # 渲染详情页
    html_detail = render(
        "order_detail.html",
        page="orders",
        order=od1_done,
        stages_list=list(queries.ORDER_STAGES.values()),
        quotes=[],
        ok="流转成功",
        err="",
        csrf="test_csrf_token"
    )
    check("订单详情页渲染成功", "履约出运与生产要素" in html_detail and "STEP 8" in html_detail)
    check("详情页包含时间轴节点", "已装柜盐田开船" in html_detail)
    check("详情页包含财务净利润", "+¥52000" in html_detail or "52,000" in html_detail or "52000" in html_detail)

    # 10. 删除与清理测试
    queries.delete_order(oid1)
    check("删除订单成功", queries.get_order(oid1) is None)

    with db.ro() as conn:
        tl_count = conn.execute("SELECT COUNT(*) FROM order_timeline WHERE order_id=?", (oid1,)).fetchone()[0]
    check("关联时间轴级联删除干净", tl_count == 0)

    # 清理测试货代与客户
    queries.delete_forwarder(fid)
    queries.delete_contact(cid)
    check("测试客户清理完成", queries.get_contact(cid) is None)
    check("测试货代清理完成", queries.get_forwarder(fid) is None)

    # 确保数据库完全无残留
    with db.ro() as conn:
        remain_o = conn.execute("SELECT COUNT(*) FROM orders WHERE title LIKE 'TEST_%'").fetchone()[0]
    check("数据库完全清理干净 (测试订单数为 0)", remain_o == 0)

    print()
    print("=" * 66)
    print(f"外贸订单流转专项测试结果：通过 {passed} 项 · 失败 {failed} 项")
    print("=" * 66)
    if failed > 0:
        sys.exit(1)
    print("外贸订单流转专项测试全部 100% 通过！")


if __name__ == "__main__":
    run_tests()
