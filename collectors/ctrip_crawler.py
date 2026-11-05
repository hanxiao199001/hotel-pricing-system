"""
携程数据抓取模块

两种实现，由 config.DATA_SOURCE 选择：
  CtripCrawler     - 模拟数据，用于开发与测试
  RealCtripCrawler - 调用 ctrip_hotel_scraper.py 抓取真实 OTA 数据

真实采集的已知边界（实测确认）：
  - OTA 搜索把自家酒店当作中心点排除，结果里不含自己，所以
    current_price / occupancy_rate 必须由 config.MY_HOTEL 人工提供；
  - 采集是有头浏览器 + 可能的人工验证，一次约 1 分钟（不含详情页房型）。
"""
import os
import random
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from scrapers.adapters import to_competitors
from scrapers.runner import ScraperError, run_scraper
from scrapers.validator import DataValidationError, validate_result

class CtripCrawler:
    def __init__(self, username: str, password: str, hotel_id: str):
        self.username = username
        self.password = password
        self.hotel_id = hotel_id
        self.session = None
        print(f"✅ 携程爬虫初始化: 酒店ID {hotel_id}")
    
    def login(self) -> bool:
        """登录携程后台"""
        print(f"🔐 登录携程账号: {self.username}")
        # TODO: 实现真实的登录逻辑
        # 使用 Selenium 或 requests 模拟登录
        print("✅ 登录成功（模拟）")
        return True
    
    def get_my_hotel_data(self) -> Optional[Dict]:
        """获取自己酒店的数据"""
        print("📊 获取酒店数据...")
        
        # TODO: 从携程后台抓取真实数据
        # 当前返回模拟数据用于测试
        
        today = datetime.now().date()
        
        # 模拟数据（后续替换为真实抓取）
        occupancy_rate = random.uniform(30, 70)
        current_price = random.uniform(110, 140)
        
        data = {
            "date": str(today),
            "hotel_id": self.hotel_id,
            "hotel_name": "格林豪泰酒店（海口店）",  # TODO: 从后台获取
            "current_price": round(current_price, 2),
            "occupancy_rate": round(occupancy_rate, 2),
            "rooms_available": random.randint(10, 40),
            "rooms_total": 50,
            "revenue_today": 0  # TODO: 获取今日收入
        }
        
        print(f"  当前价格: ¥{data['current_price']}")
        print(f"  入住率: {data['occupancy_rate']}%")
        print(f"  可用房间: {data['rooms_available']}/{data['rooms_total']}")
        
        return data
    
    def get_competitor_hotels(
        self,
        city: str,
        distance_km: float,
        price_min: float,
        price_max: float
    ) -> List[Dict]:
        """获取竞品酒店列表"""
        print(f"🔍 搜索竞品: {city} 周边{distance_km}km, ¥{price_min}-{price_max}")
        
        # TODO: 从携程搜索页面抓取真实竞品数据
        # 当前返回模拟数据
        
        competitors = [
            {
                "id": "001",
                "name": "如家酒店（海口滨海店）",
                "distance": 1.2,
                "price": random.uniform(price_min, price_max),
                "rating": 4.5,
                "reviews": 1200
            },
            {
                "id": "002",
                "name": "7天连锁（海口海甸岛店）",
                "distance": 1.8,
                "price": random.uniform(price_min, price_max),
                "rating": 4.3,
                "reviews": 890
            },
            {
                "id": "003",
                "name": "汉庭酒店（海口国贸店）",
                "distance": 2.1,
                "price": random.uniform(price_min, price_max),
                "rating": 4.4,
                "reviews": 1050
            },
            {
                "id": "004",
                "name": "速8酒店（海口火车站店）",
                "distance": 2.5,
                "price": random.uniform(price_min, price_max),
                "rating": 4.2,
                "reviews": 670
            },
            {
                "id": "005",
                "name": "锦江之星（海口龙华路店）",
                "distance": 2.9,
                "price": random.uniform(price_min, price_max),
                "rating": 4.6,
                "reviews": 1450
            }
        ]
        
        for comp in competitors:
            comp['price'] = round(comp['price'], 2)
            comp['date'] = str(datetime.now().date())
        
        print(f"✅ 找到 {len(competitors)} 家竞品酒店")
        for comp in competitors:
            print(f"  {comp['name']}: ¥{comp['price']} ({comp['distance']}km)")
        
        return competitors
    
    def adjust_price(self, new_price: float) -> bool:
        """调整酒店价格（携程后台）"""
        print(f"💰 调价请求: ¥{new_price}")
        
        # TODO: 实现真实的调价功能
        # 使用 Selenium 模拟点击携程后台调价按钮
        
        print("✅ 调价成功（模拟）")
        return True

