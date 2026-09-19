"""审核模块优化、会话详情客户次级菜单、凭证管理与本地 OCR 智能识别全流程自动化测试。"""
import os
import sys
import tempfile
from pathlib import Path

# 设置独立测试数据目录
TMP = Path(tempfile.mkdtemp(prefix="mb_test_review_ocr_"))
os.environ["MB_DATA_DIR"] = str(TMP / "data")
os.environ["MB_SECRET_KEY"] = "test_review_ocr_secret_key_12345678901234567890"

from fastapi.testclient import TestClient
from app import auth, config, crypto_util, db, ocr_engine, pipeline, queries
from app.web import app

client = TestClient(app)


def check(cond: bool, msg: str):
    if not cond:
        raise AssertionError(f"Test assertion failed: {msg}")
    print(f"  [ OK ] {msg}")


def test_review_filter_and_token_optimization():
    print("\n-- 1. 审核台过滤与目标邮件 AI 摘要 Token 节约测试 --")
    db.init_db()

    # 创建测试客户
    ok, msg, cid = queries.save_contact({
        "email": "client@globalimport.com",
        "name": "John Doe",
        "company": "Global Import LLC",
        "country": "USA"
    })

    # 创建会话
    with db.tx() as conn:
        cur = conn.execute("INSERT INTO threads (contact_id, subject, status, last_ts) VALUES (?, 'Test Thread', 'open', ?)",
                           (cid, db.now_ts()))
        tid = cur.lastrowid

        # 插入一封询盘邮件 (目标邮件: inquiry)
        cur = conn.execute(
            """INSERT INTO messages (thread_id, contact_id, fingerprint, direction, from_addr, to_addr, subject, body_text,
               category, score, summary_cn, key_points_cn, status, sent_ts, created_ts)
               VALUES (?, ?, 'fp-inq-1', 'in', 'client@globalimport.com', 'sales@ourco.com', 'Inquiry for Model X', 'Please quote us 500 pcs of Model X',
               'inquiry', 90, '客户询求 Model X 500台报价', '["需求500台", "要求速报"]', 'drafted', ?, ?)""",
            (tid, cid, db.now_ts(), db.now_ts())
        )
        mid_inquiry = cur.lastrowid

        # 插入草稿 (询盘)
        conn.execute(
            """INSERT INTO drafts (message_id, contact_id, subject, body_text, ai_model, status, created_ts)
               VALUES (?, ?, 'Re: Inquiry for Model X', 'Dear John, quote as follows...', 'deepseek-chat', 'pending', ?)""",
            (mid_inquiry, cid, db.now_ts())
        )

        # 插入一封垃圾邮件 (非目标邮件: spam)
        cur = conn.execute(
            """INSERT INTO messages (thread_id, contact_id, fingerprint, direction, from_addr, to_addr, subject, body_text,
               category, score, summary_cn, status, sent_ts, created_ts)
               VALUES (?, ?, 'fp-spam-1', 'in', 'spammer@junk.com', 'sales@ourco.com', 'Buy cheap crypto now', 'Visit our link for free coins',
               'spam', 10, NULL, 'drafted', ?, ?)""",
            (tid, cid, db.now_ts(), db.now_ts())
        )
        mid_spam = cur.lastrowid
        conn.execute(
            """INSERT INTO drafts (message_id, contact_id, subject, body_text, ai_model, status, created_ts)
               VALUES (?, ?, 'Re: Buy cheap crypto', 'Ignored...', 'template', 'pending', ?)""",
            (mid_spam, cid, db.now_ts())
        )

        # 插入一封其他邮件 (非目标邮件: other)
        cur = conn.execute(
            """INSERT INTO messages (thread_id, contact_id, fingerprint, direction, from_addr, to_addr, subject, body_text,
               category, score, summary_cn, status, sent_ts, created_ts)
               VALUES (?, ?, 'fp-other-1', 'in', 'notify@service.com', 'sales@ourco.com', 'System Notification', 'Server reboot scheduled',
               'other', 20, NULL, 'drafted', ?, ?)""",
            (tid, cid, db.now_ts(), db.now_ts())
        )
        mid_other = cur.lastrowid
        conn.execute(
            """INSERT INTO drafts (message_id, contact_id, subject, body_text, ai_model, status, created_ts)
               VALUES (?, ?, 'Re: System Notification', 'Noted.', 'template', 'pending', ?)""",
            (mid_other, cid, db.now_ts())
        )

    # 1. 验证 pending_drafts 严格过滤
    drafts = queries.pending_drafts(50)
    categories = [d["category"] for d in drafts]
    check("spam" not in categories, "审核台已严格排除 spam 垃圾邮件")
    check("other" not in categories, "审核台已严格排除 other 其他邮件")
    check("inquiry" in categories, "审核台正确包含目标询盘邮件")

    # 2. 验证已保存的 AI 摘要读取
    inquiry_draft = next(d for d in drafts if d["category"] == "inquiry")
    check(inquiry_draft.get("summary_cn") == "客户询求 Model X 500台报价", "审核台草稿能够正确读取已保存的中文摘要")

    # 3. 测试审核台页面渲染
    sess = auth.make_session("admin")
    resp = client.get("/review", cookies={"mb_sess": sess})
    check(resp.status_code == 200, "GET /review 返回 200 OK")
    check("客户询求 Model X 500台报价" in resp.text, "审核台页面右侧正确回显持久化 AI 摘要")
    check("Buy cheap crypto" not in resp.text, "审核台页面未渲染垃圾邮件草稿")
    check("System Notification" not in resp.text, "审核台页面未渲染其他类别草稿")


