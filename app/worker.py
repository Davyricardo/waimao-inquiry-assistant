"""邮件轮询守护进程。

每轮：拉新邮件 → 分析 → 起草 → 刷新统计。
异常不退出，退避重试，保证长期稳定运行。
"""
import signal
import sys
import time
import traceback

from . import config, db, pipeline

RUNNING = True


def _stop(signum, frame):
    global RUNNING
    RUNNING = False
    print(f"[worker] 收到信号 {signum}，准备退出…", flush=True)


def main() -> int:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    db.init_db()
    print(f"[worker] 启动，轮询间隔 {config.POLL_INTERVAL}s", flush=True)

    backoff = config.POLL_INTERVAL
    while RUNNING:
        cycle_start = time.time()
        try:
            accounts = pipeline.get_accounts()
            if not accounts:
                print("[worker] 未配置邮箱账户，等待中", flush=True)
            else:
                for a in accounts:
                    st = pipeline.process_new_mail(a)
                    print(f"[worker] {a['email']}: 拉取 {st['fetched']} · "
                          f"新增 {st['new']} · 错误 {st['errors']}", flush=True)
                    for e in st["error_detail"]:
                        print(f"[worker]   ! {e}", flush=True)
            pipeline.refresh_daily_stats()
            backoff = config.POLL_INTERVAL
        except Exception as e:
            print(f"[worker] 本轮异常: {type(e).__name__}: {e}", flush=True)
            traceback.print_exc()
            # 指数退避，最长 10 分钟，避免故障时疯狂重试打爆邮箱
            backoff = min(backoff * 2, 600)

        # 分片睡眠，保证停机能及时响应（1s 一片，最多 1s 延迟退出）
        slept = 0.0
        while slept < backoff and RUNNING:
            time.sleep(min(1.0, backoff - slept))
            slept += 1.0

    print("[worker] 已退出", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
