"""零依赖 HTTP 服务：静态文件 + JSON 接口。

只监听回环地址（默认 127.0.0.1），因为这个服务里没有任何鉴权——
它直接读你本机的 pricing.db。要给别人看，请先加一层反向代理与登录。
"""
import argparse
import json
import os
import posixpath
import re
import sys
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import config as cfg

from . import api, daily
from .api import BusyError

_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}

_ROUTES = [
    ("GET", re.compile(r"^/api/status$"), lambda q, m: api.get_status()),
    ("GET", re.compile(r"^/api/overview$"), lambda q, m: api.get_overview()),
    ("GET", re.compile(r"^/api/radar$"), lambda q, m: api.get_radar(q.get("date", [None])[0])),
    ("GET", re.compile(r"^/api/analysis$"), lambda q, m: api.analyze(
        occupancy_rate=q.get("occupancy", [None])[0],
        price=q.get("price", [None])[0],
        date=q.get("date", [None])[0],
    )),
    ("GET", re.compile(r"^/api/suggestions$"), lambda q, m: api.get_suggestions(
        limit=int(q.get("limit", ["20"])[0])
    )),
    ("GET", re.compile(r"^/api/adjustments$"), lambda q, m: {
        "source": "live", "adjustments": api.get_db().get_adjustments(
            limit=int(q.get("limit", ["20"])[0])
        )
    }),
    ("GET", re.compile(r"^/api/modules$"), lambda q, m: api.get_modules()),
    ("GET", re.compile(r"^/api/modules/(?P<key>[\w-]+)$"), lambda q, m: api.get_module(m["key"])),
    ("GET", re.compile(r"^/api/jobs/(?P<id>\d+)$"), lambda q, m: api.get_job(m["id"])),
    ("POST", re.compile(r"^/api/collect$"), lambda q, m, body: api.start_collect()),
    ("POST", re.compile(r"^/api/reviews/collect$"),
     lambda q, m, body: api.start_reviews_collect()),
    ("POST", re.compile(r"^/api/ebook/collect$"),
     lambda q, m, body: api.start_ebook_collect()),
    ("GET", re.compile(r"^/api/replies$"), lambda q, m: api.get_replies(
        theme=q.get("theme", [None])[0],
        status=q.get("status", [None])[0],
    )),
    ("POST", re.compile(r"^/api/replies/generate$"),
     lambda q, m, body: api.generate_reply(
         body.get("key"), prefer_llm=bool(body.get("preferLlm"))
     )),
    ("POST", re.compile(r"^/api/replies/save$"),
     lambda q, m, body: api.save_reply_text(body.get("key"), body.get("text", ""))),
    ("POST", re.compile(r"^/api/replies/status$"),
     lambda q, m, body: api.set_reply_status(
         body.get("key"), body.get("status"), body.get("note", "")
     )),
    ("POST", re.compile(r"^/api/suggestions/(?P<id>\d+)/decision$"),
     lambda q, m, body: api.decide(m["id"], body.get("decision"), body.get("note", ""))),
]


class Handler(BaseHTTPRequestHandler):
    server_version = "HotelPricingConsole/1.0"

    # ---- 输出 ----
    def _send_json(self, payload, status=200):
        raw = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    def _send_file(self, path):
        ext = os.path.splitext(path)[1].lower()
        with open(path, "rb") as f:
            raw = f.read()
        self.send_response(200)
        self.send_header("Content-Type", _CONTENT_TYPES.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    def _error(self, message, status=500):
        self._send_json({"error": message}, status=status)

    # ---- 路由 ----
    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        # 任何页面/接口访问都唤醒一次每日采集判定（只 set 一个事件，不阻塞请求）
        daily.maybe_wake()
        parsed = urlparse(self.path)
        path = posixpath.normpath(parsed.path)
        query = parse_qs(parsed.query)

        if path.startswith("/api/"):
            self._handle_api(method, path, query)
            return

        if method != "GET":
            self._error("只支持 GET", 405)
            return

        if path in ("/", "/index.html"):
            self._send_file(os.path.join(_STATIC_DIR, "index.html"))
            return

        # 静态资源：限制在 static 目录内，防目录穿越
        rel = path.lstrip("/")
        target = os.path.normpath(os.path.join(_STATIC_DIR, rel))
        if not target.startswith(_STATIC_DIR) or not os.path.isfile(target):
            self._error(f"未找到: {path}", 404)
            return
        self._send_file(target)

    def _handle_api(self, method, path, query):
        body = {}
        if method == "POST":
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            if raw:
                try:
                    body = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    self._error("请求体不是合法 JSON", 400)
                    return

        for verb, pattern, fn in _ROUTES:
            match = pattern.match(path)
            if not match:
                continue
            if verb != method:
                self._error(f"{path} 只支持 {verb}", 405)
                return
            try:
                if method == "POST":
                    result = fn(query, match.groupdict(), body)
                else:
                    result = fn(query, match.groupdict())
                self._send_json(result)
            except BusyError as exc:
                self._error(str(exc), 409)
            except KeyError as exc:
                self._error(f"未找到: {exc}", 404)
            except (ValueError, LookupError) as exc:
                self._error(str(exc), 400)
            except Exception as exc:  # noqa: BLE001
                if os.environ.get("WEBAPP_DEBUG") == "1":
                    traceback.print_exc()
                self._error(f"{type(exc).__name__}: {exc}", 500)
            return

        self._error(f"未知接口: {path}", 404)

    def log_message(self, fmt, *args):
        # 默认实现会刷屏，只在调试时输出
        if os.environ.get("WEBAPP_DEBUG") == "1":
            sys.stderr.write("[webapp] %s - %s\n" % (self.address_string(), fmt % args))


def serve(host="127.0.0.1", port=8765, open_browser=False):
    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}/"
    print("🏨 酒店智能决策中台")
    print(f"   地址: {url}")
    print(f"   数据: {cfg.DATABASE['path']}")
    print(f"   模式: {cfg.DATA_SOURCE}")
    if daily.start_watcher():
        at = cfg.DAILY_COLLECT_CONFIG.get("at", "08:00")
        print(f"   每日自动采集: 已开启（每天 {at}，或到点后当天首次进入中台触发）")
        if (cfg.DATA_SOURCE or "").lower() != "real":
            print("   ⚠️ 当前非 real 模式，每日自动采集会跳过，不会写真实库")
    else:
        print("   每日自动采集: 未开启（DAILY_COLLECT_ENABLED=false）")
    print("   Ctrl+C 停止\n")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="酒店智能决策中台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    args = parser.parse_args(argv)
    serve(args.host, args.port, args.open)


if __name__ == "__main__":
    main()
