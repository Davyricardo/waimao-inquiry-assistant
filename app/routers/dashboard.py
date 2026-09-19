"""看板与同步路由。"""
import json
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import auth, config, pipeline, queries
from ..templating import render

router = APIRouter()

COOKIE = "mb_sess"


def check_csrf(request: Request, token: str) -> bool:
    sess = request.cookies.get(COOKIE, "")
    return bool(token) and token == auth.csrf_token(sess)


@router.get("/", response_class=HTMLResponse)
def index(request: Request, ok: str = "", err: str = ""):
    d = queries.dashboard()
    tr = queries.trend(7)
    cats = queries.category_breakdown(30)
    leads = queries.top_leads(8)

    max_c = max([c["c"] for c in cats], default=1)
    for c in cats:
        c["pct"] = round(c["c"] / max_c * 100) if max_c else 0

    tr["labels_json"] = json.dumps(tr["labels"], ensure_ascii=False)
    tr["in_json"] = json.dumps(tr["in_count"])
    tr["high_json"] = json.dumps(tr["high_intent"])

    stats = {
        "today_in": d["today_in"], "high_intent": d["high_intent"],
        "pending_review": d["pending_review"], "unread": d["unread"],
    }
    pending = d["pending_review"]
    badge = f'<span class="nav-badge">{pending}</span>' if pending else ""

    return HTMLResponse(render(
        "dashboard.html", page="dashboard", d=d, tr=tr, cats=cats, leads=leads,
        stats=stats, ok=ok, err=err, high=config.HIGH_INTENT,
        review_badge=badge,
        csrf=auth.csrf_token(request.cookies.get(COOKIE, ""))))


@router.get("/api/stats")
def api_stats(request: Request):
    return JSONResponse(queries.dashboard())


@router.post("/api/sync")
def api_sync(request: Request, csrf_tok: str = Form("")):
    if not check_csrf(request, csrf_tok):
        return RedirectResponse("/?err=1", status_code=303)
    accts = pipeline.get_accounts()
    if not accts:
        return RedirectResponse("/?err=" + quote("尚未配置邮箱账户"), status_code=303)
    total = {"new": 0, "errors": 0, "detail": []}
    for a in accts:
        st = pipeline.process_new_mail(a)
        total["new"] += st["new"]
        total["errors"] += st["errors"]
        total["detail"].extend(st["error_detail"])
    pipeline.refresh_daily_stats()
    if total["errors"]:
        return RedirectResponse(
            "/?err=" + quote(f"同步完成，新增 {total['new']} 封，"
                             f"{total['errors']} 个错误："
                             f"{'; '.join(total['detail'][:2])}"),
            status_code=303)
    return RedirectResponse("/?ok=" + quote(f"同步完成，新增 {total['new']} 封"),
                            status_code=303)
