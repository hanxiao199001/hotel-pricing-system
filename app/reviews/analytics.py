"""携程点评快照的只读统计。

数据来自采集脚本 `ctrip_hotel_reviews.py` 产出的 JSON 快照。本模块只做计算：
不访问网络、不写数据库、不修改快照文件。

口径（可在 config.REVIEWS_CONFIG 调整）：
  好评 = rating >= good_rating(4.0)
  中评 = bad_rating(3.0) < rating < good_rating
  差评 = rating <= bad_rating
  星级 = rating 四舍五入（0.5~5 → 1~5）

注意：一条差评可能同时命中多个主题，所以主题计数之和会大于差评条数。
"""
import collections
import json
import os
import re
import statistics
from datetime import datetime, timedelta

from app import config as cfg

# 差评主题归类。关键词是「包含即命中」的朴素匹配，不做分词——
# 点评里同一件事的说法很集中（老旧/霉味/早餐），朴素匹配足够用，
# 也便于你直接改这张表来适配其他酒店。
NEGATIVE_THEMES = [
    ("设施老旧", ["老旧", "老久", "陈旧", "老古董", "装修", "设施", "设备",
                  "电视", "马桶", "吹风机", "残旧", "翻新", "旧", "残"]),
    ("卫生异味", ["霉味", "霉潮", "发霉", "味道", "臭", "卫生", "脏", "污渍",
                  "洗漱杯", "垃圾袋", "布草", "换洗"]),
    ("服务态度", ["服务", "前台", "态度", "不负责", "投诉", "没人", "懒"]),
    ("早餐餐饮", ["早餐", "餐饮", "自助", "餐食", "食物", "难吃"]),
    ("性价比", ["不值", "性价比", "价格", "贵"]),
    ("隔音噪音", ["隔音", "吵", "噪音", "通风口"]),
    ("泳池健身", ["泳池", "健身", "会所", "桑拿", "SPA"]),
    ("发票开票", ["开票", "发票"]),
]

# 好评提及。只用实测确认过的词——曾误以为「海景」是卖点，
# 实测 1898 条点评里只有 3 条提到海景（房型是「滨海城景/城市景致」）。
GOOD_KEYWORDS = [
    ("服务", "服务"),
    ("房间/客房", "房间|客房"),
    ("前台", "前台"),
    ("干净卫生", "干净|整洁|卫生"),
    ("位置便利", "位置|交通|出行|便利"),
    ("设施", "设施"),
    ("环境", "环境"),
    ("性价比", "性价比"),
]

DIMENSION_LABELS = {
    "location": "位置",
    "facility": "设施",
    "service": "服务",
    "room": "客房",
}

# 点评里有相当比例的繁体（港澳台与海外客人）。不做这一步，
# 「又舊又殘又沒升級」这类差评会全部落到「未归类」。
_TRAD_TO_SIMP = {
    "舊": "旧", "殘": "残", "陳": "陈", "設": "设", "備": "备", "間": "间",
    "務": "务", "衛": "卫", "難": "难", "開": "开", "發": "发", "價": "价",
    "貴": "贵", "錢": "钱", "個": "个", "這": "这", "麼": "么", "說": "说",
    "們": "们", "沒": "没", "過": "过", "還": "还", "東": "东", "車": "车",
    "電": "电", "視": "视", "覺": "觉", "廣": "广", "場": "场", "濕": "湿",
    "熱": "热", "氣": "气", "種": "种", "類": "类", "質": "质", "體": "体",
    "驗": "验", "滿": "满", "應": "应", "該": "该", "裡": "里", "麵": "面",
    "點": "点", "廳": "厅", "樓": "楼", "層": "层", "臺": "台", "灣": "湾",
    "飯": "饭", "館": "馆",
}
_TRAD_TABLE = str.maketrans(_TRAD_TO_SIMP)


