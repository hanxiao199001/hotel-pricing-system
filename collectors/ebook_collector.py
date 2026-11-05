#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
携程 eBooking 商家后台 · 经营数据采集脚本（独立版，只读）
========================================================
功能：
  1. 有头浏览器（普通模式启动 + CDP 接管），复用已登录的浏览器 profile；
  2. 依序打开「经营报告 / 首页 / 房价房态日历 / 用户行为」四个页面，*只监听*页面
     自然发出的只读接口响应（get/fetch/query 查询类），逐个捕获响应体；
  3. 绝不 post 重放任何接口 —— 经营报告接口带动态 spiderkey 反爬指纹，
     主动重放会被风控，而且违背「只采集、不操作」的边界；
  4. 输出 JSON：按 section 分类存原始响应（market/capacity/realtime/
     flow/service/inventory/orders 等），供上层解析成经营指标。

边界（写死在本脚本，不可由参数放开）：
  - 只读取（监听 response），不提交任何表单、不修改任何后台数据；
  - 命中登录墙/风控 -> 等待用户手动登录/验证（有头窗口可见）；
  - 失败不静默编造，status=error / need_login 如实上报。

用法：
  .venv\\Scripts\\python.exe ebook_collector.py --out data/ebook/ctrip_ebook.json

结果字段：
  status: ok / need_login / error
  platform / collected_at / hotel_id(取自房态接口) /
  sections: {market,capacity,realtime,flow,service,visitor,
             inventory,rooms,home,orders,
             guest_source,guest_features,guest_order_type,guest_order_dist,
             guest_sex,guest_age,guest_price_info,guest_price,
             guest_booking_days,guest_stay_days,guest_travel_time,guest_star,
             guest_type,guest_point,guest_orders}
           （每个是接口原始 JSON，缺失则缺项）
  errors: []
