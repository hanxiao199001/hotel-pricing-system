"""
酒店智能调价系统 - 配置文件
"""

# 携程账号
CTRIP_CONFIG = {
    "username": "300130",
    "password": "bk300130",
    "hotel_id": "300130"  # 酒店ID
}

# 竞品筛选条件
COMPETITOR_FILTER = {
    "city": "海口",
    "distance_km": 3,         # 周边3公里
    "price_min": 100,         # 最低价100元
    "price_max": 150,         # 最高价150元
    "exclude_self": True      # 排除自己
}

# 定价策略
PRICING_STRATEGY = {
    "low_occupancy_threshold": 30,    # 低入住率阈值(%)
    "high_occupancy_threshold": 60,   # 高入住率阈值(%)
    "low_occupancy_discount": 10,     # 低入住率降价幅度(元)
    "high_occupancy_premium": 10,     # 高入住率涨价幅度(元)
    "follow_competitor_gap": 5,       # 跟随竞品差价(元)
    "min_price": 100,                 # 最低价保护
    "max_price": 180                  # 最高价限制
}

# 微信通知（使用 Clawdbot message 工具）
WECHAT_CONFIG = {
    "enabled": True,
    "recipient": "+8613907574397"  # 老韩的 WhatsApp（可以用 message 工具）
}

# 定时任务
SCHEDULE_CONFIG = {
    "check_interval_hours": 1,  # 每小时检查一次
    "auto_adjust": False        # 建议模式（手动确认）
}

# 数据库
DATABASE = {
    "path": "/root/hotel-pricing/pricing.db"
}
