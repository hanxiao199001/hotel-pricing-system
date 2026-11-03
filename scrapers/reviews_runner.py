"""点评采集：子进程调用 ctrip_hotel_reviews.py，校验后原子替换快照。

为什么走子进程而不是 import：
  脚本依赖 playwright，运行在独立的 .venv（Python 3.12）里；主程序是 3.13 且没装
  playwright。子进程还顺带把「浏览器崩溃 / 风控卡住」隔离在外，主程序照常出页面。

写盘策略是「先写临时文件 → 校验 → os.replace 原子替换」：
  采集失败或结果可疑时，旧快照原样保留，页面不会因为一次失败的采集而变空。
"""
import json
import os
import re
import subprocess
import threading
import time
from datetime import date, datetime, timedelta

import config as cfg

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


class ReviewsCollectError(RuntimeError):
    """采集失败，或采回来的数据不可信。"""


def _log_noop(_msg):
    pass


def resolve_before(cfg_dict=None) -> str:
    """--before 的值：只保留早于该日期的点评。

    默认「明天」= 含今天为止的全部点评；显式设置 REVIEWS_BEFORE="" 表示不过滤。
    """
    cfg_dict = cfg_dict or cfg.REVIEWS_CONFIG
    explicit = cfg_dict.get("before")
    if explicit is not None:
        return str(explicit).strip()
    days = int(cfg_dict.get("before_days") or 0)
    return (date.today() + timedelta(days=days)).isoformat()


def build_command(cfg_dict, out_path: str, hotel: str, city: str = "", before: str = "") -> list:
    """拼出采集命令。抽成独立函数便于测试，不用真的起进程。"""
    cmd = [
        cfg_dict["python"],
        cfg_dict["script"],
        "--hotel", hotel,
        "--out", out_path,
        "--profile", cfg_dict["profile"],
        "--channel", cfg_dict["channel"],
        "--cdp-port", str(cfg_dict["cdp_port"]),
        "--login-wait", str(cfg_dict["login_wait"]),
        "--page-size", str(cfg_dict["page_size"]),
        "--max-pages", str(cfg_dict["max_pages"]),
    ]
    if city:
        cmd += ["--city", city]
    if before:
        cmd += ["--before", before]
    return cmd


# ---------------------------------------------------------------------------
# 结果校验
# ---------------------------------------------------------------------------
_HOTEL_SUFFIXES = ("大酒店", "酒店", "饭店", "宾馆", "公寓", "度假村")


def _hotel_core(name: str) -> str:
    name = (name or "").strip()
    for suffix in _HOTEL_SUFFIXES:
        if name.endswith(suffix) and len(name) > len(suffix):
            name = name[: -len(suffix)]
            break
    return name


