"""eBook 经营数据：采集结果校验 + 经营报表解析 的回归测试。

不真起浏览器（那依赖 profile 登录态），只验证校验逻辑与快照→页面 block
的解析链路。真实采集端到端由 ebook_collector.py 运行时覆盖。
"""
import json

import pytest

import app.config as config
from scrapers import ebook_runner
from webapp import ebook_pages


def _sections():
    """一份结构接近真实接口响应的 sections 样本。"""
    return {
        "market": {
            "data": {
                "averagePrice": 352.0,
                "rentalRate": 61.4,
                "closeRate": 0.35,
                "bookAmount": 128640.0,
                "bookQuantity": 120,
                "stayInQuantity": 118,
                "competitorNumber": 26,
            }
        },
        "capacity": {
            # 真实 fetchCapacityOverViewV4 是顶层字段，没有 data 包裹
            "occupancyRate": 61.4,
            "occupiedRooms": 120,
            "orderQuantity": 8,
        },
        "inventory": {
            "data": {
                "roomPriceResult": {
                    "roomPriceInfo": [
                        {
                            "roomTypeID": 173658932,
                            "effectDate": "2026-11-04",
                            "price": 269.0,
                            "commissionRate": 0.12,
                        }
                    ]
                },
                "roomStatusResult": [
                    {
                        "roomTypeID": 173658932,
                        "effectDate": "2026-11-04",
                        "totalQuantity": 8,
                        "canUsedQuantity": 8,
                        "hotelID": 14266732,
                    }
                ],
            }
        },
        "rooms": {
            "data": [
                {
                    "roomName": "&#2186;&#21153;&#22823;&#24202;&#25151;&#65288;&#20083;&#33014;&#24202;&#22343;+&#23433;&#30524;&#26080;&#24551;&#65289;",
                    "roomInfos": [
                        {
                            "roomTypeID": 173658932,
                            "roomName": "&#2186;&#21153;&#22823;&#24202;&#25151;&#65288;&#20083;&#33046;&#24202;&#22357;+&#23433;&#30524;&#26080;&#24551;&#65289;",
                        }
                    ],
                }
            ]
        },
        # 客源结构（数据中心 → 用户行为 → 用户分析）
        "guest_source": {
            "rcode": 0,
            "data": {
                "localCityRate": 29.55,
                "otherCityRate": 70.45,
                "topCityRate": 39.77,
                "provinces": [
                    {"name": "海南", "value": 42.05},
                    {"name": "广东", "value": 6.82},
                ],
                "cities": [
                    {"name": "海口", "value": 29.55},
                    {"name": "广州", "value": 3.41},
                ],
            },
        },
        "guest_features": {
            "rcode": 0,
            "data": [
                {
                    "age": "25-34", "sex": "男", "source": "异地",
                    "type": "休闲型", "traveltime": "工作日",
                    "consumer": "中低档", "bookingdays": "当天预定",
                    "staydays": "1天",
                }
            ],
        },
        "guest_order_type": {
            "rcode": 0,
            "data": {
                "titleList": ["预订方式占比"],
                "userColumnBos": [
                    {"titleList": ["电脑端", "移动端", "其它"], "valueList": [0.0]},
                    {"titleList": ["电脑端", "移动端", "其它"], "valueList": [94.52]},
                    {"titleList": ["电脑端", "移动端", "其它"], "valueList": [5.48]},
                ],
            },
        },
        "guest_order_dist": {
            "rcode": 0,
            "data": {
                "maxProportionHour": "22:00",
                "maxProportion": 13.89,
                "orderDistributionEntities": [
                    {"hour": "22:00", "proportion": 13.89},
                    {"hour": "17:00", "proportion": 11.11},
                ],
            },
        },
    }


def _payload(**overrides):
    data = {
        "status": "ok",
        "platform": "ctrip_ebook",
        "hotel_id": "14266732",
        "collected_at": "2026-11-04 12:00:00",
        "sections": _sections(),
        "errors": [],
    }
    data.update(overrides)
    return data


# ---------------------------------------------------------------------------
# ebook_runner.validate_payload
# ---------------------------------------------------------------------------
class TestValidatePayload:
    def test_ok(self):
        checked = ebook_runner.validate_payload(_payload(), expected_hotel_id="14266732")
        assert checked["payload"]["status"] == "ok"
        assert checked["hotelId"] == "14266732"
        assert checked["core"]["adr"] == 352.0
        assert checked["core"]["occupancy_rate"] == 61.4

    def test_status_not_ok_rejected(self):
        with pytest.raises(ebook_runner.EbookCollectError):
            ebook_runner.validate_payload(_payload(status="error", message="xx"))

    def test_missing_core_rejected(self):
        sec = _sections()
        sec.pop("market")
        sec.pop("capacity")
        with pytest.raises(ebook_runner.EbookCollectError):
            ebook_runner.validate_payload(_payload(sections=sec))

    def test_hotel_id_mismatch_rejected(self):
        with pytest.raises(ebook_runner.EbookCollectError):
            ebook_runner.validate_payload(
                _payload(), expected_hotel_id="99999999")


