#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
携程 eBooking 商家后台 · 点评采集脚本（独立版，只读）
========================================================
功能：
  1. 有头浏览器（普通模式启动 + CDP 接管），复用已登录的浏览器 profile；
  2. 打开「点评问答 → 订单点评」页，*只监听*页面自然发出的 getCommentList
     接口响应，通过点击分页器触发下一页，逐页捕获评论列表；
  3. 绝不主动 POST 重放 —— getCommentList 的 reqHead.ubt.rmsToken 是动态
     反爬埋点指纹，主动改 pageIndex 重放会触发风控，违背「只采集、不操作」；
  4. 输出与公开点评采集同构的 reviews 结构（后台没有的字段留空，不编造）。

边界（写死在本脚本，不可由参数放开）：
  - 只读取（监听 response / 点击分页翻页），不提交任何表单、不写后台；
  - 命中登录墙/风控 -> 等待用户手动登录/验证（有头窗口可见）；
  - 失败不静默编造，status=error / need_login 如实上报。

用法：
  .venv\\Scripts\\python.exe ebook_reviews_collector.py --out data/reviews/ctrip_reviews.json

与公开点评采集（ctrip_hotel_reviews.py）的字段差异（后台没有，留空）：
  travel_type / ip_location / useful_count 留空；checkin_date 仅年月；
  评分维度为 环境/设施/卫生/服务（映射到 rating_detail 的 location/facility/room/service）。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

# Windows 控制台输出 UTF-8（避免 GBK 乱码）
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
]

LOGIN_URL_MARKERS = ["passport.ctrip.com", "login.ctrip.com", "/login"]
RISK_URL_MARKERS = ["captcha", "verify", "slider", "checkcode", "nocaptcha"]
RISK_TEXT_MARKERS = ["拖动滑块", "滑动验证", "安全验证", "图形验证", "验证码拦截", "滑块", "亲，请拖动"]

COMMENT_PAGE = "https://ebooking.ctrip.com/comment/commentList"
COMMENT_API_MARKER = "getCommentList"

_MS_DATE = re.compile(r"/Date\((\d+)([+-]\d{4})\)/")


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def human_pause(lo: float = 0.4, hi: float = 1.0) -> None:
    time.sleep(random.uniform(lo, hi))


def _kill_stale_browser_on_port(cdp_port: int):
    try:
        out = subprocess.run(
            "netstat -ano | findstr :%d" % cdp_port,
            shell=True, capture_output=True, text=True,
        ).stdout
        pids = set()
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[3] == "0.0.0.0:0":
                pids.add(parts[4])
        for pid in pids:
            subprocess.run(f"taskkill /PID {pid} /T /F",
                           shell=True, capture_output=True)
    except Exception:  # noqa: BLE001
        pass


