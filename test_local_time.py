"""客户当地时间识别与时区时差判定专项测试套件。"""
import datetime
import io
import os
import sys

from app import config, geo_time, queries, templating

passed = 0
failed = 0


def check(desc: str, ok: bool, extra: str = ""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  [ OK ] {desc}")
    else:
        failed += 1
        print(f"  [FAIL] {desc} {extra}")


print("=" * 60)
print("  外贸客户当地时间与沟通时机专项自测")
print("=" * 60)

# ============ 1. 国家/地区识别与归一化测试 ============
print("\n-- 1. 国家与地区别名模糊匹配 --")

cases = [
    ("德国", "德国"),
    ("germany", "德国"),
    ("DE", "德国"),
    ("deutschland", "德国"),
    ("柏林", "德国"),
    ("汉堡", "德国"),
    ("德国汉堡", "德国"),
    ("德国企业 (GmbH)", "德国"),
    ("法国", "法国"),
    ("France", "法国"),
    ("巴黎", "法国"),
    ("英国", "英国"),
    ("UK", "英国"),
    ("United Kingdom", "英国"),
    ("伦敦", "英国"),
    ("美国", "美国"),
    ("USA", "美国"),
    ("United States", "美国"),
    ("纽约", "美国"),
    ("加州", "美国(太平洋)"),
    ("加利福尼亚", "美国(太平洋)"),
    ("洛杉矶", "美国(太平洋)"),
    ("芝加哥", "美国(中部)"),
    ("加拿大", "加拿大"),
    ("多伦多", "加拿大"),
    ("温哥华", "加拿大(西部)"),
    ("阿联酋", "阿联酋"),
    ("UAE", "阿联酋"),
    ("迪拜", "阿联酋"),
    ("沙特阿拉伯", "沙特阿拉伯"),
    ("沙特", "沙特阿拉伯"),
    ("利雅得", "沙特阿拉伯"),
    ("日本", "日本"),
    ("Tokyo", "日本"),
    ("韩国", "韩国"),
    ("首尔", "韩国"),
    ("澳大利亚", "澳大利亚"),
    ("悉尼", "澳大利亚"),
    ("珀斯", "澳大利亚(西澳)"),
    ("巴西", "巴西"),
    ("圣保罗", "巴西"),
    ("俄罗斯", "俄罗斯"),
    ("莫斯科", "俄罗斯"),
    ("新加坡", "新加坡"),
    ("越南", "越南"),
    ("印度", "印度"),
    ("墨西哥", "墨西哥"),
    ("土耳其", "土耳其"),
    ("波兰", "波兰"),
    ("意大利", "意大利"),
    ("西班牙", "西班牙"),
    ("荷兰", "荷兰"),
    ("中国香港", "中国香港"),
]

for query, expected_country in cases:
    res = geo_time.resolve_country(query)
    check(f"识别 '{query}' -> {expected_country}", res is not None and res["name"] == expected_country,
          f"实际结果: {res['name'] if res else None}")

# 无效与未识别输入兜底
check("空输入返回 None", geo_time.resolve_country("") is None)
check("None 输入返回 None", geo_time.resolve_country(None) is None)
check("'未识别' 返回 None", geo_time.resolve_country("未识别") is None)
check("'unknown' 返回 None", geo_time.resolve_country("unknown") is None)
check("未知火星国家返回 None", geo_time.resolve_country("xyz-mars-region-999") is None)


# ============ 2. 联系人对象推断国家 ============
print("\n-- 2. 联系人对象自动推断国家 --")

c1 = {"email": "john@client.de", "country": ""}
check("从邮箱后缀 .de 推断德国", geo_time.infer_country_from_contact(c1) == "de")

c2 = {"email": "buyer@acme-corp.fr", "country": "-"}
check("从邮箱后缀 .fr 推断法国", geo_time.infer_country_from_contact(c2) == "fr")

c3 = {"email": "sales@gmail.com", "country": "阿联酋"}
check("原有 country 字段优先", geo_time.infer_country_from_contact(c3) == "阿联酋")

c4 = {"email": "boss@yahoo.com", "country": "", "background": '{"country": "意大利"}'}
check("从 background JSON 兜底提取国家", geo_time.infer_country_from_contact(c4) == "意大利")


# ============ 3. 时区夏令时与时差精确计算 ============
print("\n-- 3. 时区夏令时与时差精确计算 --")

# 固定时间戳测试夏令时：2026-07-15 12:00:00 UTC (夏季)
summer_ts = datetime.datetime(2026, 7, 15, 12, 0, 0, tzinfo=datetime.timezone.utc).timestamp()
# 2026-01-15 12:00:00 UTC (冬季)
winter_ts = datetime.datetime(2026, 1, 15, 12, 0, 0, tzinfo=datetime.timezone.utc).timestamp()

# 德国：夏季 CEST (UTC+2)，冬季 CET (UTC+1)
de_summer = geo_time.get_country_time_info("德国", ts=summer_ts, base_tz_offset=8)
check("德国夏季启用夏令时 UTC+2", de_summer["is_dst"] and de_summer["utc_offset"] == 2.0)
check("德国夏季比北京时间慢 6 小时", de_summer["diff_hours"] == -6.0)
check("德国夏季时间为 14:00", de_summer["time_str"] == "14:00")

de_winter = geo_time.get_country_time_info("德国", ts=winter_ts, base_tz_offset=8)
check("德国冬季标准时间 UTC+1", not de_winter["is_dst"] and de_winter["utc_offset"] == 1.0)
check("德国冬季比北京时间慢 7 小时", de_winter["diff_hours"] == -7.0)
check("德国冬季时间为 13:00", de_winter["time_str"] == "13:00")

# 美国东部：夏季 EDT (UTC-4)，冬季 EST (UTC-5)
us_summer = geo_time.get_country_time_info("美国", ts=summer_ts, base_tz_offset=8)
check("美东夏季启用夏令时 UTC-4", us_summer["is_dst"] and us_summer["utc_offset"] == -4.0)
check("美东夏季比北京慢 12 小时", us_summer["diff_hours"] == -12.0)

us_winter = geo_time.get_country_time_info("美国", ts=winter_ts, base_tz_offset=8)
check("美东冬季标准时间 UTC-5", not us_winter["is_dst"] and us_winter["utc_offset"] == -5.0)
check("美东冬季比北京慢 13 小时", us_winter["diff_hours"] == -13.0)

# 日本：无夏令时，恒定 UTC+9
jp_info = geo_time.get_country_time_info("日本", ts=summer_ts, base_tz_offset=8)
check("日本恒定 UTC+9", not jp_info["is_dst"] and jp_info["utc_offset"] == 9.0)
check("日本比北京快 1 小时", jp_info["diff_hours"] == 1.0)

# 阿联酋：无夏令时，恒定 UTC+4
ae_info = geo_time.get_country_time_info("阿联酋", ts=summer_ts, base_tz_offset=8)
check("阿联酋恒定 UTC+4", not ae_info["is_dst"] and ae_info["utc_offset"] == 4.0)
check("阿联酋比北京慢 4 小时", ae_info["diff_hours"] == -4.0)


# ============ 4. 商务作息状态与邮件沟通建议 ============
print("\n-- 4. 商务作息状态与邮件沟通建议 --")

# 构造特定当地时刻的测试（使用周三 2026-07-15）
# 目标：测试上午工作、午休、下午工作、下班、深夜休息、清晨备工

# 当地 10:30 上午
t_1030 = datetime.datetime(2026, 7, 15, 8, 30, 0, tzinfo=datetime.timezone.utc).timestamp()
de_1030 = geo_time.get_country_time_info("德国", ts=t_1030)
check("德国 10:30 上午工作中", de_1030["status_key"] == "work_morning" and de_1030["is_work_time"])
check("工作时间沟通建议积极", "黄金工作" in de_1030["advice"] or "查阅与回复" in de_1030["advice"])

# 当地 13:00 午休
t_1300 = datetime.datetime(2026, 7, 15, 11, 0, 0, tzinfo=datetime.timezone.utc).timestamp()
de_1300 = geo_time.get_country_time_info("德国", ts=t_1300)
check("德国 13:00 午休时段", de_1300["status_key"] == "lunch" and not de_1300["is_work_time"])

# 当地 15:30 下午工作
t_1530 = datetime.datetime(2026, 7, 15, 13, 30, 0, tzinfo=datetime.timezone.utc).timestamp()
de_1530 = geo_time.get_country_time_info("德国", ts=t_1530)
check("德国 15:30 下午工作中", de_1530["status_key"] == "work_afternoon" and de_1530["is_work_time"])

# 当地 20:00 晚间已下班
t_2000 = datetime.datetime(2026, 7, 15, 18, 0, 0, tzinfo=datetime.timezone.utc).timestamp()
de_2000 = geo_time.get_country_time_info("德国", ts=t_2000)
check("德国 20:00 已下班", de_2000["status_key"] == "evening" and not de_2000["is_work_time"])

# 当地 02:00 深夜休息
t_0200 = datetime.datetime(2026, 7, 15, 0, 0, 0, tzinfo=datetime.timezone.utc).timestamp()
de_0200 = geo_time.get_country_time_info("德国", ts=t_0200)
check("德国 02:00 深夜休息", de_0200["status_key"] == "night" and not de_0200["is_work_time"])
check("深夜建议避免打扰", "深夜" in de_0200["advice"])

# 周末判断：2026-07-18 为周六
t_sat = datetime.datetime(2026, 7, 18, 10, 0, 0, tzinfo=datetime.timezone.utc).timestamp()
de_sat = geo_time.get_country_time_info("德国", ts=t_sat)
check("德国周六为周末公休", de_sat["status_key"] == "weekend" and de_sat["is_weekend"])

# 中东伊斯兰周五休息：沙特阿拉伯在 2026-07-17 (周五) 10:00
t_fri = datetime.datetime(2026, 7, 17, 7, 0, 0, tzinfo=datetime.timezone.utc).timestamp()
sa_fri = geo_time.get_country_time_info("沙特阿拉伯", ts=t_fri)
check("沙特阿拉伯周五为周末公休", sa_fri["status_key"] == "weekend" and sa_fri["is_weekend"])


# ============ 5. HTML 组件与模板引擎渲染 ============
print("\n-- 5. HTML 徽章与详情卡片渲染 --")

badge_html = geo_time.render_time_badge("德国")
check("render_time_badge 包含 tz-badge", "tz-badge" in badge_html)
check("render_time_badge 包含时差说明", "慢" in badge_html or "无时差" in badge_html)

card_html = geo_time.render_time_card(de_1030)
check("render_time_card 包含时钟图标", "🕒" in card_html)
check("render_time_card 包含沟通时机建议", "邮件沟通时机建议" in card_html)
check("render_time_card 包含时区说明", "CET/CEST" in card_html)

# 模板内置函数测试
out_badge = templating.render_str("{{{ local_time_badge('德国') }}}", {})
check("模板内调用 local_time_badge 成功", "tz-badge" in out_badge)

contact_obj = {"email": "boss@mueller-handel.de", "country": "德国"}
out_obj_badge = templating.render_str("{{{ local_time_badge(c) }}}", {"c": contact_obj})
check("模板内向 local_time_badge 传入联系人对象成功", "tz-badge" in out_obj_badge)


# ============ 6. 客户管理数据层与 CSV 导出 ============
print("\n-- 6. 客户管理业务层与 CSV 导出集成 --")

# 新增一个用于测试时区导出的客户
ok, msg, cid = queries.save_contact({
    "email": "test-timezone-client@acme.de",
    "name": "Herr Klaus",
    "company": "Klaus GmbH",
    "country": "德国",
    "stage": "engaging",
    "note": "时区自动化测试客户",
})
check("创建测试客户成功", ok and cid is not None)

# 获取客户详情验证 local_time 与 local_time_card
fetched = queries.get_contact(cid)
check("get_contact 包含 local_time", fetched.get("local_time") is not None)
check("get_contact local_time 识别为德国", fetched["local_time"]["country"] == "德国")
check("get_contact 包含 local_time_card HTML", "tz-card" in (fetched.get("local_time_card") or ""))

# 验证客户列表 list_contacts
contact_list = queries.list_contacts(q="test-timezone-client")
target_item = next((item for item in contact_list["items"] if item["email"] == "test-timezone-client@acme.de"), None)
check("list_contacts 返回条目包含 local_time", target_item is not None and target_item.get("local_time") is not None)
check("list_contacts 包含 badge_html", "tz-badge" in (target_item["local_time"]["badge_html"] if target_item["local_time"] else ""))

# 验证 CSV 导出字段
csv_str = queries.export_contacts_csv()
lines = csv_str.splitlines()
check("CSV 导出包含 UTF-8 BOM", csv_str.startswith("\ufeff"))
header = lines[0]
check("CSV 包含'当地时间'列", "当地时间" in header)
check("CSV 包含'时区时差'列", "时区时差" in header)
check("CSV 包含'商务作息与建议'列", "商务作息与建议" in header)

target_csv_line = next((line for line in lines if "test-timezone-client@acme.de" in line), None)
check("测试客户数据行导出包含时区时差", target_csv_line is not None and "UTC" in target_csv_line)

# 清理测试客户
queries.delete_contact(cid)
check("删除测试客户成功", queries.get_contact(cid) is None)


# ============ 7. 品牌更名检查 ============
print("\n-- 7. 品牌名称更名检查 --")

dash_html = templating.render("dashboard.html", page="dashboard",
                              d={"today_in": 0, "today_high": 0, "today_auto": 0,
                                 "y_in": 0, "y_high": 0, "y_auto": 0,
                                 "threads_open": 0, "contacts_total": 0,
                                 "pending_drafts": 0, "avg_score": 0},
                              tr={"days": [], "labels_json": "[]", "in_json": "[]", "high_json": "[]"},
                              cats=[], leads=[], csrf="tok", high=70, ok="", err="")
check("看板页面包含'外贸询盘助手'", "外贸询盘助手" in dash_html)
check("看板页面导航栏品牌为'外贸询盘助手'", '<div class="brand">外贸询盘助手' in dash_html)

login_html = templating.render("login.html", err="")
check("登录页面包含'外贸询盘助手'", "外贸询盘助手" in login_html)


print("\n" + "=" * 60)
print(f"  专项测试通过 {passed} 项，失败 {failed} 项")
print("=" * 60)

if failed > 0:
    sys.exit(1)
else:
    print("全部通过！\n")