def validate_payload(raw: dict, expected_hotel: str = "", before: str = "") -> dict:
    """检查脚本产出的 JSON。不可信就抛错——宁可失败，也不拿错数据覆盖旧快照。"""
    if not isinstance(raw, dict):
        raise ReviewsCollectError("采集结果不是 JSON 对象")

    status = (raw.get("status") or "").lower()
    if status == "need_login":
        raise ReviewsCollectError(
            "携程要求登录或验证：" + (raw.get("message") or "登录等待超时")
            + "。请用有头浏览器打开 SCRAPER_PROFILE 目录登录一次后再试。"
        )
    if status == "no_data":
        raise ReviewsCollectError(
            "没有采到点评：" + (raw.get("message") or "列表为空")
        )
    if status != "ok":
        detail = "; ".join(raw.get("errors") or []) or raw.get("message") or "未知错误"
        raise ReviewsCollectError(f"采集脚本返回 {status or '未知状态'}：{detail}")

    reviews = raw.get("reviews")
    if not isinstance(reviews, list) or not reviews:
        raise ReviewsCollectError("采集结果为空（reviews 为空数组）")

    # 酒店是否真的是我们要的那家——错了会静默污染全部口碑指标
    actual = (raw.get("hotel_name") or raw.get("hotel") or "").strip()
    expected_core = _hotel_core(expected_hotel)
    if expected_hotel and actual and expected_core and expected_core not in actual:
        raise ReviewsCollectError(
            f"采到的酒店是「{actual}」，与期望的「{expected_hotel}」不一致，已拒绝写入。"
        )

    clean, seen, bad_rows, zero_rating = [], set(), 0, 0
    for item in reviews:
        if not isinstance(item, dict):
            bad_rows += 1
            continue
        rid = item.get("id")
        if rid is not None:
            if rid in seen:
                continue
            seen.add(rid)
        try:
            rating = float(item.get("rating") or 0)
        except (TypeError, ValueError):
            bad_rows += 1
            continue
        if rating <= 0:
            zero_rating += 1
        if not _DATE_RE.match(str(item.get("created_at") or "")):
            bad_rows += 1
            continue
        clean.append(item)

    if not clean:
        raise ReviewsCollectError("采集结果的每一条都缺字段（rating/created_at），已拒绝写入")
    # 评分全为 0 通常意味着页面被拦、字段没渲染出来
    if zero_rating > len(clean) * 0.2:
        raise ReviewsCollectError(
            f"{zero_rating}/{len(clean)} 条评分的值为 0，疑似页面被拦或结构变更，已拒绝写入"
        )

    dates = sorted(str(r.get("created_at"))[:10] for r in clean)
    if before and dates[-1] >= before:
        raise ReviewsCollectError(
            f"结果里出现 {dates[-1]} 的点评，超出 --before {before} 的范围"
        )

    return {
        "payload": dict(raw, reviews=clean),
        "kept": len(clean),
        "rawCount": len(reviews),
        "dropped": bad_rows,
        "duplicates": len(reviews) - bad_rows - len(clean),
        "dateMin": dates[0],
        "dateMax": dates[-1],
        "hotelName": actual,
    }


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------
def _check_env(cfg_dict) -> None:
    missing = []
    if not os.path.isfile(cfg_dict.get("script") or ""):
        missing.append(f"采集脚本 {cfg_dict.get('script')}")
    if not os.path.isfile(cfg_dict.get("python") or ""):
        missing.append(f"解释器 {cfg_dict.get('python')}")
    if missing:
        raise ReviewsCollectError(
            "找不到" + "、".join(missing)
            + "。请检查 REVIEWS_SCRIPT / SCRAPER_PYTHON 配置。"
        )
    if not os.path.isdir(cfg_dict.get("profile") or ""):
        raise ReviewsCollectError(
            f"浏览器 profile 目录不存在：{cfg_dict.get('profile')}。"
            "采集需要已登录携程的 profile。"
        )


def _run_script(cmd, cfg_dict, env, log):
    """起一次子进程，逐行转发 stdout，stderr 单独收集（traceback 在那里）。"""
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        cwd=os.path.dirname(cfg_dict["script"]) or None,
    )
    timed_out = {"hit": False}

    def _kill():
        timed_out["hit"] = True
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass

    err_lines = []

    def _drain_err():
        try:
            for line in proc.stderr:
                text = line.rstrip()
                if text:
                    err_lines.append(text)
                    if len(err_lines) > 80:
                        del err_lines[0]
        except Exception:  # noqa: BLE001
            pass

    err_thread = threading.Thread(target=_drain_err, daemon=True)
    err_thread.start()

    timer = threading.Timer(int(cfg_dict.get("timeout") or 1800), _kill)
    timer.daemon = True
    timer.start()
    tail = []
    try:
        for line in proc.stdout:  # 逐行转发，长任务也能在界面上看到进度
            text = line.rstrip()
            if text:
                tail.append(text)
                if len(tail) > 40:
                    del tail[0]
                log(text)
        rc = proc.wait()
    finally:
        timer.cancel()
        err_thread.join(timeout=5)
        for stream in (proc.stdout, proc.stderr):
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass
    return {"rc": rc, "errLines": err_lines, "tail": tail, "timedOut": timed_out["hit"]}


