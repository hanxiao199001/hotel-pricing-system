"""中台 API 与 HTTP 层测试。

用临时库，不碰项目里的 pricing.db。
"""
import json
import os
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from database import PricingDatabase
from webapp import api, static_data


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """建一个带 5 家竞品 + 1 条建议的临时库，并接管 api 的单例。"""
    db = PricingDatabase(str(tmp_path / "webapp_test.db"))
    db.save_my_price("2026-11-02", 300, 45.0, 66, 120)
    db.save_competitor_prices(
        "2026-11-02",
        [
            {"name": "甲酒店", "id": "1", "price": 180, "distance": 0.3, "rating": 4.5, "reviews": 120},
            {"name": "乙酒店", "id": "2", "price": 240, "distance": 0.6, "rating": 4.4, "reviews": 80},
            {"name": "丙酒店", "id": "3", "price": 300, "distance": 0.9, "rating": 4.6, "reviews": 300},
            {"name": "丁酒店", "id": "4", "price": 420, "distance": 1.2, "rating": 4.7, "reviews": 60},
            {"name": "戊酒店", "id": "5", "price": 60, "distance": 1.8, "rating": 3.9, "reviews": 20},
        ],
    )
    db.save_price_suggestion(
        "2026-11-02", 300, 275, "测试用建议", 45.0, {"min": 60, "avg": 240, "max": 420}
    )
    monkeypatch.setattr(api, "_db_instance", db)
    yield db
    api._db_instance = None


# ---------------------------------------------------------------------------
# 数据接口
# ---------------------------------------------------------------------------
def test_radar_shape_and_histogram_covers_all(temp_db, monkeypatch):
    # 概览/雷达的当前价、入住率走 hotel_metrics（默认可能读到本机 eBook 快照）；
    # 这里固定为测试库里的值，保证断言只验证雷达本身。
    monkeypatch.setattr(
        api.hotel_metrics, "resolve",
        lambda db=None: {"current_price": 300, "occupancy_rate": 45, "source": "db"},
    )
    d = api.get_radar()
    assert d["empty"] is False
    assert d["stats"]["count"] == 5
    assert d["stats"]["min"] == 60
    assert d["stats"]["max"] == 420
    # 直方图分桶数必须等于竞品总数，不能漏价格
    assert sum(h["count"] for h in d["histogram"]) == 5
    assert d["myPrice"] == 300
    # 严格低于我价的才算「比我便宜」，同价不计
    assert d["cheaperCount"] == 3
    assert d["expensiveCount"] == 1


def test_radar_empty_db_is_explicit(tmp_path, monkeypatch):
    db = PricingDatabase(str(tmp_path / "empty.db"))
    monkeypatch.setattr(api, "_db_instance", db)
    d = api.get_radar()
    assert d["empty"] is True
    assert "采集" in d["message"]


def test_analysis_respects_step_cap(temp_db):
    d = api.analyze(occupancy_rate=70, price=400)
    detail = d["detail"]
    # 400 元、单次上限 15% → 一次最多动 60 元
    assert abs(detail["suggested_price"] - 400) <= 60.5
    assert detail["stepped"] is True
    # 三档入住率的「理论目标」必须不同，否则说明又在用绝对硬顶
    ideals = {b["occupancy"]: b["ideal"] for b in d["bands"]}
    assert len(set(ideals.values())) == 3


def test_analysis_rejects_missing_price(temp_db):
    api.get_db().db_path  # 确认库可用
    with pytest.raises(ValueError):
        api.analyze(occupancy_rate=50, price=0)


# ---------------------------------------------------------------------------
# 拍板流程：这是中台相对命令行的新增能力
# ---------------------------------------------------------------------------
def test_decide_writes_adjustment_and_is_idempotent_guarded(temp_db):
    sug = api.get_suggestions()["suggestions"][0]
    assert sug["status"] == "pending"

    out = api.decide(sug["id"], "approved", "按建议执行")
    assert out["suggestion"]["status"] == "approved"
    assert out["suggestion"]["decision_note"] == "按建议执行"

    adj = api.get_suggestions()["adjustments"]
    assert len(adj) == 1
    assert adj[0]["old_price"] == 300
    assert adj[0]["new_price"] == 275

    # 同一条建议不能重复拍板
    with pytest.raises(ValueError):
        api.decide(sug["id"], "approved")


