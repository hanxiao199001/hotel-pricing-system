"""eBook 经营数据采集：子进程调用 ebook_collector.py，校验后原子替换快照。

复用点评采集的「独立脚本 + 子进程 + 校验 + 原子替换」模式：
  - 采集脚本依赖 playwright，运行在独立的 .venv（Python 3.12）里；
  - 浏览器崩溃 / 风控卡住被隔离在子进程，主程序照常出页面；
  - 先写临时文件 → 校验 → os.replace 原子替换，失败不覆盖旧快照。
"""
import json
import os
import subprocess
import threading
import time
from datetime import datetime

import config as cfg


class EbookCollectError(RuntimeError):
    """采集失败，或采回来的数据不可信。"""


def _log_noop(_msg):
    pass


def build_command(cfg_dict, out_path: str) -> list:
    """拼出采集命令。抽成独立函数便于测试，不用真的起进程。"""
    return [
        cfg_dict["python"],
        cfg_dict["script"],
        "--out", out_path,
        "--profile", cfg_dict["profile"],
        "--channel", cfg_dict["channel"],
        "--cdp-port", str(cfg_dict["cdp_port"]),
        "--login-wait", str(cfg_dict["login_wait"]),
        "--page-wait", str(cfg_dict.get("page_wait", 8)),
    ]


# ---------------------------------------------------------------------------
# 结果校验
# ---------------------------------------------------------------------------
def _dig(data, *keys):
    """安全取嵌套字段。"""
    cur = data
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def _core_payload(sections: dict) -> dict | None:
    """从原始 sections 里挑出经营指标。任一缺则返回 None（说明没采到）。"""
    out = {}
    market = sections.get("market") or {}
    capacity = sections.get("capacity") or {}
    inv = sections.get("inventory") or {}

    # market 有 data 包裹；capacity 实测是顶层字段——两种都兼容
    m_data = market.get("data") if isinstance(market.get("data"), dict) else market
    c_data = capacity.get("data") if isinstance(capacity.get("data"), dict) else capacity

    out["adr"] = m_data.get("averagePrice")
    out["rental_rate"] = m_data.get("rentalRate")
    out["occupancy_rate"] = c_data.get("occupancyRate")
    out["occupied_rooms"] = c_data.get("occupiedRooms")
    out["order_quantity"] = c_data.get("orderQuantity")
    out["book_amount"] = m_data.get("bookAmount")
    out["book_quantity"] = m_data.get("bookQuantity")
    out["stay_in_quantity"] = m_data.get("stayInQuantity")
    out["close_rate"] = m_data.get("closeRate")
    out["competitor_number"] = m_data.get("competitorNumber")

    if out.get("adr") is None and out.get("occupancy_rate") is None:
        return None
    return out


def _extract_hotel_id(sections: dict) -> str:
    """从库存接口提取酒店 ID；缺失返回空。"""
    inv = sections.get("inventory") or {}
    data = inv.get("data") or {}
    for r in (data.get("roomStatusResult") or []):
        if r.get("hotelID"):
            return str(r["hotelID"])
    return ""


def validate_payload(raw: dict, expected_hotel_id: str = "") -> dict:
    """检查脚本产出的 JSON。不可信就抛错——宁可失败，也不拿错数据覆盖旧快照。"""
    if not isinstance(raw, dict):
        raise EbookCollectError("采集结果不是 JSON 对象")

    status = (raw.get("status") or "").lower()
    if status != "ok":
        raise EbookCollectError(
            "eBook 采集未成功：" + (raw.get("message") or raw.get("status") or "未知错误")
            + "。请确认浏览器 profile 已登录 ebooking，再试。"
        )

    sections = raw.get("sections")
    if not isinstance(sections, dict) or not sections:
        raise EbookCollectError("采集结果为空（sections 为空）")

    core = _core_payload(sections)
    if core is None:
        raise EbookCollectError(
            "没有采到经营概览字段（ADR/入住率等），疑似页面被拦或接口结构变化，已拒绝写入。"
        )

    # 酒店是否是我们配的那家——错了会污染经营指标
    actual = _extract_hotel_id(sections) or ""
    if expected_hotel_id and actual and actual != str(expected_hotel_id):
        raise EbookCollectError(
            f"采到的酒店 ID 是「{actual}」，与配置的「{expected_hotel_id}」不一致，已拒绝写入。"
        )

    return {
        "payload": dict(raw),
        "sections": len(sections),
        "hotelId": actual or raw.get("hotel_id") or "",
        "core": core,
    }