def test_message_detail_return_and_sub_menu():
    print("\n-- 2. 会话详情页返回审核台与客户次级菜单测试 --")
    sess = auth.make_session("admin")

    with db.ro() as conn:
        mid = conn.execute("SELECT id FROM messages WHERE category='inquiry' LIMIT 1").fetchone()[0]

    resp = client.get(f"/messages/{mid}", cookies={"mb_sess": sess})
    check(resp.status_code == 200, f"GET /messages/{mid} 正常返回 200")
    check('href="/review">← 返回审核台</a>' in resp.text, "会话详情页返回链接明确指向审核台 /review")
    check("查看客户 ▾" in resp.text, "查看客户功能已改造为次级菜单按钮")
    check("Global Import LLC" in resp.text, "次级菜单中正确匹配并展示已存在的客户公司")

    # 测试全新发件人（无客户档案）
    with db.tx() as conn:
        cur = conn.execute(
            """INSERT INTO messages (thread_id, contact_id, fingerprint, direction, from_addr, to_addr, subject, body_text,
               category, score, status, sent_ts, created_ts)
               VALUES (1, NULL, 'fp-new-1', 'in', 'brandnew@stranger.org', 'sales@ourco.com', 'New Inquiry Unknown', 'Hello need catalog',
               'inquiry', 85, 'drafted', ?, ?)""",
            (db.now_ts(), db.now_ts())
        )
        new_mid = cur.lastrowid

    resp_new = client.get(f"/messages/{new_mid}", cookies={"mb_sess": sess})
    check(resp_new.status_code == 200, "全新发件人详情页访问正常")
    check("暂无客户信息（检测为新客户线索）" in resp_new.text, "新发件人次级菜单正确提示暂无客户信息")
    check("新增客户信息" in resp_new.text, "新发件人次级菜单提供一键新增客户信息入口")
    check('id="fastContactModal"' in resp_new.text, "页面包含快速新增客户弹窗")


