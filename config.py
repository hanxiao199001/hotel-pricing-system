"""
酒店智能调价系统 - 配置文件

敏感信息（携程账号、密码、酒店ID、通知手机号等）不再硬编码在本文件中，
统一从环境变量读取。请复制 .env.example 为 .env 并填入真实值。
启动时调用 validate_startup_config()：必填项缺失或为空则立即失败。
"""
import os

# 可选：如安装了 python-dotenv，自动加载项目根目录的 .env 文件
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 携程账号（从环境变量读取，切勿写死在代码里）
CTRIP_CONFIG = {
    "username": os.environ.get("CTRIP_USERNAME", ""),
    "password": os.environ.get("CTRIP_PASSWORD", ""),
    "hotel_id": os.environ.get("CTRIP_HOTEL_ID", "")  # 酒店ID
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

# 微信/WhatsApp 通知（接收人从环境变量读取）
WECHAT_CONFIG = {
    "enabled": os.environ.get("NOTIFY_ENABLED", "true").lower() == "true",
    "recipient": os.environ.get("NOTIFY_RECIPIENT", "")
}

# 定时任务
SCHEDULE_CONFIG = {
    "check_interval_hours": 1,  # 每小时检查一次
    "auto_adjust": False        # 建议模式（手动确认）
}

# 数据库（默认放在项目目录下，可用环境变量覆盖）
DATABASE = {
    "path": os.environ.get("PRICING_DB_PATH", os.path.join(_BASE_DIR, "pricing.db"))
}

# 启动必填项：字段名 -> 对应环境变量。空白与缺失同等对待。
_REQUIRED_CTRIP_FIELDS = (
    ("username", "CTRIP_USERNAME"),
    ("password", "CTRIP_PASSWORD"),
    ("hotel_id", "CTRIP_HOTEL_ID"),
)


class ConfigError(RuntimeError):
    """启动配置不完整，拒绝带着空凭据继续运行。"""


def _is_blank(value) -> bool:
    return value is None or str(value).strip() == ""


def validate_startup_config(ctrip_config=None, wechat_config=None):
    """检查爬虫与通知所需配置。缺失或为空时抛出 ConfigError。

    未传入参数时检查本模块已加载的配置（即进程环境变量 / .env）。
    NOTIFY_ENABLED 为真（默认即为真）时，NOTIFY_RECIPIENT 也必填。
    """
    ctrip = CTRIP_CONFIG if ctrip_config is None else ctrip_config
    wechat = WECHAT_CONFIG if wechat_config is None else wechat_config

    missing = [
        env_name
        for field, env_name in _REQUIRED_CTRIP_FIELDS
        if _is_blank(ctrip.get(field))
    ]
    if wechat.get("enabled") and _is_blank(wechat.get("recipient")):
        missing.append("NOTIFY_RECIPIENT")

    if not missing:
        return

    hint = "请在环境变量或 .env 中填写这些值（参考 .env.example）。"
    if "NOTIFY_RECIPIENT" in missing:
        hint += " 若不需要通知，请设置 NOTIFY_ENABLED=false。"
    raise ConfigError(f"缺少必填环境变量: {', '.join(missing)}。{hint}")
