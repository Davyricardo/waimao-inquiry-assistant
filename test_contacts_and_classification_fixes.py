"""客户管理分页容错、垃圾客户防污染与历史清洗、询盘邮件智能分类优化与货代液态玻璃 UI 自动化测试。"""
import os
import sys
import tempfile
from pathlib import Path

# 设置独立测试数据目录
TMP = Path(tempfile.mkdtemp(prefix="mb_test_contacts_class_"))
os.environ["MB_DATA_DIR"] = str(TMP / "data")
os.environ["MB_SECRET_KEY"] = "test_contacts_class_secret_key_12345678901234567890"

from fastapi.testclient import TestClient
from app import ai_agent, auth, config, db, mail_engine, pipeline, queries
from app.templating import render
from app.web import app

client = TestClient(app)


def check(cond: bool, msg: str):
    if not cond:
        raise AssertionError(f"Test assertion failed: {msg}")
    print(f"  [ OK ] {msg}")


def test_pagination_and_template_arithmetic():
    print("\n-- 1. 模板引擎简单算术与客户管理分页容错测试 --")
    db.init_db()

    # 1. 验证 templating._resolve 支持 a.page + 1 / a.page - 1
    from app.templating import _resolve
    ctx = {"data": {"page": 2, "pages": 5}}
    next_val = _resolve("data.page + 1", ctx)
    prev_val = _resolve("data.page - 1", ctx)
    check(next_val == 3, f"data.page + 1 应该解析为 3，实际为 {next_val}")
    check(prev_val == 1, f"data.page - 1 应该解析为 1，实际为 {prev_val}")

    # 2. 验证 queries.list_contacts 与 list_messages 返回 prev_page 和 next_page
    c_res = queries.list_contacts(page=2, size=5)
    check("prev_page" in c_res and "next_page" in c_res, "list_contacts 包含 prev_page 与 next_page")
    m_res = queries.list_messages(page=1, size=5)
    check("prev_page" in m_res and "next_page" in m_res, "list_messages 包含 prev_page 与 next_page")

    # 3. 登录并请求 GET /contacts?page= （模拟用户点击或传空字符串）
    sid = auth.make_session("admin")
    client.cookies.set("mb_sess", sid)

    resp_empty = client.get("/contacts?page=")
    check(resp_empty.status_code == 200, f"GET /contacts?page= 应该返回 200 而非 422 报错，实际状态: {resp_empty.status_code}")

    resp_p2 = client.get("/contacts?page=2")
    check(resp_p2.status_code == 200, f"GET /contacts?page=2 应该返回 200，实际状态: {resp_p2.status_code}")

    # 4. 邮件列表分页容错
    resp_msg_empty = client.get("/messages?page=")
    check(resp_msg_empty.status_code == 200, f"GET /messages?page= 应该返回 200，实际状态: {resp_msg_empty.status_code}")

    # 5. 页面渲染分页链接测试：确保下一页 href 包含具体数字而不是空
    dummy_data = {
        "items": [{"id": 1, "email": "client@example.com", "name": "Client", "stage": "engaging", "score": 80}],
        "total": 30, "page": 1, "size": 10, "pages": 3,
        "prev_page": 1, "next_page": 2
    }
    rendered_html = render("contacts.html", data=dummy_data, f={"q": "", "stage": "", "min_score": ""}, csrf="dummy")
    check('page=2' in rendered_html, "渲染的 contacts.html 中包含 page=2 的分页链接")
    check('page=&' not in rendered_html and 'page="' not in rendered_html, "分页链接中绝不出现空的 page= 参数")


def test_contacts_anti_pollution_and_cleaning():
    print("\n-- 2. 客户管理垃圾发件人防污染与历史清洗测试 --")
    db.init_db()

    # 创建一个纯垃圾邮件客户
    with db.tx() as conn:
        cur = conn.execute(
            """INSERT INTO contacts (email, name, source, first_seen_ts, last_seen_ts, msg_count, reply_count, score, stage)
               VALUES ('spam_bot@seo-scam.com', 'SEO Bot', 'email', ?, ?, 1, 0, 10, 'new')""",
            (db.now_ts(), db.now_ts())
        )
        cid_spam = cur.lastrowid

        # 插入关联线索
        cur_t = conn.execute(
            "INSERT INTO threads (contact_id, subject, status, last_ts) VALUES (?, 'SEO offer', 'open', ?)",
            (cid_spam, db.now_ts())
        )
        tid = cur_t.lastrowid

        # 插入一封对应的 spam 邮件
        conn.execute(
            """INSERT INTO messages (thread_id, contact_id, fingerprint, direction, from_addr, to_addr,
               subject, body_text, category, score, is_read, status, sent_ts, created_ts)
               VALUES (?, ?, 'fp-spam-clean', 'in', 'spam_bot@seo-scam.com', 'sales@ourco.com',
               'Buy SEO traffic', 'Cheap SEO services for you', 'spam', 10, 0, 'processed', ?, ?)""",
            (tid, cid_spam, db.now_ts(), db.now_ts())
        )

    # 1. 验证 clean_spam_contacts() 能自动将纯 spam 发件人 stage 置为 spam
    cleaned = queries.clean_spam_contacts()
    check(cleaned >= 1, f"clean_spam_contacts 应该清洗至少 1 个垃圾客户，实际清洗: {cleaned}")

    with db.ro() as conn:
        row = conn.execute("SELECT stage FROM contacts WHERE id=?", (cid_spam,)).fetchone()
        check(row["stage"] == "spam", f"垃圾发件人阶段应被置为 spam，实际为: {row['stage']}")

    # 2. 验证默认 list_contacts() 不展示 spam 客户
    c_list = queries.list_contacts()
    c_emails = [c["email"] for c in c_list["items"]]
    check("spam_bot@seo-scam.com" not in c_emails, "默认客户列表自动过滤排除 spam 客户")

    # 3. 显式选择 stage='spam' 可以查到该客户
    c_spam_list = queries.list_contacts(stage="spam")
    c_spam_emails = [c["email"] for c in c_spam_list["items"]]
    check("spam_bot@seo-scam.com" in c_spam_emails, "显式筛选 stage=spam 时能查看到垃圾客户")


