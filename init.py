"""部署初始化：生成密钥、设置管理员密码、建库。

用法：
    python init.py                 # 交互式设置管理员密码
    python init.py --pass 'xxx'    # 非交互
    python init.py --show-secrets  # 打印已生成的密钥（从 .env 读取）
"""
import argparse
import getpass
import os
import secrets
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
ENV_PATH = BASE / ".env"


def load_env() -> dict:
    out = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def write_env(vals: dict) -> None:
    lines = ["# 外贸询盘助手 环境配置（含密钥，切勿提交到代码库）", ""]
    for k, v in vals.items():
        lines.append(f"{k}={v}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(ENV_PATH, 0o600)
    except OSError:
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pass", dest="password", default=None)
    ap.add_argument("--user", default="admin")
    ap.add_argument("--show-secrets", action="store_true")
    ap.add_argument("--ai-key", default=None, help="模型 API 密钥（可选）")
    ap.add_argument("--ai-base", default=None, help="模型 API 地址（可选）")
    ap.add_argument("--ai-model", default=None, help="模型名（可选）")
    args = ap.parse_args()

    env = load_env()

    if args.show_secrets:
        for k in ("MB_SECRET_KEY", "MB_CRED_KEY", "MB_ADMIN_USER"):
            print(f"{k}={env.get(k, '(未设置)')}")
        print(f"MB_ADMIN_PASS_HASH={'已设置' if env.get('MB_ADMIN_PASS_HASH') else '(未设置)'}")
        return 0

    # --- 生成缺失的密钥 ---
    changed = False
    if not env.get("MB_SECRET_KEY"):
        env["MB_SECRET_KEY"] = secrets.token_urlsafe(48)
        changed = True
        print("[init] 已生成会话签名密钥 MB_SECRET_KEY")
    if not env.get("MB_CRED_KEY"):
        env["MB_CRED_KEY"] = secrets.token_urlsafe(48)
        changed = True
        print("[init] 已生成凭据加密密钥 MB_CRED_KEY")

    env.setdefault("MB_HOST", "127.0.0.1")
    env.setdefault("MB_PORT", "8090")
    env.setdefault("MB_ADMIN_USER", args.user)
    env.setdefault("MB_POLL_INTERVAL", "60")
    env.setdefault("MB_AUTO_SEND", "0")
    env.setdefault("MB_AI_FALLBACK", "1")
    env.setdefault("MB_TZ_OFFSET", "8")

    if args.ai_key:
        env["MB_AI_API_KEY"] = args.ai_key
        changed = True
    if args.ai_base:
        env["MB_AI_BASE_URL"] = args.ai_base
        changed = True
    if args.ai_model:
        env["MB_AI_MODEL"] = args.ai_model
        changed = True

    # --- 管理员密码 ---
    pw = args.password
    if not pw and not env.get("MB_ADMIN_PASS_HASH"):
        if sys.stdin.isatty():
            pw1 = getpass.getpass("设置管理员密码: ")
            pw2 = getpass.getpass("再输一次确认: ")
            if pw1 != pw2:
                print("[init] 两次输入不一致", file=sys.stderr)
                return 1
            if len(pw1) < 8:
                print("[init] 密码至少 8 位", file=sys.stderr)
                return 1
            pw = pw1
        else:
            pw = secrets.token_urlsafe(12)
            print(f"[init] 非交互模式，已生成随机密码: {pw}")

    if pw:
        sys.path.insert(0, str(BASE))
        from app.crypto_util import hash_password
        env["MB_ADMIN_PASS_HASH"] = hash_password(pw)
        changed = True
        print("[init] 管理员密码已设置")

    if changed or not ENV_PATH.exists():
        write_env(env)
        print(f"[init] 配置已写入 {ENV_PATH}")

    # --- 建库 ---
    sys.path.insert(0, str(BASE))
    for k, v in env.items():
        os.environ.setdefault(k, v)
    from app import db
    db.init_db()
    print("[init] 数据库已初始化")

    if not env.get("MB_AI_API_KEY"):
        print("[init] 未配置模型 API 密钥 —— 将使用规则分析模式（功能可用，"
              "意向打分精度较低）。配置方法：")
        print("       python init.py --ai-key '你的密钥' --ai-base 'https://api.deepseek.com' "
              "--ai-model deepseek-chat")
    return 0


if __name__ == "__main__":
    sys.exit(main())