def _extract_hotel_id(sections: dict) -> str:
    """从库存接口提取酒店 ID；缺失返回空。"""
    inv = sections.get("inventory") or {}
    data = inv.get("data") or {}
    for r in (data.get("roomStatusResult") or []):
        if r.get("hotelID"):
            return str(r["hotelID"])
    return ""


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
        raise EbookCollectError(
            "找不到" + "、".join(missing)
            + "。请检查 EBOOK_SCRIPT / SCRAPER_PYTHON 配置。"
        )
    if not os.path.isdir(cfg_dict.get("profile") or ""):
        raise EbookCollectError(
            f"浏览器 profile 目录不存在：{cfg_dict.get('profile')}。"
            "采集需要已登录 ebooking 的 profile。"
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
        for line in proc.stdout:
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


def collect(log=_log_noop, cfg_dict=None) -> dict:
    """跑一次 eBook 经营数据采集。成功返回摘要，失败抛 EbookCollectError。"""
    cfg_dict = cfg_dict or cfg.EBOOK_CONFIG
    _check_env(cfg_dict)

    primary = cfg_dict["path"]
    os.makedirs(os.path.dirname(primary), exist_ok=True)
    tmp_out = f"{primary}.collecting.json"
    if os.path.exists(tmp_out):
        os.remove(tmp_out)

    cmd = build_command(cfg_dict, tmp_out)
    log("📊 采集 eBook 经营数据（只读）")
    log(f"   脚本: {cfg_dict['script']} · profile: {cfg_dict['profile']}")

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    attempts = max(1, int(cfg_dict.get("attempts") or 2))
    _fast = cfg_dict.get("fast_fail_seconds")
    fast_fail = 25 if _fast is None else int(_fast)

    for attempt in range(1, attempts + 1):
        if os.path.exists(tmp_out):
            os.remove(tmp_out)
        started = time.time()
        run = _run_script(cmd, cfg_dict, env, log)
        elapsed = time.time() - started

        if run["timedOut"]:
            raise EbookCollectError(
                f"采集超时（{cfg_dict.get('timeout')} 秒）。浏览器可能卡在登录/风控页，"
                "请手动处理后再试。"
            )
        if run["rc"] == 0:
            break

        detail = " / ".join(run["errLines"][-6:]) or (run["tail"][-1] if run["tail"] else "无输出")
        if attempt < attempts and elapsed < fast_fail:
            log(
                f"⚠️ 脚本 {int(elapsed)} 秒内退出（退出码 {run['rc']}），"
                f"可能是浏览器/ profile 未释放，3 秒后重试（{attempt}/{attempts}）"
            )
            log(f"   细节: {detail}")
            time.sleep(3)
            continue
        raise EbookCollectError(f"采集脚本退出码 {run['rc']}：{detail}")

    try:
        with open(tmp_out, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise EbookCollectError(f"采集脚本没有产出可解析的结果文件：{exc}") from exc

    expected_hotel_id = str(cfg.CTRIP_CONFIG.get("hotel_id") or "").strip()
    checked = validate_payload(raw, expected_hotel_id=expected_hotel_id)
    checked["payload"]["_collected_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    checked["payload"]["_source_script"] = cfg_dict["script"]

    os.replace(tmp_out, primary)

    core = checked["core"]
    log(
        f"✅ eBook 采集完成：ADR={core.get('adr')} 出租率={core.get('rental_rate')} "
        f"入住率={core.get('occupancy_rate')} 已售房={core.get('occupied_rooms')} "
        f"订单={core.get('order_quantity')}（{checked['sections']} 个 section）"
    )
    return {
        "status": "ok",
        "path": primary,
        "hotelId": checked["hotelId"],
        "sections": checked["sections"],
        "adr": core.get("adr"),
        "rentalRate": core.get("rental_rate"),
        "occupancyRate": core.get("occupancy_rate"),
        "occupiedRooms": core.get("occupied_rooms"),
        "orderQuantity": core.get("order_quantity"),
    }


def snapshot_info(cfg_dict=None) -> dict:
    """给状态页用：脚本/解释器/profile/快照是否就绪。"""
    cfg_dict = cfg_dict or cfg.EBOOK_CONFIG
    path = cfg_dict["path"]
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
    }
    if info["snapshotExists"]:
        st = os.stat(path)
        info["snapshotMtime"] = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
    return info