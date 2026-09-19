"""测试客户管理完整业务流及多主题配置"""
from app import db, queries
from app.templating import render

def test_all():
    print("==================================================")
    print(" 客户管理 CRUD + 主题系统 自动化测试")
    print("==================================================")

    # 0. 预先清理可能残留的测试邮箱
    with db.tx() as conn:
        conn.execute("DELETE FROM contacts WHERE email='test_buyer@globaltrade.de'")

    # 1. 保存/新建客户
    ok, msg, cid = queries.save_contact({
        "email": "test_buyer@globaltrade.de",
        "name": "Hans Gruber",
        "country": "德国",
        "company": "Gruber Logistics GmbH",
        "stage": "quoted",
        "channel_role": "批发商",
        "company_type": "德国本土企业",
        "credibility": "高",
        "note": "意向极强，询价 500 台设备"
    })
    assert ok and cid > 0, f"新建客户应成功返回 ID, 实际: {msg}"
    print(f"  [ OK ] 新建客户成功 (ID: {cid})")

    # 2. 查询客户
    c = queries.get_contact(cid)
    assert c is not None, "客户应能查询到"
    assert c["name"] == "Hans Gruber"
    assert c["stage"] == "quoted"
    assert c["company"] == "Gruber Logistics GmbH"
    print("  [ OK ] 查询客户资料核验通过")

    # 3. 快捷更新阶段
    queries.update_contact_stage(cid, "negotiating")
    c_updated = queries.get_contact(cid)
    assert c_updated["stage"] == "negotiating"
    print("  [ OK ] 快捷修改跟进阶段通过")

    # 4. 导出 CSV
    csv_text = queries.export_contacts_csv()
    assert len(csv_text) > 0, "导出 CSV 不能为空"
    assert "Hans Gruber" in csv_text
    assert "Gruber Logistics GmbH" in csv_text
    print("  [ OK ] CSV 导出校验通过")

    # 5. 页面模板渲染
    out = render("contacts.html", data=queries.list_contacts(page=1, size=20),
                 f={"q": "", "stage": "", "min_score": ""}, csrf="test_csrf", ok="", err="")
    assert "Hans Gruber" in out
    assert "Gruber Logistics GmbH" in out
    assert "modal-backdrop" in out
    assert "modal-card" in out
    assert "theme-selector" in out
    print("  [ OK ] 客户管理页面模板渲染通过，包含动效类与主题选择器")

    # 6. 清理测试数据
    queries.delete_contact(cid)
    assert queries.get_contact(cid) is None
    print("  [ OK ] 删除测试客户通过")

    # 7. 确保测试后数据库客户数为 0
    with db.ro() as conn:
        count = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
    assert count == 0, f"测试后客户数应保持为 0, 当前: {count}"
    print("  [ OK ] 数据库保持绝对干净 (0 客户)")

    print("==================================================")
    print(" 全部 7 项端到端业务流自动化测试通过！")
    print("==================================================")

if __name__ == "__main__":
    test_all()
