"""每日自动采集（方案乙：两时段采集链），先到的触发者说了算。

时段编排（共用浏览器 profile/CDP 端口，链内串行）：
  - 早链（DAILY_COLLECT_AT，默认 09:00）：竞品价格 → 经营数据 → 全渠道点评
  - 晚链（DAILY_COLLECT_AT2，默认 16:00）：竞品价格 → 经营数据（不采点评）

两个触发者（每时段统一不早于各自时点）：
  - 监视线程每 poll_seconds 自醒一次：覆盖「中台开着没人动，到点自动采」
    和「时点后中台才启动，启动后尽快补当天这轮」；
  - 任何页面请求都会唤醒一次判定：覆盖「到点之后当天首次进入中台」。

判定规则（纯函数 plan_action，时钟由调用方注入）：
  - 该「数据×时段」已采到数据 → 不再采。判重读 daily_collects 表，
    collect_type 用「类型-时段」键（prices-am / prices-pm / ebook-am / ebook-pm /
    reviews-am），手动采集成功写「类型」（不带时段）意味着当天所有时段跳过该类，
    价格类还会核对 competitor_prices 里是否已有今天的数据（跑过 main.py 也算）；
  - 还没到当日时点 → 不动手；
  - 有任务在跑（含手动）→ 本轮让位，不占尝试次数；
  - 该时段自动尝试次数用完（默认 1，失败不重试）→ 等下一时段/明天。

执行链：单线程顺序跑（价格 → 停 5 秒 → 经营 → 停 5 秒 → 点评）。
起步前对每一步再判一次，避免「判定到起跑」之间有人手动采过造成重复。

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

# 时段键与展示名。早链 am 覆盖三类数据；晚链 pm 只补价格与经营。
_SLOTS = ("am", "pm")


# ---------------------------------------------------------------------------
# 判定（纯函数，测试不依赖真实时间）
# ---------------------------------------------------------------------------
def plan_action(done, attempts, max_attempts, running, now, at_time,
                cooldown=timedelta(0), last_attempt=None):
    """判定一类数据此刻该不该自动采。

    返回动作名（状态页与测试共用）：
      run                —— 该采，现在就采
      skip_done          —— 已采到数据（本时段或当天手动）
      postpone_running   —— 有任务在跑，本轮让位（不占次数）
      postpone_before_at —— 还没到当日时点
      skip_cap           —— 该时段自动尝试次数已用完
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


def _slot_settings(slot):
    """取某时段的 (时点, 是否含点评)。"""
    s = cfg.DAILY_COLLECT_CONFIG
    if slot == "am":
        return s["at_time"], bool(s.get("at_reviews", True))
    return s["at2_time"], bool(s.get("at2_reviews", False))


def _types_for_slot(slot):
    """该时段链要跑哪些数据类型（顺序即执行顺序）。"""
    _, with_reviews = _slot_settings(slot)
    ctypes = ["prices", "ebook"]
    if with_reviews:
        ctypes.append("reviews")
    return ctypes


