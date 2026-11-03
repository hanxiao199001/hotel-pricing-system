"""采集层测试：命令拼装、结果校验、结构适配、真实采集器行为。

不启动浏览器：runner 的 subprocess 行为在需要时用 monkeypatch 替换。
"""
import pytest

import config
from scrapers import adapters
from scrapers.runner import ScraperError, build_command, run_scraper
from scrapers.validator import DataValidationError, validate_result


def _hotel(name, price, distance=350.0, score="4.6", comments="1,200", url=""):
    return {
        "platform": "ctrip",
        "name": name,
        "price": price,
        "distance": distance,
        "distance_text": "距酒店直线%d米" % distance,
        "score": score,
        "comments": comments,
        "address": "距酒店直线%d米" % distance,
        "url": url,
    }


def _result(hotels, status="ok"):
    return {"status": status, "hotels": hotels, "place": "君华海逸酒店", "city": "海口"}


# ---------------------------------------------------------------------------
# 命令拼装
# ---------------------------------------------------------------------------
def test_build_command_includes_edge_and_skips_rooms():
    cfg = {
        "python": r"D:\v\python.exe",
        "script": r"D:\s\ctrip.py",
        "profile": r"D:\p\profile",
        "channel": "edge",
        "cdp_port": 9222,
        "login_wait": 60,
        "collect_rooms": False,
    }
    cmd = build_command(cfg, "君华海逸酒店", "海口", 2000, "2026-11-02", "2026-11-03", "out.json")

    assert "--channel" in cmd and cmd[cmd.index("--channel") + 1] == "edge"
    assert "--no-collect-rooms" in cmd
    assert "--collect-rooms" not in cmd
    assert cmd[cmd.index("--distance") + 1] == "2000"
    assert cmd[cmd.index("--place") + 1] == "君华海逸酒店"


def test_run_scraper_rejects_missing_script():
    with pytest.raises(ScraperError, match="采集脚本不存在|解释器不存在"):
        run_scraper(
            {"python": "no-such-python", "script": "no-such-script"},
            "place", "city", 1000, "2026-11-02", "2026-11-03",
        )


# ---------------------------------------------------------------------------
# 结果校验
# ---------------------------------------------------------------------------
def test_validate_passes_normal_result():
    out = validate_result(
        _result([_hotel("海口朗廷酒店", 697.0), _hotel("荷泰海景酒店", 173.0)]),
        expected_city="海口",
    )
    assert out["stats"]["total_valid"] == 2
    assert out["stats"]["city_hits"] == 1  # 只有「海口朗廷酒店」名称含城市名


def test_validate_drops_zero_price():
    out = validate_result(
        _result([_hotel("海口朗廷酒店", 697.0), _hotel("某酒店", 0.0)]),
        expected_city="海口",
    )
    assert out["stats"]["total_valid"] == 1
    assert out["stats"]["dropped_no_price"] == 1


def test_validate_dedupes_same_name():
    out = validate_result(
        _result([_hotel("麗枫酒店", 218.0), _hotel("麗枫酒店", 218.0)]),
        expected_city="海口",
    )
    assert out["stats"]["total_valid"] == 1
    assert out["stats"]["dropped_duplicate"] == 1


def test_validate_rejects_wrong_city():
    """实测场景：请求海口，采回杭州——必须整批拒绝，不能落库。"""
    hotels = [
        _hotel("杭州西湖国宾馆", 630.0),
        _hotel("杭州莲遇酒店", 300.0),
        _hotel("杭州某客栈", 200.0),
    ]
    with pytest.raises(DataValidationError, match="其他城市"):
        validate_result(_result(hotels), expected_city="海口")


def test_validate_rejects_when_all_zero_price():
    with pytest.raises(DataValidationError, match="全部无效"):
        validate_result(_result([_hotel("某酒店", 0.0)]), expected_city="海口")


def test_validate_rejects_non_ok_status():
    with pytest.raises(DataValidationError, match="需要人工登录"):
        validate_result({"status": "need_login", "hotels": [], "message": "等待超时"}, "海口")