def test_decide_rejected_does_not_write_adjustment(temp_db):
    sug = api.get_suggestions()["suggestions"][0]
    api.decide(sug["id"], "rejected", "不跟")
    assert api.get_suggestions()["adjustments"] == []


def test_decide_rejects_bad_decision(temp_db):
    sug = api.get_suggestions()["suggestions"][0]
    with pytest.raises(ValueError):
        api.decide(sug["id"], "maybe")


# ---------------------------------------------------------------------------
# 静态模块
# ---------------------------------------------------------------------------
def test_all_static_modules_have_renderable_blocks():
    mods = api.get_modules()["modules"]
    keys = [m["key"] for m in mods]
    assert set(keys) >= {"reviews", "reputation", "reports", "guests", "tasks", "group"}
    allowed = {"kpi", "list", "table", "bars", "quote", "banner", "note"}
    checked = 0
    for m in mods:
        # 有真实数据源的模块（差评/口碑/回复）不走 static.json，
        # 由各自的构建器或专用前端页面处理，这里只校验示例数据的完整性
        if m["status"] != "static":
            continue
        mod = api.get_module(m["key"])
        assert mod["source"] == "static"
        types = {b["type"] for b in mod["blocks"]}
        assert types <= allowed, f"{m['key']} 用了未实现的 block 类型: {types - allowed}"
        checked += 1
    # reports 可能被本机真实 eBook 快照接管（变成 snapshot 而跳过），
    # 所以至少保障其余静态模块（guests/tasks/group）完整即可
    assert checked >= 3  # guests / tasks / group


def test_static_data_returns_copy():
    a = static_data.load()
    a["modules"]["reviews"]["title"] = "被改掉了"
    assert static_data.load()["modules"]["reviews"]["title"] != "被改掉了"


# ---------------------------------------------------------------------------
# HTTP 层：真的起一个服务，走一遍路由
# ---------------------------------------------------------------------------
@pytest.fixture
def live_server(temp_db):
    from webapp.server import Handler

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()
    httpd.server_close()


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def test_http_get_endpoints(live_server):
    for path in ("/api/status", "/api/overview", "/api/radar", "/api/suggestions", "/api/modules"):
        status, payload = _get(live_server + path)
        assert status == 200, path
        assert "source" in payload, path

    status, payload = _get(live_server + "/api/modules/reviews")
    assert status == 200 and payload["key"] == "reviews"


def test_http_index_and_static(live_server):
    with urllib.request.urlopen(live_server + "/", timeout=5) as r:
        assert r.status == 200
        assert "酒店智能决策中台".encode("utf-8") in r.read()

    with urllib.request.urlopen(live_server + "/app.css", timeout=5) as r:
        assert r.status == 200


def test_http_404_and_post_decision(live_server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(live_server + "/api/nope")
    assert exc.value.code == 404

    sug_id = api.get_suggestions()["suggestions"][0]["id"]
    req = urllib.request.Request(
        live_server + f"/api/suggestions/{sug_id}/decision",
        data=json.dumps({"decision": "approved", "note": "http"}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        payload = json.loads(r.read().decode("utf-8"))
    assert payload["suggestion"]["status"] == "approved"


def test_http_replies_endpoints(live_server):
    """回复草稿的四个接口都要能走通（生成 / 保存 / 标记 / 列表）。"""
    status, payload = _get(live_server + "/api/replies")
    assert status == 200
    if not payload.get("available"):
        pytest.skip("没有点评快照")
    assert payload["items"], "差评列表不应为空"
    key = payload["items"][0]["key"]

    req = urllib.request.Request(
        live_server + "/api/replies/generate",
        data=json.dumps({"key": key, "preferLlm": False}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        gen = json.loads(r.read().decode("utf-8"))
    assert gen["ok"] is True
    assert gen["source"] == "template"

    req = urllib.request.Request(
        live_server + "/api/replies/save",
        data=json.dumps({"key": key, "text": gen["draft"]["text"] + "\n\n补充一句。"}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        saved = json.loads(r.read().decode("utf-8"))
    assert saved["draft"]["edited"] is True

    req = urllib.request.Request(
        live_server + "/api/replies/status",
        data=json.dumps({"key": key, "status": "used", "note": "http"}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        done = json.loads(r.read().decode("utf-8"))
    assert done["draft"]["status"] == "used"


def test_http_replies_unknown_key_is_400(live_server):
    req = urllib.request.Request(
        live_server + "/api/replies/generate",
        data=json.dumps({"key": "deadbeef"}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    assert exc.value.code == 400