def _norm(text: str) -> str:
    """繁转简 + 去空白，用于关键词匹配（不改变展示原文）。"""
    return (text or "").translate(_TRAD_TABLE)

_cache = {"key": None, "data": None}


class ReviewsUnavailable(RuntimeError):
    """快照文件不存在或不可解析。"""


def snapshot_path() -> str:
    """当前应当读取的快照文件。

    优先读项目内 data/reviews 下的采集结果；还没有采集过时，
    回退到外部那份历史文件（旧脚本产出的快照），保证页面先有数据看。
    """
    primary = cfg.REVIEWS_CONFIG["path"]
    legacy = cfg.REVIEWS_CONFIG.get("legacy_path")
    if os.path.isfile(primary):
        return primary
    if legacy and os.path.isfile(legacy):
        return legacy
    return primary


def load(path=None, force=False):
    """读取快照，按 (路径, mtime, size) 缓存。3 MB 的文件没必要每次请求都解析。"""
    path = path or snapshot_path()
    try:
        stat = os.stat(path)
    except OSError as exc:
        raise ReviewsUnavailable(f"未找到点评快照: {path}") from exc

    key = (os.path.abspath(path), stat.st_mtime, stat.st_size)
    if not force and _cache["key"] == key:
        return _cache["data"]

    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise ReviewsUnavailable(f"点评快照无法解析: {path}（{exc}）") from exc

    if not isinstance(raw, dict) or not isinstance(raw.get("reviews"), list):
        raise ReviewsUnavailable(f"点评快照结构不符合预期（缺 reviews 数组）: {path}")

    raw["_snapshot_at"] = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
    _cache["key"] = key
    _cache["data"] = raw
    return raw


def _content(item) -> str:
    return (item.get("content") or "").strip()


def _avg(values):
    return round(statistics.mean(values), 2) if values else None


def _star(rating: float) -> int:
    return max(1, min(5, int(rating + 0.5)))


def _short(text: str, limit: int = 44) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _themes_of(text: str):
    norm = _norm(text)
    return [name for name, kws in NEGATIVE_THEMES if any(k in norm for k in kws)]


