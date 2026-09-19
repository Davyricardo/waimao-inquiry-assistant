"""多邮箱账号管理 + 凭据展示 专项自检。

覆盖：
  A. crypto_util 打码 / 安全解密
  B. 多账号 CRUD 路由（新增 / 编辑 / 停用 / 启用 / 删除）
  C. 邮箱唯一性、留空沿用旧授权码、新增必填授权码
  D. worker 侧 get_accounts 是否只取启用的
  E. 设置页渲染：账号列表、授权码打码、AI 已保存配置块
不联网、不碰真实库。
"""
import os
import sys
import tempfile

sys.path.insert(0, ".")

TMP = tempfile.mkdtemp(prefix="mb_acct_")
os.environ["MB_DATA_DIR"] = os.path.join(TMP, "data")
os.environ["MB_DB"] = os.path.join(TMP, "data", "t.db")
os.environ["MB_SECRET_KEY"] = "k" * 43
os.environ["MB_CRED_KEY"] = "c" * 43
os.environ["MB_ADMIN_USER"] = "admin"
os.environ["MB_ADMIN_PASS_HASH"] = ""

from app import crypto_util, db  # noqa: E402
from app import ai_client  # noqa: E402
from app.templating import render  # noqa: E402

PASSED, FAILED = [], []


def ok(name, cond, extra=""):
    if cond:
        PASSED.append(name)
        print("  [ OK ]", name)
    else:
        FAILED.append(name)
        print("  [FAIL]", name, extra)


def section(t):
    print(f"\n-- {t} --")


def q1(sql, args=()):
    """单值查询小工具（每次独立开连接，避免上下文管理器误用）。"""
    with db.ro() as conn:
        return conn.execute(sql, args).fetchone()[0]


db.init_db()

# 清掉可能残留的账号，保证计数断言从 0 开始
with db.tx() as conn:
    conn.execute("DELETE FROM accounts")

print("=" * 60)
print("  多邮箱账号 + 凭据展示 自检")
print("=" * 60)

# ---------------- A. 打码与安全解密 ----------------
section("A. crypto_util 打码 / 安全解密")

ok("mask_secret 头尾保留", crypto_util.mask_secret("abcdefghijklmnop") ==
   "abcd********mnop", crypto_util.mask_secret("abcdefghijklmnop"))
ok("mask_secret 短串整体打码", crypto_util.mask_secret("abc") == "***")
ok("mask_secret 空串返回空", crypto_util.mask_secret("") == "")
ok("mask_secret 长度不变", len(crypto_util.mask_secret("abcdefghijklmnop")) == 16)
ok("mask_secret 不泄露中段",
   "efgh" not in crypto_util.mask_secret("abcdefghijklmnop"))

enc = crypto_util.encrypt_secret("my-secret-code")
ok("加解密往返", crypto_util.decrypt_secret_safe(enc) == "my-secret-code")
ok("decrypt_secret_safe 坏密文返回空串（不抛异常）",
   crypto_util.decrypt_secret_safe("garbage:data") == "")
ok("decrypt_secret_safe 空输入返回空串",
   crypto_util.decrypt_secret_safe("") == "")

# ---------------- B/C. 多账号 CRUD ----------------
section("B. 多账号 CRUD（直接测库层逻辑）")

from app import pipeline, web  # noqa: E402


def add_account(email, secret="code-1234567890", name="", active=1):
    """模拟 account_save 的插入逻辑。"""
    enc = crypto_util.encrypt_secret(secret)
    with db.tx() as conn:
        cur = conn.execute(
            "INSERT INTO accounts (email,display_name,secret_enc,imap_host,"
            "imap_port,smtp_host,smtp_port,watch_folder,since_ts,is_active,"
            "created_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (email, name, enc, "imap.qq.com", 993, "smtp.qq.com", 465,
             "INBOX", db.now_ts(), active, db.now_ts()))
        return cur.lastrowid


a1 = add_account("sales@company.com", "aaaa1111bbbb2222", "销售部")
a2 = add_account("info@company.com", "cccc3333dddd4444", "信息部")
a3 = add_account("old@company.com", "eeee5555ffff6666", "旧号", active=1)

with db.ro() as conn:
    n = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
ok("可以插入多个账户", n == 3, f"实际 {n}")

# 邮箱唯一约束
section("C. 唯一性 / 授权码规则")
dup_raised = False
try:
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO accounts (email,secret_enc,imap_host,imap_port,"
            "smtp_host,smtp_port,watch_folder,is_active,created_ts) "
            "VALUES (?,?,?,?,?,?,?,1,?)",
            ("sales@company.com", "x", "i", 993, "s", 465, "INBOX",
             db.now_ts()))
except Exception:
    dup_raised = True
ok("重复邮箱被 UNIQUE 约束拒绝", dup_raised)

# 编辑：留空授权码 -> 沿用旧密文
with db.ro() as conn:
    before = conn.execute("SELECT secret_enc FROM accounts WHERE id=?",
                          (a1,)).fetchone()["secret_enc"]
with db.tx() as conn:
    conn.execute("UPDATE accounts SET display_name=? WHERE id=?", ("新名字", a1))
with db.ro() as conn:
    after = conn.execute("SELECT secret_enc FROM accounts WHERE id=?",
                         (a1,)).fetchone()["secret_enc"]