"""
from __future__ import annotations

import argparse
import json
import random
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

# 要捕获的只读接口：section 名 -> URL 关键字（命中即捕获该响应体）。
# 全部是「查询/概览」类接口，不含任何 set/update/delete 等写接口。
SECTIONS = [
    # 经营报告页（/datacenter/inland/businessreport/outline）
    ("market", "fetchMarketOverView"),          # 平均卖价/出租率/成交率/销售/间夜
    ("capacity", "fetchCapacityOverView"),      # 入住率/已售房/分渠道订单
    ("realtime", "getDayReportRealTime"),       # 实时订单/访客/热度
    ("flow", "getDayReportFlowCompete"),        # 流量对比
    ("service", "getDayReportServer"),          # 服务量（点评分/回复率/收藏）
    ("advice", "getHotelAdvice"),               # 经营提醒
    ("visitor", "fetchVisitorTitle"),           # 访客
    # 首页（home/mainland）
    ("home", "queryHomePageRealTimeData"),      # 首页实时数据
    ("orders", "queryOrderList"),               # 订单列表
    # 房价房态日历（ebkovsroom/inventory/calendar）
    ("inventory", "getRoomInventoryInfo"),      # 各房型库存+房价+佣金
    ("rooms", "getRcProductList"),              # 房型/产品列表
    # 数据中心 → 用户行为 → 用户分析（客源结构，统计口径为过去 180 天）
    # 注意：queryUserPriceInfo 必须排在 queryUserPrice 之前，否则前者会被后者前缀命中。
    ("guest_source", "userbehavior/queryUserSource"),          # 客源地：本地/异地、省份、城市TOP
    ("guest_features", "userbehavior/queryUserFeatures"),      # 用户特征画像（年龄/性别/类型等）
    ("guest_order_type", "userbehavior/queryOrderType"),       # 预订方式占比（电脑/移动/其它）
    ("guest_order_dist", "userbehavior/getOrderDistribution"),  # 24 小时预订分布
    ("guest_sex", "userbehavior/queryUserSex"),                # 性别
    ("guest_age", "userbehavior/queryUserAge"),                # 年龄
    ("guest_price_info", "userbehavior/queryUserPriceInfo"),   # 消费能力
    ("guest_price", "userbehavior/queryUserPrice"),            # 价格敏感
    ("guest_booking_days", "userbehavior/queryUserBookingDays"),  # 提前预订天数
    ("guest_stay_days", "userbehavior/queryUserStayDays"),      # 连住天数
    ("guest_travel_time", "userbehavior/queryUserTravelTime"),  # 出行时间
    ("guest_star", "userbehavior/queryUserStar"),              # 星级偏好
    ("guest_type", "userbehavior/queryUserType"),              # 出行类型（休闲/商务）
    ("guest_point", "userbehavior/queryUserPoint"),            # 预订时段
    ("guest_orders", "userbehavior/queryUserOrders"),          # 订购酒店次数
]

# 依次访问的页面（section 在这些页面触发）
PAGE_URLS = [
    "https://ebooking.ctrip.com/datacenter/inland/businessreport/outline",
    "https://ebooking.ctrip.com/home/mainland",
    "https://ebooking.ctrip.com/ebkovsroom/inventory/calendar",
    "https://ebooking.ctrip.com/datacenter/inland/userbehavior/user",
]

# 每个页面打开后等待秒数（等页面自然发完接口）
_PAGE_WAIT = (5, 9)


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def human_pause(lo: float = 0.5, hi: float = 1.5) -> None:
    """模拟真人操作节奏的随机停顿（秒），避免固定间隔暴露自动化特征。"""
    time.sleep(random.uniform(lo, hi))


def _kill_stale_browser_on_port(cdp_port: int):
    """清理占用调试端口的残留浏览器进程（上次异常退出的残留实例）。"""
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
    """轮询等待页面离开登录墙/风控页。返回 True=已就绪。"""
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
            if is_login:
                log(f"[{label}] 检测到登录墙，请在浏览器中手动登录（扫码/账号密码），脚本持续等待…")
            else:
                log(f"[{label}] 检测到验证码/风控（滑块验证等），请在浏览器中手动完成验证，脚本持续等待…")
            page.wait_for_timeout(4000)
            continue
        return True
    return False


# ---------------------------------------------------------------------------
# 只读响应捕获
# ---------------------------------------------------------------------------
class ResponseCapture:
    """监听页面自然发出的只读接口响应，按 section 保存原始 JSON。"""

    def __init__(self):
        self.by_section: dict = {}
        self.by_url: dict = {}
        self.read_errors: list = []

    def on_response(self, resp):
        try:
            url = resp.url
        except Exception:  # noqa: BLE001
            return
        for name, marker in SECTIONS:
            if marker in url:
                try:
                    data = resp.json()
                except Exception as e:  # noqa: BLE001 - 诊断：记下状态码与 body 开头
                    detail = f"{name} 解析失败: {type(e).__name__}: {str(e)[:80]}"
                    try:
                        raw = resp.body()
                        detail += f" | HTTP {resp.status} body_start={raw[:160]!r}"
                    except Exception as e2:  # noqa: BLE001
                        detail += f" | body不可读: {type(e2).__name__}"
                    self.read_errors.append(detail)
                    log(f"[诊断] {detail}")
                    return
                self.by_section[name] = data
                self.by_url[name] = url
                log(f"[捕获] {name}")
                return


def _wait_for_keys(page, capture: ResponseCapture, keys: list, timeout_s: float):
    """轮询等待**所有**必需接口都被捕获；期间滚动触发懒加载。"""
    deadline = time.time() + max(2.0, timeout_s)
    while time.time() < deadline:
        if all(k in capture.by_section for k in keys):
            return
        try:
            page.evaluate("window.scrollBy(0, 600)")
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(1200)


def collect(context, login_wait: int, page_wait: float) -> dict:
    """打开三个页面，捕获只读接口响应。返回 (sections, hotel_id, errors)。"""
    capture = ResponseCapture()
    page = context.new_page()
    page.on("response", capture.on_response)
    errors: list = []
    hotel_id = ""

    # 每个页面「必需」捕获的接口：全部到齐才进下一页（防止 early-exit 漏掉核心数据）
    want = {
        PAGE_URLS[0]: ["market", "capacity"],   # 经营报告核心
        PAGE_URLS[1]: ["home"],                 # 首页实时
        PAGE_URLS[2]: ["inventory"],           # 房价房态（含各房型价格/库存）
        PAGE_URLS[3]: ["guest_source", "guest_features", "guest_order_type"],  # 客源结构核心
    }
    for url in PAGE_URLS:
        try:
            log(f"[加载] {url}")
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            if not wait_until_ready(page, login_wait, "eBook"):
                errors.append(f"登录/风控等待超时: {url}")
                break
            _wait_for_keys(page, capture, want.get(url, []), float(page_wait))
        except Exception as e:  # noqa: BLE001
            errors.append(f"页面 {url} 加载异常: {e}")
            log(f"[异常] {url}: {e}")
            try:
                page.wait_for_timeout(2000)
            except Exception:  # noqa: BLE001
                pass

    if capture.read_errors:
        errors.append("; ".join(capture.read_errors[:3]))

    # 从库存接口提取酒店 ID（各房型 hotelID，取第一个）
    inv = capture.by_section.get("inventory")
    if inv:
        data = inv.get("data") or {}
        rooms = data.get("roomStatusResult") or []
        for r in rooms:
            if r.get("hotelID"):
                hotel_id = str(r["hotelID"])
                break
        if not hotel_id:
            prices = data.get("roomPriceResult") or {}
            for p in (prices.get("roomPriceInfo") or []):
                if p.get("roomTypeID"):
                    hotel_id = p.get("roomTypeID")
                    break

    try:
        page.close()
    except Exception:  # noqa: BLE001
        pass
    return capture.by_section, hotel_id, errors


def write_result(out: Path, result: dict):
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    log(f"[结果] 已写入: {out}（sections: {', '.join(result['sections']) or '(空)'}）")


def main():
    ap = argparse.ArgumentParser(description="携程eBook 经营数据采集（只读）")
    ap.add_argument("--out", default="data/ebook/ctrip_ebook.json", help="结果文件路径")
    ap.add_argument("--profile", default="profiles/default",
                    help="浏览器持久化登录目录（需已登录 ebooking）")
    ap.add_argument("--channel", default="chrome", help="chrome/edge/或浏览器exe路径")
    ap.add_argument("--cdp-port", type=int, default=9222, help="浏览器CDP调试端口")
    ap.add_argument("--keep-browser", action="store_true",
                    help="脚本结束后不关闭浏览器（默认自动关闭）")
    ap.add_argument("--login-wait", type=int, default=600, help="登录/验证等待秒数")
    ap.add_argument("--page-wait", type=float, default=8,
                    help="每个页面等待接口返回的秒数（默认8）")
    args = ap.parse_args()

    result = {
        "status": "ok",
        "message": "",
        "platform": "ctrip_ebook",
        "hotel_id": "",
        "collected_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "sections": {},
        "errors": [],
    }

    profile_dir = Path(args.profile).resolve()
    out = Path(args.out).resolve()

    try:
        with sync_playwright() as pw:
            context, chrome_proc = launch_browser(
                pw, profile_dir, args.channel, args.cdp_port)
            try:
                sections, hotel_id, errors = collect(context, args.login_wait, args.page_wait)
                result["sections"] = sections
                result["hotel_id"] = hotel_id
                result["errors"] = errors
                # 至少要有经营概览（market 或 capacity）才算可用
                core = ("market" in sections) or ("capacity" in sections)
                if errors and not sections:
                    result["status"] = "error"
                    result["message"] = "; ".join(errors)
                elif not core:
                    result["status"] = "error"
                    result["message"] = (
                        "没有捕获到经营概览接口（market/capacity），"
                        "可能未登录或页面结构变化。errors=" + "; ".join(errors))
                else:
                    result["status"] = "ok"
                    if errors:
                        result["message"] = "部分页面异常: " + "; ".join(errors)
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
                        log("已关闭脚本启动的浏览器")
                    except Exception:  # noqa: BLE001
                        pass
    except Exception as e:  # noqa: BLE001 - 浏览器本身没起来
        result["status"] = "error"
        result["message"] = str(e)
        log(f"[异常] {e}")

    if result["status"] == "ok" and not result["sections"]:
        result["status"] = "error"
    write_result(out, result)


if __name__ == "__main__":
    main()