def test_validate_allows_unnamed_city_hotels_when_no_other_city():
    """酒店名不含城市名、也没命中其他城市时不应误判。"""
    out = validate_result(
        _result([_hotel("荷泰海景酒店", 173.0), _hotel("金舟海景公寓", 217.0)]),
        expected_city="海口",
    )
    assert out["stats"]["total_valid"] == 2


# ---------------------------------------------------------------------------
# 结构适配（重点：米 -> 千米）
# ---------------------------------------------------------------------------
def test_adapter_converts_meters_to_km():
    comp = adapters.to_competitor(_hotel("海口朗廷酒店", 697.0, distance=390.0), "2026-11-02")
    assert comp["distance"] == 0.39
    assert comp["price"] == 697.0
    assert comp["rating"] == 4.6
    assert comp["reviews"] == 1200


def test_adapter_extracts_hotel_id_from_url():
    url = "https://hotels.ctrip.com/hotels/detail?hotelId=70161482&checkIn=2026/11/02"
    comp = adapters.to_competitor(_hotel("海口朗廷酒店", 697.0, url=url), "2026-11-02")
    assert comp["id"] == "70161482"


def test_adapter_handles_missing_fields():
    comp = adapters.to_competitor({"name": "某酒店", "price": 100.0}, "2026-11-02")
    assert comp["distance"] is None
    assert comp["rating"] is None
    assert comp["reviews"] is None


# ---------------------------------------------------------------------------
# 真实采集器
# ---------------------------------------------------------------------------
def test_real_crawler_requires_manual_price_and_occupancy():
    from ctrip_crawler import RealCtripCrawler

    crawler = RealCtripCrawler({"python": "x", "script": "y"}, {"name": "君华海逸酒店"})
    with pytest.raises(config.ConfigError, match="MY_HOTEL_CURRENT_PRICE"):
        crawler.get_my_hotel_data()


def test_real_crawler_builds_my_hotel_data():
    from ctrip_crawler import RealCtripCrawler

    crawler = RealCtripCrawler(
        {"python": "x", "script": "y"},
        {"name": "君华海逸酒店", "current_price": 180, "occupancy_rate": 45, "rooms_total": 50},
    )
    data = crawler.get_my_hotel_data()
    assert data["current_price"] == 180.0
    assert data["occupancy_rate"] == 45.0
    assert data["rooms_available"] == 28  # 50 * (1 - 0.45) = 27.5 -> 28


def test_real_crawler_returns_competitors_with_km(monkeypatch):
    import ctrip_crawler

    raw = _result([
        _hotel("海口朗廷酒店", 697.0, distance=390.0),
        _hotel("荷泰海景酒店", 173.0, distance=200.0),
    ])
    monkeypatch.setattr(ctrip_crawler, "run_scraper", lambda *a, **k: raw)

    crawler = ctrip_crawler.RealCtripCrawler(
        {"python": "x", "script": "y"},
        {"name": "君华海逸酒店", "current_price": 180, "occupancy_rate": 45},
    )
    comps = crawler.get_competitor_hotels("海口", 2.0)

    assert len(comps) == 2
    assert all(c["distance"] < 1.0 for c in comps)
    assert crawler.last_stats["total_valid"] == 2


def test_real_crawler_does_not_filter_by_price(monkeypatch):
    """价格区间只在 mock 模式用于生成随机值；真实模式不能拿它过滤，否则会清空。"""
    import ctrip_crawler

    raw = _result([_hotel("海口朗廷酒店", 697.0), _hotel("荷泰海景酒店", 173.0)])
    monkeypatch.setattr(ctrip_crawler, "run_scraper", lambda *a, **k: raw)

    crawler = ctrip_crawler.RealCtripCrawler(
        {"python": "x", "script": "y"},
        {"name": "君华海逸酒店", "current_price": 180, "occupancy_rate": 45},
    )
    comps = crawler.get_competitor_hotels("海口", 2.0, price_min=100, price_max=150)

    assert len(comps) == 2