def launch_browser(pw, profile_dir: Path, channel: str, cdp_port: int = 9222):
    """返回 (context, chrome_proc)。chrome_proc=None 表示复用已运行实例。"""
    profile_dir.mkdir(parents=True, exist_ok=True)

    def find_exec() -> str | None:
        if channel == "chrome":
            for c in CHROME_CANDIDATES[:2]:
                if Path(c).exists():
                    return c
        elif channel == "edge":
            for c in CHROME_CANDIDATES[2:]:
                if Path(c).exists():
                    return c
        else:
            p = Path(channel)
            if p.exists():
                return str(p)
        return None

    try:
        browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        log(f"复用已运行浏览器实例(CDP :{cdp_port})")
        return ctx, None
    except Exception:  # noqa: BLE001
        pass

    _kill_stale_browser_on_port(cdp_port)
    exec_path = find_exec()
    if not exec_path:
        raise RuntimeError("未找到本机 Chrome/Edge，请用 --channel 指定浏览器 exe 路径")

    log(f"以普通模式启动本机浏览器(带CDP调试端口): {exec_path}")
    proc = subprocess.Popen(
        [
            exec_path,
            f"--remote-debugging-port={cdp_port}",
            f"--user-data-dir={profile_dir}",
            "--start-maximized",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled",
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    last_err = None
    for _ in range(30):
        try:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            log(f"CDP 接管成功(:{cdp_port})，浏览器环境为普通用户模式")
            return ctx, proc
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(1)
    raise RuntimeError(f"CDP 接管失败: {last_err}")


def wait_until_ready(page, login_wait: int, label: str) -> bool:
    start = time.time()
    while time.time() - start < login_wait:
        url = page.url.lower()
        body = ""
        for _ in range(3):
            try:
                body = page.inner_text("body", timeout=3000)[:3000]
                if body.strip():
                    break
            except Exception:  # noqa: BLE001
                pass
            page.wait_for_timeout(1500)
        is_login = any(m in url for m in LOGIN_URL_MARKERS)
        is_risk = any(m in url for m in RISK_URL_MARKERS) or any(
            m in body for m in RISK_TEXT_MARKERS)
        if is_login or is_risk:
            log(f"[{label}] 检测到登录墙/风控，请手动处理，脚本持续等待…")
            page.wait_for_timeout(4000)
            continue
        return True
    return False


# ---------------------------------------------------------------------------
# 评论捕获（只监听自然响应，不重放）
# ---------------------------------------------------------------------------
class CommentCapture:
    def __init__(self):
        self.by_page: dict = {}
        self.read_errors: list = []

    def on_response(self, resp):
        if COMMENT_API_MARKER not in resp.url:
            return
        try:
            data = resp.json()
        except Exception as e:  # noqa: BLE001
            self.read_errors.append(f"getCommentList 解析失败: {type(e).__name__}: {str(e)[:80]}")
            return
        idx = data.get("currentPageIndex")
        cl = data.get("commentlist")
        if idx is not None and isinstance(cl, list):
            self.by_page[idx] = cl
            log(f"[捕获] 第 {idx} 页（{len(cl)} 条）")


def _ms_to_str(ms) -> str:
    try:
        return datetime.fromtimestamp(int(ms) / 1000.0).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:  # noqa: BLE001
        return ""


def extract_review(c: dict) -> dict:
    """单条点评 -> 与公开点评 reviews.py 同构的字段（后台没有的留空）。"""
    score = c.get("score") or {}
    avg = c.get("avgScore") or score.get("avgScore") or 0
    image_urls = [(p.get("path") or "") for p in (c.get("pictureList") or [])]
    image_urls = [u for u in image_urls if u]
    reply = (c.get("replyDetail") or {}).get("replyContent") or ""
    addtime = c.get("addtime") or ""
    m = _MS_DATE.match(addtime) if isinstance(addtime, str) else None
    created_at = _ms_to_str(m.group(1)) if m else ""

    return {
        "id": c.get("commentId"),
        "user": c.get("userName") or "",
        "rating": avg,
        "rating_detail": {
            "location": c.get("locationScore"),
            "facility": c.get("facilityScore"),
            "service": c.get("serviceScore"),
            "room": c.get("healthScore"),   # 后台「卫生」分
        },
        "content": (c.get("content") or "").strip(),
        "created_at": created_at,
        "checkin_date": c.get("checkinTimeStr") or "",   # 后台仅年月，原样保留
        "room": c.get("hotelRoomInfo") or "",
        "travel_type": "",            # 后台没有
        "ip_location": "",            # 后台没有
        "useful_count": 0,            # 后台没有
        "images": len(image_urls),
        "image_urls": image_urls,
        "recommend": bool(avg and float(avg) >= 4),
        "reply": reply,
        "source": c.get("channelSource") or c.get("sourceName") or "",
    }


def build_result_payload(capture, meta, hotel_name, errors, hotel, city,
                         before, incremental=False, stopped_at=None) -> dict:
    """把捕获数据转成结果 dict。

    incremental=True 时，reviews 里包含「新增」以及边界确认页里的旧点评
    （上层合并时会按 commentId 去重，不会重复计入）。这种模式下即使一条
    都没采到也视为成功——说明后台没有新点评，而不是采集失败。
    """
    reviews, seen = [], set()
    for idx in sorted(capture.by_page.keys()):
        for c in capture.by_page[idx]:
            rid = c.get("commentId")
            if rid in seen:
                continue
            seen.add(rid)
            reviews.append(extract_review(c))

    status = "ok"
    if not reviews and not incremental:
        status = "error"
        if not errors:
            errors = ["没有采到任何点评（getCommentList 未返回数据）"]
    elif any("登录/验证等待超时" in e for e in errors):
        status = "need_login"

    return {
        "status": status,
        "message": "; ".join(errors) if status != "ok" else "",
        "platform": "ctrip_ebook",
        "hotel": hotel,
        "city": city,
        "hotel_id": str(meta.get("masterHotelId") or ""),
        "hotel_name": hotel_name,
        "before": before,
        "total_count": meta.get("commentCount") or len(reviews),
        "page_count": meta.get("pageCount") or 0,
        "page_size": 10,
        "collected": len(reviews),
        "kept": len(reviews),
        "skipped_by_date": 0,
        "mode": "incremental" if incremental else "full",
        "stopped_at_existing": stopped_at,
        "reviews": reviews,
        "errors": [e for e in errors if e],
    }


def _wait_for_page(page, capture: CommentCapture, target: int, timeout_s: float) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if target in capture.by_page:
            return True
        page.wait_for_timeout(1000)
    return target in capture.by_page


def _click_next(page) -> bool:
    try:
        nxt = page.locator("li[title='下一页']").first
        if nxt.count() > 0:
            nxt.scroll_into_view_if_needed()
            nxt.click()
            log("[翻页] 点击「下一页」")
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def collect(context, login_wait: int, max_pages: int, existing_ids=None,
            full: bool = False, confirm_pages: int = 2):
    """打开点评页，捕获第1页，再点击分页自然翻页。

    返回 (capture, meta, hotel_name, errors) 的 dict。

    增量模式（existing_ids 非空且非 full）：点评列表按时间倒序，从第 1 页
    往下翻，一旦某页出现旧快照里已有的 commentId，即认为越过「新评论边界」，
    再冗余翻 confirm_pages 页确认后停止。这样日常只需翻最前面几页。
    找不到旧边界时（例如上次采集很久以前、ID 体系变化）自动翻到上限，
    行为与全量一致，由上层做全量替换。
    """
    capture = CommentCapture()
    meta: dict = {}
    errors: list = []
    hotel_name = ""
    existing_ids = {str(x) for x in (existing_ids or []) if x is not None}
    incremental = bool(existing_ids) and not full

    def _page_ids(idx):
        return {
            str(c.get("commentId"))
            for c in (capture.by_page.get(idx) or [])
            if c.get("commentId") is not None
        }

    page = context.new_page()
    page.on("response", capture.on_response)

    def on_page_resp(resp):
        if COMMENT_API_MARKER in resp.url:
            try:
                d = resp.json()
                meta.setdefault("pageCount", d.get("pageCount") or 0)
                meta.setdefault("commentCount", d.get("commentCount") or 0)
                meta.setdefault("masterHotelId", d.get("masterHotelId") or "")
            except Exception:  # noqa: BLE001
                pass

    page.on("response", on_page_resp)

    reached_at = None
    try:
        log(f"[加载] {COMMENT_PAGE}")
        page.goto(COMMENT_PAGE, wait_until="domcontentloaded", timeout=45000)
        if not wait_until_ready(page, login_wait, "点评页"):
            return {"capture": capture, "meta": meta, "hotel_name": hotel_name,
                    "errors": ["登录/验证等待超时"],
                    "incremental": incremental, "stopped_at": None}

        time.sleep(2)
        try:
            el = page.get_by_text("订单点评", exact=True).first
            if el.count() > 0:
                el.click()
                log("已切到「订单点评」")
                time.sleep(2)
        except Exception:  # noqa: BLE001
            pass

        try:
            body = page.inner_text("body", timeout=5000)[:800]
            m = re.search(r"([\u4e00-\u9fff（）()A-Za-z0-9·]{2,40}?(?:酒店|饭店|宾馆|度假村))", body)
            if m:
                hotel_name = m.group(1)
        except Exception:  # noqa: BLE001
            pass

        _wait_for_page(page, capture, 1, 8)
        total_pages = int(meta.get("pageCount") or 0)
        log(f"[点评] 总数={meta.get('commentCount')} 页数={total_pages}")

        limit = min(total_pages, max_pages) if total_pages else max_pages

        # 第 1 页就可能已经全是旧点评（当天没有新增），先判定一次边界
        if incremental and _page_ids(1) & existing_ids:
            reached_at = 1
            log("[增量] 第 1 页已出现旧点评，无新增")

        cur = 1
        while cur < limit:
            if incremental and reached_at is not None and cur >= reached_at + confirm_pages:
                log(f"[增量] 越过新评论边界（第 {reached_at} 页）并确认 {confirm_pages} 页，停止翻页")
                break
            cur_next = cur + 1
            if not _click_next(page):
                log(f"第 {cur} 页后找不到下一页，停止翻页")
                break
            if not _wait_for_page(page, capture, cur_next, 8):
                log(f"第 {cur_next} 页未捕获，停止")
                break
            cur = cur_next
            if incremental and reached_at is None and _page_ids(cur) & existing_ids:
                reached_at = cur
                log(f"[增量] 第 {cur} 页出现旧点评，继续确认 {confirm_pages} 页")
            if cur % 10 == 0 or cur == limit:
                log(f"[点评] 已采至第 {cur}/{limit} 页")
    except Exception as e:  # noqa: BLE001
        log(f"[异常] {e}")
        return {"capture": capture, "meta": meta, "hotel_name": hotel_name,
                "errors": [f"采集异常: {e}"],
                "incremental": incremental, "stopped_at": reached_at}
    finally:
        try:
            page.close()
        except Exception:  # noqa: BLE001
            pass
    return {"capture": capture, "meta": meta, "hotel_name": hotel_name, "errors": [],
            "incremental": incremental, "stopped_at": reached_at}


def main():
    ap = argparse.ArgumentParser(description="携程 eBooking 点评采集（只读）")
    ap.add_argument("--out", default="data/reviews/ctrip_reviews.json", help="结果文件路径")
    ap.add_argument("--hotel", default="", help="酒店名（兼容参数，校验用）")
    ap.add_argument("--city", default="", help="城市（兼容参数）")
    ap.add_argument("--before", default="", help="截止日期（兼容参数，由上层校验过滤）")
    ap.add_argument("--page-size", type=int, default=10, help="后台固定10，此参数忽略")
    ap.add_argument("--max-pages", type=int, default=300, help="最多翻页数安全上限")
    ap.add_argument("--existing", default="",
                    help="旧快照路径：增量采集时用其中的 commentId 作为停止边界")
    ap.add_argument("--full", action="store_true",
                    help="全量采集（忽略 --existing，翻到上限/页码上限）")
    ap.add_argument("--confirm-pages", type=int, default=2,
                    help="增量到达旧点评边界后再确认的页数")
    ap.add_argument("--profile", default="profiles/default",
                    help="浏览器持久化登录目录（需已登录 ebooking）")
    ap.add_argument("--channel", default="chrome", help="chrome/edge/或浏览器exe路径")
    ap.add_argument("--cdp-port", type=int, default=9222, help="浏览器CDP调试端口")
    ap.add_argument("--keep-browser", action="store_true",
                    help="脚本结束后不关闭浏览器（默认自动关闭）")
    ap.add_argument("--login-wait", type=int, default=600, help="登录/验证等待秒数")
    args = ap.parse_args()

    result = {
        "status": "error", "message": "", "platform": "ctrip_ebook",
        "hotel": args.hotel, "city": args.city,
        "hotel_id": "", "hotel_name": "", "before": args.before,
        "total_count": 0, "page_count": 0, "page_size": 10,
        "collected": 0, "kept": 0, "skipped_by_date": 0,
        "mode": "full" if args.full else "incremental",
        "reviews": [], "errors": [],
    }

    # 增量模式：加载旧快照的 commentId 集合作为翻页停止边界。
    # 读不到旧快照（首次采集/路径不对）就退回全量，不带着错误边界去翻页。
    existing_ids = set()
    if args.existing and not args.full:
        try:
            with open(args.existing, "r", encoding="utf-8") as f:
                old = json.load(f)
            for item in old.get("reviews") or []:
                cid = item.get("id")
                if cid is not None:
                    existing_ids.add(str(cid))
            log(f"[增量] 旧快照 {len(existing_ids)} 条评论作为停止边界")
        except Exception as e:  # noqa: BLE001
            existing_ids = set()
            log(f"[增量] 读取旧快照失败，改全量采集：{type(e).__name__}: {e}")
    if args.full:
        log("[全量] 忽略旧快照，做一次全量校准")
    elif existing_ids:
        log("[增量] 只采到出现旧点评为止（+确认页）")
    else:
        log("[全量] 无可用旧快照，按全量采集")

    profile_dir = Path(args.profile).resolve()
    out = Path(args.out).resolve()

    try:
        with sync_playwright() as pw:
            context, chrome_proc = launch_browser(pw, profile_dir, args.channel, args.cdp_port)
            try:
                r = collect(context, args.login_wait, args.max_pages,
                            existing_ids=existing_ids, full=args.full,
                            confirm_pages=args.confirm_pages)
                result.update(build_result_payload(
                    r["capture"], r["meta"], args.hotel or r["hotel_name"], r["errors"],
                    args.hotel, args.city, args.before,
                    incremental=r.get("incremental", False),
                    stopped_at=r.get("stopped_at")))
            except Exception as e:  # noqa: BLE001
                result["status"] = "error"
                result["message"] = str(e)
                log(f"[异常] {e}")
            finally:
                try:
                    context.close()
                except Exception:  # noqa: BLE001
                    pass
                if chrome_proc is not None and not args.keep_browser:
                    try:
                        subprocess.run(f"taskkill /PID {chrome_proc.pid} /T /F",
                                       shell=True, capture_output=True)
                    except Exception:  # noqa: BLE001
                        pass
    except Exception as e:  # noqa: BLE001
        result["status"] = "error"
        result["message"] = str(e)
        log(f"[异常] {e}")

    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    log(f"[结果] 已写入: {out}（{len(result.get('reviews', []))} 条点评）")


if __name__ == "__main__":
    main()