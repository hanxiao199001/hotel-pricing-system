"""房型口碑 → 定价参考。

为什么需要这一层：点评里有房型字段，定价侧只有「起价」一个数。
要把两者连起来，必须先解决房型名不统一的问题——

    豪华客房-大床丨滨海城景        ← 1375 条
    豪华客房-双床丨滨海城景        ← 397 条
    雅致豪华房-大床丨滨海城景      ← 72 条

这三个名字在点评里是三个不同取值，但对定价来说前两个是同一档位的不同床型，
第三个是更高一档。不归一化就只能看到 12 个碎片，无法判断「哪一档撑得住溢价」。

本模块只做两件事：归一化 + 判断，**不改动建议价**。
价格仍由 algorithm.PricingAlgorithm 计算，这里回答的是
「这个价有没有口碑支撑」「哪一档在拖后腿」。

实测结论（1975 条快照）：档位越高口碑越弱。基础档豪华客房差评率 1.3%，
行政商务房 5.3%、家庭连通房 4.8%——高价房型的溢价缺乏口碑支撑。
这种判断是规则写出来的，可复现、可解释，不需要模型参与。
"""
import collections

import config as cfg

# 档位优先级：靠前先匹配。顺序有讲究——
# 「豪华套房」要落进套房而不是豪华客房，「雅致豪华房」要落进雅致而不是豪华客房。
_TIER_RULES = (
    ("套房", ("套房",)),
    ("行政商务房", ("行政商务", "行政房")),
    ("雅致豪华房", ("雅致",)),
    ("云杉优眠精选", ("云杉",)),
    ("家庭连通房", ("连通", "家庭房")),
    ("豪华客房", ("豪华客房", "豪华房")),
)

# 展示用档次顺序（高到低），用于判断「档位与口碑是否反向」
TIER_ORDER = [name for name, _ in _TIER_RULES] + ["其他"]

_BED_RULES = (
    ("家庭", ("连通", "家庭", "单床")),
    ("双床", ("双床",)),
    ("大床", ("大床", "特大床")),
)

LEVEL_LABELS = {
    "strong": "口碑支撑强",
    "mixed": "口碑中性",
    "weak": "溢价承压",
    "insufficient": "样本不足",
}


def normalize_room(name: str) -> dict:
    """把一个原始房型名拆成 (档位, 床型)。

    未识别的名字落到「其他」，不丢数据——页面会照实显示，
    这样新上线的房型不会凭空消失，只会先归到其他。
    """
    text = (name or "").strip()
    tier = "其他"
    for candidate, keywords in _TIER_RULES:
        if any(k in text for k in keywords):
            tier = candidate
            break

    bed = "未标注"
    for candidate, keywords in _BED_RULES:
        if any(k in text for k in keywords):
            bed = candidate
            break

    return {"tier": tier, "bed": bed, "raw": text}


def _pct(n, digits=1):
    return round(n, digits) if n is not None else None


def build_profiles(summary: dict) -> list:
    """按档位聚合口碑。

    只用 summary 里已有的数据（rooms 的计数/均分/差评数 + badRows 的主题），
    不重新读快照文件。

    均分按点评数加权，否则「套房 15 条」会和「豪华客房 1772 条」等权，
    一个档位的均分被十几条样本左右。
    """
    rooms = summary.get("rooms") or []
    bad_rows = summary.get("badRows") or []
    overall = summary.get("overall") or {}
    total = overall.get("count") or 0
    min_sample = cfg.ROOM_PRICING["min_sample"]

    # 档位 -> 床型 -> 原始房型计数
    tier_acc = collections.defaultdict(
        lambda: {"count": 0, "bad": 0, "ratingSum": 0.0, "beds": collections.Counter(),
                 "rawNames": []}
    )
    for r in rooms:
        info = normalize_room(r.get("room"))
        acc = tier_acc[info["tier"]]
        count = int(r.get("count") or 0)
        acc["count"] += count
        acc["bad"] += int(r.get("bad") or 0)
        acc["ratingSum"] += float(r.get("avg") or 0) * count
        acc["beds"][info["bed"]] += count
        acc["rawNames"].append({"name": info["raw"], "count": count})

    # 差评主题：从 badRows 按归一化档位归集
    tier_themes = collections.defaultdict(collections.Counter)
    for row in bad_rows:
        info = normalize_room(row.get("room"))
        for theme in (row.get("themes") or "").split("、"):
            theme = theme.strip()
            if theme and theme != "未归类":
                tier_themes[info["tier"]][theme] += 1

    profiles = []
    for tier, acc in tier_acc.items():
        count = acc["count"]
        if not count:
            continue
        avg = round(acc["ratingSum"] / count, 2)
        bad_rate = round(acc["bad"] / count * 100, 1)
        themes = [
            {"label": label, "count": n}
            for label, n in tier_themes.get(tier, collections.Counter()).most_common(3)
        ]
        profiles.append({
            "tier": tier,
            "count": count,
            "share": _pct(count / total * 100) if total else None,
            "avg": avg,
            "bad": acc["bad"],
            "badRate": bad_rate,
            "reliable": count >= min_sample,
            "topThemes": themes,
            "beds": [
                {"bed": bed, "count": n}
                for bed, n in acc["beds"].most_common()
            ],
            "rawNames": sorted(acc["rawNames"], key=lambda x: -x["count"]),
        })

    # 点评量大的排前面：量的多少本身就说明这是主力房型
    profiles.sort(key=lambda p: -p["count"])
    return profiles


