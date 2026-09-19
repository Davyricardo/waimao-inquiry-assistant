"""AI 服务商获取指引、管理员凭据修改与密保问题验证自动化测试套件。"""
import os
import sys
import tempfile
from pathlib import Path

# 设置独立测试数据目录
TMP = Path(tempfile.mkdtemp(prefix="mb_test_ai_sec_"))
os.environ["MB_DATA_DIR"] = str(TMP / "data")
os.environ["MB_SECRET_KEY"] = "test_ai_security_secret_key_12345678901234567890"
os.environ["MB_ADMIN_USER"] = "admin"
# 默认密码 hash: "admin123"
from app import crypto_util
h = crypto_util.hash_password("admin123")
os.environ["MB_ADMIN_PASS_HASH"] = h
from app import config
config.ADMIN_PASS_HASH = h

from fastapi.testclient import TestClient
from app import ai_client, auth, config, db, queries
from app.web import app

client = TestClient(app)


def check(cond: bool, msg: str):
    if not cond:
        raise AssertionError(f"Test assertion failed: {msg}")
    print(f"  [ OK ] {msg}")


def test_ai_presets_and_guidance():
    print("\n-- 1. AI 模型服务商预设及官方 API Key 获取地址完整性测试 --")
    db.init_db()
    presets = ai_client.AI_PRESETS

    # 验证重点国内与国际主流模型均已配置
    expected_providers = [
        ("dashscope", "通义千问", "bailian.console.aliyun.com"),
        ("hunyuan", "腾讯混元", "console.cloud.tencent.com/hunyuan"),
        ("moonshot", "Moonshot", "platform.moonshot.cn"),
        ("deepseek", "DeepSeek", "platform.deepseek.com"),
        ("zhipu", "智谱", "open.bigmodel.cn"),
        ("qianfan", "百度千帆", "console.bce.baidu.com/qianfan"),
        ("minimax", "MiniMax", "platform.minimaxi.com"),
        ("yi", "零一万物", "platform.lingyiwanwu.com"),
        ("openai", "OpenAI", "platform.openai.com"),
        ("custom", "自定义", ""),
    ]

    for key, name, domain in expected_providers:
        check(key in presets, f"AI_PRESETS 包含服务商: {key} ({name})")
        p = presets[key]
        check("label" in p and "hint" in p, f"{key} 包含 label 和 hint 说明")
        if domain:
            check(domain in p["key_url"], f"{key} 包含官方获取地址: {p['key_url']}")

    # 模拟登录并测试设置页面渲染
    sid = auth.make_session("admin")
    client.cookies.set("mb_sess", sid)

    resp = client.get("/settings")
    check(resp.status_code == 200, "GET /settings 正常返回 200")
    html = resp.text
    check("aiKeyGuideBox" in html, "页面包含动态 API Key 获取网络地址指引卡片 (#aiKeyGuideBox)")
    check("主流 AI 大模型 API Key 获取与控制台直达速查表" in html, "页面包含 AI 服务商 API Key 获取速查表")
    check("adminSecurityCard" in html, "页面包含管理员账户与安全保护中心卡片")
    check("bailian.console.aliyun.com" in html, "页面包含千问百炼官方地址")
    check("console.cloud.tencent.com" in html, "页面包含腾讯混元官方地址")
    check("platform.moonshot.cn" in html, "页面包含 Kimi 官方地址")


def test_admin_credentials_and_security_questions():
    print("\n-- 2. 管理员凭据管理与密保问题加盐哈希验证测试 --")
    db.init_db()

    # 初始状态
    check(auth.get_admin_user() == "admin", "初始管理员用户名为 admin")
    check(auth.has_security_question() is False, "初始状态未配置密保问题")
    check(auth.check_login("admin", "admin123") is True, "初始密码 admin123 登录有效")

    # 1. 错误原密码测试
    ok, msg, _ = auth.update_admin_credentials(
        current_password="wrong_password",
        new_username="admin_new",
    )
    check(not ok and "原密码输入错误" in msg, f"错误原密码被成功拦截: {msg}")

    # 2. 首次修改必须设定密保测试
    ok, msg, _ = auth.update_admin_credentials(
        current_password="admin123",
        new_username="admin_new",
        # 未传密保问题与答案
    )
    check(not ok and "首次修改管理员信息时必须设定密保问题" in msg, f"未设密保时强制要求配置拦截成功: {msg}")

    # 3. 首次正确更新凭据与密保
    ok, msg, target_user = auth.update_admin_credentials(
        current_password="admin123",
        new_username="davy_admin",
        new_password="new_password_888",
        new_security_question="您母亲的姓名是？",
        new_security_answer="李秀华",
    )
    check(ok is True, f"首次凭据与密保设置成功: {msg}")
    check(target_user == "davy_admin", f"更新后管理员用户名为 {target_user}")
    check(auth.get_admin_user() == "davy_admin", "auth.get_admin_user() 动态读取成功")
    check(auth.has_security_question() is True, "密保状态标记为已设置")
    check(auth.get_security_question() == "您母亲的姓名是？", "密保问题文本获取正确")

    # 4. 验证旧密码与新密码登录鉴权
    check(auth.check_login("admin", "admin123") is False, "旧用户名旧密码已失效")
    check(auth.check_login("davy_admin", "admin123") is False, "旧密码对新用户名无效")
    check(auth.check_login("davy_admin", "new_password_888") is True, "新用户名 + 新密码登录成功")

    # 5. 验证密保加盐哈希核验（不区分大小写、去前后空格）
    check(auth.verify_security_answer("李秀华") is True, "密保答案匹配正确")
    check(auth.verify_security_answer("  李秀华  ") is True, "密保答案前后空格容错匹配正确")
    check(auth.verify_security_answer("王翠花") is False, "错误密保答案被拦截")

    # 6. 已有密保状态下修改：密保答案错误拦截测试
    ok, msg, _ = auth.update_admin_credentials(
        current_password="new_password_888",
        security_answer="错误答案",
        new_username="davy_admin2",
    )
    check(not ok and "密保答案不正确" in msg, f"密保答案错误成功拦截修改: {msg}")

    # 7. 已有密保状态下修改：密保答案正确修改成功测试
    ok, msg, target_user = auth.update_admin_credentials(
        current_password="new_password_888",
        security_answer="李秀华",
        new_username="boss_davy",
        new_password="final_password_999",
    )
    check(ok is True, f"带密保答案成功修改账号密码: {msg}")
    check(auth.get_admin_user() == "boss_davy", "管理员已更新为 boss_davy")
    check(auth.check_login("boss_davy", "final_password_999") is True, "最新凭据登录成功")


