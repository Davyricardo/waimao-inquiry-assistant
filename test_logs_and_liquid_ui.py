"""日志模块、Token测算、24H邮件时点分析与 iOS 液态磨砂玻璃特效自动化测试。"""
import os
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from app import ai_client, config, db, queries
from app.routers.logs import api_ai_stats, api_daily_stats, api_timing_stats, logs_page
from app.templating import render
from starlette.requests import Request


def check(cond: bool, msg: str):
    if not cond:
        raise AssertionError(f"Test failed: {msg}")
    print(f"  [ OK ] {msg}")


def run_tests():
    print("=" * 65)
    print(" 日志模块、Token测算、24H邮件时点分析与 iOS 液态玻璃 UI 自动化测试")
    print("=" * 65)

    # 1. 验证数据库初始化与 ai_logs 表结构
    db.init_db()
    with db.ro() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(ai_logs)").fetchall()}
        check("prompt_tokens" in cols, "ai_logs 表存在 prompt_tokens 字段")
        check("completion_tokens" in cols, "ai_logs 表存在 completion_tokens 字段")
        check("cost_usd" in cols, "ai_logs 表存在 cost_usd 字段")
        check("cost_rmb" in cols, "ai_logs 表存在 cost_rmb 字段")
        check("purpose" in cols, "ai_logs 表存在 purpose 字段")

    # 2. 验证 Token 计费测算函数
    usd, rmb = ai_client.calculate_cost(1000, 500)
    # 1000 * 0.27 / 1M = 0.00027, 500 * 1.10 / 1M = 0.00055, total = 0.00082
    check(abs(usd - 0.00082) < 0.00001, f"Token USD 费用测算准确: {usd}")
    check(abs(rmb - round(0.00082 * 7.2, 6)) < 0.0001, f"Token RMB 费用折算准确: {rmb}")

    # 3. 验证 record_ai_usage 记录与查询
    test_ts = db.now_ts()
    ai_client.record_ai_usage(
        model="deepseek-chat",
        purpose="analysis",
        prompt_tokens=850,
        completion_tokens=220,
        total_tokens=1070,
        latency_ms=650,
        status="ok",
    )
    ai_client.record_ai_usage(
        model="deepseek-chat",
        purpose="summary",
        prompt_tokens=1200,
        completion_tokens=300,
        total_tokens=1500,
        latency_ms=720,
        status="ok",
    )
    ai_client.record_ai_usage(
        model="deepseek-chat",
        purpose="draft",
        prompt_tokens=600,
        completion_tokens=400,
        total_tokens=1000,
        latency_ms=810,
        status="ok",
    )

    # 4. 验证操作日志记录 audit_log
    queries.log("test_actor", "email_send_manual", "message", 999, "手动审核发出邮件测试")
    queries.log("test_actor", "voucher_create", "voucher", 888, "录入测试单证")

    # 5. 验证 queries.get_daily_work_stats()
    daily = queries.get_daily_work_stats()
    check("sent_emails" in daily, "daily_stats 包含 sent_emails")
    check("received_emails" in daily, "daily_stats 包含 received_emails")
    check("drafts_approved" in daily, "daily_stats 包含 drafts_approved")
    check("ai_tokens" in daily, "daily_stats 包含 ai_tokens")
    check(daily["ai_tokens"] >= 3570, f"今日 AI tokens 聚合统计正确: {daily['ai_tokens']}")
    check("daily_report_text" in daily, "daily_stats 包含 daily_report_text")
    check("【外贸业务工作日报" in daily["daily_report_text"], "日报包含外贸业务工作日报抬头")
    check("消耗 Token 总计" in daily["daily_report_text"], "日报包含 AI 提效与 Token 成本明细")

    # 6. 验证 queries.list_audit_logs()
    audit = queries.list_audit_logs(page=1, size=10)
    check(audit["total"] >= 2, f"audit_log 总数大于等于 2: {audit['total']}")
    check(len(audit["items"]) >= 2, "audit_log 分页拉取数据成功")

    # 7. 验证 queries.get_ai_usage_summary()
    ai_sum = queries.get_ai_usage_summary(days=30)
    check(ai_sum["calls"] >= 3, f"AI 汇总统计调用次数正确: {ai_sum['calls']}")
    check(ai_sum["total_tokens"] >= 3570, "AI 汇总统计 Token 总数正确")
    check(ai_sum["cost_rmb"] > 0, f"AI 汇总统计折合费用大于 0: {ai_sum['cost_rmb']}")
    check(len(ai_sum["purposes"]) >= 3, "AI 用途分布统计包含 analysis, summary, draft")

    # 8. 验证 queries.list_ai_logs()
    ai_logs = queries.list_ai_logs(page=1, size=10, purpose="analysis")
    check(ai_logs["total"] >= 1, "按用途筛选 ai_logs 正常工作")
    check(ai_logs["items"][0]["purpose"] == "analysis", "返回的流水项用途匹配")

    # 9. 验证 queries.get_email_24h_timing_analysis()
    timing = queries.get_email_24h_timing_analysis()
    check(len(timing["hours_data"]) == 24, "24 小时桶完整覆盖 00:00 - 23:00")
    check(len(timing["regions"]) >= 5, "全球主要外贸区域建议包含 5 个以上主要商区")
    check(any("欧洲" in r["region"] for r in timing["regions"]), "区域建议包含欧洲黄金窗口")
    check(any("北美洲" in r["region"] for r in timing["regions"]), "区域建议包含北美洲黄金窗口")

    # 10. 验证路由渲染 GET /logs 各种 Tab
    req = Request({'type': 'http', 'method': 'GET', 'headers': []})

    # Tab: daily
    resp_daily = logs_page(req, tab="daily")
    check(resp_daily.status_code == 200, "GET /logs?tab=daily 返回 200 OK")
    body_daily = resp_daily.body.decode("utf-8")
    check("日志与工作复盘中心" in body_daily, "页面包含大标题")
    check("下班工作复盘日报" in body_daily, "包含下班工作复盘日报卡片")
    check("copyDailyReport" in body_daily, "包含一键复制日报函数")
    check("业务操作明细流水" in body_daily, "包含操作记录流水")
    check("nav-item active" in body_daily, "侧边栏日志导航高亮激活")

    # Tab: ai
    resp_ai = logs_page(req, tab="ai")
    check(resp_ai.status_code == 200, "GET /logs?tab=ai 返回 200 OK")
    body_ai = resp_ai.body.decode("utf-8")
    check("Token 消耗总计" in body_ai, "包含 Token 消耗指标卡")
    check("DeepSeek 原厂官方计费标准" in body_ai, "包含官方计费说明与成本透明卡")
    check("AI 业务用途分布与费用占比" in body_ai, "包含用途分布卡")
    check("AI 实时调用明细记录" in body_ai, "包含实时调用流水明细")

    # Tab: timing
    resp_timing = logs_page(req, tab="timing")
    check(resp_timing.status_code == 200, "GET /logs?tab=timing 返回 200 OK")
    body_timing = resp_timing.body.decode("utf-8")
    check("24 小时邮件收发时点全天分布" in body_timing, "包含 24H 柱状时点图")
    check("主要外贸目标区域黄金沟通时段" in body_timing, "包含全球主要外贸时区建议")
    check("北京时间黄金收件箱置顶时段" in body_timing, "包含北京时间黄金窗口提示")

    # 11. 验证 JSON API 接口
    api_d = api_daily_stats()
    check(api_d.status_code == 200, "GET /api/logs/daily 返回 200")
    api_a = api_ai_stats()
    check(api_a.status_code == 200, "GET /api/logs/ai 返回 200")
    api_t = api_timing_stats()
    check(api_t.status_code == 200, "GET /api/logs/timing 返回 200")

    # 12. 验证 CSS 中 iOS 液态磨砂玻璃样式
    css_path = os.path.join(BASE_DIR, "app", "static", "style.css")
    with open(css_path, "r", encoding="utf-8") as f:
        css_content = f.read()
    check(".glass-card" in css_content, "CSS 包含 .glass-card 液态磨砂玻璃卡片类")
    check("backdrop-filter" in css_content, "CSS 包含 backdrop-filter 模糊效果")
    check(".glass-tabs" in css_content, "CSS 包含 .glass-tabs 胶囊导航类")
    check("liquidGlow" in css_content, "CSS 包含环境液态微光微晕动画")
    check("cubic-bezier(0.34, 1.56, 0.64, 1)" in css_content, "CSS 包含拟物理弹性过渡动画")

    print("\n" + "=" * 65)
    print(" ALL TESTS PASSED! 日志模块与 iOS 液态玻璃 UI 校验全部通过！")
    print("=" * 65)


if __name__ == "__main__":
    run_tests()