def _judge(profile: dict, overall: dict) -> dict:
    """给单个档位下判断。

    用「差评率相对整体」而不是绝对分，因为 4.90 和 4.85 的差别
    在 5 分制里几乎是噪声，而差评率 1.3% vs 5.3% 是 4 倍差距。
    """
    base_rate = overall.get("badRate") or 0
    cfg_room = cfg.ROOM_PRICING

    if not profile["reliable"]:
        return {
            "level": "insufficient",
            "label": LEVEL_LABELS["insufficient"],
            "badRateVsOverall": None,
            "avgVsOverall": None,
            "hint": (
                f"只有 {profile['count']} 条点评，少于 {cfg_room['min_sample']} 条的判定门槛，"
                "宁可不表态也不猜。"
            ),
            "adjustRange": None,
        }

    ratio = round(profile["badRate"] / base_rate, 2) if base_rate else None
    avg_delta = round(profile["avg"] - (overall.get("avg") or 0), 2)

    if ratio is not None and ratio <= cfg_room["strong_bad_rate_ratio"]:
        level = "strong"
        hint = (
            f"差评率 {profile['badRate']}%，低于整体 {base_rate}%，"
            "这一档的口碑撑得住当前定价。"
        )
        adjust = [0, 0]
    elif ratio is not None and ratio >= cfg_room["weak_bad_rate_ratio"]:
        level = "weak"
        low, high = cfg_room["weak_hint_range"]
        hint = (
            f"差评率 {profile['badRate']}%，是整体的 {ratio} 倍"
            + (f"，差评集中在「{profile['topThemes'][0]['label']}」" if profile["topThemes"] else "")
            + f"；建议在算法价基础上再让 {low}–{high}% 作为缓冲。"
        )
        adjust = [-high, -low]
    else:
        level = "mixed"
        hint = (
            f"差评率 {profile['badRate']}%，与整体 {base_rate}% 基本持平，"
            "维持算法价即可。"
        )
        adjust = [0, 0]

    return {
        "level": level,
        "label": LEVEL_LABELS[level],
        "badRateVsOverall": ratio,
        "avgVsOverall": avg_delta,
        "hint": hint,
        "adjustRange": adjust,
    }


def _pick_base_tier(profiles: list) -> tuple:
    """起价对应哪一档。

    默认取点评量最大的一档——OTA 上最低价房型通常就是点评量最多的基础房型
    （实测豪华客房 1772 条，占 89.7%）。允许用 MY_HOTEL_BASE_ROOM_TIER 覆盖，
    因为不同酒店的起价口径可能不是这样。
    """
    configured = (cfg.ROOM_PRICING.get("base_tier") or "").strip()
    if configured:
        return configured, "config"
    if not profiles:
        return None, "none"
    return profiles[0]["tier"], "auto"


def _structure_note(profiles: list, base_tier: str) -> str:
    """档位结构与口碑的关系，一句话。

    只在「有档位差评率明显高于基础档」时才说，否则不硬造结论。
    """
    by_tier = {p["tier"]: p for p in profiles}
    base = by_tier.get(base_tier)
    if not base:
        return ""
    worse = [
        p for p in profiles
        if p["reliable"] and p["tier"] != base_tier and p["badRate"] > base["badRate"]
    ]
    if not worse:
        return ""

    worse.sort(key=lambda p: -p["badRate"])
    top = worse[0]
    ratio = round(top["badRate"] / base["badRate"], 1) if base["badRate"] else None
    parts = [
        f"「{top['tier']}」差评率 {top['badRate']}%"
        + (f"，是基础档「{base_tier}」（{base['badRate']}%）的 {ratio} 倍" if ratio else "")
    ]
    if len(worse) > 1:
        others = "、".join(f"{p['tier']} {p['badRate']}%" for p in worse[1:3])
        parts.append(f"另有 {others}")
    parts.append("档位越高口碑越弱，靠升级房型提价的路径暂时走不通，应先整改再提价")
    return "；".join(parts) + "。"


def signal(summary: dict = None) -> dict:
    """给定价侧用的完整信号。

    调用方拿到的 level / adjustRange / hint 只是「参考」，
    不会被自动并入建议价——调价必须由人拍板，这一点和现有流程一致。
    """
    if summary is None:
        import reviews_analytics

        summary = reviews_analytics.summary_or_none()
    if not summary:
        return {"available": False, "reason": "缺少点评快照"}

    overall = summary.get("overall") or {}
    profiles = build_profiles(summary)
    if not profiles:
        return {"available": False, "reason": "快照里没有房型字段"}

    for p in profiles:
        p["signal"] = _judge(p, overall)

    base_tier, base_source = _pick_base_tier(profiles)
    base = next((p for p in profiles if p["tier"] == base_tier), None)

    return {
        "available": True,
        "source": "snapshot",
        "snapshotDate": (summary.get("snapshot") or {}).get("dateMax"),
        "kept": (summary.get("snapshot") or {}).get("kept"),
        "baseTier": base_tier,
        "baseTierSource": base_source,
        "baseSignal": base["signal"] if base else None,
        "overall": {
            "count": overall.get("count"),
            "avg": overall.get("avg"),
            "badRate": overall.get("badRate"),
        },
        "profiles": profiles,
        "structureNote": _structure_note(profiles, base_tier),
        "minSample": cfg.ROOM_PRICING["min_sample"],
    }
