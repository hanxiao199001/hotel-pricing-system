"""算法层用例：中位数抗异常值、同档分层、幅度限制、真实场景回归。

这些用例对应的都是真实数据暴露出来的问题，不是假想的边界情况：
改造前 48 家海口竞品下，当前价 ¥367 会被建议降到 ¥180（-51%）。
"""
import statistics

import pytest

import app.config as config
from app.pricing.algorithm import PricingAlgorithm


@pytest.fixture
def algo():
    return PricingAlgorithm(dict(config.PRICING_STRATEGY))


def test_median_resists_high_end_outlier(algo):
    """一家五星酒店不该把建议价整体拉高——这是原来用算术均值的病。"""
    base_prices = [180, 200, 210, 220, 230]
    with_outlier = base_prices + [697]

    price_clean, _ = algo.calculate_optimal_price(50, 220, base_prices)
    price_outlier, _ = algo.calculate_optimal_price(50, 220, with_outlier)

    # 均值被一家酒店抬高了 80 多元，但建议价几乎不动（中位数抗干扰）
    assert statistics.mean(with_outlier) - statistics.mean(base_prices) > 60
    assert abs(price_outlier - price_clean) <= 15


def test_peers_filtered_by_price_band(algo):
    """只跟同档竞品比，不与远超自己价位的酒店对标。"""
    peers, fell_back = algo._pick_peers(200, [60, 80, 150, 190, 210, 260, 650, 697])

    assert fell_back is False
    assert 60 not in peers and 697 not in peers
    assert all(120 <= p <= 320 for p in peers)


def test_peers_fallback_when_band_too_small(algo):
    """同档样本不足时退回全量，避免用一两家定生死。"""
    peers, fell_back = algo._pick_peers(200, [60, 80, 650, 697])

    assert fell_back is True
    assert len(peers) == 4


def test_single_step_limit(algo):
    """目标价再远，单次调整也不超过当前价的 15%。"""
    price, reason = algo.calculate_optimal_price(20, 400, [100, 110, 120])

    assert 400 - price <= 400 * algo.config["max_step_ratio"] + 1
    assert "分步靠近" in reason


def test_no_absolute_cap_clamps_price(algo):
    """配置里的 max_price 已改为 None，不能出现「所有建议都等于某个数」。"""
    assert algo.config["max_price"] is None

    prices = [250, 280, 300, 320, 350]
    low, _ = algo.calculate_optimal_price(20, 367, prices)
    normal, _ = algo.calculate_optimal_price(45, 367, prices)
    high, _ = algo.calculate_optimal_price(75, 367, prices)

    # 原实现三个档位全部输出 180；现在至少要随入住率变化且方向正确
    assert len({low, normal, high}) > 1
    assert high > low
    assert low <= normal <= high


def test_real_world_case_no_longer_clamps_to_180(algo):
    """真实回归：海口 48 家竞品下，当前价 ¥367 不应再被硬压到 ¥180。"""
    prices = [
        63, 98, 120, 138, 149, 151, 162, 173, 185, 193, 206, 212, 217,
        220, 230, 233, 241, 258, 268, 280, 297, 310, 317, 330, 346,
        367, 387, 398, 410, 445, 480, 520, 550, 576, 600, 640, 697,
    ]
    price, reason = algo.calculate_optimal_price(45, 367, prices)

    assert price > 200, f"建议价 {price} 仍被压得过低"
    assert abs(price - 367) <= 367 * algo.config["max_step_ratio"] + 1
    assert "同档" in reason


def test_market_position_handles_ties(algo):
    """价格并列时应同名次，而不是像 list.index() 那样排到最前面。"""
    assert algo.analyze_market_position(100, [100, 200, 300])["rank"] == 1
    assert algo.analyze_market_position(200, [100, 200, 300])["rank"] == 2
    assert algo.analyze_market_position(300, [100, 200, 300])["rank"] == 3


def test_market_position_low_tier_reachable(algo):
    """小样本下「低价位」应可达——原实现里是永远走不到的死代码。"""
    prices = list(range(100, 1000, 100))
    pos = algo.analyze_market_position(150, prices)

    assert pos["rank"] == 2
    assert pos["position"] == "低价位"
