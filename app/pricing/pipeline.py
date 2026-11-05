"""询价工作流 —— 全项目唯一实现。

流程（步骤顺序与计算方式沿用原 main.run_pricing_check，未做改动）：
    1. 校验配置
    2. 建库（幂等：同一天同一对象只留一条）
    3. 创建采集器并检查采集环境
    4. 取自家酒店数据（真实模式下来自 .env 的人工值）
    5. 采集竞品并落库
    6. 用 PricingAlgorithm 算建议价（同档中位价 × 入住率系数，单次幅度上限）
    7. 算市场定位
    8. 落库建议

为什么要抽出来：原来这段逻辑长在 main.py 里，中台要复用只能 import main，
于是网页端和命令行成了两条路径，很容易算出两个不同的价。现在只有一个实现，
CLI 负责打印、中台负责展示，计算口径天然一致。
"""
from datetime import datetime
from typing import Dict, Optional

from app import config as cfg
from app.database import PricingDatabase
from app.pricing import hotel_metrics
from app.pricing.algorithm import PricingAlgorithm
from collectors.ctrip_crawler import create_crawler
from scrapers.runner import ScraperError  # noqa: F401 - 供调用方复用异常类型
from scrapers.validator import DataValidationError  # noqa: F401


def _resolve_source(data_source=None):
    return (data_source or cfg.DATA_SOURCE or "mock").strip().lower()


class WorkflowError(RuntimeError):
    """工作流中断：采集环境不可用、拿不到自家数据、竞品为空等业务性失败。"""


def run_pricing_check(
    data_source: Optional[str] = None,
    persist: bool = True,
    verbose: bool = True,
) -> Dict:
    """执行一次询价。

    Args:
        data_source: "mock" / "real"，默认取 config.DATA_SOURCE
        persist: 是否写库（False 用于试算/联调，不污染历史）
        verbose: 是否打印过程日志

    Returns:
        结构化结果，含自家数据、竞品列表、价格统计、算法中间量、市场定位。
        注意：建议价的「计算」全部发生在这一步，中台不再自己算一遍。
    """
    log = print if verbose else (lambda *a, **k: None)

    cfg.validate_startup_config(data_source=data_source)
    source = _resolve_source(data_source)
    db = PricingDatabase(cfg.DATABASE["path"])
    crawler = create_crawler(cfg.CTRIP_CONFIG, source)
    algorithm = PricingAlgorithm(cfg.PRICING_STRATEGY)

    # 1. 采集环境
    if not crawler.login():
        raise WorkflowError("携程登录/采集环境检查失败")

    # 2. 自家酒店数据：real 模式从 eBook 采集取，mock 模式仍用模拟值（测试用）
    if source == "real":
        metrics = hotel_metrics.resolve(db)
        current_price = metrics["current_price"]
        occupancy_rate = metrics["occupancy_rate"]
        if current_price is None or occupancy_rate is None:
            raise WorkflowError(
                "缺少当前价/入住率：请先采集 eBook 经营数据（ebook_collector.py）。"
                "本系统已不再从 .env 读取 MY_HOTEL_CURRENT_PRICE / MY_HOTEL_OCCUPANCY_RATE。"
            )
        rooms_total = cfg.MY_HOTEL.get("rooms_total")
        rooms_available = None
        if rooms_total:
            rooms_available = int(round(
                float(rooms_total) * (1 - float(occupancy_rate) / 100.0)))
        my_data = {
            "date": str(datetime.now().date()),
            "hotel_id": cfg.MY_HOTEL.get("hotel_id"),
            "hotel_name": cfg.MY_HOTEL.get("name") or "自家酒店",
            "current_price": round(float(current_price), 2),
            "occupancy_rate": round(float(occupancy_rate), 2),
            "rooms_available": rooms_available,
            "rooms_total": int(rooms_total) if rooms_total else None,
            "revenue_today": 0,
            "metrics_source": metrics.get("source"),
        }
        log(
            f"  自家指标来自采集（{metrics.get('source')}）："
            f"当前价 ¥{current_price:.0f}，入住率 {occupancy_rate:.1f}%"
        )
    else:
        my_data = crawler.get_my_hotel_data()
        if not my_data:
            raise WorkflowError("获取酒店数据失败")
        current_price = my_data["current_price"]
        occupancy_rate = my_data["occupancy_rate"]

    if persist:
        db.save_my_price(
            date=my_data["date"],
            price=current_price,
            occupancy_rate=occupancy_rate,
            rooms_available=my_data["rooms_available"],
            rooms_total=my_data["rooms_total"],
        )

    # 3. 竞品
    competitors = crawler.get_competitor_hotels(
        city=cfg.COMPETITOR_FILTER["city"],
        distance_km=cfg.COMPETITOR_FILTER["distance_km"],
        price_min=cfg.COMPETITOR_FILTER["price_min"],
        price_max=cfg.COMPETITOR_FILTER["price_max"],
    )
    if not competitors:
        raise WorkflowError("未找到竞品酒店")

    if persist:
        db.save_competitor_prices(date=my_data["date"], competitors=competitors)

    prices = [c["price"] for c in competitors]

    # 4. 算价：口径完全沿用原实现（explain 只是把中间量一并返回）
    detail = algorithm.explain(
        occupancy_rate=occupancy_rate,
        current_price=current_price,
        competitor_prices=prices,
    )
    position = algorithm.analyze_market_position(
        my_price=current_price, competitor_prices=prices
    )

    if persist:
        db.save_price_suggestion(
            date=my_data["date"],
            current_price=current_price,
            suggested_price=detail["suggested_price"],
            reason=detail["reason"],
            occupancy_rate=occupancy_rate,
            competitor_stats={
                "min": min(prices),
                "avg": sum(prices) / len(prices),
                "max": max(prices),
            },
        )

    log(
        f"✅ 询价完成 {datetime.now().strftime('%H:%M:%S')}："
        f"{len(competitors)} 家竞品，建议价 ¥{detail['suggested_price']:.0f}"
    )

    return {
        "date": my_data["date"],
        "my": my_data,
        "competitors": competitors,
        "prices": prices,
        "stats": {
            "count": len(prices),
            "min": min(prices),
            "avg": sum(prices) / len(prices),
            "median": sorted(prices)[len(prices) // 2]
            if len(prices) % 2
            else (
                sorted(prices)[len(prices) // 2 - 1] + sorted(prices)[len(prices) // 2]
            )
            / 2,
            "max": max(prices),
        },
        "suggestion": detail,
        "position": position,
        "persisted": persist,
    }