def evaluate(now=None):
    """判定当天「数据×时段」各自的动作。只判定，不启动任何任务。

    判重口径（读 daily_collects，collect_type 存「类型-时段」或裸「类型」）：
      - 裸类型记录（手动采集/旧版写入）：视为全时段已采——手动采过就不再自动采；
      - prices 特例：competitor_prices 里已有当天数据也算早/晚链都已完成，
        防止外部跑过 main.py 后到点又采一遍。
    返回 {"am": {"prices": action, "ebook": action, "reviews": action},
          "pm": {...}}；不含点评的时段不出现 reviews 键。
    """
    now = now or datetime.now()
    settings = cfg.DAILY_COLLECT_CONFIG
    db = api.get_db()
    today = now.date().isoformat()
    records = db.get_daily_collects(today)
    running = api.has_running_job()
    have_prices = today in db.get_dates()

    plans = {}
    for slot in _SLOTS:
        at_time, with_reviews = _slot_settings(slot)
        slot_plans = {}
        for ctype in _types_for_slot(slot):
            if ctype == "reviews" and not with_reviews:
                continue
            # 裸类型记录（手动/旧版）优先：手动采成功 = 全时段已完成
            rec_any = records.get(ctype)
            if rec_any and rec_any["status"] == "done":
                slot_plans[ctype] = "skip_done"
                continue
            if ctype == "prices" and have_prices:
                # 外部已采到今天的竞品价（跑过 main.py）：两个时段都不再自动采价格
                slot_plans[ctype] = "skip_done"
                continue
            rec_slot = records.get(f"{ctype}-{slot}")
            slot_plans[ctype] = plan_action(
                done=bool(rec_slot and rec_slot["status"] == "done"),
                attempts=(rec_slot or {}).get("attempts") or 0,
                max_attempts=int(settings["max_attempts"]),
                running=running,
                now=now,
                at_time=at_time,
                cooldown=timedelta(minutes=int(settings["cooldown_minutes"])),
                last_attempt=_parse_dt((rec_slot or {}).get("updatedAt")),
            )
        plans[slot] = slot_plans
    return plans


def current_slots(now=None):
    """当前时间已到点、值得判定的时段列表（按时间顺序）。"""
    now = now or datetime.now()
    return [
        slot for slot in _SLOTS
        if now.time() >= _slot_settings(slot)[0]
    ]


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


def _ebook_wait_seconds():
    """eBook 任务的最坏时长：单次超时 × 重试次数，再加校验余量。"""
    ec = cfg.EBOOK_CONFIG
    per_run = int(ec.get("timeout") or 600)
    attempts = max(1, int(ec.get("attempts") or 2))
    return per_run * attempts + 300


_STARTERS = {
    "prices": (lambda: api.start_collect, _price_wait_seconds),
    "ebook": (lambda: api.start_ebook_collect, _ebook_wait_seconds),
    "reviews": (lambda: api.start_reviews_collect, _reviews_wait_seconds),
}


def _job_detail(ctype, job):
    """结果给状态页看的一句话。"""
    if job.get("state") == "done":
        if ctype == "prices":
            return f"{job.get('count')} 家竞品"
        if ctype == "ebook":
            return "eBook 经营数据已更新"
        return f"{job.get('count') or job.get('kept')} 条点评"
    return (job.get("error") or "未知原因")[:200]


def _run_one(slot, ctype, today, start_fn, wait_seconds):
    """跑一类采集并把结果写入当天该时段记录（自动路径才计尝试次数）。"""
    try:
        job = start_fn()
    except api.BusyError as exc:
        # 有人手动占着（或链上一步的浏览器还没退干净）：让位、不计数，
        # 下一次唤醒/轮询自然会再判一次。
        print(f"[每日采集] {ctype} 让位：{exc}")
        return
    state = api.wait_job(str(job["id"]), timeout=wait_seconds)
    if state == "done":
        print(f"[每日采集] ✅ {ctype}-{slot} 完成（任务 #{job['id']}）")
    elif state == "timeout":
        print(f"[每日采集] ⚠️ {ctype}-{slot} 任务 #{job['id']} 超过 {wait_seconds}s 未结束，放弃等待")
    else:
        print(f"[每日采集] ❌ {ctype}-{slot} 失败（任务 #{job['id']}）")
    try:
        api.get_db().mark_daily_collect(
            f"{ctype}-{slot}", today,
            "done" if state == "done" else "failed",
            job_id=str(job["id"]),
            detail="等待超时" if state == "timeout" else _job_detail(ctype, job),
            count_attempt=True,
        )
    except Exception as exc:  # noqa: BLE001 - 记录写失败不等于采集失败
        print(f"[每日采集] ⚠️ 写入每日记录失败：{type(exc).__name__}: {exc}")


