"""基本冒烟测试：模块可导入、核心定价函数可运行"""
import importlib

import pytest


@pytest.mark.parametrize("mod", [
    "app.config", "app.pricing.algorithm", "app.database",
    "collectors.ctrip_crawler", "infra.notifier",
])
def test_modules_importable(mod):
    importlib.import_module(mod)


def _algo():
    from app.pricing.algorithm import PricingAlgorithm
    import app.config as config
    return PricingAlgorithm(config.PRICING_STRATEGY)


def test_low_occupancy_suggests_discount():
    algo = _algo()
    price, reason = algo.calculate_optimal_price(
        occupancy_rate=20, current_price=140, competitor_prices=[120, 130, 140])
    # 同档中位 130，入住率低 → 下浮 8% = 119.6
    assert price == 120
    assert price < 130
    assert "让价" in reason


def test_high_occupancy_suggests_premium():
    algo = _algo()
    price, reason = algo.calculate_optimal_price(
        occupancy_rate=80, current_price=120, competitor_prices=[120, 130, 140])
    # 同档中位 130，入住率高 → 上浮 6% = 137.8
    assert price == 138
    assert price > 130
    assert "高于" in reason


def test_price_bounds_respected():
    algo = _algo()
    # 竞品远低于当前价：不一步跳到底，受单次调价幅度上限约束
    price, _ = algo.calculate_optimal_price(
        occupancy_rate=20, current_price=200, competitor_prices=[90, 95, 100])
    assert price >= algo.config["min_price"]
    assert 200 - price <= 200 * algo.config["max_step_ratio"] + 1

    # 竞品远高于当前价：同样受幅度上限约束
    price, _ = algo.calculate_optimal_price(
        occupancy_rate=90, current_price=100, competitor_prices=[200, 210, 220])
    assert price >= algo.config["min_price"]
    assert price - 100 <= 100 * algo.config["max_step_ratio"] + 1


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
    from app.database import PricingDatabase
    db = PricingDatabase(str(tmp_path / "test.db"))
    db.save_my_price(date="2026-08-28", price=128.0, occupancy_rate=45.0,
                     rooms_available=20, rooms_total=50)


def test_database_upsert_is_idempotent(tmp_path):
    """同一天重复跑不应累积数据。

    原实现是纯 INSERT，实测一天内多次运行堆出 93 条竞品、10 条自家价，
    导致历史分析的 COUNT/AVG 全失真。现在靠唯一索引 + UPSERT 保证幂等。
    """
    import sqlite3

    from app.database import PricingDatabase

    db = PricingDatabase(str(tmp_path / "test.db"))
    date = "2026-11-02"
    competitors = [
        {"name": "海口雅高索菲特酒店", "id": "1", "price": 697,
         "distance": 0.35, "rating": 4.8, "reviews": 7914},
        {"name": "海口希尔顿酒店", "id": "2", "price": 576,
         "distance": 0.47, "rating": 4.6, "reviews": 8528},
    ]

    for _ in range(3):
        db.save_my_price(date=date, price=367, occupancy_rate=45,
                         rooms_available=66, rooms_total=120)
        db.save_competitor_prices(date=date, competitors=competitors)
        db.save_price_suggestion(
            date=date, current_price=367, suggested_price=180, reason="测试",
            occupancy_rate=45,
            competitor_stats={"min": 63, "avg": 242.5, "max": 697},
        )

    con = sqlite3.connect(db.db_path)
    try:
        assert con.execute("SELECT COUNT(*) FROM my_hotel_prices").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM competitor_prices").fetchone()[0] == 2
        assert con.execute("SELECT COUNT(*) FROM price_suggestions").fetchone()[0] == 1

        # 重复写入应是「更新最新值」而不是「忽略」
        db.save_competitor_prices(
            date=date,
            competitors=[dict(competitors[0], price=699)],
        )
        price = con.execute(
            "SELECT price FROM competitor_prices WHERE hotel_name = ?",
            (competitors[0]["name"],),
        ).fetchone()[0]
        assert price == 699
    finally:
        con.close()


def test_database_dedupes_existing_rows_on_init(tmp_path):
    """老库若已有重复行，初始化时应自动清理，否则唯一索引建不上。"""
    import sqlite3

    from app.database import PricingDatabase

    path = str(tmp_path / "legacy.db")
    db = PricingDatabase(path)

    con = sqlite3.connect(path)
    con.execute("DROP INDEX IF EXISTS ux_competitor_prices_date_hotel_name")
    for price in (100, 200, 300):
        con.execute(
            "INSERT INTO competitor_prices (date, hotel_name, price) VALUES (?, ?, ?)",
            ("2026-11-01", "同一家酒店", price),
        )
    con.commit()
    con.close()

    # 重新打开：应去重 + 重建唯一索引，并保留最后写入的那条
    PricingDatabase(path)

    con = sqlite3.connect(path)
    try:
        rows = con.execute(
            "SELECT price FROM competitor_prices WHERE hotel_name = '同一家酒店'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == 300
    finally:
        con.close()
