"""
酒店智能调价系统 - 配置文件

敏感信息（携程账号、密码、酒店ID、通知手机号等）不再硬编码在本文件中，
统一从环境变量读取。请复制 .env.example 为 .env 并填入真实值。
启动时调用 validate_startup_config()：必填项缺失或为空则立即失败。

数据来源由 DATA_SOURCE 控制：
  mock = 内置模拟数据（默认，用于开发与测试）
  real = 调用携程采集脚本抓取真实 OTA 数据
"""
import os
import sys
from datetime import time as _clock_time

# 项目根目录 = 本文件（app/config.py）的上级目录。所有路径（.env、data/、
# 采集脚本、pricing.db）都相对项目根目录推导，与启动目录无关。
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ENV_PATH = os.path.join(_BASE_DIR, ".env")

# 记录 .env 的加载方式，便于启动失败时诊断
DOTENV_LOADED_BY = None


class ConfigError(RuntimeError):
    """启动配置不完整，拒绝带着空凭据/空参数继续运行。"""


def _is_blank(value) -> bool:
    return value is None or str(value).strip() == ""


def _parse_env_file(path: str):
    """极简 .env 解析：python-dotenv 不可用时的兜底，保证任何解释器都能读到配置。"""
    values = {}
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export "):].strip()
                if "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key:
                    values[key] = val
    except OSError:
        return {}
    return values


def _load_env():
    """加载项目根目录的 .env（绝对路径，不受启动目录影响）。"""
    global DOTENV_LOADED_BY
    if not os.path.isfile(_ENV_PATH):
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        # 没装 python-dotenv：用内置解析，避免 .env 被静默忽略
        for key, val in _parse_env_file(_ENV_PATH).items():
            os.environ.setdefault(key, val)
        DOTENV_LOADED_BY = "builtin"
        return
    load_dotenv(dotenv_path=_ENV_PATH, override=False)
    DOTENV_LOADED_BY = "python-dotenv"


