"""AI 模块自检：中文摘要 / 客户背调 / 模型凭据层。

不联网、不消耗额度 —— 全部走「无 Key 规则降级」路径，
外加用假 Key 验证加密存储与配置读写。

用法：python selftest_ai.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

# 独立临时库，不污染真实数据
TMP = Path(tempfile.mkdtemp(prefix="mb_ai_test_"))
os.environ["MB_DATA_DIR"] = str(TMP / "data")
os.environ["MB_DB"] = str(TMP / "data" / "test.db")
os.environ["MB_CRED_KEY"] = "test-cred-key-for-ai"
os.environ["MB_SECRET_KEY"] = "test-session-key"
os.environ.pop("MB_AI_API_KEY", None)
os.environ.pop("MB_AI_BASE_URL", None)
os.environ.pop("MB_AI_MODEL", None)

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ OK ] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


from app import ai_client, ai_profile, ai_summary, crypto_util, db   # noqa: E402

db.init_db()

# ==================================================================
print("=" * 66)
print("1. 模型凭据层（无 Key 起始状态）")
print("=" * 66)
check("默认未配置", not ai_client.is_configured())
check("默认未启用", not ai_client.is_enabled())
check("状态文案含「未配置」", "未配置" in ai_client.status_text())
check("默认 base_url 有值", ai_client.get_base_url().startswith("http"))
check("默认 model 有值", bool(ai_client.get_model()))
check("chat() 无 Key 返回 None",
      ai_client.chat([{"role": "user", "content": "hi"}]) is None)
check("预设表含 6 个服务商", len(ai_client.AI_PRESETS) == 6,
      f"实际 {len(ai_client.AI_PRESETS)}")

# ==================================================================
print()
print("=" * 66)
print("2. 摘要：无 Key 时的规则降级")
print("=" * 66)
r = ai_summary.ai_summary(
    "Inquiry about model A-1200",
    "Dear Sales, we need 5000 pcs of model A-1200. "
    "Please quote FOB Shenzhen price and MOQ. We need it ASAP.",
    category="inquiry", score=88)
check("来源标记为 rule", r["source"] == "rule")
check("有中文摘要", bool(r["summary_cn"]))
check("摘要确实是中文", ai_summary._looks_chinese(r["summary_cn"]))
check("要点是 list", isinstance(r["key_points_cn"], list))
check("含紧急度字段", "urgency_cn" in r)
check("translated 为空（规则模式无法翻译）",
      r["translated_body_cn"] == "")

# ==================================================================
print()
print("=" * 66)
print("3. 中文检测函数")
print("=" * 66)
check("纯中文通过", ai_summary._looks_chinese("这是一个中文句子，用于测试"))
check("纯英文不通过", not ai_summary._looks_chinese(
    "This is a purely English sentence for testing purposes"))
check("空串不通过", not ai_summary._looks_chinese(""))
check("None 不通过", not ai_summary._looks_chinese(None))
check("_contains_cjk 识别汉字", ai_summary._contains_cjk("abc中文def"))
check("_contains_cjk 拒绝纯英文", not ai_summary._contains_cjk("abcdef"))

# ==================================================================
print()
print("=" * 66)
print("4. 背调：无 Key 时的规则降级（多国样本）")
print("=" * 66)
cases = [
    # (邮箱, 正文, 期望国家, 期望身份关键词)
    ("k.mueller@mueller-handel.de",
     "We are a distributor in Germany.\nMueller Handel GmbH\n+49 30 1234567",
     "德国", "经销商"),
    ("buyer@acme-corp.fr",
     "Bonjour, nous sommes un grossiste.\nAcme SARL\n+33 1 2345 6789",
     "法国", ""),
    ("x@company.com.br",
     "We are an importer in Brazil.\nComercio Ltda",
     "巴西", "进口商"),
    ("a@shop.co.uk",
     "We run a retail chain.\nShop Ltd",
     "英国", "零售商"),
]
for addr, body, want_country, want_role in cases:
    p = ai_profile.ai_profile(addr, body=body)
    ok_c = (p["country"] == want_country)
    check(f"{addr} → 国家 {want_country}", ok_c,
          f"实际 {p['country']!r}")
    if want_role:
        check(f"{addr} → 身份含「{want_role}」", want_role in (p["channel_role"] or ""),
              f"实际 {p['channel_role']!r}")

p_free = ai_profile.ai_profile("someone@gmail.com", body="Hello, please send catalog.")
check("免费邮箱被识别", p_free["is_free_mail"])
check("免费邮箱可信度偏低", p_free["credibility"] == "偏低",
      f"实际 {p_free['credibility']!r}")
check("背调来源标记 rule", p_free["source"] == "rule")
check("背调含证据列表", isinstance(p_free["evidence"], list))
# ai_profile() 的返回值里 country 是有的；是 format_profile_for_db()
# 把它拆出去单独存 contacts.country 列，JSON 里不再重复。
check("format 后 JSON 不含 country（已分离）",
      "country" not in json.loads(ai_profile.format_profile_for_db(p_free)[1]))

country_col, bg_json = ai_profile.format_profile_for_db(p_free)
check("format 返回国家列", country_col == p_free["country"])
check("format 返回合法 JSON", isinstance(json.loads(bg_json), dict))
check("format 后保留其余字段",
      "channel_role" in json.loads(bg_json) and "source" in json.loads(bg_json))

# 电话区号不能误判：+1 不该吃掉 +1242
p_bs = ai_profile.ai_profile("c@x.bs", body="Call us at +1242 555 1234")
check("+1 不误吃 +1242（巴哈马/未收录时不报美国）",
      p_bs["country"] != "美国/加拿大", f"实际 {p_bs['country']!r}")

# ==================================================================
print()
print("=" * 66)
print("5. 凭据保存与加密存储（假 Key 验证往返）")
print("=" * 66)
FAKE = "sk-test-abcdefghijklmnopqrstuvwxyz012345"
ai_client.save_credentials(FAKE, "https://api.deepseek.com", "deepseek-chat", True)
check("保存后 is_configured", ai_client.is_configured())
check("保存后 is_enabled", ai_client.is_enabled())
check("Key 解密往返一致", ai_client.get_api_key() == FAKE)
check("base_url 已更新",
      ai_client.get_base_url() == "https://api.deepseek.com")
check("model 已更新", ai_client.get_model() == "deepseek-chat")
check("状态文案含模型名", "deepseek-chat" in ai_client.status_text())

with db.ro() as conn:
    stored = conn.execute("SELECT value FROM settings WHERE key=?",
                          (ai_client.K_API_KEY,)).fetchone()["value"]
check("落库是密文（不含明文）", FAKE not in stored and len(stored) > 20)

# 传 None 表示不改动已存的 Key
ai_client.save_credentials(None, "https://api.moonshot.cn/v1",
                           "moonshot-v1-8k", True)
check("传 None 时 Key 保留", ai_client.get_api_key() == FAKE)
check("但 base_url 已换", ai_client.get_base_url() == "https://api.moonshot.cn/v1")

# 关闭开关
ai_client.save_credentials(None, "https://api.moonshot.cn/v1",
                           "moonshot-v1-8k", False)
check("关闭后 is_enabled 为假", not ai_client.is_enabled())
check("关闭后 Key 仍在", ai_client.is_configured())
check("关闭状态文案", "已关闭" in ai_client.status_text())

# 清空 Key
ai_client.clear_api_key()
check("清空后 is_configured 为假", not ai_client.is_configured())

# ==================================================================
print()
print("=" * 66)
print("6. JSON 抽取容错（模型输出常见形态）")
print("=" * 66)
ex = ai_client.extract_json
check("裸 JSON", ex('{"a":1}') == {"a": 1})
check("代码围栏包裹", ex('```json\n{"a":1}\n```') == {"a": 1})
check("前后有废话", ex('好的，这是结果：{"a":1} 请查收') == {"a": 1})
check("坏 JSON 返回 None", ex('{"a":') is None)
check("空串返回 None", ex("") is None)
check("顶层数组返回 None", ex('[1,2,3]') is None)

# ==================================================================
print()
print("=" * 66)
print("7. 设置页上下文（web._ai_context）")
print("=" * 66)
from app import web   # noqa: E402
ctx = web._ai_context()
check("含 ai_status", "ai_status" in ctx)
check("含 ai_presets", len(ctx["ai_presets"]) == 6)
check("含 ai_conf", isinstance(ctx["ai_conf"], dict))
check("conf 含 configured/enabled",
      "configured" in ctx["ai_conf"] and "enabled" in ctx["ai_conf"])
check("conf 含 base_url/model/preset",
      all(k in ctx["ai_conf"] for k in ("base_url", "model", "preset")))
check("presets 可 JSON 序列化",
      isinstance(json.loads(ctx["ai_presets_json"]), dict))

# 配一个与预设完全一致的地址，应能反查出 preset
ai_client.save_credentials(FAKE, "https://api.deepseek.com", "deepseek-chat", True)
ctx2 = web._ai_context()
check("反查预设 deepseek", ctx2["ai_conf"]["preset"] == "deepseek",
      f"实际 {ctx2['ai_conf']['preset']!r}")
check("Key 脱敏显示（保留头4尾4）",
      ctx2["ai_conf"]["key_masked"] == crypto_util.mask_secret(FAKE),
      f"实际 {ctx2['ai_conf']['key_masked']!r}")
check("脱敏保留头尾、中段打码",
      ctx2["ai_conf"]["key_masked"].startswith(FAKE[:4]) and
      ctx2["ai_conf"]["key_masked"].endswith(FAKE[-4:]) and
      "*" in ctx2["ai_conf"]["key_masked"],
      f"实际 {ctx2['ai_conf']['key_masked']!r}")
check("脱敏不含完整 Key", FAKE not in ctx2["ai_conf"]["key_masked"])
check("新增：key_len 正确", ctx2["ai_conf"]["key_len"] == len(FAKE),
      f"实际 {ctx2['ai_conf']['key_len']}")
check("新增：key_ok 为真", ctx2["ai_conf"]["key_ok"] is True)
check("新增：key_plain 供点击展开",
      ctx2["ai_conf"]["key_plain"] == FAKE)

# 自定义地址应落到 custom
ai_client.save_credentials(None, "https://my-own-llm.internal/v1", "my-model", True)
ctx3 = web._ai_context()
check("自定义地址 → preset=custom", ctx3["ai_conf"]["preset"] == "custom",
      f"实际 {ctx3['ai_conf']['preset']!r}")

# ==================================================================
print()
print("=" * 66)
print("8. 迁移：老库补列（不丢数据）")
print("=" * 66)
import sqlite3   # noqa: E402
old_db = TMP / "old.db"
c = sqlite3.connect(str(old_db))
c.executescript("""
CREATE TABLE contacts (id INTEGER PRIMARY KEY, email TEXT, name TEXT, company TEXT,
  country TEXT, source TEXT, first_seen_ts INTEGER, last_seen_ts INTEGER,
  msg_count INTEGER DEFAULT 0, reply_count INTEGER DEFAULT 0,
  score INTEGER DEFAULT 0, stage TEXT DEFAULT 'new', note TEXT);
