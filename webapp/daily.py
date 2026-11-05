"""每日自动采集：每天价格、点评各采一次，先到的触发者说了算。

两个触发者（统一不早于 DAILY_COLLECT_AT，默认 08:00）：
  - 监视线程每 poll_seconds 自醒一次：覆盖「中台开着没人动，到点自动采」
    和「8 点后中台才启动，启动后尽快补当天这次」；
  - 任何页面请求都会唤醒一次判定：覆盖「到点之后当天首次进入中台」。

判定规则（纯函数 plan_action，时钟由调用方注入）：
  - 当天已采到数据 → 不再采。判重读 daily_collects 表（含手动「立即采集」），
    价格类还会核对 competitor_prices 里是否已有今天的数据（外部跑过 main.py 也算）；
  - 还没到当天时点 → 不动手；
  - 有任务在跑（含手动）→ 本轮让位，不占当天尝试次数；
  - 当天自动尝试次数用完（默认 1，失败不重试）→ 等明天。

执行链：单线程顺序跑（价格 → 停几秒 → 点评），两类采集共用同一个
浏览器 profile 和 CDP 端口，绝不能并发。起步前对每一步再判一次，
避免「判定到起跑」之间有人手动采过造成重复。

线程边界：本模块只在 daemon 线程里干活，异常兜在链内打印到中台终端，
绝不能拖垮 webapp 的请求线程。
"""
import threading
import time
from datetime import date, datetime, timedelta

from app import config as cfg

from . import api

_WAKE = threading.Event()     # 请求钩子 → 监视线程的唤醒信号
_LOCK = threading.Lock()
_INFLIGHT = False            # 执行链是否在跑（含等待上一步结束的过程）
_LAST_ERROR = [None]         # 判定异常去重用：同一错误不重复刷屏


# ---------------------------------------------------------------------------
# 判定（纯函数，测试不依赖真实时间）
# ---------------------------------------------------------------------------
def plan_action(done, attempts, max_attempts, running, now, at_time,
                cooldown=timedelta(0), last_attempt=None):
    """判定一类数据此刻该不该自动采。

    返回动作名（状态页与测试共用）：
      run                —— 该采，现在就采
      skip_done          —— 今天已经采到数据
      postpone_running   —— 有任务在跑，本轮让位（不占次数）
      postpone_before_at —— 还没到当天时点
      skip_cap           —— 当天自动尝试次数已用完
      skip_cooldown      —— 刚失败过，还在冷却间隔里
    """
    if done:
        return "skip_done"
    if running:
        return "postpone_running"
    if now.time() < at_time:
        return "postpone_before_at"
    if attempts >= max_attempts:
        return "skip_cap"
    if attempts and last_attempt and (now - last_attempt) < cooldown:
        return "skip_cooldown"
    return "run"


def _parse_dt(value):
    """daily_collects.updated_at（本地时间字符串）→ datetime。解析不了返回 None。"""
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def evaluate(now=None):
    """判定当天两类数据各自的动作。只判定，不启动任何任务。

    读 daily_collects 表 + 竞品当日数据，不依赖进程内存——
    中台重启后判重依然正确，不会把当天已采过的再来一遍。
    返回 {"prices": action, "reviews": action}。
    """
    now = now or datetime.now()
    settings = cfg.DAILY_COLLECT_CONFIG
    db = api.get_db()
    today = now.date().isoformat()
    records = db.get_daily_collects(today)
    running = api.has_running_job()
    have_prices = today in db.get_dates()

    plans = {}
    for ctype in ("prices", "reviews"):
        rec = records.get(ctype)
        plans[ctype] = plan_action(
            done=bool(rec and rec["status"] == "done")
                 or (ctype == "prices" and have_prices),
            attempts=(rec or {}).get("attempts") or 0,
            max_attempts=int(settings["max_attempts"]),
            running=running,
            now=now,
            at_time=settings["at_time"],
            cooldown=timedelta(minutes=int(settings["cooldown_minutes"])),
            last_attempt=_parse_dt((rec or {}).get("updatedAt")),
        )
    return plans


# ---------------------------------------------------------------------------
# 执行链
# ---------------------------------------------------------------------------
def _price_wait_seconds():
    return int(cfg.SCRAPER_CONFIG.get("timeout") or 900) + 120


def _reviews_wait_seconds():
    """点评任务的最坏时长：单次超时 × 重试次数，再加校验余量。"""
    sc = cfg.REVIEWS_CONFIG
    per_run = int(sc.get("timeout") or 1800)
    attempts = max(1, int(sc.get("attempts") or 2))
    return per_run * attempts + 300


def _job_detail(ctype, job):
    """结果给状态页看的一句话。"""
    if job.get("state") == "done":
        n = job.get("count") or job.get("kept")
        return f"{n} 家竞品" if ctype == "prices" else f"{n} 条点评"
    return (job.get("error") or "未知原因")[:200]