def test_forgot_password_workflow():
    print("\n-- 3. 登录页密保重置密码端点与防护测试 --")
    # 访问 /forgot-password 页面
    client.cookies.clear()
    resp = client.get("/forgot-password")
    check(resp.status_code == 200, "GET /forgot-password 页面返回 200")
    check("您母亲的姓名是？" in resp.text, "重置密码页面显示当前设置的密保问题")

    # 1. 错误密保重置失败
    resp_err = client.post(
        "/forgot-password",
        data={
            "security_answer": "错误回答",
            "new_password": "reset_pass_123456",
            "confirm_password": "reset_pass_123456",
        },
    )
    check(resp_err.status_code == 400, f"密保答案错误返回 400, status={resp_err.status_code}")
    check("密保答案验证不正确" in resp_err.text, "返回答案不正确提示")

    # 2. 两次密码不一致失败
    resp_mismatch = client.post(
        "/forgot-password",
        data={
            "security_answer": "李秀华",
            "new_password": "reset_pass_123456",
            "confirm_password": "different_password",
        },
    )
    check(resp_mismatch.status_code == 400, "两次输入新密码不一致返回 400")

    # 3. 正确密保答案成功重置密码
    resp_ok = client.post(
        "/forgot-password",
        data={
            "security_answer": "李秀华",
            "new_password": "recovered_password_666",
            "confirm_password": "recovered_password_666",
        },
        follow_redirects=False,
    )
    check(resp_ok.status_code == 303, "重置成功 303 重定向至登录页")
    check("/login" in resp_ok.headers.get("location", ""), "重定向至 /login")

    # 4. 使用密保重置后的新密码登录成功
    admin_u = auth.get_admin_user()
    check(auth.check_login(admin_u, "recovered_password_666") is True, "密保重置后的新密码成功登录")


def test_settings_route_credentials_post():
    print("\n-- 4. 设置页 POST /settings/admin/credentials 表单接口测试 --")
    admin_u = auth.get_admin_user()
    sid = auth.make_session(admin_u)
    client.cookies.set("mb_sess", sid)
    csrf = auth.csrf_token(sid)

    # 1. CSRF 校验失败
    r_bad_csrf = client.post(
        "/settings/admin/credentials",
        data={"csrf_tok": "invalid"},
        follow_redirects=False,
    )
    check(r_bad_csrf.status_code == 303 and "err=" in r_bad_csrf.headers["location"], "CSRF 非法被拦截")

    # 2. 原密码校验失败
    r_bad_pwd = client.post(
        "/settings/admin/credentials",
        data={
            "csrf_tok": csrf,
            "current_password": "wrong_password",
            "security_answer": "李秀华",
            "new_username": "new_admin",
        },
        follow_redirects=False,
    )
    check(r_bad_pwd.status_code == 303 and "err=" in r_bad_pwd.headers["location"], "原密码错误重定向提示 err")

    # 3. 正常修改并自动刷新 Cookie
    r_success = client.post(
        "/settings/admin/credentials",
        data={
            "csrf_tok": csrf,
            "current_password": "recovered_password_666",
            "security_answer": "李秀华",
            "new_username": "super_trader_admin",
            "new_password": "brand_new_password_888",
            "confirm_password": "brand_new_password_888",
            "new_security_question": "您出生的城市是？",
            "new_security_answer": "深圳",
        },
        follow_redirects=False,
    )
    check(r_success.status_code == 303, "正常提交后 303 重定向")
    check("ok=" in r_success.headers["location"], "重定向包含 ok 参数")
    check("mb_sess=" in r_success.headers.get("set-cookie", ""), "响应包含刷新后的新登录 Session Cookie")

    # 验证最新用户名及新密保生效
    check(auth.get_admin_user() == "super_trader_admin", "最终管理员用户名为 super_trader_admin")
    check(auth.get_security_question() == "您出生的城市是？", "新密保问题已生效")
    check(auth.verify_security_answer("深圳") is True, "新密保答案已生效")
    check(auth.check_login("super_trader_admin", "brand_new_password_888") is True, "新用户名与新密码登录成功")


if __name__ == "__main__":
    test_ai_presets_and_guidance()
    test_admin_credentials_and_security_questions()
    test_forgot_password_workflow()
    test_settings_route_credentials_post()
    print("\n=======================================================")
    print("  ALL TESTS PASSED! 全部 AI 预设与管理员密保测试通过！")
    print("=======================================================")