CREATE TABLE messages (id INTEGER PRIMARY KEY, account_id INTEGER, contact_id INTEGER,
  thread_id INTEGER, fingerprint TEXT, direction TEXT, sent_ts INTEGER,
  subject TEXT, body_text TEXT, status TEXT DEFAULT 'new', created_ts INTEGER);
CREATE TABLE rules (id INTEGER PRIMARY KEY, name TEXT, cond_json TEXT, action TEXT,
  template_key TEXT, priority INTEGER, is_active INTEGER DEFAULT 1, created_ts INTEGER);
CREATE TABLE templates (id INTEGER PRIMARY KEY, key TEXT, name TEXT, category TEXT,
  lang TEXT, subject TEXT, body TEXT, ai_hint TEXT, is_active INTEGER DEFAULT 1,
  created_ts INTEGER);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT, updated_ts INTEGER);
CREATE TABLE accounts (id INTEGER PRIMARY KEY, email TEXT, display_name TEXT,
  secret_enc TEXT, imap_host TEXT, imap_port INTEGER, smtp_host TEXT,
  smtp_port INTEGER, watch_folder TEXT, since_ts INTEGER, is_active INTEGER DEFAULT 1,
  created_ts INTEGER);
CREATE TABLE daily_stats (day TEXT PRIMARY KEY, in_count INTEGER, out_count INTEGER,
  auto_count INTEGER, high_intent INTEGER, new_contacts INTEGER, avg_score REAL,
  updated_ts INTEGER);
