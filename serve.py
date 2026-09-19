"""Web 服务入口：uvicorn app.web:app

默认只监听 127.0.0.1，靠 SSH 隧道访问。
"""
import sys

import uvicorn

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from app import config

if __name__ == "__main__":
    uvicorn.run("app.web:app", host=config.HOST, port=config.PORT,
                log_level="info", access_log=False)
