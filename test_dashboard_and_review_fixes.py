"""看板指标净化与审核模块一致性全流程自动化测试。"""
import os
import sys
import tempfile
from pathlib import Path

# 设置独立测试数据目录
TMP = Path(tempfile.mkdtemp(prefix="mb_test_dash_review_"))
os.environ["MB_DATA_DIR"] = str(TMP / "data")
os.environ["MB_SECRET_KEY"] = "test_dash_review_secret_key_12345678901234567890"

from fastapi.testclient import TestClient
from app import auth, config, db, pipeline, queries
from app.templating import render
from app.web import app

client = TestClient(app)


def check(cond: bool, msg: str):
    if not cond:
        raise AssertionError(f"Test assertion failed: {msg}")
    print(f"  [ OK ] {msg}")


def test_dashboard_and_review_consistency():
    print("\n-- 1. 看板指标与审核台一致性核心逻辑测试 --")
    db.init_db()

    # 1. 验证目标邮件分类定义
    check("inquiry" in queries.TARGET_CATEGORIES, "目标分类包含 inquiry")
    check("quote" in queries.TARGET_CATEGORIES, "目标分类包含 quote")
    check("after_sale" in queries.TARGET_CATEGORIES, "目标分类包含 after_sale")
    check("complaint" in queries.TARGET_CATEGORIES, "目标分类包含 complaint")
    check("spam" not in queries.TARGET_CATEGORIES, "目标分类严格排除 spam")
    check("other" not in queries.TARGET_CATEGORIES, "目标分类严格排除 other")

    # 创建测试客户
    ok, _, c_high = queries.save_contact({
        "email": "lead_high@buyer.com",
        "name": "High Lead",
        "company": "Buyer Inc",
        "country": "USA",
        "score": 85,
        "stage": "engaging",
    })
    ok, _, c_low = queries.save_contact({
        "email": "lead_low@spamcorp.com",
        "name": "Low Lead",
        "company": "Spam Corp",
        "country": "Unknown",
        "score": 20,
        "stage": "spam",
    })
    ok, _, c_quoted = queries.save_contact({
        "email": "lead_quoted@customer.de",
        "name": "Quoted Lead",
        "company": "Customer GmbH",
        "country": "Germany",
        "score": 60,
        "stage": "quoted",
    })

    with db.tx() as conn:
        # 会话 1 (目标客户)
        cur = conn.execute("INSERT INTO threads (contact_id, subject, status, last_ts) VALUES (?, 'Inquiry X', 'open', ?)",
                           (c_high, db.now_ts()))
        tid_1 = cur.lastrowid

        # 会话 2 (无效邮件)
        cur = conn.execute("INSERT INTO threads (contact_id, subject, status, last_ts) VALUES (?, 'Spam Promo', 'open', ?)",
                           (c_low, db.now_ts()))
        tid_2 = cur.lastrowid

        # 会话 3 (报价咨询)
        cur = conn.execute("INSERT INTO threads (contact_id, subject, status, last_ts) VALUES (?, 'Quote Request', 'open', ?)",
                           (c_quoted, db.now_ts()))
        tid_3 = cur.lastrowid

        # 邮件 1: 有效询盘 (inquiry, score=85, unread)
        cur = conn.execute(
            """INSERT INTO messages (thread_id, contact_id, fingerprint, direction, from_addr, to_addr,
               subject, body_text, category, score, is_read, status, sent_ts, created_ts)
               VALUES (?, ?, 'fp-1', 'in', 'lead_high@buyer.com', 'sales@ourco.com',
               'Inquiry for 1000 pcs', 'Need quote for 1000 pcs', 'inquiry', 85, 0, 'drafted', ?, ?)""",
            (tid_1, c_high, db.now_ts(), db.now_ts())
        )
        mid_inquiry = cur.lastrowid

        # 邮件 2: 垃圾邮件 (spam, score=10, unread)
        cur = conn.execute(
            """INSERT INTO messages (thread_id, contact_id, fingerprint, direction, from_addr, to_addr,
               subject, body_text, category, score, is_read, status, sent_ts, created_ts)
               VALUES (?, ?, 'fp-2', 'in', 'lead_low@spamcorp.com', 'sales@ourco.com',
               'Cheap SEO Services', 'Buy SEO now', 'spam', 10, 0, 'processed', ?, ?)""",
            (tid_2, c_low, db.now_ts(), db.now_ts())
        )
        mid_spam = cur.lastrowid

        # 邮件 3: 无关其他邮件 (other, score=30, unread)
        cur = conn.execute(
            """INSERT INTO messages (thread_id, contact_id, fingerprint, direction, from_addr, to_addr,
               subject, body_text, category, score, is_read, status, sent_ts, created_ts)
               VALUES (?, ?, 'fp-3', 'in', 'other@mail.com', 'sales@ourco.com',
               'Hello random notice', 'Just notice', 'other', 30, 0, 'processed', ?, ?)""",
            (tid_2, c_low, db.now_ts(), db.now_ts())
        )
        mid_other = cur.lastrowid

        # 邮件 4: 报价咨询 (quote, score=75, unread)
        cur = conn.execute(
            """INSERT INTO messages (thread_id, contact_id, fingerprint, direction, from_addr, to_addr,
               subject, body_text, category, score, is_read, status, sent_ts, created_ts)
               VALUES (?, ?, 'fp-4', 'in', 'lead_quoted@customer.de', 'sales@ourco.com',
               'Request quotation', 'Please send price list', 'quote', 75, 0, 'drafted', ?, ?)""",
            (tid_3, c_quoted, db.now_ts(), db.now_ts())
        )
        mid_quote = cur.lastrowid

        # 草稿 1: 关联有效询盘 (pending)
        cur = conn.execute(
            """INSERT INTO drafts (message_id, contact_id, subject, body_text, status, created_ts)
               VALUES (?, ?, 'Re: Inquiry for 1000 pcs', 'Thank you for inquiry...', 'pending', ?)""",
            (mid_inquiry, c_high, db.now_ts())
        )
        did_valid1 = cur.lastrowid

        # 草稿 2: 关联垃圾邮件 (由于误操作或历史规则遗留产生 pending 草稿)
        cur = conn.execute(
            """INSERT INTO drafts (message_id, contact_id, subject, body_text, status, created_ts)
               VALUES (?, ?, 'Re: Cheap SEO Services', 'No thanks...', 'pending', ?)""",
            (mid_spam, c_low, db.now_ts())
        )
        did_spam = cur.lastrowid

        # 草稿 3: 关联报价邮件 (pending)
        cur = conn.execute(
            """INSERT INTO drafts (message_id, contact_id, subject, body_text, status, created_ts)
               VALUES (?, ?, 'Re: Request quotation', 'Here is our price...', 'pending', ?)""",
            (mid_quote, c_quoted, db.now_ts())
        )
        did_valid2 = cur.lastrowid

    # 2. 检查 pending_review_count()：应该只有 2 个（mid_inquiry 和 mid_quote），排除 mid_spam
    cnt = queries.pending_review_count()
    check(cnt == 2, f"pending_review_count() 应排除垃圾邮件草稿，实际为 {cnt}")

    # 3. 检查 pending_drafts()：列表应该只返回 2 个草稿
    p_drafts = queries.pending_drafts()
    check(len(p_drafts) == 2, f"pending_drafts() 应返回 2 个草稿，实际为 {len(p_drafts)}")
    draft_msg_ids = [d["message_id"] for d in p_drafts]
    check(mid_spam not in draft_msg_ids, "pending_drafts() 中不应含有垃圾邮件草稿")
    check(mid_inquiry in draft_msg_ids and mid_quote in draft_msg_ids, "pending_drafts() 包含目标询盘与报价草稿")

    # 4. 执行 clean_invalid_pending_drafts()
    cleaned = queries.clean_invalid_pending_drafts()
    check(cleaned >= 1, f"clean_invalid_pending_drafts 应清理至少 1 个无效草稿，清理数: {cleaned}")
    with db.ro() as conn:
        spam_draft_status = conn.execute("SELECT status FROM drafts WHERE id=?", (did_spam,)).fetchone()["status"]
        check(spam_draft_status == "cancelled", f"垃圾邮件草稿状态应自动置为 cancelled，实际为 {spam_draft_status}")

    # 5. 看板指标与过滤口径验证
    d = queries.dashboard()
    print("  Dashboard metrics:", d)
    check(d["today_in"] == 2, f"今日有效询盘应为 2 (排除 spam 和 other)，实际为 {d['today_in']}")
    check(d["high_intent"] == 2, f"高意向目标询盘应为 2 (85分和75分)，实际为 {d['high_intent']}")
    check(d["pending_review"] == 2, f"看板待人工审核应为 2 (与审核台完全同步)，实际为 {d['pending_review']}")
    check(d["pending_review"] == len(queries.pending_drafts()), "看板待审数量与 pending_drafts 列表长度 100% 一致")
    check(d["unread"] == 2, f"有效未读邮件应为 2 (排除 spam 和 other)，实际为 {d['unread']}")
    # 客户统计：c_high (score=85, stage=engaging), c_quoted (stage=quoted) 计入；c_low (stage=spam, score=20) 排除
    check(d["total_contacts"] == 2, f"累计客户应仅统计高意向及跟进客户 (共2位)，实际为 {d['total_contacts']}")

    # 6. 近 30 日分类分布验证
    cats = queries.category_breakdown(30)
    cat_names = [c["cat"] for c in cats]
    print("  Category breakdown:", cats)
    check("inquiry" in cat_names and "quote" in cat_names, "分类分布包含 inquiry 和 quote")
    check("spam" not in cat_names, "分类分布严格剔除 spam")
    check("other" not in cat_names, "分类分布严格剔除 other")

    # 7. 近 7 日趋势验证
    pipeline.refresh_daily_stats()
    tr = queries.trend(7)
    today_label = tr["labels"][-1]
    today_in_count = tr["in_count"][-1]
    today_high_intent = tr["high_intent"][-1]
    check(today_in_count == 2, f"趋势图今日入信仅统计目标邮件 (2封)，实际为 {today_in_count}")
    check(today_high_intent == 2, f"趋势图高意向数仅统计目标邮件 (2封)，实际为 {today_high_intent}")

    # 8. 全局侧边栏红标验证 (templating.render)
    html_rendered = render("contacts.html", contacts=[], page="contacts")
    check('nav-item  " href="/review">审核<span class="nav-badge">2</span></a>' in html_rendered or
          'href="/review">审核<span class="nav-badge">2</span>' in html_rendered,
          "侧边栏在存在 2 个待审草稿时必须渲染红标 2")

    # 处理/审核通过所有草稿后，红标与待审数应同步归零并消失
    with db.tx() as conn:
        conn.execute("UPDATE drafts SET status='approved' WHERE status='pending'")

    check(queries.pending_review_count() == 0, "待审草稿全部处理后 pending_review_count 变为 0")
    d_after = queries.dashboard()
    check(d_after["pending_review"] == 0, "看板待审数量归零")
    html_zero = render("contacts.html", contacts=[], page="contacts")
    check('<span class="nav-badge">' not in html_zero, "待审为 0 时侧边栏红标彻底隐藏")


def test_web_routes():
    print("\n-- 2. Web 路由与页面渲染端到端测试 --")
    sid = auth.make_session("admin")
    client.cookies.set("mb_sess", sid)

    # 1. 访问看板首页
    resp = client.get("/")
    check(resp.status_code == 200, "GET / 响应 200")
    html = resp.text
    check("今日询盘" in html, "看板包含 '今日询盘' 文案")
    check("近 7 日询盘趋势" in html, "看板包含 '近 7 日询盘趋势' 文案")
    check("仅统计有效目标邮件" in html, "看板包含剔除无效邮件提示说明")
    check("近 30 日分类分布（目标邮件）" in html, "看板包含目标邮件分类分布说明")

    # 2. 访问审核台
    resp_review = client.get("/review")
    check(resp_review.status_code == 200, "GET /review 响应 200")
    print("  Web routes verified successfully.")


if __name__ == "__main__":
    try:
        test_dashboard_and_review_consistency()
        test_web_routes()
        print("\n==========================================")
        print("ALL TESTS PASSED: 看板与审核台一致性测试全部通过！")
        print("==========================================\n")
    except Exception as e:
        import traceback
        traceback.print_exc()
        sys.exit(1)