def test_vouchers_management_and_ocr():
    print("\n-- 3. 凭证管理模块与本地 OCR 识别测试 --")
    sess = auth.make_session("admin")

    # 1. 列表页面访问
    resp = client.get("/vouchers", cookies={"mb_sess": sess})
    check(resp.status_code == 200, "GET /vouchers 导航与页面返回 200 OK")
    check("凭证管理" in resp.text, "页面标题正确包含「凭证管理」")
    check("本地 OCR 智能识别" in resp.text, "页面包含本地 OCR 识别提示")

    # 2. 本地 OCR 文本解析测试 (Voucher)
    raw_invoice = """
    COMMERCIAL INVOICE
    Invoice No: INV-20260919-88
    Date: 2026-09-19
    Shipper: Shenzhen Forward Machinery Co., Ltd.
    Consignee: Hamburg European Tools GmbH
    Description of Goods: Heavy Duty Servo Motors 2.2KW
    TOTAL AMOUNT: USD 36,800.00
    """
    v_info = ocr_engine.parse_voucher_info(raw_invoice)
    check(v_info["voucher_no"] == "INV-20260919-88", f"本地提取发票单号准确: {v_info['voucher_no']}")
    check(v_info["voucher_type"] == "commercial_invoice", f"本地单证类型研判准确: {v_info['voucher_type']}")
    check(v_info["currency"] == "USD", f"提取币种准确: {v_info['currency']}")
    check(v_info["amount"] == 36800.0, f"提取总金额准确: {v_info['amount']}")

    # 3. 路由 OCR 测试接口
    resp_ocr = client.post("/vouchers/ocr", data={"raw_text": raw_invoice}, cookies={"mb_sess": sess})
    check(resp_ocr.status_code == 200, "POST /vouchers/ocr 接口响应 200")
    json_data = resp_ocr.json()
    check(json_data["ok"] is True, "OCR 接口返回 ok=True")
    check(json_data["data"]["amount"] == 36800.0, "接口解析金额无误")

    # 4. 新建凭证录入
    csrf = auth.csrf_token(sess)
    resp_create = client.post("/vouchers", data={
        "voucher_no": "INV-20260919-88",
        "voucher_type": "commercial_invoice",
        "title": "商业发票 INV-20260919-88",
        "trade_date": "2026-09-19",
        "currency": "USD",
        "amount": 36800.0,
        "shipper": "Shenzhen Forward Machinery Co., Ltd.",
        "consignee": "Hamburg European Tools GmbH",
        "product_desc": "Heavy Duty Servo Motors 2.2KW",
        "ocr_status": "success",
        "ocr_raw_text": raw_invoice,
        "status": "confirmed",
        "notes": "电子扫描件已归档",
        "csrf_tok": csrf,
    }, cookies={"mb_sess": sess}, follow_redirects=False)
    check(resp_create.status_code == 303, "POST /vouchers 提交并重定向成功 (303)")

    # 5. 校验凭证入库数据
    with db.ro() as conn:
        row = conn.execute("SELECT * FROM vouchers WHERE voucher_no='INV-20260919-88'").fetchone()
        check(row is not None, "数据库成功持久化单证记录")
        vid = row["id"]
        check(row["amount"] == 36800.0, "入库金额与币种一致")

    # 6. 单据详情 JSON
    resp_detail = client.get(f"/vouchers/{vid}", cookies={"mb_sess": sess})
    check(resp_detail.status_code == 200, "GET /vouchers/{vid} 返回 200")
    check(resp_detail.json()["data"]["voucher_no"] == "INV-20260919-88", "详情 JSON 正确返回发票单号")

    # 7. 批量操作：更新状态为 archived
    resp_batch_st = client.post("/vouchers/batch", data={
        "batch_action": "status",
        "target_status": "archived",
        "vids": [vid],
        "csrf_tok": csrf,
    }, cookies={"mb_sess": sess}, follow_redirects=False)
    check(resp_batch_st.status_code == 303, "批量修改状态响应 303")
    with db.ro() as conn:
        st = conn.execute("SELECT status FROM vouchers WHERE id=?", (vid,)).fetchone()[0]
        check(st == "archived", "凭证状态已批量更新为 archived")

    # 8. 批量导出 CSV
    resp_export = client.post("/vouchers/batch", data={
        "batch_action": "export",
        "vids": [vid],
        "csrf_tok": csrf,
    }, cookies={"mb_sess": sess})
    check(resp_export.status_code == 200, "批量导出 CSV 返回 200")
    check("INV-20260919-88" in resp_export.text, "导出的 CSV 内容包含该发票单号")

    # 9. 批量删除
    resp_batch_del = client.post("/vouchers/batch", data={
        "batch_action": "delete",
        "vids": [vid],
        "csrf_tok": csrf,
    }, cookies={"mb_sess": sess}, follow_redirects=False)
    check(resp_batch_del.status_code == 303, "批量删除响应 303")
    with db.ro() as conn:
        cnt = conn.execute("SELECT COUNT(*) FROM vouchers WHERE id=?", (vid,)).fetchone()[0]
        check(cnt == 0, "凭证已彻底从数据库删除")