def _run_one(ctype, today, start_fn, wait_seconds):
    """跑一类采集并把结果写入当天记录（自动路径才计尝试次数）。"""
    try:
        job = start_fn()
    except api.BusyError as exc:
        # 有人手动占着（或链上一步的浏览器还没退干净）：让位、不计数，
        # 下一次唤醒/轮询自然会再判一次。
        print(f"[每日采集] {ctype} 让位：{exc}")
        return
    state = api.wait_job(str(job["id"]), timeout=wait_seconds)
    if state == "done":
        print(f"[每日采集] ✅ {ctype} 完成（任务 #{job['id']}）")
    elif state == "timeout":
        print(f"[每日采集] ⚠️ {ctype} 任务 #{job['id']} 超过 {wait_seconds}s 未结束，放弃等待")
    else:
        print(f"[每日采集] ❌ {ctype} 失败（任务 #{job['id']}）")
    try:
        api.get_db().mark_daily_collect(
            ctype, today,
            "done" if state == "done" else "failed",
            job_id=str(job["id"]),
            detail="等待超时" if state == "timeout" else _job_detail(ctype, job),
            count_attempt=True,
        )
    except Exception as exc:  # noqa: BLE001 - 记录写失败不等于采集失败
        print(f"[每日采集] ⚠️ 写入每日记录失败：{type(exc).__name__}: {exc}")


def _run_chain(today, plans):
    """顺序执行：价格 → 缓冲 → 点评。每步起跑前再判一次，防窗口期重复。"""
    global _INFLIGHT
    ran_prev = False
    try:
        for ctype, start_fn, wait_seconds in (
            ("prices", api.start_collect, _price_wait_seconds()),
            ("reviews", api.start_reviews_collect, _reviews_wait_seconds()),
        ):
            if plans.get(ctype) != "run":
                continue
            # 从判定到起跑可能隔了几秒：中间有人手动采过就别重复了
            fresh = evaluate()
            if fresh.get(ctype) != "run":
                print(f"[每日采集] {ctype} 跳过：{fresh.get(ctype)}")
                continue
            if ran_prev:
                time.sleep(5)  # 等浏览器/profile 完全释放，秒退重试兜不住的留给间隔
            _run_one(ctype, today, start_fn, wait_seconds)
            ran_prev = True
    finally:
        with _LOCK:
            _INFLIGHT = False


# ---------------------------------------------------------------------------
# 监视线程与请求钩子
# ---------------------------------------------------------------------------
def maybe_wake():
    """请求钩子：唤醒监视线程做一次判定。必须永不抛错、永不阻塞请求。"""
    try:
        _WAKE.set()
    except Exception:  # noqa: BLE001
        pass


def _wake_and_dispatch():
    """判定 → 有 run 就起执行链。判定异常去重打印：不静默，也不每 30 秒刷一次屏。"""
    global _INFLIGHT
    try:
        if not cfg.DAILY_COLLECT_CONFIG.get("enabled"):
            return
        if (cfg.DATA_SOURCE or "").lower() != "real":
            return  # mock 模式自动跳过：自动跑会把模拟数据写进真实库
        today = date.today().isoformat()
        plans = evaluate()
    except Exception as exc:  # noqa: BLE001
        msg = f"{type(exc).__name__}: {exc}"
        if msg != _LAST_ERROR[0]:
            _LAST_ERROR[0] = msg
            print(f"[每日采集] 判定异常：{msg}")
        return
    _LAST_ERROR[0] = None

    if not any(a == "run" for a in plans.values()):
        return
    with _LOCK:
        if _INFLIGHT:
            return
        _INFLIGHT = True
    threading.Thread(target=_run_chain, args=(today, plans), daemon=True).start()


def _watch_loop():
    poll = max(5, int(cfg.DAILY_COLLECT_CONFIG.get("poll_seconds") or 30))
    while True:
        _WAKE.wait(timeout=poll)
        _WAKE.clear()
        _wake_and_dispatch()


def start_watcher():
    """启动监视线程。enabled=false 时不启动；返回是否真的启动了。"""
    if not cfg.DAILY_COLLECT_CONFIG.get("enabled"):
        return False
    threading.Thread(target=_watch_loop, name="daily-collect", daemon=True).start()
    return True


# ---------------------------------------------------------------------------
# 状态页摘要
# ---------------------------------------------------------------------------
def status():
    """系统状态页的每日采集摘要。任何异常都降级成带 error 的字典，
    绝不让状态页因为这里挂掉。"""
    out = {
        "enabled": bool(cfg.DAILY_COLLECT_CONFIG.get("enabled")),
        "at": cfg.DAILY_COLLECT_CONFIG.get("at") or "08:00",
        "dataSource": cfg.DATA_SOURCE,
        "today": date.today().isoformat(),
        "skipReason": "",
        "inflight": _INFLIGHT,
        "prices": None,
        "reviews": None,
    }
    if (cfg.DATA_SOURCE or "").lower() != "real":
        out["skipReason"] = "mock 模式自动跳过，避免把模拟数据写进真实库"
    try:
        db = api.get_db()
        records = db.get_daily_collects(out["today"])
        have_prices = out["today"] in db.get_dates()
        out["prices"] = _type_view(records.get("prices"), have_prices)
        out["reviews"] = _type_view(records.get("reviews"), False)
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def _type_view(rec, have_data):
    """单类数据的展示字段。have_data：库里已有今天竞品价时，视同已采。"""
    view = {
        "status": None, "attempts": 0, "jobId": None,
        "detail": None, "updatedAt": None, "done": bool(have_data),
    }
    if rec:
        view.update({
            "status": rec.get("status"),
            "attempts": rec.get("attempts") or 0,
            "jobId": rec.get("jobId"),
            "detail": rec.get("detail"),
            "updatedAt": rec.get("updatedAt"),
            "done": rec.get("status") == "done" or bool(have_data),
        })
    return view