def summarize(data=None):
    """把快照算成一页统计结果。纯函数，输入相同则输出相同。"""
    data = load() if data is None else data
    reviews = [r for r in data.get("reviews", []) if isinstance(r, dict)]

    bad_max = cfg.REVIEWS_CONFIG["bad_rating"]
    good_min = cfg.REVIEWS_CONFIG["good_rating"]

    ratings = [float(r.get("rating") or 0) for r in reviews]
    bad = [r for r in reviews if float(r.get("rating") or 0) <= bad_max]
    good = [r for r in reviews if float(r.get("rating") or 0) >= good_min]
    neutral = [r for r in reviews if bad_max < float(r.get("rating") or 0) < good_min]

    # 星级分布：保证各档之和等于总数
    star_counter = collections.Counter(_star(x) for x in ratings)
    total = len(reviews) or 1
    star_dist = [
        {
            "star": s,
            "label": f"{s} 星",
            "count": star_counter.get(s, 0),
            "ratio": round(star_counter.get(s, 0) / total * 100, 1),
        }
        for s in (5, 4, 3, 2, 1)
    ]

    # 四维评分
    dims = collections.defaultdict(list)
    for r in reviews:
        for key, val in (r.get("rating_detail") or {}).items():
            if isinstance(val, (int, float)) and val:
                dims[key].append(float(val))
    dimensions = []
    for key, label in DIMENSION_LABELS.items():
        vals = dims.get(key) or []
        if not vals:
            continue
        dimensions.append({
            "key": key,
            "label": label,
            "avg": _avg(vals),
            "count": len(vals),
            "lowRatio": round(sum(1 for v in vals if v <= 3) / len(vals) * 100, 1),
        })
    # 均分从低到高排，最弱的一维排最前，方便一眼看到问题
    dimensions.sort(key=lambda d: d["avg"])

    # 差评主题
    theme_counter = collections.Counter()
    theme_examples = collections.defaultdict(list)
    for r in bad:
        text = _content(r)
        matched = _themes_of(text)
        for name in matched:
            theme_counter[name] += 1
            theme_examples[name].append({
                "date": (r.get("created_at") or "")[:10],
                "rating": float(r.get("rating") or 0),
                "text": _short(text, 60),
                "focus": len(matched),
            })
    themes = []
    for name, n in theme_counter.most_common():
        # 示例优先选「只命中这一个主题」且最短的那条，
        # 否则多个主题会展示同一条点评，看起来像复制粘贴。
        samples = sorted(theme_examples[name], key=lambda x: (x["focus"], len(x["text"])))
        themes.append({
            "label": name,
            "count": n,
            "ratio": round(n / (len(bad) or 1) * 100, 1),
            "examples": samples[:2],
        })

    # 按月趋势（近 N 个月，以数据里最新一条为基准）
    months = collections.defaultdict(list)
    for r in reviews:
        created = (r.get("created_at") or "")[:7]
        if created:
            months[created].append(float(r.get("rating") or 0))
    recent_months = sorted(months)[-cfg.REVIEWS_CONFIG["monthly_window"]:]
    monthly = [
        {
            "month": m,
            "count": len(months[m]),
            "avg": _avg(months[m]),
            "bad": sum(1 for x in months[m] if x <= bad_max),
        }
        for m in recent_months
    ]

    # 房型
    room_map = collections.defaultdict(list)
    for r in reviews:
        room_map[(r.get("room") or "未标注").strip()].append(r)
    rooms = [
        {
            "room": room,
            "count": len(items),
            "avg": _avg([float(x.get("rating") or 0) for x in items]),
            "bad": sum(1 for x in items if float(x.get("rating") or 0) <= bad_max),
        }
        for room, items in sorted(room_map.items(), key=lambda kv: -len(kv[1]))
    ]

    # 出游类型
    travel = collections.Counter((r.get("travel_type") or "未标注") for r in reviews)

    # 好评关键词提及率
    good_texts = [_norm(_content(r)) for r in good]
    good_mentions = [
        {
            "label": label,
            "count": sum(1 for t in good_texts if re.search(pat, t)),
        }
        for label, pat in GOOD_KEYWORDS
    ]
    for item in good_mentions:
        item["ratio"] = round(item["count"] / (len(good_texts) or 1) * 100, 1)
    good_mentions.sort(key=lambda x: -x["count"])

    # 差评明细（按时间倒序，最近的排最前）
    bad_rows = []
    for r in sorted(bad, key=lambda x: x.get("created_at") or "", reverse=True):
        text = _content(r)
        reply = (r.get("reply") or "").strip()
        bad_rows.append({
            "date": (r.get("created_at") or "")[:10],
            "rating": round(float(r.get("rating") or 0), 1),
            "room": r.get("room") or "—",
            "summary": _short(text, 46),
            "themes": "、".join(_themes_of(text)) or "未归类",
            "replied": "已回复" if reply else "未回复",
            "repliedTone": "" if reply else "warm",
            "full": text,
            "reply": reply,
        })

    unreplied = sum(1 for r in bad if not (r.get("reply") or "").strip())

    # 最近 90 天（以数据最新日期为基准，不是「今天」——快照是历史切片）
    dates = [(r.get("created_at") or "")[:10] for r in reviews if r.get("created_at")]
    latest = max(dates) if dates else ""
    earliest = min(dates) if dates else ""
    recent_bad = 0
    if latest:
        cutoff = (datetime.strptime(latest, "%Y-%m-%d") - timedelta(days=90)).strftime("%Y-%m-%d")
        recent_bad = sum(1 for row in bad_rows if row["date"] >= cutoff)

    worst = min(bad, key=lambda r: float(r.get("rating") or 0), default=None)
    worst_payload = None
    if worst is not None:
        worst_payload = {
            "date": (worst.get("created_at") or "")[:10],
            "rating": round(float(worst.get("rating") or 0), 1),
            "room": worst.get("room") or "—",
            "content": _short(_content(worst), 220),
            "reply": _short((worst.get("reply") or "").strip(), 160),
            "themes": _themes_of(_content(worst)),
        }

    total_count = int(data.get("total_count") or 0)
    return {
        "snapshot": {
            "path": snapshot_path(),
            "at": data.get("_snapshot_at"),
            "platform": (data.get("platform") or "ctrip").lower(),
            "hotel": data.get("hotel_name") or data.get("hotel"),
            "city": data.get("city"),
            "before": data.get("before"),
            "dateMin": earliest,
            "dateMax": latest,
            "totalCount": total_count,
            "collected": int(data.get("collected") or len(reviews)),
            "kept": int(data.get("kept") or len(reviews)),
            # 翻页元数据：用来向用户解释「为什么不是 100%」——
            # 站内总数与列表接口可翻页数是携程自己的两个口径，不是采集漏了。
            "pageCount": int(data.get("page_count") or 0),
            "pageSize": int(data.get("page_size") or 0),
            "coverage": round(len(reviews) / total_count * 100, 1) if total_count else None,
        },
        "overall": {
            "count": len(reviews),
            "avg": _avg(ratings),
            "good": len(good),
            "neutral": len(neutral),
            "bad": len(bad),
            "goodRate": round(len(good) / total * 100, 1),
            "badRate": round(len(bad) / total * 100, 1),
            "unreplied": unreplied,
            "recentBad": recent_bad,
        },
        "starDist": star_dist,
        "dimensions": dimensions,
        "themes": themes,
        "monthly": monthly,
        "rooms": rooms,
        "travelTypes": ([
            {"label": k, "count": v, "ratio": round(v / total * 100, 1)}
            for k, v in travel.most_common()
        ] if any((r.get("travel_type") or "").strip() for r in reviews) else []),
        "goodMentions": good_mentions,
        "badRows": bad_rows,
        "worst": worst_payload,
        "thresholds": {"badRating": bad_max, "goodRating": good_min},
    }