def collect(hotel: str = "", city: str = "", log=_log_noop, cfg_dict=None) -> dict:
    """跑一次点评采集。成功返回摘要，失败抛 ReviewsCollectError。"""
    cfg_dict = cfg_dict or cfg.REVIEWS_CONFIG
    _check_env(cfg_dict)

    hotel = hotel or cfg.MY_HOTEL.get("name") or ""
    city = city or cfg.MY_HOTEL.get("city") or ""
    if not hotel:
        raise ReviewsCollectError("未配置酒店名（MY_HOTEL_NAME），无法采集点评。")

    primary = cfg_dict["path"]
    os.makedirs(os.path.dirname(primary), exist_ok=True)
    tmp_out = f"{primary}.collecting.json"
    if os.path.exists(tmp_out):
        os.remove(tmp_out)

    before = resolve_before(cfg_dict)
    cmd = build_command(cfg_dict, tmp_out, hotel, city, before)
    log(f"🏨 采集点评：{hotel}（{city or '未指定城市'}）")
    log(f"   脚本: {cfg_dict['script']}")
    log(f"   截止: {before or '不过滤'} · 最多 {cfg_dict['max_pages']} 页")

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    attempts = max(1, int(cfg_dict.get("attempts") or 2))
    # 注意 0 是合法值（关闭重试判定），不能用 `or 25`，否则 0 会被当成未设置
    _fast = cfg_dict.get("fast_fail_seconds")
    fast_fail = 25 if _fast is None else int(_fast)

    for attempt in range(1, attempts + 1):
        if os.path.exists(tmp_out):
            os.remove(tmp_out)
        started = time.time()
        run = _run_script(cmd, cfg_dict, env, log)
        elapsed = time.time() - started

        if run["timedOut"]:
            raise ReviewsCollectError(
                f"采集超时（{cfg_dict.get('timeout')} 秒）。浏览器可能卡在登录/风控页，"
                "请手动处理后再试。"
            )
        if run["rc"] == 0:
            break

        detail = " / ".join(run["errLines"][-6:]) or (run["tail"][-1] if run["tail"] else "无输出")
        # 秒退通常是上一次的浏览器还没退干净（profile 被占用）——换一次往往就好了
        if attempt < attempts and elapsed < fast_fail:
            log(
                f"⚠️ 脚本 {int(elapsed)} 秒内退出（退出码 {run['rc']}），"
                f"可能是浏览器/ profile 未释放，3 秒后重试（{attempt}/{attempts}）"
            )
            log(f"   细节: {detail}")
            time.sleep(3)
            continue
        raise ReviewsCollectError(f"采集脚本退出码 {run['rc']}：{detail}")

    try:
        with open(tmp_out, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise ReviewsCollectError(f"采集脚本没有产出可解析的结果文件：{exc}") from exc

    checked = validate_payload(raw, expected_hotel=hotel, before=before)
    checked["payload"]["_collected_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    checked["payload"]["_source_script"] = cfg_dict["script"]

    # 校验通过才替换，原子操作，中途失败不会留下半个文件
    os.replace(tmp_out, primary)

    log(
        f"✅ 点评采集完成：保留 {checked['kept']} 条"
        f"（原始 {checked['rawCount']} 条，去重 {checked['duplicates']} 条，"
        f"丢弃 {checked['dropped']} 条），"
        f"日期 {checked['dateMin']} ~ {checked['dateMax']}"
    )
    return {
        "status": "ok",
        "path": primary,
        "kept": checked["kept"],
        "rawCount": checked["rawCount"],
        "duplicates": checked["duplicates"],
        "dropped": checked["dropped"],
        "dateMin": checked["dateMin"],
        "dateMax": checked["dateMax"],
        "hotel": checked["hotelName"] or hotel,
        "before": before,
    }


def snapshot_info(cfg_dict=None) -> dict:
    """给状态页用：脚本/解释器/profile/快照是否就绪。"""
    cfg_dict = cfg_dict or cfg.REVIEWS_CONFIG
    import reviews_analytics

    path = reviews_analytics.snapshot_path()
    info = {
        "script": cfg_dict.get("script"),
        "scriptExists": os.path.isfile(cfg_dict.get("script") or ""),
        "python": cfg_dict.get("python"),
        "pythonExists": os.path.isfile(cfg_dict.get("python") or ""),
        "profile": cfg_dict.get("profile"),
        "profileExists": os.path.isdir(cfg_dict.get("profile") or ""),
        "channel": cfg_dict.get("channel"),
        "timeout": cfg_dict.get("timeout"),
        "snapshot": path,
        "snapshotExists": os.path.isfile(path),
        "snapshotMtime": None,
        "snapshotCount": None,
        "before": resolve_before(cfg_dict),
    }
    if info["snapshotExists"]:
        st = os.stat(path)
        info["snapshotMtime"] = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
        data = reviews_analytics.summary_or_none()
        if data:
            info["snapshotCount"] = data["snapshot"]["kept"]
            info["snapshotDateMax"] = data["snapshot"]["dateMax"]
    return info