def _run_chain(slot, today, plans):
    """顺序执行一个时段的链。每步起跑前再判一次，防窗口期重复。"""
    global _INFLIGHT
    ran_prev = False
    try:
        for ctype in _types_for_slot(slot):
            if plans.get(ctype) != "run":
                continue
            # 从判定到起跑可能隔了几秒：中间有人手动采过就别重复了
            fresh = evaluate()
            slot_fresh = fresh.get(slot) or {}
            if slot_fresh.get(ctype) != "run":
                print(f"[每日采集] {ctype}-{slot} 跳过：{slot_fresh.get(ctype)}")
                continue
            if ran_prev:
                time.sleep(5)  # 等浏览器/profile 完全释放，秒退重试兜不住的留给间隔
            starter, waiter = _STARTERS[ctype]
            _run_one(slot, ctype, today, starter(), waiter())
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
    """判定 → 按时段顺序起执行链。判定异常去重打印：不静默，也不每 30 秒刷一次屏。

    一次唤醒只起一个时段的链（时段间共享浏览器互斥），已在跑就让位——
    未完成的时段由监视线程接下来几次轮询自然补上。
    """
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

    for slot in _SLOTS:
        slot_plans = plans.get(slot) or {}
        if not any(a == "run" for a in slot_plans.values()):
            continue
        with _LOCK:
            if _INFLIGHT:
                return  # 已有链在跑：本时段留给下一次轮询
            _INFLIGHT = True
        threading.Thread(
            target=_run_chain, args=(slot, today, slot_plans), daemon=True
        ).start()
        return  # 一次只跑一个时段的链，结束时由轮询再判下一时段


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
def _slot_label(slot):
    return "早链" if slot == "am" else "晚链"


def status():
    """系统状态页的每日采集摘要。任何异常都降级成带 error 的字典，
    绝不让状态页因为这里挂掉。"""
    cfgs = cfg.DAILY_COLLECT_CONFIG
    out = {
        "enabled": bool(cfgs.get("enabled")),
        "at": cfgs.get("at") or "09:00",
        "at2": cfgs.get("at2") or "16:00",
        "atReviews": bool(cfgs.get("at_reviews", True)),
        "at2Reviews": bool(cfgs.get("at2_reviews", False)),
        "dataSource": cfg.DATA_SOURCE,
        "today": date.today().isoformat(),
        "skipReason": "",
        "inflight": _INFLIGHT,
        "slots": {"am": None, "pm": None},
    }
    if (cfg.DATA_SOURCE or "").lower() != "real":
        out["skipReason"] = "mock 模式自动跳过，避免把模拟数据写进真实库"
    try:
        db = api.get_db()
        records = db.get_daily_collects(out["today"])
        have_prices = out["today"] in db.get_dates()
        for slot in _SLOTS:
            _, with_reviews = _slot_settings(slot)
            view = {}
            for ctype in _types_for_slot(slot):
                rec_slot = records.get(f"{ctype}-{slot}")
                rec_any = records.get(ctype)
                view[ctype] = _type_view(rec_slot, rec_any, have_prices)
            out["slots"][slot] = view
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def _type_view(rec_slot, rec_any, have_prices=False):
    """单条数据在某时段的展示字段。

    rec_slot：该时段的自动记录；rec_any：裸类型记录（手动采/旧版）。
    展示优先本时段记录，否则回退手动记录。have_prices 仅对 prices 有意义。
    """
    rec = rec_slot or rec_any
    view = {
        "status": None, "attempts": 0, "jobId": None,
        "detail": None, "updatedAt": None, "done": False,
        "viaManual": bool(rec is rec_any and rec_any and not rec_slot),
        "slotDone": bool(rec_slot and rec_slot.get("status") == "done"),
    }
    if rec:
        view.update({
            "status": rec.get("status"),
            "attempts": rec.get("attempts") or 0,
            "jobId": rec.get("jobId"),
            "detail": rec.get("detail"),
            "updatedAt": rec.get("updatedAt"),
            "done": rec.get("status") == "done",
        })
    if view.get("status") is None and have_prices:
        view["done"] = True
    return view