# ---------------------------------------------------------------------------
# 归因：短板落在硬件还是服务
# ---------------------------------------------------------------------------
# 这两个问题以前是写死一句话回答的（"口碑短板在硬件而非服务"）。但实测
# 1975 条快照发现这句话并不总成立：全量口径硬件 31 次、服务 11 次（2.82 倍），
# 切到近 6 个月却变成 2:2 打平，第一主题直接是「服务态度」——写死的结论会和
# 页面上的主题排序自相矛盾。所以改成分档判定，宁可说"样本不足"也不硬下结论。
HARDWARE_THEMES = ("设施老旧", "卫生异味", "隔音噪音", "泳池健身")
SERVICE_THEMES = ("服务态度",)

# 任一族提及数低于此值就不下结论。差评全量只有 30 条，切到近 6 个月只剩 4 条，
# 在 4 条上判断"短板在哪"等于抛硬币。门槛取 5 与 ROOM_PRICING.min_sample(30)
# 同一套思路：宁可少表态。注意这里的分母是"提及数"，天然比点评条数多。
ATTRIBUTION_MIN_MENTIONS = 5

# 差距达到 2 倍才算"显著偏向某一侧"，与 ROOM_PRICING.weak_bad_rate_ratio 同口径：
# 5 分制里 4.90 与 4.85 是噪声，2 倍的提及差距才值得动作。
ATTRIBUTION_STRONG_RATIO = 2.0


