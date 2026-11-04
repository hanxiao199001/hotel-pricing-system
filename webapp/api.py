"""中台 API：把真实数据、算法中间量、静态示例数据组装成前端要的结构。

每个返回体都带 source 字段：
  live   —— 来自 pricing.db / 本次实时计算
  static —— 来自 data/static.json 的示例数据
  mixed  —— 同一页面里两者都有（概览页就是这种情况）
"""
import collections
import contextlib
import io
import os
import statistics
import threading
import time
import traceback
from datetime import datetime

import config as cfg
from algorithm import PricingAlgorithm
from database import DatabasePathError, PricingDatabase

from . import review_pages, static_data

_ALGO = PricingAlgorithm(cfg.PRICING_STRATEGY)

_db_instance = None
_db_lock = threading.Lock()


class BusyError(RuntimeError):
    """已有采集任务在跑，拒绝并发。"""


def get_db() -> PricingDatabase:
    """惰性单例。数据库文件被删掉也能自动重建，不至于让整个服务挂掉。"""
    global _db_instance
    with _db_lock:
        if _db_instance is None:
            _db_instance = PricingDatabase(cfg.DATABASE["path"])
        return _db_instance


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
_BUCKETS = [
    (0, 100),
    (100, 150),
    (150, 200),
    (200, 250),
    (250, 300),
    (300, 400),
    (400, 500),
    (500, 700),
    (700, 10**9),
]


def _percentile(sorted_prices, q: float):
    if not sorted_prices:
        return None
    if len(sorted_prices) == 1:
        return sorted_prices[0]
    idx = (len(sorted_prices) - 1) * q
    lo = int(idx)
    hi = min(lo + 1, len(sorted_prices) - 1)
    frac = idx - lo
    return sorted_prices[lo] * (1 - frac) + sorted_prices[hi] * frac


def _price_stats(prices):
    """价格分布统计。中位数是算法依据，均值只作参考。"""
    if not prices:
        return None
    s = sorted(prices)
    return {
        "count": len(s),
        "min": round(s[0], 2),
        "max": round(s[-1], 2),
        "avg": round(statistics.mean(s), 2),
        "median": round(statistics.median(s), 2),
        "p10": round(_percentile(s, 0.10), 2),
        "p25": round(_percentile(s, 0.25), 2),
        "p75": round(_percentile(s, 0.75), 2),
        "p90": round(_percentile(s, 0.90), 2),
    }


def _histogram(prices):
    out = []
    for lo, hi in _BUCKETS:
        n = sum(1 for p in prices if lo <= p < hi)
        if n == 0 and lo > (max(prices) if prices else 0):
            continue
        label = f"{lo}-{hi}" if hi < 10**9 else f"{lo}+"
        out.append({"label": label, "from": lo, "to": None if hi >= 10**9 else hi, "count": n})
    return out


def _config_check():
    """启动配置是否就绪。不抛异常，让前端能显示具体缺什么。"""
    try:
        cfg.validate_startup_config()
        return {"ok": True, "message": ""}
    except cfg.ConfigError as exc:
        return {"ok": False, "message": str(exc).split("\n诊断信息")[0]}


# ---------------------------------------------------------------------------
# 采集任务：一次只能跑一个
# ---------------------------------------------------------------------------
_jobs = {}
_jobs_lock = threading.Lock()
_job_seq = [0]
_MAX_LOG_LINES = 500


class _JobLog(io.TextIOBase):
    """把 main.run_pricing_check 的 stdout 收进任务日志。"""

    def __init__(self, job):
        self.job = job

    def write(self, text):
        if text:
            for line in text.splitlines():
                if line.strip():
                    self.job["log"].append(line.rstrip())
            if len(self.job["log"]) > _MAX_LOG_LINES:
                del self.job["log"][: len(self.job["log"]) - _MAX_LOG_LINES]
        return len(text)

    def flush(self):
        pass