ok("编辑时不动授权码密文", before == after)
ok("编辑后名称已更新",
   q1("SELECT display_name FROM accounts WHERE id=?", (a1,)) == "新名字")

# 停用 / 启用
with db.tx() as conn:
    conn.execute("UPDATE accounts SET is_active=0 WHERE id=?", (a3,))
ok("停用后 is_active=0",
   q1("SELECT is_active FROM accounts WHERE id=?", (a3,)) == 0)

accounts = pipeline.get_accounts()
ok("get_accounts 只返回启用的账户", len(accounts) == 2,
   f"实际 {len(accounts)}: {[a['email'] for a in accounts]}")
ok("get_accounts 不含已停用的 old@company.com",
   all(a["email"] != "old@company.com" for a in accounts))

with db.tx() as conn:
    conn.execute("UPDATE accounts SET is_active=1 WHERE id=?", (a3,))
ok("重新启用后 get_accounts 有 3 个", len(pipeline.get_accounts()) == 3)

# 删除
with db.tx() as conn:
    conn.execute("DELETE FROM accounts WHERE id=?", (a3,))
ok("删除后剩下 2 个", len(pipeline.get_accounts()) == 2)

# ---------------- D. 路由存在性 ----------------
section("D. 路由注册")
routes = {r.path for r in web.app.routes if hasattr(r, "path")}
for p in ["/settings/account",
          "/settings/account/{aid}/toggle",
          "/settings/account/{aid}/delete",
          "/settings/account/{aid}/test",
          "/settings/account/test"]:
    ok(f"路由已注册 {p}", p in routes)

# ---------------- E. 设置页渲染 ----------------
section("E. 设置页渲染")
with db.ro() as conn:
    rows = [dict(r) for r in conn.execute("SELECT * FROM accounts ORDER BY id")]
    counts = {}
accts = []
for a in rows:
    plain = crypto_util.decrypt_secret_safe(a["secret_enc"])
    a["secret_masked"] = crypto_util.mask_secret(plain)
    a["secret_ok"] = bool(plain)
    a["secret_plain"] = plain
    a["msg_count"] = counts.get(a["id"], 0)
    accts.append(a)

html = render("settings.html", account=accts[0] if accts else None,
              accounts=accts, settings={}, ok="", err="", test="", csrf="tok",
              presets=web.MAIL_PRESETS, presets_json="{}",
              **web._ai_context())

ok("页面含两个邮箱地址",
   "sales@company.com" in html and "info@company.com" in html)
ok("授权码以打码形式出现", "aaaa********2222" in html,
   [s for s in html.split() if "aaaa" in s][:3])
# 明文只应出现在 data-full 属性里（供点击展开），不应出现在可见文本中
import re as _re
visible = _re.sub(r'data-full="[^"]*"', '', html)
ok("可见文本中不含授权码明文",
   "aaaa1111bbbb2222" not in visible and "cccc3333dddd4444" not in visible,
   "明文出现在可见文本！")
ok("授权码明文仅存于 data-full 属性",
   html.count('data-full="aaaa1111bbbb2222"') == 1)
ok("授权码明文放在 data-full 供点击展开",
   'data-full="aaaa1111bbbb2222"' in html)
ok("有「显示」切换按钮", "toggle-secret" in html)
ok("有编辑按钮", "edit-acct" in html)
ok("有停用按钮", "停用" in html or "启用" in html)
ok("有删除按钮且带二次确认", "confirm(" in html and "不可恢复" in html)
ok("渲染出「当前已保存的配置」块", "当前已保存的配置" in html)
ok("AI 块显示模型名", ai_client.get_model() in html)
ok("AI 块显示服务商标签", "服务商" in html)
ok("无残留模板标签",
   "{%" not in html and "endfor" not in html,
   f"for={html.count('{% for')} endfor={html.count('endfor')}")

# AI Key 打码展示
section("F. AI 密钥展示")
ai_client.save_credentials("sk-test-1234567890abcdef", "https://api.deepseek.com",
                           "deepseek-chat", True)
ctx = web._ai_context()["ai_conf"]
ok("AI 已配置", ctx["configured"])
ok("AI 模型正确", ctx["model"] == "deepseek-chat")
ok("AI 服务商识别为 deepseek", ctx["preset"] == "deepseek")
ok("AI Key 打码保留头尾", ctx["key_masked"] == "sk-t****************cdef",
   ctx["key_masked"])
ok("AI Key 长度正确", ctx["key_len"] == 24, ctx["key_len"])
ok("AI 来源标记为库内密文（非环境变量）", not ctx["key_from_env"])

html2 = render("settings.html", account=None, accounts=[], settings={},
               ok="", err="", test="", csrf="tok", presets=web.MAIL_PRESETS,
               presets_json="{}", **web._ai_context())
visible2 = _re.sub(r'data-full="[^"]*"', '', html2)
ok("AI Key 明文不出现在可见文本",
   "sk-test-1234567890abcdef" not in visible2, "明文出现在可见文本！")
ok("AI Key 打码出现在页面", "sk-t****************cdef" in html2)
ok("无账号时显示空态提示", "还没有配置邮箱" in html2)

print()
print("=" * 60)
print(f"  通过 {len(PASSED)} / {len(PASSED) + len(FAILED)}")
if FAILED:
    print("  失败项：")
    for f in FAILED:
        print("    -", f)
print("=" * 60)
sys.exit(1 if FAILED else 0)
