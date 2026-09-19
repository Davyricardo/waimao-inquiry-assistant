"""FastAPI 主应用——路由注册中心。

原 1656 行的单文件已按功能域拆分为 app/routers/ 下的独立模块：
    dashboard  → GET /、/api/stats、/api/sync
    messages   → /messages/*
    contacts   → /contacts/*
    orders     → /orders/*
    freight    → /freight/*
    products   → /products/*
    social     → /social/*
    settings   → /settings、/rules、/templates、/review

本文件仅保留：
    1. FastAPI 实例创建与静态文件挂载
    2. 两个跨域中间件（guard 鉴权 + security_headers）
    3. 公共路由：/health、/login、/logout
    4. include_router 注册各功能域路由

安全取舍（重要）：本应用设计为**只监听 127.0.0.1**，通过 SSH 隧道访问。
因此不做 HTTPS —— 隧道本身已是加密的，浏览器也把 localhost 视为安全上下文。
若将来要直接暴露公网，必须先在前面加 HTTPS 反代。
"""
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import auth, config, db, queries
from .routers import (company, contacts, dashboard, freight, messages, orders,
                      products, settings, social)
from .routers.settings import MAIL_PRESETS, _ai_context  # noqa: F401 – 向后兼容，测试文件引用
from .templating import render

app = FastAPI(title="外贸询盘助手", docs_url=None, redoc_url=None)

STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

COOKIE = "mb_sess"

# ============ 中间件 ============
PUBLIC_PATHS = {"/login", "/health", "/favicon.ico"}


def current_user(request: Request):
    return auth.read_session(request.cookies.get(COOKIE, ""))


@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    if path in PUBLIC_PATHS or path.startswith("/static/"):
        return await call_next(request)
    if not current_user(request):
        if path.startswith("/api/"):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return RedirectResponse("/login", status_code=303)
    return await call_next(request)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ============ 公共路由 ============

@app.get("/health")
def health():
    """健康检查——隧道可达性探针，无需登录。"""
    try:
        with db.ro() as conn:
            conn.execute("SELECT 1").fetchone()
        return PlainTextResponse("ok")
    except Exception as e:
        return PlainTextResponse(f"db error: {e}", status_code=500)


@app.get("/login")
def login_page(request: Request, err: str = ""):
    if current_user(request):
        return RedirectResponse("/", status_code=303)
    return HTMLResponse(render("login.html", err=err))


@app.post("/login")
def login_submit(request: Request, username: str = Form(""),
                 password: str = Form("")):
    ip = request.client.host if request.client else "?"
    locked, wait = auth.throttle_check(ip)
    if locked:
        return HTMLResponse(render("login.html",
                                   err=f"尝试次数过多，请 {wait} 秒后再试"),
                            status_code=429)
    if not auth.check_login(username, password):
        auth.throttle_fail(ip)
        queries.log(ip, "login_failed", detail=username[:60])
        return HTMLResponse(render("login.html", err="用户名或密码错误"),
                            status_code=401)
    auth.throttle_reset(ip)
    queries.log(ip, "login_ok", detail=username[:60])
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(COOKIE, auth.make_session(username), httponly=True,
                    samesite="lax", max_age=config.SESSION_TTL, path="/")
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE, path="/")
    return resp


# ============ 注册各功能域路由 ============
app.include_router(dashboard.router)
app.include_router(messages.router)
app.include_router(contacts.router)
app.include_router(orders.router)
app.include_router(freight.router)
app.include_router(products.router)
app.include_router(social.router)
app.include_router(company.router)
app.include_router(settings.router)