class RealCtripCrawler:
    """调用独立采集脚本获取真实携程数据。"""

    def __init__(self, scraper_config: Dict, my_hotel: Dict,
                 competitor_filter: Optional[Dict] = None):
        self.cfg = scraper_config or {}
        self.my_hotel = my_hotel or {}
        self.filter = competitor_filter or {}
        self.last_stats: Dict = {}

    def login(self) -> bool:
        """公开列表页无需登录，这里只确认采集环境就绪。"""
        problems = []
        if not os.path.isfile(self.cfg.get("python") or ""):
            problems.append(f"采集解释器不存在: {self.cfg.get('python')}")
        if not os.path.isfile(self.cfg.get("script") or ""):
            problems.append(f"采集脚本不存在: {self.cfg.get('script')}")
        if problems:
            for line in problems:
                print(f"❌ {line}")
            print("   提示: 用 SCRAPER_PYTHON / CTRIP_SCRAPER_SCRIPT 指定正确路径。")
            return False
        print(f"✅ 采集环境就绪（公开页无需登录）: 渠道={self.cfg.get('channel')}")
        return True

    def get_my_hotel_data(self) -> Optional[Dict]:
        """自家酒店数据：来自 eBook 采集（当前价=整晚最低房型价，入住率=eBook 实时）。

        OTA 搜索不含自家酒店，但 eBooking 商家后台能拿到自家经营数据，
        所以不再依赖 .env 的人工配置。
        """
        from app.pricing import hotel_metrics

        m = hotel_metrics.resolve()
        price = m.get("current_price")
        occupancy = m.get("occupancy_rate")

        if price is None or occupancy is None:
            from app import config as cfg
            raise cfg.ConfigError(
                "真实模式下缺少自家酒店的当前价/入住率：请先采集 eBook 经营数据"
                "（ebook_collector.py 或中台「重新采集经营数据」）。"
                "本系统不再从 .env 读取 MY_HOTEL_CURRENT_PRICE / MY_HOTEL_OCCUPANCY_RATE。"
            )

        rooms_total = self.my_hotel.get("rooms_total")
        rooms_available = None
        if rooms_total:
            rooms_available = int(round(float(rooms_total) * (1 - float(occupancy) / 100.0)))

        data = {
            "date": str(datetime.now().date()),
            "hotel_id": self.my_hotel.get("hotel_id"),
            "hotel_name": self.my_hotel.get("name") or "自家酒店",
            "current_price": round(float(price), 2),
            "occupancy_rate": round(float(occupancy), 2),
            "rooms_available": rooms_available,
            "rooms_total": int(rooms_total) if rooms_total else None,
            "revenue_today": 0,
        }
        print(f"  当前价格: ¥{data['current_price']}（eBook 采集）")
        print(f"  入住率: {data['occupancy_rate']}%（eBook 采集）")
        return data

    def get_competitor_hotels(self, city: str, distance_km: float,
                              price_min: float = None, price_max: float = None,
                              checkin: str = None, checkout: str = None) -> List[Dict]:
        """抓取竞品酒店。

        price_min / price_max 在真实模式下不参与过滤：实测同城周边价格跨度可达
        63~601 元，用固定区间会直接清空结果。价格分层交给算法层处理。
        """
        place = self.my_hotel.get("name") or ""
        if not place:
            from app import config as cfg
            raise cfg.ConfigError("真实模式需要 MY_HOTEL_NAME 作为采集中心点。")

        checkin = checkin or str(datetime.now().date())
        checkout = checkout or str(datetime.now().date() + timedelta(days=1))
        distance_m = int(round(float(distance_km) * 1000))

        print(f"🔍 调用携程采集脚本: 中心点={place} 城市={city} 距离<={distance_m}m")
        raw = run_scraper(self.cfg, place, city, distance_m, checkin, checkout)

        try:
            validated = validate_result(
                raw,
                expected_city=city,
                expected_place=place,
                profile_hint=self.cfg.get("profile", ""),
            )
        except DataValidationError as exc:
            # 把采集脚本的日志尾巴带出来，否则「0 家」这种结果无法定位原因
            log_tail = (raw.get("_stdout") or "").strip()[-800:]
            if log_tail:
                raise DataValidationError(f"{exc}\n\n采集脚本日志（末尾）:\n{log_tail}") from exc
            raise
        self.last_stats = validated["stats"]
        stats = self.last_stats
        print(
            f"✅ 采集校验通过: 原始 {stats['total_raw']} 条 → 有效 {stats['total_valid']} 条"
            f"（无价格丢弃 {stats['dropped_no_price']}，重复丢弃 {stats['dropped_duplicate']}）"
        )

        competitors = to_competitors(validated["hotels"], checkin)

        if price_min is not None and price_max is not None:
            in_range = [c for c in competitors if price_min <= c["price"] <= price_max]
            if len(in_range) < len(competitors):
                print(
                    f"ℹ️  价格区间 {price_min:.0f}-{price_max:.0f} 元仅供参考，未用于过滤："
                    f"区间内有 {len(in_range)} 家，实际保留全部 {len(competitors)} 家。"
                )

        return competitors

    def adjust_price(self, new_price: float) -> bool:
        """真实调价需要商家后台权限，当前 MVP 不实现。"""
        print(f"⚠️  真实模式不支持自动调价（请求价 ¥{new_price}）。")
        return False


def create_crawler(config: Dict, data_source: Optional[str] = None):
    """按 config.DATA_SOURCE 创建采集器：real = 真实脚本，mock = 模拟数据。"""
    if data_source is None:
        from app import config as cfg
        data_source = cfg.DATA_SOURCE

    if (data_source or "mock").strip().lower() == "real":
        from app import config as cfg
        return RealCtripCrawler(cfg.SCRAPER_CONFIG, cfg.MY_HOTEL, cfg.COMPETITOR_FILTER)

    return CtripCrawler(
        username=config['username'],
        password=config['password'],
        hotel_id=config['hotel_id']
    )