def _opt_float(env_name: str, default=None):
    """读取可选数字型环境变量；格式错误时明确报错，避免静默用错值。"""
    raw = (os.environ.get(env_name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"环境变量 {env_name}={raw!r} 不是合法数字。") from exc


_load_env()

# ---------------------------------------------------------------------------
# 数据来源
# ---------------------------------------------------------------------------
# mock = 内置模拟数据；real = 调用携程采集脚本
DATA_SOURCE = (os.environ.get("DATA_SOURCE", "mock") or "mock").strip().lower()

# 携程商家账号（真实采集公开页时并不需要，仅保留兼容旧配置）
CTRIP_CONFIG = {
    "username": os.environ.get("CTRIP_USERNAME", ""),
    "password": os.environ.get("CTRIP_PASSWORD", ""),
    "hotel_id": os.environ.get("CTRIP_HOTEL_ID", "")  # 酒店ID
}

# 自己酒店的基础信息。当前价与入住率不再人工配置：
# 由 eBooking 经营数据采集提供（见 hotel_metrics.py）。
MY_HOTEL = {
    "name": (os.environ.get("MY_HOTEL_NAME") or "").strip(),
    "city": (os.environ.get("MY_HOTEL_CITY") or "海口").strip(),
    "rooms_total": _opt_float("MY_HOTEL_ROOMS_TOTAL"),
}

# 竞品筛选条件（price_min/price_max 只在 mock 模式生效；
# real 模式下只按距离筛选，价格分层交给算法层处理）
COMPETITOR_FILTER = {
    "city": MY_HOTEL["city"],
    "distance_km": float(os.environ.get("COMPETITOR_DISTANCE_KM", "2") or 2),
    "price_min": 100,         # 仅 mock 模式：随机价格下限
    "price_max": 150,         # 仅 mock 模式：随机价格上限
    "exclude_self": True      # 排除自己
}

# 真实采集配置（DATA_SOURCE=real 时生效）
SCRAPER_CONFIG = {
    "platform": "ctrip",
    "script": os.environ.get(
        "CTRIP_SCRAPER_SCRIPT",
        r"D:\TeleAgentWrork\酒店AI\ctrip_hotel_scraper.py",
    ),
    # 采集脚本依赖 playwright，运行在独立的 venv（Python 3.12）里
    "python": os.environ.get(
        "SCRAPER_PYTHON",
        r"D:\TeleAgentWrork\酒店AI\.venv\Scripts\python.exe",
    ),
    # 浏览器 profile 目录。必须包含携程登录态：
    # 未登录时携程列表页只显示「登录以查看会员价」，价格元素是空 div，
    # 脚本能采到酒店名和房型却采不到价格（实测 price 全为 0）。
    # profiles/default 是采集脚本的默认目录，已登录携程，可直接复用。
    "profile": os.environ.get(
        "SCRAPER_PROFILE",
        r"D:\TeleAgentWrork\酒店AI\profiles\default",
    ),
    # 本机没装 Chrome，只有 Edge
    "channel": os.environ.get("SCRAPER_CHANNEL", "edge"),
    "cdp_port": int(os.environ.get("SCRAPER_CDP_PORT", "9222") or 9222),
    "collect_rooms": (os.environ.get("SCRAPER_COLLECT_ROOMS", "false").lower() == "true"),
    "login_wait": int(os.environ.get("SCRAPER_LOGIN_WAIT", "60") or 60),
    "timeout": int(os.environ.get("SCRAPER_TIMEOUT", "900") or 900),
}

# 点评（口碑分析 / 差评预警）
#
# 由本项目的 ebook_reviews_collector.py 从携程 eBooking 商家后台「订单点评」
# 采集产出，覆盖携程/去哪儿/同程/智行全渠道真实点评，只读（不写后台）。
# 相比原公开点评采集，后台没有的字段（出游类型/IP属地/有用数）留空，不编造。
#
# 采集结果写到项目内的 data/reviews/ctrip_reviews.json（不覆盖原始外部快照）；
# 首次没有新快照时回退读 legacy_path 里的历史文件。
REVIEWS_CONFIG = {
    "path": os.environ.get(
        "REVIEWS_JSON",
        os.path.join(_BASE_DIR, "data", "reviews", "ctrip_reviews.json"),
    ),
    "legacy_path": os.environ.get(
        "REVIEWS_LEGACY_JSON",
        r"D:\TeleAgentWrork\酒店AI\ctrip_hotel_reviews.json",
    ),
    "bad_rating": 3.0,      # 评分 <= 此值算差评
    "good_rating": 4.0,     # 评分 >= 此值算好评
    "monthly_window": 12,   # 月度趋势展示最近几个月

    # 采集脚本与运行环境（脚本依赖 playwright，必须用独立的 venv 解释器）
    # 从携程 eBooking 后台采集（只读、复用已登录 ebooking 的 profile）。
    "script": os.environ.get(
        "REVIEWS_SCRIPT",
        os.path.join(_BASE_DIR, "collectors", "ebook_reviews_collector.py"),
    ),
    "python": os.environ.get(
        "SCRAPER_PYTHON",
        r"D:\TeleAgentWrork\酒店AI\.venv\Scripts\python.exe",
    ),
    # 与竞品采集共用同一个 profile 和调试端口：
    # 同一 user-data-dir 不能同时开两个实例，共用端口才能复用已登录的浏览器。
    "profile": os.environ.get(
        "SCRAPER_PROFILE",
        r"D:\TeleAgentWrork\酒店AI\profiles\default",
    ),
    "channel": os.environ.get("SCRAPER_CHANNEL", "edge"),
    "cdp_port": int(os.environ.get("REVIEWS_CDP_PORT", "9222") or 9222),
    # 登录/风控等待。采集需要可见浏览器，等待期是在等人手动扫码/拖滑块，
    # 后台任务不宜无限等，默认 3 分钟。
    "login_wait": int(os.environ.get("REVIEWS_LOGIN_WAIT", "180") or 180),
    "page_size": int(os.environ.get("REVIEWS_PAGE_SIZE", "50") or 50),
    # 实测 pageSize=50 能被接受时，站内 2659 条可翻页 1975 条 ≈ 40 页；
    # 若接口钳到 10 条/页则要 ~198 页（脚本会自动降级并重拉第 1 页）。
    # 上限 220 覆盖两种情形；翻页间隔 0.6–1.4 秒。
    "max_pages": int(os.environ.get("REVIEWS_MAX_PAGES", "220") or 220),
    "timeout": int(os.environ.get("REVIEWS_TIMEOUT", "1800") or 1800),
    # 秒退重试：连续两次采集时，上一次的浏览器可能还没释放 profile，
    # 脚本会在接管 CDP 后立刻退出。实测重试一次即可成功。
    "attempts": int(os.environ.get("REVIEWS_ATTEMPTS", "2") or 2),
    "fast_fail_seconds": 25,
    # 只保留早于该日期的点评。默认明天 = 含今天为止的全部点评。
    # 设成 REVIEWS_BEFORE="" 表示不过滤。
    "before_days": int(os.environ.get("REVIEWS_BEFORE_DAYS", "1") or 1),
    "before": os.environ.get("REVIEWS_BEFORE", None),
}

# 携程 eBooking 商家后台 · 经营数据采集（只读）
#
# 覆盖系统原来拿不到的经营指标：入住率、出租率、平均卖价 ADR、成交率、
# 已售房、各房型挂牌价与库存、订单。数据来源是 ebooking 后台登录后的
# 只读查询接口（经营报告 / 房价房态 / 首页）。
#
# 采集脚本跑 playwright，必须用独立的 venv（SCRAPER_PYTHON）执行；
# profile 需已登录 ebooking（没登录会等 login_wait 秒让人工扫码/验证）。
# 采集器只监听页面自然发出的只读接口响应，不主动 POST 重放，不写任何后台。
EBOOK_CONFIG = {
    "script": os.environ.get(
        "EBOOK_SCRIPT",
        os.path.join(_BASE_DIR, "collectors", "ebook_collector.py"),
    ),
    "python": os.environ.get(
        "SCRAPER_PYTHON",
        r"D:\TeleAgentWrork\酒店AI\.venv\Scripts\python.exe",
    ),
    # 与点评/竞品共用同一个 profile 与调试端口（同一 user-data-dir 不能开两个实例）
    "profile": os.environ.get(
        "SCRAPER_PROFILE",
        r"D:\TeleAgentWrork\酒店AI\profiles\default",
    ),
    "channel": os.environ.get("SCRAPER_CHANNEL", "edge"),
    "cdp_port": int(os.environ.get("EBOOK_CDP_PORT", "9222") or 9222),
    # 登录/风控等待：需要可见浏览器让人扫码/拖滑块
    "login_wait": int(os.environ.get("EBOOK_LOGIN_WAIT", "180") or 180),
    # 每个页面打开后等接口返回的秒数
    "page_wait": float(os.environ.get("EBOOK_PAGE_WAIT", "8") or 8),
    "timeout": int(os.environ.get("EBOOK_TIMEOUT", "600") or 600),
    # 秒退重试：连续两次采集时，上一次浏览器可能还没释放 profile
    "attempts": int(os.environ.get("EBOOK_ATTEMPTS", "2") or 2),
    "fast_fail_seconds": 25,
    # 采集结果落盘路径（最终快照）
    "path": os.environ.get(
        "EBOOK_JSON",
        os.path.join(_BASE_DIR, "data", "ebook", "ctrip_ebook.json"),
    ),
}

# 房型口碑 → 定价参考（见 room_reputation.py）（见 room_reputation.py）
#
# 这里只影响「判断」，不影响「算价」。建议价仍由 PRICING_STRATEGY 决定，
# 本组参数回答的是「这一档有没有口碑支撑」以及「要不要留让价缓冲」。
ROOM_PRICING = {
    # 少于这么多条点评的档位不下结论。实测套房只有 15 条、云杉优眠精选 2 条，
    # 凭十几条点评说「套房口碑好」是不负责的。
    "min_sample": int(os.environ.get("ROOM_MIN_SAMPLE", "30") or 30),

    # 差评率相对整体的倍数。用倍数而不是绝对分：5 分制里 4.90 与 4.85 的差别
    # 基本是噪声，而差评率 1.3% 与 5.3% 是 4 倍差距，后者才值得动作。
    "strong_bad_rate_ratio": float(os.environ.get("ROOM_STRONG_RATIO", "1.0") or 1.0),
    "weak_bad_rate_ratio": float(os.environ.get("ROOM_WEAK_RATIO", "2.0") or 2.0),

    # 「溢价承压」时建议再让价的百分比区间。只是给人看的参考区间，
    # 不会自动并入建议价——调价仍需人工拍板。
    "weak_hint_range": [3, 5],

    # 统计窗口：只统计快照内最近 N 个自然月（基准取快照最新日期，不取今天），
    # 避免多年前的旧差评拖累当前判定。0 = 全量（默认，行为与历史一致）。
    # 启用后样本量会变小，部分档位可能跌破 min_sample 而判「样本不足」——
    # 这是设计使然：宁可少表态，不用旧数据猜。
    "window_months": int(os.environ.get("ROOM_WINDOW_MONTHS", "0") or 0),

    # 起价对应哪一档。留空则自动取点评量最大的一档
    # （OTA 最低价房型通常就是基础房型，点评也最多）。
    "base_tier": (os.environ.get("MY_HOTEL_BASE_ROOM_TIER") or "").strip(),
}

# 大模型（可选，仅用于「差评回复草稿」的自然语言生成）
#
# 三条边界，写在这里也是给以后的人看：
#   1. 只读：模型只拿到一条点评的文本和房型，碰不到数据库、碰不到价格；
#   2. 不参与算价：建议价永远由 algorithm.PricingAlgorithm 算，本配置改不了任何数字；
#   3. 可降级：没配 key 时全部走 reply_draft.py 的模板，功能照常可用。
# 接口按 OpenAI 兼容格式（/chat/completions），国内多数服务商都支持。
LLM_CONFIG = {
    "base_url": (os.environ.get("LLM_BASE_URL") or "").strip(),
    "api_key": (os.environ.get("LLM_API_KEY") or "").strip(),
    "model": (os.environ.get("LLM_MODEL") or "").strip(),
    "timeout": int(os.environ.get("LLM_TIMEOUT", "60") or 60),
    "max_tokens": int(os.environ.get("LLM_MAX_TOKENS", "700") or 700),
    "temperature": float(os.environ.get("LLM_TEMPERATURE", "0.4") or 0.4),
}

# 点评归因的模型层（可选）。
#
# 与上面的「差评回复草稿」是两个用途，边界也不同：
#   - 统计数字和「硬件 vs 服务」的归因结论仍然由 reviews_analytics 算，
#     模型只负责把差评聚成「整改动作」级别的问题、给整改建议；
#   - 每条依据都要能对上点评原文，对不上就丢弃（见 reviews_llm._validate）；
#   - 没配 key / 超时 / 输出不合规 → 页面回退到规则文案，功能照常。
#
# 结果按 (快照路径, mtime, size, 模型名, prompt 版本) 落盘缓存。
# 这是可复现性的来源：同一份快照永远同一份结论，不会刷一次页面变一次。
REVIEWS_LLM_CONFIG = {
    "enabled": os.environ.get("REVIEWS_LLM_ENABLED", "true").lower() == "true",
    "cache": os.environ.get(
        "REVIEWS_LLM_CACHE",
        os.path.join(_BASE_DIR, "data", "cache", "reviews_llm.json"),
    ),
    # 归因要稳定，温度压到 0.2；配合缓存后同一份快照输出完全一致
    "temperature": float(os.environ.get("REVIEWS_LLM_TEMPERATURE", "0.2") or 0.2),
    # 输出是结构化 JSON，比回复草稿长。实测 1600 token 只能写 1300 字左右，
    # 4 个问题各带 3 条依据就会被截断；3000 留足余量。
    "max_tokens": int(os.environ.get("REVIEWS_LLM_MAX_TOKENS", "3000") or 3000),
    # 页面首次生成是同步调用，超时设短一点，宁可这次不出结论也别把页面卡死
    "timeout": int(os.environ.get("REVIEWS_LLM_TIMEOUT", "25") or 25),
    # 全量 1975 条里只有 30 条差评，全部喂得下；上限只为防快照变大
    "max_bad_samples": int(os.environ.get("REVIEWS_LLM_MAX_SAMPLES", "40") or 40),
}

# 定价策略
#
# 核心变化：不再用「竞品均价 ± 固定金额」，改为「同档竞品中位价 × 比例」。
# 原因是真实数据下原逻辑会崩掉：
#   1. 算术均值被少数高价酒店拉偏——48 家里索菲特 ¥697 一家就把均值抬到 242，
#      而中位只有 220；
#   2. 固定金额（-10/-5/+10 元）对 ¥100 和 ¥600 的酒店力度完全不同；
#   3. 原来 max_price=180 的绝对硬顶会让所有 >=185 元的酒店建议价都变成 180
#      （实测当前价 ¥367 被建议降到 ¥180，砍掉 51%）。
PRICING_STRATEGY = {
    # 入住率分区
    "low_occupancy_threshold": 30,      # 低于此值按降价策略
    "high_occupancy_threshold": 60,     # 高于此值按涨价策略

    # 各区间相对「同档中位价」的调整比例
    "low_occupancy_ratio": -0.08,       # 入住率低 → 低于中位价 8%，让价吸客
    "normal_occupancy_ratio": -0.03,    # 入住率正常 → 略低于中位价 3%
    "high_occupancy_ratio": 0.06,       # 入住率高 → 高于中位价 6%，吃溢价

    # 同档竞品筛选：以「当前价」为中心的区间。
    # 不设价格带的话会把 ¥63 的客栈和 ¥697 的五星放在一起平均，毫无意义。
    "band_low_ratio": 0.6,
    "band_high_ratio": 1.6,
    "band_min_samples": 3,              # 同档不足此数则退回用全部竞品，避免样本过小

    # 单次调价幅度上限（占当前价的比例）。酒店定价不宜一天大起大落，
    # 即使目标价很远也分多步走。
    "max_step_ratio": 0.15,

    # 建议价相对同档中位价的硬边界（替代原来的绝对 min/max 硬顶）
    "floor_ratio": 0.6,
    "cap_ratio": 1.4,

    # 绝对价格保护
    "min_price": 80,                    # 成本底线，任何建议不低于此
    "max_price": None,                  # 不设绝对上限，改用上面的相对 cap

    # 变化小于该金额则不调价（避免频繁微调）
    "min_change": 5,
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


def _parse_hhmm(value):
    """解析 "HH:MM" 形式的每日时点。格式错误启动即报错——
    定时点写错不该静默用默认值，否则机制会在错误的时间段悄悄跑。"""
    raw = (value or "").strip()
    try:
        hh, mm = raw.split(":")
        return _clock_time(int(hh), int(mm))
    except (ValueError, TypeError) as exc:
        raise ConfigError(
            f"DAILY_COLLECT_AT={value!r} 不是合法的 HH:MM（示例：08:00）。"
        ) from exc


# 每日自动采集（webapp 内置，实现见 webapp/daily.py）
#
# 语义：每天价格、点评各自动采集一次，统一不早于 DAILY_COLLECT_AT：
#   - 中台开着没人动 → 到点自动执行（8 点后中台才启动的，启动后
#     poll_seconds 内补采当天这次）；
#   - 到点之后当天首次有人进入中台 → 立即触发。
# 判重记录在 daily_collects 表（含手动「立即采集」），当天采过不再重复。
# DATA_SOURCE=mock 自动跳过，避免把模拟数据写进真实库。
# 默认 max_attempts=1：自动尝试失败当天不重试——频控、登录失效都需要
# 人工判断，盲目重试只会加剧限流；次日自动重新开始。
DAILY_COLLECT_CONFIG = {
    "enabled": os.environ.get("DAILY_COLLECT_ENABLED", "true").lower() == "true",
    "at": (os.environ.get("DAILY_COLLECT_AT", "08:00") or "08:00").strip(),
    "poll_seconds": int(os.environ.get("DAILY_COLLECT_POLL_SECONDS", "30") or 30),
    "max_attempts": int(os.environ.get("DAILY_COLLECT_MAX_ATTEMPTS", "1") or 1),
    "cooldown_minutes": int(os.environ.get("DAILY_COLLECT_COOLDOWN_MINUTES", "60") or 60),
}
DAILY_COLLECT_CONFIG["at_time"] = _parse_hhmm(DAILY_COLLECT_CONFIG["at"])

# 数据库（默认放在项目目录下，可用环境变量覆盖）
DATABASE = {
    "path": os.environ.get("PRICING_DB_PATH", os.path.join(_BASE_DIR, "pricing.db"))
}

# mock 模式启动必填项：字段名 -> 对应环境变量。空白与缺失同等对待。
_REQUIRED_CTRIP_FIELDS = (
    ("username", "CTRIP_USERNAME"),
    ("password", "CTRIP_PASSWORD"),
    ("hotel_id", "CTRIP_HOTEL_ID"),
)

# real 模式启动必填项：名称用于定位竞品、城市用于搜索。
# 当前价/入住率已改为从 eBook 采集取，不再要求人工配置。
_REQUIRED_MY_HOTEL_FIELDS = (
    ("name", "MY_HOTEL_NAME"),
    ("city", "MY_HOTEL_CITY"),
)


def _env_diagnostics() -> str:
    """配置缺失时的现场信息，用来区分「.env 没读到」和「值没填」。"""
    lines = [
        f"数据来源: {DATA_SOURCE}",
        f"当前解释器: {sys.executable}",
        f"当前工作目录: {os.getcwd()}",
    ]
    if os.path.isfile(_ENV_PATH):
        lines.append(f"已找到 .env: {_ENV_PATH}")
        lines.append(f".env 加载方式: {DOTENV_LOADED_BY or '未加载'}")
        keys = sorted(_parse_env_file(_ENV_PATH).keys())
        lines.append(f".env 识别到的键: {', '.join(keys) if keys else '（空文件或格式有误）'}")
    else:
        lines.append(f"未找到 .env: {_ENV_PATH}")
    return "\n".join("  " + line for line in lines)


def validate_startup_config(ctrip_config=None, wechat_config=None, data_source=None):
    """检查启动所需配置。缺失或为空时抛出 ConfigError。

    mock 模式校验携程账号三件套（沿用旧行为）。
    real 模式改为校验自己酒店参数与采集脚本路径——真实采集公开页不需要账号。
    未传入参数时检查本模块已加载的配置（即进程环境变量 / .env）。
    """
    source = (data_source or DATA_SOURCE or "mock").strip().lower()
    ctrip = CTRIP_CONFIG if ctrip_config is None else ctrip_config
    wechat = WECHAT_CONFIG if wechat_config is None else wechat_config

    missing = []
    if source == "real":
        missing += [
            env_name
            for field, env_name in _REQUIRED_MY_HOTEL_FIELDS
            if _is_blank(MY_HOTEL.get(field))
        ]
    else:
        missing += [
            env_name
            for field, env_name in _REQUIRED_CTRIP_FIELDS
            if _is_blank(ctrip.get(field))
        ]

    if wechat.get("enabled") and _is_blank(wechat.get("recipient")):
        missing.append("NOTIFY_RECIPIENT")

    if not missing:
        return

    if source == "real":
        hint = (
            "请在 .env 中填写 MY_HOTEL_NAME / MY_HOTEL_CITY。"
            "当前价与入住率由 eBook 经营数据采集提供，无需人工填写。"
        )
    else:
        hint = "请在环境变量或 .env 中填写这些值（参考 .env.example）。"
    if "NOTIFY_RECIPIENT" in missing:
        hint += " 若不需要通知，请设置 NOTIFY_ENABLED=false。"
    raise ConfigError(
        f"缺少必填配置: {', '.join(missing)}。{hint}\n诊断信息:\n{_env_diagnostics()}"
    )