# ---------------------------------------------------------------------------
# ebook_pages.build
# ---------------------------------------------------------------------------
@pytest.fixture
def _snapshot_file(tmp_path, monkeypatch):
    """把快照写到临时文件并让 config 指向它。"""
    out = tmp_path / "ebook.json"
    monkeypatch.setitem(config.EBOOK_CONFIG, "path", str(out))
    return out


class TestBuild:
    def test_only_reports(self):
        assert ebook_pages.build("reviews") is None
        assert ebook_pages.build("unknown") is None

    def test_missing_snapshot_returns_none(self, _snapshot_file):
        assert ebook_pages.build("reports") is None

    def test_ok_snapshot_renders(self, _snapshot_file, monkeypatch):
        with open(_snapshot_file, "w", encoding="utf-8") as f:
            json.dump(_payload(), f, ensure_ascii=False)
        mod = ebook_pages.build("reports")
        assert mod is not None
        assert mod["status"] == "snapshot"
        assert mod["key"] == "reports"
        types = [b["type"] for b in mod["blocks"]]
        assert "kpi" in types
        # 房型名 HTML 实体应被解码（不再残留 &#xxx;）
        table = next(b for b in mod["blocks"] if b["type"] == "table")
        assert "&#" not in table["rows"][0]["room"]
        assert table["rows"][0]["price"] == "269"
        assert table["rows"][0]["commission"] == "12%"

    def test_bad_snapshot_status_returns_none(self, _snapshot_file):
        with open(_snapshot_file, "w", encoding="utf-8") as f:
            json.dump(_payload(status="error"), f, ensure_ascii=False)
        assert ebook_pages.build("reports") is None


class TestBuildGuests:
    """客源结构页：从用户行为接口解析成 kpi/bars/table。"""

    def test_missing_snapshot_returns_none(self, _snapshot_file):
        assert ebook_pages.build("guests") is None

    def test_ok_snapshot_renders_guests(self, _snapshot_file):
        with open(_snapshot_file, "w", encoding="utf-8") as f:
            json.dump(_payload(), f, ensure_ascii=False)
        mod = ebook_pages.build("guests")
        assert mod is not None
        assert mod["key"] == "guests"
        assert mod["source"] == "snapshot"
        types = [b["type"] for b in mod["blocks"]]
        assert "kpi" in types and "bars" in types and "table" in types

        bars = [b for b in mod["blocks"] if b["type"] == "bars"]
        city_bar = next(b for b in bars if b["title"] == "客源地城市 TOP")
        assert city_bar["items"][0]["label"] == "海口"
        assert city_bar["items"][0]["value"] == 29.55

        tables = [b for b in mod["blocks"] if b["type"] == "table"]
        ch = next(t for t in tables if t["title"] == "预订方式占比")
        mobile = next(r for r in ch["rows"] if r["channel"] == "移动端")
        assert mobile["share"] == "94.52%"

    def test_guests_without_guest_sections_returns_none(self, _snapshot_file):
        sec = _sections()
        for k in ("guest_source", "guest_features",
                  "guest_order_type", "guest_order_dist"):
            sec.pop(k, None)
        with open(_snapshot_file, "w", encoding="utf-8") as f:
            json.dump(_payload(sections=sec), f, ensure_ascii=False)
        assert ebook_pages.build("guests") is None
        # 经营报表不受影响
        assert ebook_pages.build("reports") is not None


# ---------------------------------------------------------------------------
# ebook_runner.collect：成功日志 / 返回值（回归：sections 是 int，不能再 len()）
# ---------------------------------------------------------------------------
def test_collect_returns_int_sections_and_logs(tmp_path, monkeypatch):
    from scrapers import ebook_runner

    out = tmp_path / "ebook.json"
    cfg_dict = {
        "path": str(out), "script": "x.py", "python": "py", "profile": ".",
        "channel": "edge", "cdp_port": 9222, "login_wait": 1, "page_wait": 1,
        "timeout": 60, "attempts": 1, "fast_fail_seconds": 25,
    }
    monkeypatch.setattr(ebook_runner, "_check_env", lambda c: None)
    monkeypatch.setitem(config.CTRIP_CONFIG, "hotel_id", "14266732")

    def fake_run(cmd, cfgd, env, log):
        tmp = cfgd["path"] + ".collecting.json"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_payload(), f, ensure_ascii=False)
        return {"rc": 0, "errLines": [], "tail": [], "timedOut": False}

    monkeypatch.setattr(ebook_runner, "_run_script", fake_run)
    logs = []
    result = ebook_runner.collect(log=logs.append, cfg_dict=cfg_dict)
    assert result["status"] == "ok"
    # sections 是「数量(int)」，曾误用 len() 导致 TypeError
    assert result["sections"] == len(_sections())
    assert isinstance(result["sections"], int)
    assert out.exists()
    assert any("eBook 采集完成" in line for line in logs)