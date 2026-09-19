import json
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import app.db as db
import app.queries as queries
from app.templating import render

PASSED = 0
FAILED = 0

def check(condition: bool, msg: str):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  [ PASS ] {msg}")
    else:
        FAILED += 1
        print(f"  [ FAIL ] {msg}")
        raise AssertionError(f"Test failed: {msg}")

def run_tests():
    print("=" * 66)
    print(" 社媒外贸获客开发与智能触达（Phase 3）自动化测试")
    print("=" * 66)

    db.init_db()

    # 测试专用的前缀标识
    PREFIX = "TEST_SOC_"
    test_lead_ids = []
    test_contact_ids = []
    test_product_ids = []

    try:
        # ======================================================================
        # 1. WhatsApp 国际号码规范化与免存直连 wa.me 链接引擎测试
        # ======================================================================
        raw_phones = [
            ("+49 (151) 123-4567", "491511234567"),
            ("+1-555-234-5678", "15552345678"),
            ("86-13800138000", "8613800138000"),
            (" 0044 20 7946 0958 ", "00442079460958"),
            ("", ""),
            (None, ""),
        ]
        for raw, expected in raw_phones:
            cleaned = queries.format_whatsapp_number(raw)
            check(cleaned == expected, f"WhatsApp 号码清洗: '{raw}' -> '{cleaned}'")

        wa_url_empty = queries.generate_whatsapp_click_url("")
        check(wa_url_empty == "", "空号码生成 WhatsApp URL 返回空字符串")

        wa_url_simple = queries.generate_whatsapp_click_url("+1 (555) 0199")
        check(wa_url_simple == "https://wa.me/15550199", f"生成免存直连 URL: {wa_url_simple}")

        wa_url_text = queries.generate_whatsapp_click_url("+1 (555) 0199", "Hello Davy! Ready for trade?")
        check("https://wa.me/15550199?text=" in wa_url_text, "直连 URL 携带预填消息")
        check("Hello%20Davy" in wa_url_text, "预填消息已正确 URL 编码")

        # ======================================================================
        # 2. 社媒潜客线索 CRUD、多平台支持与星级意向测试
        # ======================================================================
        lead1_data = {
            "name": f"{PREFIX}Michael Schmidt",
            "company": f"{PREFIX}Schmidt Industrial GmbH",
            "country": "Germany",
            "position": "Purchasing Manager",
            "source_platform": "linkedin",
            "whatsapp": "+49 151 8888 9999",
            "linkedin_url": "https://www.linkedin.com/in/test-michael-schmidt",
            "email": f"{PREFIX.lower()}michael@schmidt-industrial.de",
            "website": "www.schmidt-industrial.de",
            "industry_or_niche": "High Precision Electric Motors",
            "stage": "discovered",
            "rating": 5,
            "next_followup_date": queries._today(),
            "notes": "德国头部机电批发商，对无刷高转速电机有明确批量代工意向。",
        }
        ok, msg, lid1 = queries.save_social_lead(lead1_data)
        check(ok and lid1 is not None, f"创建 LinkedIn 潜客 1 成功 (ID: {lid1})")
        test_lead_ids.append(lid1)

        lead1 = queries.get_social_lead(lid1)
        check(lead1 is not None, "获取潜客 1 详情")
        check(lead1["name"] == lead1_data["name"], "潜客 1 姓名正确")
        check(lead1["platform_info"]["id"] == "linkedin", "潜客 1 平台识别为 LinkedIn")
        check(lead1["platform_info"]["icon"] == "💼", "潜客 1 平台图标为 💼")
        check(lead1["stage_info"]["id"] == "discovered", "潜客 1 初始阶段为 discovered")
        check(lead1["whatsapp_formatted"] == "4915188889999", "WhatsApp 号码清洗正确")
        check(lead1["wa_click_url"] == "https://wa.me/4915188889999", "WhatsApp 直连免存链接正确")
        check(lead1["rating_stars"] == "★★★★★", "5星意向度展示正确")
        check(lead1["followup_status"] == "due_today", "今日日程标记为 due_today")
        check(lead1["followup_badge"] == "今日待跟进", "展示「今日待跟进」徽章")
        check(lead1["local_time"] is not None, "当地时间与时区成功推算")
        check(lead1["local_time"]["country"] == "德国", "国家归一化识别为德国")

        # 创建第二个潜客 (WhatsApp 渠道，美国客户，逾期未跟进测试)
        lead2_data = {
            "name": f"{PREFIX}Carlos Hernandez",
            "company": f"{PREFIX}Apex Sourcing LLC",
            "country": "USA",
            "position": "Senior Sourcing Specialist",
            "source_platform": "whatsapp",
            "whatsapp": "+1 555 777 8888",
            "industry_or_niche": "Automotive Aftermarket Parts",
            "stage": "connected",
            "rating": 4,
            "next_followup_date": "2026-01-01",  # 过去日期 -> 逾期未跟进
            "notes": "北美汽配买家，寻找中国稳定 OEM 供应链。",
        }
        ok, msg, lid2 = queries.save_social_lead(lead2_data)
        check(ok and lid2 is not None, f"创建 WhatsApp 潜客 2 成功 (ID: {lid2})")
        test_lead_ids.append(lid2)

        lead2 = queries.get_social_lead(lid2)
        check(lead2["platform_info"]["id"] == "whatsapp", "潜客 2 平台识别为 WhatsApp")
        check(lead2["platform_info"]["icon"] == "💬", "潜客 2 平台图标为 💬")
        check(lead2["followup_status"] == "overdue", "过去日期标记为 overdue (逾期)")
        check(lead2["followup_badge"] == "逾期未跟进", "展示「逾期未跟进」告警")

        # 更新潜客资料
        update_data = dict(lead1_data)
        update_data["position"] = "Head of Global Sourcing"
        update_data["rating"] = 4
        ok, msg, _ = queries.save_social_lead(update_data, lead_id=lid1)
        check(ok, "更新潜客资料成功")
        lead1_updated = queries.get_social_lead(lid1)
        check(lead1_updated["position"] == "Head of Global Sourcing", "更新后职位验证通过")
        check(lead1_updated["rating"] == 4, "更新后评级验证通过")

        # 列表与筛选检索
        leads_all = queries.list_social_leads(search=PREFIX)
        check(len(leads_all) == 2, "关键词检索命中全部测试潜客 (2条)")

        leads_li = queries.list_social_leads(platform="linkedin", search=PREFIX)
        check(len(leads_li) == 1 and leads_li[0]["id"] == lid1, "按 LinkedIn 渠道筛选命中潜客 1")

        leads_due = queries.list_social_leads(followup_status="due_today", search=PREFIX)
        check(len(leads_due) == 1 and leads_due[0]["id"] == lid1, "按今日待跟进筛选命中潜客 1")

        leads_overdue = queries.list_social_leads(followup_status="overdue", search=PREFIX)
        check(len(leads_overdue) == 1 and leads_overdue[0]["id"] == lid2, "按逾期未跟进筛选命中潜客 2")

        # ======================================================================
        # 3. 社媒触达流水记录（social_touchpoints）测试
        # ======================================================================
        # 自动生成的初始流水验证
        check(len(lead1["touchpoints"]) >= 1, "潜客 1 创建时已自动生成初始发掘流水")

        # 手动新增一条触达记录
        ok, msg, tpid = queries.save_social_touchpoint(
            lead_id=lid1,
            channel="linkedin",
            touch_type="first_touch",
            content="在 LinkedIn 发送了 300 字符好友邀请并附言介绍我们无刷电机工厂背景。",
            feedback="replied",
            next_action="明天在 WhatsApp 发送 2026 最新电子产品图册",
            operator="sales_alice"
        )
        check(ok and tpid is not None, f"录入触达流水成功 (ID: {tpid})")

        tps = queries.list_social_touchpoints(lid1)
        check(len(tps) >= 2, "潜客 1 拥有至少 2 条触达记录")
        check(tps[0]["feedback_label"] == "已积极探讨", "买家反馈标签映射为「已积极探讨」")
        check(tps[0]["next_action"] == "明天在 WhatsApp 发送 2026 最新电子产品图册", "下一步待办计划正确")

        # 删除单条触达流水
        ok, msg = queries.delete_social_touchpoint(tpid)
        check(ok, "删除触达流水成功")

        # ======================================================================
        # 4. 阶段流转与漏斗统计（Funnel & Kanban Summary）
        # ======================================================================
        ok, msg = queries.advance_social_lead_stage(lid1, "chatted", note="买家通过了 LinkedIn 好友并询问了报价")
        check(ok, "流转推进至【沟通中/已破冰】")
        lead1_chatted = queries.get_social_lead(lid1)
        check(lead1_chatted["stage"] == "chatted", "潜客 1 当前阶段已变为 chatted")

        summary = queries.get_social_leads_funnel_summary()
        check(summary["total_leads"] >= 2, "线索总数汇总正确")
        check(summary["due_today"] >= 1, "今日待跟进统计正确")
        check(summary["overdue"] >= 1, "逾期未跟进统计正确")
        check("linkedin" in summary["platform_counts"], "平台分布包含 linkedin")
        check("chatted" in summary["stage_counts"], "阶段分布包含 chatted")

        # ======================================================================
        # 5. 多场景多语言社媒破冰话术与开发信生成器测试
        # ======================================================================
        # 创建一个测试产品供话术注入
        prod_data = {
            "sku": f"{PREFIX}MTR-900",
            "name_en": "Brushless DC Motor 900W",
            "name_cn": "900W高速无刷直流电机",
            "category": "Motor",
            "specs": "IP68 waterproof, 15000 RPM, high torque",
            "unit": "PCS",
            "price_usd": 45.0,
            "cost_cny": 180.0,
            "moq": 100,
            "is_active": 1
        }
        ok, msg, pid = queries.save_product(prod_data)
        check(ok, "创建测试产品成功")
        test_product_ids.append(pid)
        test_prod = queries.get_product(pid)

        # 5.1 英文默认话术生成
        scripts_en = queries.generate_outreach_scripts(
            lead1_chatted, product=test_prod, lang="en", custom_pitch="CE & RoHS certified"
        )
        check("linkedin_note" in scripts_en, "包含 LinkedIn 好友邀请话术")
        check("linkedin_inmail" in scripts_en, "包含 LinkedIn InMail 开发信")
        check("whatsapp_first" in scripts_en, "包含 WhatsApp 首次破冰")
        check("whatsapp_product" in scripts_en, "包含 WhatsApp 产品推介")
        check("exhibition_followup" in scripts_en, "包含展会跟进")
        check("reengage_greeting" in scripts_en, "包含潜客激活")

        # 核心：验证 LinkedIn Connection Note 严格不超过 300 字符限制！
        li_note_len = len(scripts_en["linkedin_note"])
        check(li_note_len <= 300, f"LinkedIn 好友邀请字符数合法 ({li_note_len} <= 300 字符)")
        check("Michael" in scripts_en["linkedin_note"], "LinkedIn 邀请包含买家名 Michael")
        check(scripts_en["linkedin_note"] != "", "LinkedIn 邀请内容不为空")

        # 验证产品参数成功注入
        check("Brushless DC Motor 900W" in scripts_en["whatsapp_product"], "WhatsApp 产品推介包含产品名")
        check("MTR-900" in scripts_en["whatsapp_product"], "WhatsApp 产品推介包含 SKU")
        check("MOQ 100" in scripts_en["whatsapp_product"], "WhatsApp 产品推介包含 MOQ 100")
        check("CE & RoHS certified" in scripts_en["whatsapp_product"], "附加自定义卖点成功拼入")

        # 5.2 多语言支持验证（西语、德语、法语、阿语、俄语、葡语）
        scripts_es = queries.generate_outreach_scripts(lead1_chatted, product=test_prod, lang="es")
        check("Hola" in scripts_es["linkedin_note"] and "Michael" in scripts_es["linkedin_note"], "西语版 LinkedIn 邀请问候正常")
        check(len(scripts_es["linkedin_note"]) <= 300, "西语版 LinkedIn 邀请 <= 300 字符")

        scripts_de = queries.generate_outreach_scripts(lead1_chatted, product=test_prod, lang="de")
        check("Guten Tag" in scripts_de["linkedin_note"] and "Michael" in scripts_de["linkedin_note"], "德语版 LinkedIn 邀请问候正常")
        check(len(scripts_de["linkedin_note"]) <= 300, "德语版 LinkedIn 邀请 <= 300 字符")

        scripts_fr = queries.generate_outreach_scripts(lead1_chatted, product=test_prod, lang="fr")
        check("Bonjour" in scripts_fr["linkedin_note"] and "Michael" in scripts_fr["linkedin_note"], "法语版 LinkedIn 邀请问候正常")

        scripts_ar = queries.generate_outreach_scripts(lead1_chatted, product=test_prod, lang="ar")
        check("مرحباً" in scripts_ar["linkedin_note"] and "Michael" in scripts_ar["linkedin_note"], "阿语版 LinkedIn 邀请问候正常")

        scripts_ru = queries.generate_outreach_scripts(lead1_chatted, product=test_prod, lang="ru")
        check("Здравствуйте" in scripts_ru["linkedin_note"] and "Michael" in scripts_ru["linkedin_note"], "俄语版 LinkedIn 邀请问候正常")

        # ======================================================================
        # 6. 一键将社媒潜客转化为 CRM 正式客户测试
        # ======================================================================
        target_email = f"{PREFIX.lower()}michael_new@schmidt-industrial.de"
        ok, msg, new_cid = queries.convert_lead_to_contact(lid1, email=target_email)
        check(ok and new_cid is not None, f"一键转化为正式客户成功 (Contact ID: {new_cid})")
        test_contact_ids.append(new_cid)

        # 检查 contacts 表数据
        contact = queries.get_contact(new_cid)
        check(contact is not None, "新客户已在 contacts 表中创建")
        check(contact["name"] == lead1_chatted["name"], "客户姓名已继承潜客姓名")
        check(contact["company"] == lead1_chatted["company"], "客户公司已继承潜客公司")
        check(contact["email"] == target_email, "客户邮箱绑定正确")
        check(contact["source"] == "social_linkedin", "客户来源标记为社媒渠道")
        check(contact["whatsapp"] == lead1_chatted["whatsapp"], "客户 WhatsApp 号已继承")
        check(contact["linkedin_url"] == lead1_chatted["linkedin_url"], "客户 LinkedIn 主页已继承")

        # 检查 social_leads 表状态联动
        lead1_converted = queries.get_social_lead(lid1)
        check(lead1_converted["stage"] == "converted", "潜客流转阶段已自动更新为 converted")
        check(lead1_converted["contact_id"] == new_cid, "潜客绑定 contact_id 正确")

        # 检查转化记录自动进入流水
        converted_tps = queries.list_social_touchpoints(lid1)
        check(any("正式 CRM 客户" in tp["content"] for tp in converted_tps), "触达流水包含转客户事件记录")

        # 再次对现有同邮箱客户进行关联转换，测试幂等合并逻辑
        ok, msg, merged_cid = queries.convert_lead_to_contact(lid2, email=target_email)
        check(ok and merged_cid == new_cid, "相同邮箱潜客自动合并不重复建档")

        # ======================================================================
        # 7. CSV 导出测试
        # ======================================================================
        csv_content = queries.export_social_leads_csv([lead1_converted, lead2])
        check(csv_content.startswith("\ufeff"), "潜客 CSV 包含 UTF-8 BOM")
        check("联系人姓名" in csv_content and "WhatsApp/电话" in csv_content, "CSV 包含标准表头")
        check("Michael Schmidt" in csv_content, "CSV 包含潜客 1")
        check("Carlos Hernandez" in csv_content, "CSV 包含潜客 2")

        # ======================================================================
        # 8. 模板页面渲染测试
        # ======================================================================
        # 8.1 漏斗看板视图渲染
        kanban_cols = {}
        for s_key, s_info in queries.LEAD_STAGES.items():
            col_leads = [l for l in [lead1_converted, lead2] if l.get("stage") == s_key]
            kanban_cols[s_key] = {"stage_info": s_info, "count": len(col_leads), "leads": col_leads}

        html_kanban = render(
            "social_leads.html",
            view_mode="kanban",
            summary=summary,
            leads=[lead1_converted, lead2],
            kanban_columns=kanban_cols,
            current_platform="",
            current_stage="",
            current_followup="",
            search_kw="",
            today_str=queries._today(),
            csrf="test-token"
        )
        check("社媒外贸获客开发" in html_kanban, "看板页面包含页面大标题")
        check("漏斗看板" in html_kanban, "包含漏斗看板视图切换")
        check("Michael Schmidt" in html_kanban, "看板包含潜客 1 卡片")
        check("Carlos Hernandez" in html_kanban, "看板包含潜客 2 卡片")

        # 8.2 列表视图渲染
        html_list = render(
            "social_leads.html",
            view_mode="list",
            summary=summary,
            leads=[lead1_converted, lead2],
            kanban_columns=kanban_cols,
            current_platform="",
            current_stage="",
            current_followup="",
            search_kw="",
            today_str=queries._today(),
            csrf="test-token"
        )
        check("详细列表" in html_list, "列表页面包含表格表头")
        check("4915188889999" in html_list, "列表展示 WhatsApp 清洗后号码")

        # 8.3 潜客详情页渲染
        html_detail = render(
            "social_lead_detail.html",
            lead=lead1_converted,
            products=[test_prod],
            today_str=queries._today(),
            csrf="test-token"
        )
        check("潜客详情: TEST_SOC_Michael Schmidt" in html_detail, "详情页包含潜客姓名")
        check("WhatsApp 免存直聊" in html_detail, "详情页包含免存 WhatsApp 按钮")
        check("智能社媒破冰话术与开发信" in html_detail, "包含破冰话术生成器模块")
        check("社媒触达与沟通流水" in html_detail, "包含触达时间轴流水")

    finally:
        # ======================================================================
        # 9. 数据库零残留自动清理 (Teardown)
        # ======================================================================
        print("\n-- 正在清理 Phase 3 测试数据 --")
        with db.tx() as conn:
            for lid in test_lead_ids:
                conn.execute("DELETE FROM social_touchpoints WHERE lead_id = ?", (lid,))
                conn.execute("DELETE FROM social_leads WHERE id = ?", (lid,))
            for cid in test_contact_ids:
                conn.execute("DELETE FROM contacts WHERE id = ?", (cid,))
            for pid in test_product_ids:
                conn.execute("DELETE FROM products WHERE id = ?", (pid,))

        with db.ro() as conn:
            rem_leads = conn.execute(
                "SELECT COUNT(*) FROM social_leads WHERE name LIKE ?", (f"{PREFIX}%",)
            ).fetchone()[0]
            rem_contacts = conn.execute(
                "SELECT COUNT(*) FROM contacts WHERE name LIKE ?", (f"{PREFIX}%",)
            ).fetchone()[0]
            rem_prods = conn.execute(
                "SELECT COUNT(*) FROM products WHERE sku LIKE ?", (f"{PREFIX}%",)
            ).fetchone()[0]

        check(rem_leads == 0, "测试潜客线索已完全清理干净 (剩余 0)")
        check(rem_contacts == 0, "测试转化客户已完全清理干净 (剩余 0)")
        check(rem_prods == 0, "测试产品已完全清理干净 (剩余 0)")

    print("\n" + "=" * 66)
    print(f"Phase 3 专项测试结果：通过 {PASSED} 项 · 失败 {FAILED} 项")
    print("=" * 66)
    if FAILED == 0:
        print("社媒外贸获客开发与智能触达系统专项测试全部 100% 通过！\n")
    else:
        raise SystemExit(1)

if __name__ == "__main__":
    run_tests()