def test_contacts_ocr_module():
    print("\n-- 4. 客户管理名片与社媒 OCR 智能识别测试 --")
    sess = auth.make_session("admin")

    raw_card = """
    Michael Schmidt
    Purchasing Director
    Schmidt Industrial Tools GmbH
    Email: m.schmidt@schmidt-tools.de
    Phone: +49 89 87654321
    WhatsApp: +49 89 87654321
    LinkedIn: linkedin.com/in/michael-schmidt-procurement
    Website: www.schmidt-tools.de
    Munich, Germany
    """

    # 1. 测试名片解析提取
    c_info = ocr_engine.parse_contact_info(raw_card)
    check(c_info["name"] == "Michael Schmidt", f"名片姓名识别准确: {c_info['name']}")
    check(c_info["company"] == "Schmidt Industrial Tools GmbH", f"名片公司识别准确: {c_info['company']}")
    check(c_info["email"] == "m.schmidt@schmidt-tools.de", f"名片邮箱提取准确: {c_info['email']}")
    check("+49 89 87654321" in c_info["phone"], f"电话/WhatsApp提取准确: {c_info['phone']}")
    check("Germany" in c_info["country"] or "德国" in c_info["country"], f"推断国家准确: {c_info['country']}")

    # 2. 测试 POST /contacts/ocr 接口
    resp_c_ocr = client.post("/contacts/ocr", data={"raw_text": raw_card}, cookies={"mb_sess": sess})
    check(resp_c_ocr.status_code == 200, "POST /contacts/ocr 返回 200 OK")
    c_res_json = resp_c_ocr.json()
    check(c_res_json["ok"] is True, "名片识别返回 ok=True")
    check(c_res_json["data"]["email"] == "m.schmidt@schmidt-tools.de", "名片识别返回邮箱无误")

    # 3. 一键录入客户库
    csrf = auth.csrf_token(sess)
    resp_save_c = client.post("/contacts/save", data={
        "name": c_info["name"],
        "email": c_info["email"],
        "company": c_info["company"],
        "country": c_info["country"],
        "stage": "new",
        "note": f"Phone: {c_info['phone']}\nLinkedIn: {c_info['linkedin_url']}",
        "csrf_tok": csrf,
    }, cookies={"mb_sess": sess}, follow_redirects=False)
    check(resp_save_c.status_code == 303, "客户保存响应 303 重定向")

    with db.ro() as conn:
        c_row = conn.execute("SELECT * FROM contacts WHERE email='m.schmidt@schmidt-tools.de'").fetchone()
        check(c_row is not None, "OCR 识别客户已成功存入客户数据库")
        check(c_row["company"] == "Schmidt Industrial Tools GmbH", "存入客户公司名一致")

    # 4. 检查 contacts 页面渲染
    resp_contacts_ui = client.get("/contacts", cookies={"mb_sess": sess})
    check("名片/社媒智能识别" in resp_contacts_ui.text, "客户管理页面已包含名片/社媒智能识别入口按钮")
    check('id="contact-ocr-modal"' in resp_contacts_ui.text, "客户管理页面包含名片 OCR 识别弹窗")


def test_draft_send_and_header_safety():
    print("\n-- 5. 草稿发送长标题/换行容错与防 500 异常测试 --")
    from unittest.mock import patch
    from app import sender

    sess = auth.make_session("admin")
    csrf = auth.csrf_token(sess)

    # 1. 直接测试 sender.send_mail 标题换行与长标题容错 (过去会抛 ValueError: Header values may not contain linefeed)
    fake_account = {
        "id": 1,
        "email": "sales@ourcompany.com",
        "display_name": "外贸业务部 专员",
        "secret_enc": crypto_util.encrypt_secret("fake_auth_code_123"),
        "smtp_host": "127.0.0.1",
        "smtp_port": 465,
    }
    long_multiline_subj = (
        "Re: Urgent: Quotation required for Heavy Duty Industrial Servo Motors 2.2KW\n"
        "with custom planetary gearbox and 17-bit encoder options: 2026 最新工业级电机\r\n"
        "特需配置询价"
    )

    # 模拟 SMTP 发送，验证 msg 对象在组装与折行时不再抛出任何 ValueError
    with patch("smtplib.SMTP_SSL") as mock_smtp:
        instance = mock_smtp.return_value.__enter__.return_value
        ok, msgid = sender.send_mail(
            fake_account,
            "buyer@oversea-client.de",
            long_multiline_subj,
            "Dear Customer,\n\nPlease find our quote attached.\n\nBest regards,",
            reply_to_msgid="<inquiry_9988@oversea-client.de>",
            references="<inquiry_9988@oversea-client.de>"
        )
        check(ok is True, f"长标题与带换行邮件组装发送成功, 未抛出 500 异常, msgid: {msgid}")
        check(instance.send_message.called, "正确调用了 SMTP.send_message")

    # 2. 测试 POST /review/{did} 真实路由触发发送
    with db.tx() as conn:
        conn.execute(
            """INSERT INTO accounts (email, display_name, secret_enc, smtp_host, smtp_port, is_active, created_ts)
               VALUES ('sales@ourcompany.com', '外贸业务部', ?, '127.0.0.1', 465, 1, ?)""",
            (crypto_util.encrypt_secret("fake_pass_123"), db.now_ts())
        )

    with db.ro() as conn:
        draft = conn.execute("SELECT id, message_id FROM drafts WHERE status='pending' LIMIT 1").fetchone()
        did = draft["id"]
        mid = draft["message_id"]

    # 模拟发信抛出网络/认证异常，验证系统平稳降级为友好的 303 重定向与 send_error 记录，绝不暴露 Internal Server Error
    with patch("app.sender.send_mail", return_value=(False, "SMTP 认证失败：授权码错误")):
        resp_send = client.post(
            f"/review/{did}",
            data={"action": "send", "final_text": "Updated body text", "csrf_tok": csrf},
            cookies={"mb_sess": sess},
            headers={"referer": f"http://testserver/messages/{mid}"},
            follow_redirects=False
        )
        check(resp_send.status_code == 303, f"发信失败时平稳返回 303 重定向而非 500, status: {resp_send.status_code}")
        check(f"/messages/{mid}" in resp_send.headers.get("location", ""), "来源为详情页时精准跳回该会话详情页")

        with db.ro() as conn:
            d_row = conn.execute("SELECT send_error FROM drafts WHERE id=?", (did,)).fetchone()
            check("SMTP 认证失败" in (d_row["send_error"] or ""), "drafts.send_error 准确记录了错误详情")

    # 3. 模拟发信成功场景
    with patch("app.sender.send_mail", return_value=(True, "<test_sent_msgid_123@ourcompany.com>")):
        resp_ok = client.post(
            f"/review/{did}",
            data={"action": "send", "final_text": "Updated body text", "csrf_tok": csrf},
            cookies={"mb_sess": sess},
            follow_redirects=False
        )
        check(resp_ok.status_code == 303, "发信成功正常返回 303 重定向")
        check("/review" in resp_ok.headers.get("location", ""), "审核台发信后跳回审核台")

        with db.ro() as conn:
            d_sent = conn.execute("SELECT status FROM drafts WHERE id=?", (did,)).fetchone()
            check(d_sent["status"] == "sent", "草稿状态成功更新为 sent")


