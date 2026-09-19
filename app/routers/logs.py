"""日志与工作复盘中心路由模块 (Logs Router)

三大核心能力：
1. 每日工作进度与操作检阅：汇总邮件外发、草稿审批、客户跟进、单证处理等，提供一键生成下班工作复盘日报。
2. 大模型调用记录与 Token 花销测算：实时记录 Prompt/Completion/Total Tokens 及折合 USD/RMB 费用，分析日度趋势与用途占比。
3. 24H 邮件时点分析与黄金沟通窗口：统计全天 24 小时买家来信与回复时点，结合全球时区算法提供外贸黄金沟通时间建议。
"""
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .. import auth, queries
from ..templating import render

COOKIE = "mb_sess"
router = APIRouter()


@router.get("/logs", response_class=HTMLResponse)
def logs_page(
    request: Request,
    tab: str = "daily",
    day: str = "",
    action_type: str = "",
    audit_page: int = 1,
    ai_purpose: str = "",
    ai_status: str = "",
    ai_page: int = 1,
    ok: str = "",
    err: str = ""
):
    """日志模块主页面：包含工作检阅、Token测算、24H时点分析三大Tab。"""
    active_tab = tab if tab in ("daily", "ai", "timing") else "daily"

    # 1. 每日工作检阅与动作
    daily_stats = queries.get_daily_work_stats(day_str=day)
    audit_data = queries.list_audit_logs(
        page=audit_page, size=20, day_str=day, action_type=action_type
    )

    # 2. AI 模型 Token 与费用测算
    ai_summary = queries.get_ai_usage_summary(days=30)
    ai_logs_data = queries.list_ai_logs(
        page=ai_page, size=20, purpose=ai_purpose, status=ai_status
    )

    # 3. 24H 邮件时点分析与黄金窗口
    timing_data = queries.get_email_24h_timing_analysis()

    return HTMLResponse(render(
        "logs.html",
        page="logs",
        active_tab=active_tab,
        day=day or daily_stats["day"],
        action_type=action_type,
        audit_page=audit_page,
        ai_purpose=ai_purpose,
        ai_status=ai_status,
        ai_page=ai_page,
        daily=daily_stats,
        audit=audit_data,
        ai=ai_summary,
        ai_logs=ai_logs_data,
        timing=timing_data,
        ok=ok,
        err=err,
        csrf=auth.csrf_token(request.cookies.get(COOKIE, "")),
    ))


@router.get("/api/logs/daily")
def api_daily_stats(day: str = ""):
    """获取指定日期的工作数据与文本日报 JSON。"""
    stats = queries.get_daily_work_stats(day_str=day)
    return JSONResponse(stats)


@router.get("/api/logs/ai")
def api_ai_stats():
    """获取大模型 Token 汇总统计 JSON。"""
    return JSONResponse(queries.get_ai_usage_summary())


@router.get("/api/logs/timing")
def api_timing_stats():
    """获取 24H 邮件收发时点分析 JSON。"""
    return JSONResponse(queries.get_email_24h_timing_analysis())