def test_inquiry_classification_optimization():
    print("\n-- 3. 询盘邮件分类标记算法优化与 Subject 权重测试 --")

    # 案例 A: 主题含有 Inquiry，正文同时提到 FOB、price 和 MOQ（外贸典型询盘问价场景）
    subj_a = "Re: Inquiry about model A-1200"
    body_a = (
        "Dear Sales team,\n\n"
        "We are interested in purchasing 5,000 pcs of model A-1200.\n"
        "Please quote us your best FOB Shenzhen price and MOQ.\n"
        "We are a distributor in Germany and this could become an annual order.\n\n"
        "Best regards,\nKlaus Mueller\n"
    )
    res_a = ai_agent.rule_analyze(subj_a, body_a, "k.mueller@mueller-handel.de")
    print(f"  案例 A (Inquiry about model A-1200) -> 类别: {res_a['category']}, 分数: {res_a['score']}")
    check(res_a["category"] == "inquiry", f"主题含有 Inquiry 的采购询盘必须归类为 inquiry，严禁误判为 {res_a['category']}")
    check(res_a["score"] >= 70, f"意向采购邮件意向分应高意向 (>=70)，实际为: {res_a['score']}")

    # 案例 B: 商业采购意向，问能否提供产品规格和目录
    subj_b = "RFQ for Solar Inverters 5KW"
    body_b = "Hi, we are looking for 5KW hybrid solar inverters. Can you supply this model? Please provide datasheet."
    res_b = ai_agent.rule_analyze(subj_b, body_b, "buyer@solarpower.com")
    print(f"  案例 B (RFQ for Solar Inverters) -> 类别: {res_b['category']}, 分数: {res_b['score']}")
    check(res_b["category"] == "inquiry", f"RFQ 意向必须归为 inquiry，实际为: {res_b['category']}")

    # 案例 C: 深入议价 / 还价 / 索取形式发票（真正属于 quote 的场景）
    subj_c = "Target price and Proforma Invoice for order #889"
    body_c = "Dear sales, your price is a bit high. Our counter-offer is USD 28.00/pc. If acceptable, please issue the Proforma Invoice (PI) immediately."
    res_c = ai_agent.rule_analyze(subj_c, body_c, "buyer@vipclient.com")
    print(f"  案例 C (Counter-offer & PI) -> 类别: {res_c['category']}, 分数: {res_c['score']}")
    check(res_c["category"] == "quote", f"明确还价与索要形式发票应归为 quote，实际为: {res_c['category']}")

    # 案例 D: 潜在买家泛泛询问，绝不能误判为 other
    subj_d = "Product query from buyer"
    body_d = "Hello, do you manufacture custom plastic parts? What is your factory delivery time?"
    res_d = ai_agent.rule_analyze(subj_d, body_d, "john@plastics.co.uk")
    print(f"  案例 D (General product query) -> 类别: {res_d['category']}")
    check(res_d["category"] == "inquiry", f"有效客户商业咨询严禁误判为 other，实际为: {res_d['category']}")


def test_freight_liquid_ui():
    print("\n-- 4. 货运代理模块 iOS 液态磨砂玻璃 UI 渲染测试 --")
    sid = auth.make_session("admin")
    client.cookies.set("mb_sess", sid)

    resp = client.get("/freight")
    check(resp.status_code == 200, "GET /freight 正常返回 200")
    html = resp.text

    # 验证选项卡升级为 glass-tabs
    check("glass-tabs" in html, "货代页面顶部包含 .glass-tabs 胶囊导航类")
    check("glass-tab " in html or "glass-tab active" in html, "选项卡按钮使用 .glass-tab 样式")

    # 验证卡片使用 glass-card
    check("glass-card" in html, "货代页面卡片应用了 .glass-card 液态磨砂玻璃效果")

    # 验证报价库与货代名录 tab
    resp_quotes = client.get("/freight?tab=quotes")
    check(resp_quotes.status_code == 200, "GET /freight?tab=quotes 正常返回 200")
    check("glass-card" in resp_quotes.text, "渠道报价库页面包含 .glass-card")

    resp_fwds = client.get("/freight?tab=forwarders")
    check(resp_fwds.status_code == 200, "GET /freight?tab=forwarders 正常返回 200")
    check("glass-card" in resp_fwds.text, "货代名录页面包含 .glass-card")


if __name__ == "__main__":
    try:
        test_pagination_and_template_arithmetic()
        test_contacts_anti_pollution_and_cleaning()
        test_inquiry_classification_optimization()
        test_freight_liquid_ui()
        print("\n==================================================================")
        print("ALL TESTS PASSED: 客户分页、防污染清洗、分类优化与货代UI全部通过！")
        print("==================================================================\n")
    except Exception as e:
        import traceback
        traceback.print_exc()
        sys.exit(1)