def test_messages_filter_excludes_spam():
    print("\n-- 6. 询盘/邮件列表自动排除垃圾邮件测试 --")
    sess = auth.make_session("admin")

    # 1. 默认查询 (category=""): 验证已自动排除 spam 垃圾邮件
    default_res = queries.list_messages(page=1, category="")
    categories = [m["category"] for m in default_res["items"]]
    check("spam" not in categories, "询盘邮件列表默认已严格过滤排除垃圾邮件 (spam)")
    check("inquiry" in categories, "询盘邮件列表中正常保留真实客户询盘 (inquiry)")

    # 2. 显式按询盘筛选 (category="inquiry")
    inquiry_res = queries.list_messages(page=1, category="inquiry")
    check(len(inquiry_res["items"]) >= 1, "分类筛选 inquiry 正常返回结果")
    check(all(m["category"] == "inquiry" for m in inquiry_res["items"]), "结果全为 inquiry，无 spam 泄露")

    # 3. 显式进入垃圾邮件回收站 (category="spam")
    spam_res = queries.list_messages(page=1, category="spam")
    check(len(spam_res["items"]) >= 1, "主动选择 spam 类别可查看垃圾邮件")
    check(all(m["category"] == "spam" for m in spam_res["items"]), "垃圾邮件回收站中仅展示垃圾邮件")

    # 4. 全量查看 (category="all")
    all_res = queries.list_messages(page=1, category="all")
    all_cats = [m["category"] for m in all_res["items"]]
    check("spam" in all_cats and "inquiry" in all_cats, "category=all 模式包含全量类别")

    # 5. 测试 /messages 页面渲染
    resp = client.get("/messages", cookies={"mb_sess": sess})
    check(resp.status_code == 200, "GET /messages 访问正常 (200)")
    check("Inquiry for Model X" in resp.text, "询盘列表页面正常渲染客户询盘")
    check("Buy cheap crypto" not in resp.text, "询盘列表页面默认未渲染垃圾邮件内容")
    check("全部有效邮件（自动排除垃圾）" in resp.text, "页面筛选下拉框包含自动排除垃圾提示")


def run_all():
    print("=" * 65)
    print(" 审核台优化、客户次级菜单、凭证管理、发信保护与垃圾邮件过滤测试")
    print("=" * 65)
    test_review_filter_and_token_optimization()
    test_message_detail_return_and_sub_menu()
    test_vouchers_management_and_ocr()
    test_contacts_ocr_module()
    test_draft_send_and_header_safety()
    test_messages_filter_excludes_spam()
    print("\n" + "=" * 65)
    print(" 全部 6 大模块新特性自动化集成测试 100% 顺利通过！")
    print("=" * 65)


if __name__ == "__main__":
    run_all()
