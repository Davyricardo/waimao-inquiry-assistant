"""询盘列表与详情路由。"""
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import auth, config, db, pipeline, queries
from ..templating import render

router = APIRouter()

COOKIE = "mb_sess"


def check_csrf(request: Request, token: str) -> bool:
    sess = request.cookies.get(COOKIE, "")
    return bool(token) and token == auth.csrf_token(sess)


@router.get("/messages", response_class=HTMLResponse)
def messages_page(request: Request, page: int = 1, category: str = "",
                  min_score: str = "", status: str = "", q: str = ""):
    ms = None
    try:
        ms = int(min_score) if min_score not in ("", None) else None
    except ValueError:
        ms = None
    data = queries.list_messages(page=page, category=category, min_score=ms,
                                 status=status, q=q)
    return HTMLResponse(render(
        "messages.html", data=data, f={"category": category,
                                       "min_score": min_score,
                                       "status": status, "q": q},
        csrf=auth.csrf_token(request.cookies.get(COOKIE, ""))))


@router.get("/messages/{mid}", response_class=HTMLResponse)
def message_detail(request: Request, mid: int, ok: str = "", err: str = ""):
    m = queries.get_message(mid)
    if not m:
        return HTMLResponse(render("error.html", msg="邮件不存在"), status_code=404)
    with db.ro() as conn:
        conn.execute("UPDATE messages SET is_read=1 WHERE id=?", (mid,))
    tmpl = queries.list_templates()
    return HTMLResponse(render(
        "message_detail.html", page="messages",
        m=m, templates=tmpl, ok=ok, err=err,
        csrf=auth.csrf_token(request.cookies.get(COOKIE, "")),
        high=config.HIGH_INTENT))


@router.post("/messages/{mid}/summarize")
def message_summarize(request: Request, mid: int, csrf_tok: str = Form("")):
    if not check_csrf(request, csrf_tok):
        return RedirectResponse(f"/messages/{mid}?err=1", status_code=303)
    ok, msg = pipeline.reprocess_message(mid)
    key = "ok" if ok else "err"
    return RedirectResponse(f"/messages/{mid}?{key}=" + quote(msg),
                            status_code=303)