""")
c.execute("INSERT INTO contacts (email,name,first_seen_ts) VALUES ('keep@x.de','KEEP',1)")
c.commit()
c.close()

os.environ["MB_DB"] = str(old_db)
import importlib   # noqa: E402
from app import config as cfg   # noqa: E402
importlib.reload(cfg)
from app import db as db2   # noqa: E402
importlib.reload(db2)
db2.init_db()
conn = sqlite3.connect(str(old_db))
cols = {r[1] for r in conn.execute("PRAGMA table_info(contacts)")}
mcols = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
check("contacts 补 background", "background" in cols)
check("contacts 补 credibility", "credibility" in cols)
check("contacts 补 profiled_ts", "profiled_ts" in cols)
check("messages 补 summary_cn", "summary_cn" in mcols)
check("messages 补 translated_cn", "translated_cn" in mcols)
check("messages 补 summary_source", "summary_source" in mcols)
check("老数据未丢失",
      conn.execute("SELECT COUNT(*) FROM contacts WHERE email='keep@x.de'")
      .fetchone()[0] == 1)
idx = [r[0] for r in conn.execute(
    "SELECT name FROM sqlite_master WHERE name='idx_contacts_cred'")]
check("迁移后建出 idx_contacts_cred", bool(idx))
db2.init_db()
check("二次 init_db 幂等", True)
conn.close()

# ==================================================================
print()
print("=" * 66)
print(f" 结果：通过 {PASS} · 失败 {FAIL}")
print("=" * 66)
if FAIL:
    print("存在失败项。")
sys.exit(1 if FAIL else 0)