def apply_window(summary: dict, months: int) -> dict:
    """把统计切到「快照内最近 N 个自然月」，纯函数、不改原 dict。

    基准取快照内最新一条点评的月份，不取今天——快照是历史切片，
    用「今天」当基准会在快照变旧时把窗口错位到没数据的月份。
    months <= 0 或窗口覆盖全部数据时不切，行为与历史版本完全一致。

    幂等：对已切过窗的 summary 再调一次会直接返回（dateMin 已 >= cutoff）。
    """
    if not months or months <= 0:
        return summary
    snap = summary.get("snapshot") or {}
    latest = snap.get("dateMax") or ""
    if not latest:
        return summary
    # 含最新月在内的最近 N 个自然月：2026-10 往前 12 个月 → cutoff = 2025-11
    idx = int(latest[:4]) * 12 + int(latest[5:7]) - months
    if idx < 0:
        return summary
    cutoff = f"{idx // 12:04d}-{idx % 12 + 1:02d}"
    if snap.get("dateMin", "")[:7] >= cutoff:
        return summary  # 窗口已覆盖全部数据，无需重算

    try:
        data = load()
    except ReviewsUnavailable:
        return summary  # 原始快照读不到时退回已算好的全量统计
    filtered = [
        r for r in data.get("reviews", [])
        if isinstance(r, dict) and (r.get("created_at") or "")[:7] >= cutoff
    ]
    if not filtered:
        return summary  # 窗口内没有数据，不硬造空结论
    return summarize({"reviews": filtered})


def attribution(summary: dict) -> dict:
    """判断差评短板落在硬件还是服务。纯函数，只读 summary，不产生文案。

    verdict 取值：
      hardware     —— 硬件类提及数显著多于服务类
      service      —— 服务类显著多于硬件类
      mixed        —— 两类相当，不足以归因到单一环节
      insufficient —— 至少一侧样本不足，不下结论
      none         —— 没有可归因的差评

    口径说明：计数用的是"提及数"而非"点评条数"。一条差评同时提到设施老旧
    和服务态度时两侧各计一次，这正是想要的——归因看的是某类问题被提到的
    频次，不是点评条数。
    """
    themes = summary.get("themes") or []
    mentions = {t["label"]: t["count"] for t in themes}
    hard = sum(mentions.get(k, 0) for k in HARDWARE_THEMES)
    soft = sum(mentions.get(k, 0) for k in SERVICE_THEMES)
    bad_total = (summary.get("overall") or {}).get("bad") or 0

    base = {
        "hardware": hard,
        "service": soft,
        "badTotal": bad_total,
        "minMentions": ATTRIBUTION_MIN_MENTIONS,
        "strongRatio": ATTRIBUTION_STRONG_RATIO,
    }

    if not bad_total or (hard == 0 and soft == 0):
        return {**base, "verdict": "none", "ratio": None,
                "weakSide": None, "weakCount": 0}

    # 一侧为零时倍数无定义（分母为 0）。单独处理，不能写成"0 倍"或"持平"。
    if soft == 0 and hard >= ATTRIBUTION_MIN_MENTIONS:
        return {**base, "verdict": "hardware", "ratio": None,
                "weakSide": "hardware", "weakCount": hard}
    if hard == 0 and soft >= ATTRIBUTION_MIN_MENTIONS:
        return {**base, "verdict": "service", "ratio": None,
                "weakSide": "service", "weakCount": soft}

    if min(hard, soft) < ATTRIBUTION_MIN_MENTIONS:
        return {**base, "verdict": "insufficient", "ratio": None,
                "weakSide": None, "weakCount": 0}

    if hard >= soft:
        ratio, side, weak = round(hard / soft, 2), "hardware", hard
    else:
        ratio, side, weak = round(soft / hard, 2), "service", soft

    if ratio < ATTRIBUTION_STRONG_RATIO:
        return {**base, "verdict": "mixed", "ratio": ratio,
                "weakSide": None, "weakCount": 0}

    return {**base, "verdict": side, "ratio": ratio,
            "weakSide": side, "weakCount": weak}


def summary_or_none():
    """给 Web 层用：快照缺失时返回 None 而不是抛异常。"""
    try:
        return summarize()
    except ReviewsUnavailable:
        return None