def _run_collect(job):
    try:
        import pipeline

        buffer = _JobLog(job)
        with contextlib.redirect_stdout(buffer):
            result = pipeline.run_pricing_check()
        job["suggested"] = result["suggestion"]["suggested_price"]
        job["count"] = result["stats"]["count"]
        job["state"] = "done"
    except BaseException as exc:  # noqa: BLE001 - 任务线程必须兜住所有异常
        job["state"] = "failed"
        job["error"] = f"{type(exc).__name__}: {exc}"
        job["log"].append(f"❌ {job['error']}")
        if os.environ.get("WEBAPP_DEBUG") == "1":
            job["log"].extend(traceback.format_exc().splitlines())
    finally:
        job["finished"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _mark_daily_result("prices", job)


def _new_job(kind):
    """建一个任务记录；已有任务在跑就拒绝（两种采集共用一个互斥锁）。"""
    with _jobs_lock:
        for j in _jobs.values():
            if j["state"] == "running":
                raise BusyError(f"已有采集任务在运行（#{j['id']} {j.get('kind', '')}），请等它结束。")
        _job_seq[0] += 1
        jid = str(_job_seq[0])
        job = {
            "id": jid,
            "kind": kind,
            "state": "running",
            "started": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "finished": None,
            "error": None,
            "log": [],
        }
        _jobs[jid] = job
    return job


def _append_log(job, line):
    if not line:
        return
    job["log"].append(str(line).rstrip())
    if len(job["log"]) > _MAX_LOG_LINES:
        del job["log"][: len(job["log"]) - _MAX_LOG_LINES]


def start_collect():
    job = _new_job("prices")
    threading.Thread(target=_run_collect, args=(job,), daemon=True).start()
    return dict(job, log=list(job["log"]))


def _run_reviews_collect(job):
    try:
        from scrapers import reviews_runner

        result = reviews_runner.collect(log=lambda line: _append_log(job, line))
        job["kept"] = result["kept"]
        job["count"] = result["kept"]
        job["state"] = "done"
    except BaseException as exc:  # noqa: BLE001
        job["state"] = "failed"
        job["error"] = str(exc) if isinstance(exc, RuntimeError) else f"{type(exc).__name__}: {exc}"
        _append_log(job, f"❌ {job['error']}")
        if os.environ.get("WEBAPP_DEBUG") == "1":
            job["log"].extend(traceback.format_exc().splitlines())
    finally:
        job["finished"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _mark_daily_result("reviews", job)


def start_reviews_collect():
    job = _new_job("reviews")
    threading.Thread(target=_run_reviews_collect, args=(job,), daemon=True).start()
    return dict(job, log=list(job["log"]))


def get_job(job_id: str):
    job = _jobs.get(str(job_id))
    if job is None:
        raise KeyError(job_id)
    return dict(job, log=list(job["log"]))


def list_jobs():
    return [
        {k: v for k, v in j.items() if k != "log"}
        for j in sorted(_jobs.values(), key=lambda x: int(x["id"]), reverse=True)
    ]


def has_running_job() -> bool:
    """有没有采集任务在跑。每日自动采集用它决定让不让位。"""
    with _jobs_lock:
        return any(j["state"] == "running" for j in _jobs.values())


def wait_job(job_id, timeout, poll=2.0):
    """等一个任务离开 running 状态。

    返回最终 state；超过 timeout 返回 "timeout"（任务可能仍在跑，
    交给它自己的超时机制收尾，这里只放弃等待）。供每日采集链使用。
    """
    deadline = time.time() + float(timeout)
    while True:
        with _jobs_lock:
            job = _jobs.get(str(job_id))
            state = job["state"] if job else "missing"
        if state != "running":
            return state
        if time.time() >= deadline:
            return "timeout"
        time.sleep(poll)


def _mark_daily_result(ctype, job):
    """任务结束时把结果写进 daily_collects（手动点按钮与每日自动链共用）。

    两边都走这里，「当天已采过」的判重口径才一致：手动采成功后，
    当天的自动任务直接跳过，不会到点再采一遍。写失败不抛——
    记录写不进去不该影响采集本身，但会打印到终端，不静默。
    """
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        ok = job.get("state") == "done"
        detail = (
            (f"{job.get('count')} 家竞品" if ctype == "prices"
             else f"{job.get('kept')} 条点评")
            if ok else (job.get("error") or "")[:200]
        )
        get_db().mark_daily_collect(
            ctype, today, "done" if ok else "failed",
            job_id=str(job.get("id")), detail=detail,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[每日采集] ⚠️ 写入每日记录失败：{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# 接口
# ---------------------------------------------------------------------------
def _modules_with_status():
    """模块清单：static.json 里的 status 是写死的，点评两页要按快照是否可用改写。"""
    mods = static_data.list_modules()
    try:
        import reviews_analytics

        rv = reviews_analytics.summary_or_none()
    except Exception:  # noqa: BLE001
        rv = None
    if not rv:
        return mods
    for m in mods:
        if m["key"] in ("reviews", "reputation"):
            m["status"] = "snapshot"
            m["statusNote"] = (
                f"真实点评快照：{rv['snapshot']['kept']} 条携程点评，"
                f"截至 {rv['snapshot']['dateMax']}"
            )
    # 回复草稿是本次新增的实装模块，不在 static.json 的示例清单里，单独补一条
    try:
        drafts = get_db().get_reply_stats()
        mods.append({
            "key": "replies",
            "title": "差评回复",
            "status": "snapshot",
            "statusNote": (
                f"基于同一份点评快照；已保存 {drafts['total']} 条草稿、"
                f"已采用 {drafts['used']} 条"
            ),
        })
    except Exception:  # noqa: BLE001 - 库异常不该让清单消失
        pass
    return mods


def get_status():
    db = get_db()
    counts = {}
    for table in ("my_hotel_prices", "competitor_prices", "price_suggestions",
                  "price_adjustments", "daily_collects"):
        try:
            counts[table] = db._rows(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]
        except Exception:  # noqa: BLE001
            counts[table] = None

    serialized = {
        "script": cfg.SCRAPER_CONFIG.get("script"),
        "scriptExists": os.path.isfile(cfg.SCRAPER_CONFIG.get("script") or ""),
        "python": cfg.SCRAPER_CONFIG.get("python"),
        "pythonExists": os.path.isfile(cfg.SCRAPER_CONFIG.get("python") or ""),
        "profile": cfg.SCRAPER_CONFIG.get("profile"),
        "profileExists": os.path.isdir(cfg.SCRAPER_CONFIG.get("profile") or ""),
        "channel": cfg.SCRAPER_CONFIG.get("channel"),
        "timeout": cfg.SCRAPER_CONFIG.get("timeout"),
    }

    try:
        from scrapers import reviews_runner

        reviews_info = reviews_runner.snapshot_info()
    except Exception as exc:  # noqa: BLE001 - 点评链路坏了不该让状态页挂掉
        reviews_info = {"snapshotExists": False, "error": f"{type(exc).__name__}: {exc}"}

    try:
        from . import daily as daily_module

        daily_info = daily_module.status()
    except Exception as exc:  # noqa: BLE001
        daily_info = {"enabled": False, "error": f"{type(exc).__name__}: {exc}"}

    return {
        "source": "live",
        "serverTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "hotel": {
            "name": cfg.MY_HOTEL.get("name") or "（未配置）",
            "city": cfg.MY_HOTEL.get("city"),
            "currentPrice": cfg.MY_HOTEL.get("current_price"),
            "occupancyRate": cfg.MY_HOTEL.get("occupancy_rate"),
            "roomsTotal": cfg.MY_HOTEL.get("rooms_total"),
        },
        "dataSource": cfg.DATA_SOURCE,
        "config": _config_check(),
        "database": {
            "path": cfg.DATABASE["path"],
            "counts": counts,
            "dates": db.get_dates(),
        },
        "scraper": serialized,
        "reviews": reviews_info,
        "daily": daily_info,
        "llm": _llm_status(),
        "replies": get_db().get_reply_stats(),
        "strategy": cfg.PRICING_STRATEGY,
        "jobs": list_jobs(),
        "modules": _modules_with_status(),
        "staticMeta": static_data.load().get("meta", {}),
    }


def get_radar(date=None):
    db = get_db()
    dates = db.get_dates()
    if not dates:
        return {
            "source": "live",
            "empty": True,
            "message": "库里还没有竞品数据。先在「系统状态」里点一次采集，或运行 python main.py。",
            "dates": [],
        }

    date = date or dates[0]
    if date not in dates:
        date = dates[0]

    competitors = db.get_competitors(date)
    prices = [c["price"] for c in competitors]
    my = db.get_latest_my_price() or {}
    my_price = my.get("price")

    bands = []
    if my_price:
        lo = my_price * cfg.PRICING_STRATEGY["band_low_ratio"]
        hi = my_price * cfg.PRICING_STRATEGY["band_high_ratio"]
        peers = [p for p in prices if lo <= p <= hi]
        bands = [
            {"key": "all", "label": "全部竞品", "range": None, "count": len(prices),
             "median": round(statistics.median(prices), 2) if prices else None},
            {"key": "peers", "label": "同档竞品（算法使用）", "range": [round(lo), round(hi)],
             "count": len(peers), "median": round(statistics.median(peers), 2) if peers else None},
        ]

    cheaper = sum(1 for p in prices if my_price and p < my_price)
    expensive = sum(1 for p in prices if my_price and p > my_price)

    return {
        "source": "live",
        "empty": False,
        "date": date,
        "dates": dates,
        "myPrice": my_price,
        "occupancyRate": my.get("occupancy_rate"),
        "competitors": competitors,
        "stats": _price_stats(prices),
        "histogram": _histogram(prices),
        "bands": bands,
        "cheaperCount": cheaper,
        "expensiveCount": expensive,
        "sameCount": len(prices) - cheaper - expensive,
        "position": _ALGO.analyze_market_position(my_price, prices) if my_price else None,
        "trend": db.get_price_trend(14),
        "distanceKm": cfg.COMPETITOR_FILTER.get("distance_km"),
    }


def analyze(occupancy_rate=None, price=None, date=None):
    """what-if：用真实竞品数据，按指定入住率/当前价重算一遍。

    这是中台相对命令行的主要增量——店长可以拖动入住率看建议价怎么变，
    而不必改 .env 再跑一次一分钟的采集。
    """
    radar = get_radar(date)
    if radar.get("empty"):
        return dict(radar, source="live")

    prices = [c["price"] for c in radar["competitors"]]
    occ = float(occupancy_rate) if occupancy_rate is not None else (
        radar.get("occupancyRate") or cfg.MY_HOTEL.get("occupancy_rate") or 45.0
    )
    if price is None or price == "":
        cur = radar.get("myPrice") or cfg.MY_HOTEL.get("current_price")
    else:
        cur = price
    if cur is None or cur == "":
        raise ValueError("缺少当前价，无法计算建议价。")
    cur = float(cur)
    if cur <= 0:
        raise ValueError("当前价必须大于 0。")

    detail = _ALGO.explain(occ, cur, prices)
    position = _ALGO.analyze_market_position(cur, prices)

    # 三个入住率档位的对比，让「为什么是这个价」更直观
    bands = []
    for o in (20, 45, 75):
        d = _ALGO.explain(o, cur, prices)
        bands.append({
            "occupancy": o,
            "suggested": d["suggested_price"],
            "ideal": d["ideal_price"],
            "stepped": d["stepped"],
        })

    return {
        "source": "live",
        "date": radar["date"],
        "detail": detail,
        "position": position,
        "bands": bands,
        "stats": radar["stats"],
        "histogram": radar["histogram"],
        "competitorCount": len(prices),
    }


def get_suggestions(limit=20):
    db = get_db()
    items = db.get_suggestions(limit=limit)
    adjustments = db.get_adjustments(limit=limit)
    pending = [s for s in items if s.get("status") == "pending"]
    return {
        "source": "live",
        "suggestions": items,
        "adjustments": adjustments,
        "pendingCount": len(pending),
    }


def decide(suggestion_id, decision, note=""):
    db = get_db()
    updated = db.decide_suggestion(int(suggestion_id), decision, note or "")
    return {"source": "live", "suggestion": updated}


def _current_price():
    """当前挂牌价：优先用最近一次采集写入库的值，其次用 .env 配置。"""
    try:
        my = get_db().get_latest_my_price() or {}
    except Exception:  # noqa: BLE001 - 库坏了不该让页面挂掉
        my = {}
    return my.get("price") or cfg.MY_HOTEL.get("current_price")


def _room_signal():
    """房型口碑 → 定价信号。快照缺失时返回不可用，页面据此隐藏区块。"""
    try:
        import room_reputation

        return room_reputation.signal()
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# 差评回复草稿
# ---------------------------------------------------------------------------
def _reviews_context():
    """读一次快照，返回 (summary, hotel_name)。没有快照时返回 (None, '')。"""
    import reviews_analytics

    summary = reviews_analytics.summary_or_none()
    if not summary:
        return None, ""
    return summary, (summary["snapshot"].get("hotel") or "")


def _llm_status():
    try:
        import llm

        ok, reason = llm.available()
        return {
            "available": ok,
            "reason": reason,
            "model": cfg.LLM_CONFIG.get("model") or None,
            "baseUrl": cfg.LLM_CONFIG.get("base_url") or None,
        }
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}


def get_replies(theme=None, status=None):
    """差评列表 + 每条对应的回复草稿。

    模板稿是确定性的、随时能算（纯字符串拼接，30 条约 5 ms），
    所以即使一条都没保存过，打开页面也有草稿可看；
    一旦人工生成或编辑过并落库，就以库里的为准。
    """
    import reply_draft

    summary, hotel = _reviews_context()
    if not summary:
        return {"available": False, "reason": "缺少点评快照，先采集一次点评"}

    db = get_db()
    saved_map = db.get_reply_draft_map()
    theme_counts = collections.Counter()
    items = []

    for row in summary["badRows"]:
        key = reply_draft.review_key(row)
        saved = saved_map.get(key)
        generated = reply_draft.build_draft(row, hotel_name=hotel)
        if not generated.get("available"):
            continue

        for t in generated["themes"]:
            theme_counts[t] += 1

        if saved:
            text = saved["draft"]
            source = saved["source"]
            draft_status = saved["status"]
            edited = bool(saved["edited"])
            updated = saved.get("updated_at")
        else:
            text = generated["text"]
            source = "template"
            draft_status = "draft"
            edited = False
            updated = None

        items.append({
            "key": key,
            "date": generated["date"],
            "rating": generated["rating"],
            "room": generated["room"],
            "roomTier": generated["roomTier"],
            "tone": generated["tone"],
            "themes": generated["themes"],
            "skippedThemes": generated["skippedThemes"],
            "droppedThemes": generated["droppedThemes"],
            "warnings": generated["warnings"],
            "text": text,
            "templateText": generated["text"],
            "placeholders": generated["placeholders"],
            "checklist": generated["checklist"],
            "wordCount": generated["wordCount"],
            "source": source,
            "status": draft_status,
            "edited": edited,
            "updatedAt": updated,
            "alreadyReplied": generated["alreadyReplied"],
            "content": row.get("full") or "",
        })

    if theme:
        items = [i for i in items if theme in i["themes"]]
    if status:
        items = [i for i in items if i["status"] == status]

    stats = db.get_reply_stats()
    return {
        "available": True,
        "source": "snapshot",
        "snapshotDate": summary["snapshot"].get("dateMax"),
        "hotel": hotel,
        "badTotal": summary["overall"]["bad"],
        "items": items,
        "stats": {
            "shown": len(items),
            "drafted": sum(1 for i in items if i["status"] != "draft" or i["source"] != "template"),
            "used": sum(1 for i in items if i["status"] == "used"),
            "discarded": sum(1 for i in items if i["status"] == "discarded"),
            "savedTotal": stats["total"],
        },
        "themeCounts": [{"label": k, "count": v} for k, v in theme_counts.most_common()],
        "llm": _llm_status(),
    }


def _find_bad_row(key):
    import reply_draft

    summary, hotel = _reviews_context()
    if not summary:
        raise ValueError("缺少点评快照")
    for row in summary["badRows"]:
        if reply_draft.review_key(row) == key:
            return row, hotel
    raise ValueError("找不到这条点评（快照可能已更新，请刷新页面）")


def generate_reply(key, prefer_llm=False):
    """生成（或重新生成）一条草稿并落库。

    prefer_llm=True 且模型可用时才走模型；模型失败不抛给前端，
    回退模板并在 fallbackReason 里说明原因——回复功能不该因为
    外部服务抖动而不可用。
    """
    import llm
    import reply_draft

    row, hotel = _find_bad_row(key)
    base = reply_draft.build_draft(row, hotel_name=hotel)
    if not base.get("available"):
        raise ValueError(base.get("reason") or "这条点评无法生成草稿")

    text, source, fallback, note = base["text"], "template", None, ""

    if prefer_llm:
        ok, reason = llm.available()
        if not ok:
            fallback = f"未使用模型：{reason}"
        else:
            try:
                result = llm.draft_reply(
                    row, hotel_name=hotel,
                    themes=base["themes"], room_tier=base["roomTier"] or "",
                )
                text, source = result["text"], "llm"
            except llm.LLMError as exc:
                fallback = f"模型生成失败，已回退模板：{exc}"
            except Exception as exc:  # noqa: BLE001 - 兜住任何意外，不能阻塞页面
                fallback = f"模型调用异常，已回退模板：{type(exc).__name__}: {exc}"

    saved = get_db().save_reply_draft(
        review_key=key,
        draft=text,
        source=source,
        review_date=base["date"] or "",
        rating=base["rating"],
        room=base["room"] or "",
        themes=base["themes"],
        note=note,
        edited=False,
    )
    return {
        "ok": True,
        "source": source,
        "fallbackReason": fallback,
        "draft": {
            "key": key,
            "text": text,
            "status": saved["status"],
            "updatedAt": saved.get("updated_at"),
        },
    }


def save_reply_text(key, text):
    """人工编辑后的正文存回库（标记 edited，便于区分哪些是原样模板稿）。"""
    text = (text or "").strip()
    if len(text) < 20:
        raise ValueError("正文太短，至少 20 字")
    db = get_db()
    existing = db.get_reply_draft_map().get(key)
    if not existing:
        raise ValueError("这条还没有草稿，请先生成")
    saved = db.save_reply_draft(
        review_key=key,
        draft=text,
        source=existing["source"],
        review_date=existing.get("review_date") or "",
        rating=existing.get("rating"),
        room=existing.get("room") or "",
        themes=existing.get("themes") or "",
        note=existing.get("note") or "",
        edited=True,
    )
    return {"ok": True, "draft": {"key": key, "text": saved["draft"],
                                  "edited": bool(saved["edited"]),
                                  "status": saved["status"]}}


def set_reply_status(key, status, note=""):
    """标记草稿状态（draft / used / discarded）。

    页面上的模板稿是现场算的、不落库，所以「标记已采用/弃用」可能是这条
    点评第一次写库：记录不存在时先落一份当前模板稿再标状态，
    否则对没生成过的差评直接标记会一直 400。
    """
    db = get_db()
    try:
        saved = db.set_reply_draft_status(key, status, note=note or None)
    except ValueError:
        if status not in ("draft", "used", "discarded"):
            raise
        generate_reply(key, prefer_llm=False)
        saved = db.set_reply_draft_status(key, status, note=note or None)
    return {"ok": True, "draft": {"key": key, "status": saved["status"],
                                  "note": saved.get("note")}}


def get_module(key):
    """有真实数据源的模块走真实数据，没有的用示例数据。

    「差评预警」「口碑分析」由点评快照计算；快照缺失时自动退回 static.json，
    页面不会白屏，只是标记回「示例数据」。
    """
    live = review_pages.build(key, price=_current_price())
    if live is not None:
        return live
    mod = static_data.get_module(key)
    mod["source"] = "static"
    return mod


def get_modules():
    mods = _modules_with_status()
    has_snapshot = any(m["status"] == "snapshot" for m in mods)
    return {
        "source": "mixed" if has_snapshot else "static",
        "meta": static_data.load().get("meta", {}),
        "modules": mods,
    }


def get_overview():
    """概览页：真实数据与静态数据混排，各自标记来源。"""
    radar = get_radar()
    empty = radar.get("empty", True)

    my = get_db().get_latest_my_price() or {}
    my_price = my.get("price") or cfg.MY_HOTEL.get("current_price")
    pending = [s for s in get_db().get_suggestions(limit=10) if s.get("status") == "pending"]

    static = static_data.load().get("modules", {})
    reviews_mod = static.get("reviews", {}).get("blocks", [])
    review_pending = 0
    review_live = False

    # 优先用真实点评快照：这里的「待处理」是未回复的差评条数
    try:
        import reviews_analytics

        rv = reviews_analytics.summary_or_none()
    except Exception:  # noqa: BLE001 - 快照坏了也不该拖垮概览页
        rv = None
    if rv:
        review_pending = rv["overall"]["unreplied"]
        review_live = True
    else:
        for block in reviews_mod:
            if block.get("type") == "kpi":
                for item in block.get("items", []):
                    if item.get("label") == "待人工确认":
                        try:
                            review_pending = int(str(item.get("value", "0")).split()[0])
                        except ValueError:
                            review_pending = 0

    kpis = [
        {
            "label": "今日挂牌价",
            "value": f"¥{my_price:.0f}" if my_price else "—",
            "sub": "配置值 / 最近一次采集写入",
            "source": "live",
        },
        {
            "label": "入住率",
            "value": f"{(my.get('occupancy_rate') or cfg.MY_HOTEL.get('occupancy_rate') or 0):.0f}%",
            "sub": "人工填写，决定调价方向",
            "source": "live",
            "tone": "warm",
        },
        {
            "label": "竞品中位价",
            "value": f"¥{radar['stats']['median']:.0f}" if not empty else "—",
            "sub": f"{radar['stats']['count']} 家竞品 · 算法依据" if not empty else "库内无数据",
            "source": "live",
        },
        {
            "label": "待我拍板",
            "value": f"{len(pending) + review_pending} 项",
            "sub": (
                f"调价 {len(pending)} · 差评待回复 {review_pending}"
                if review_live
                else f"调价 {len(pending)} · 差评 {review_pending}（示例）"
            ),
            "source": "live" if review_live else "mixed",
            "tone": "warm",
        },
    ]

    if not empty:
        detail = _ALGO.explain(
            my.get("occupancy_rate") or cfg.MY_HOTEL.get("occupancy_rate") or 45,
            my_price or (radar["stats"]["median"] or 0),
            [c["price"] for c in radar["competitors"]],
        )
    else:
        detail = None

    review_block = {"available": False}
    if rv:
        review_block = {
            "available": True,
            "avg": rv["overall"]["avg"],
            "bad": rv["overall"]["bad"],
            "badRate": rv["overall"]["badRate"],
            "unreplied": rv["overall"]["unreplied"],
            "snapshotDate": rv["snapshot"]["dateMax"],
            "kept": rv["snapshot"]["kept"],
        }

    return {
        "source": "mixed",
        "hotel": get_status()["hotel"],
        "kpis": kpis,
        "reviews": review_block,
        "radar": {
            "empty": empty,
            "date": radar.get("date"),
            "myPrice": radar.get("myPrice"),
            "stats": radar.get("stats"),
            "position": radar.get("position"),
            "cheaperCount": radar.get("cheaperCount"),
            "distanceKm": radar.get("distanceKm"),
            "topExpensive": sorted(
                radar.get("competitors", []), key=lambda c: c["price"], reverse=True
            )[:3] if not empty else [],
            "topCheap": sorted(
                radar.get("competitors", []), key=lambda c: c["price"]
            )[:3] if not empty else [],
        },
        "suggestion": detail,
        "roomSignal": _room_signal(),
        "pendingSuggestions": pending,
        "config": _config_check(),
    }
