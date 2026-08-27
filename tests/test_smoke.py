"""基本冒烟测试：模块可导入、核心定价函数可运行"""
import importlib

import pytest


@pytest.mark.parametrize("mod", [
    "config", "algorithm", "database", "ctrip_crawler", "notifier",
])
def test_modules_importable(mod):
    importlib.import_module(mod)


def _algo():
    from algorithm import PricingAlgorithm
    import config
    return PricingAlgorithm(config.PRICING_STRATEGY)


def test_low_occupancy_suggests_discount():
    algo = _algo()
    price, reason = algo.calculate_optimal_price(
        occupancy_rate=20, current_price=140, competitor_prices=[120, 130, 140])
    assert price == 120  # 竞品均价130 - 降价10
    assert "降价" in reason


def test_high_occupancy_suggests_premium():
    algo = _algo()
    price, reason = algo.calculate_optimal_price(
        occupancy_rate=80, current_price=120, competitor_prices=[120, 130, 140])
    assert price == 140  # 竞品均价130 + 涨价10
    assert "涨价" in reason


def test_price_bounds_respected():
    algo = _algo()
    price, _ = algo.calculate_optimal_price(
        occupancy_rate=20, current_price=200, competitor_prices=[90, 95, 100])
    assert price >= algo.config["min_price"]
    price, _ = algo.calculate_optimal_price(
        occupancy_rate=90, current_price=100, competitor_prices=[200, 210, 220])
    assert price <= algo.config["max_price"]


def test_no_competitors_keeps_price():
    algo = _algo()
    price, reason = algo.calculate_optimal_price(
        occupancy_rate=50, current_price=130, competitor_prices=[])
    assert price == 130
    assert reason == "无竞品数据"


def test_market_position():
    algo = _algo()
    pos = algo.analyze_market_position(my_price=110, competitor_prices=[120, 130, 140])
    assert pos["rank"] == 1
    assert pos["total"] == 4
    assert pos["position"] == "最低价"


def test_database_init_and_save(tmp_path):
    from database import PricingDatabase
    db = PricingDatabase(str(tmp_path / "test.db"))
    db.save_my_price(date="2026-08-28", price=128.0, occupancy_rate=45.0,
                     rooms_available=20, rooms_total=50)
