"""把点评快照的统计结果翻译成页面 block。

前端只认 block schema（kpi / list / table / bars / quote / banner / note），
所以这里只负责「算好的数 → 有信息量的文案」，不做任何再计算。
数据本身全部来自 reviews_analytics.summarize()。
"""
from . import static_data

import config as cfg
import room_reputation


def _fmt(n, digits=0):
    if n is None:
        return "—"
    return f"{n:,.{digits}f}"


def _pct(n, digits=1):
    return "—" if n is None else f"{n:.{digits}f}%"


def _module(key, title, status_note, blocks, snapshot):
    base = static_data.get_module(key)
    return {
        "key": key,
        "title": base.get("title") or title,
        "subtitle": base.get("subtitle"),
        "status": "snapshot",
        "statusNote": status_note,
        "source": "snapshot",
        "snapshot": snapshot,
        "blocks": blocks,
    }


# ---------------------------------------------------------------------------
# 房型 → 定价影响
# ---------------------------------------------------------------------------
_ACTION_TEXT = {
    "strong": "算法价不需要额外让价",
    "mixed": "维持算法价即可",
    "weak": "建议让价留缓冲",
    "insufficient": "不给让价参考",
}


def _room_pricing_blocks(s, price=None):
    """把房型档位的口碑判断翻成一张表 + 一句起价结论。

    为什么放在口碑页而不是定价页：判断的依据全在点评里，
    定价页只该出现一句结论。两边都由 room_reputation.signal() 算，
    口径不会分叉。
    """
    sig = room_reputation.signal(s)
    if not sig.get("available"):
        return []

    base_tier = sig.get("baseTier")
    base = next((p for p in sig["profiles"] if p["tier"] == base_tier), None)
    base_price = price if price is not None else cfg.MY_HOTEL.get("current_price")

    rows = []
    for p in sig["profiles"]:
        g = p["signal"]
        ratio = g.get("badRateVsOverall")
        rows.append({
            "tier": p["tier"],
            "count": _fmt(p["count"]),
            "avg": _fmt(p["avg"], 2),
            "badRate": _pct(p["badRate"]),
            "ratio": f"×{ratio:.2f}".rstrip("0").rstrip(".") if ratio is not None else "—",
            "verdict": g["label"],
            "verdictTone": {"weak": "warm", "strong": "ok"}.get(g["level"], ""),
            "hint": g["hint"],
        })

    table = {
        "type": "table",
        "title": "房型 → 定价影响（按档位归并）",
        "meta": f"少于 {sig['minSample']} 条点评的档位不下结论",
        "columns": [
            {"key": "tier", "label": "档位", "width": "132px", "nowrap": True},
            {"key": "count", "label": "点评", "align": "right", "width": "76px"},
            {"key": "avg", "label": "均分", "align": "right", "width": "68px"},
            {"key": "badRate", "label": "差评率", "align": "right", "width": "80px"},
            {"key": "ratio", "label": "相对整体", "align": "right", "width": "92px", "nowrap": True},
            {"key": "verdict", "label": "判断", "align": "right", "width": "104px", "nowrap": True},
        ],
        "rows": rows,
    }

    blocks = [table]

    if base and sig.get("baseSignal"):
        g = sig["baseSignal"]
        head = f"起价按「{base_tier}」这一档计算" if base_price is None else \
               f"起价 ¥{base_price:.0f} 按「{base_tier}」这一档计算"
        detail = (
            f"{_fmt(base['count'])} 条点评、均分 {_fmt(base['avg'], 2)}、"
            f"差评率 {_pct(base['badRate'])}"
        )
        if g.get("badRateVsOverall") is not None:
            detail += f"，是整体的 {g['badRateVsOverall']:.2f} 倍"
        action = _ACTION_TEXT.get(g["level"], "")
        if g.get("adjustRange") and g["level"] == "weak":
            low, high = -g["adjustRange"][1], -g["adjustRange"][0]
            action = f"建议在算法价基础上再让 {low}–{high}% 作为缓冲"
        text = f"{head}（{detail}）→ {g['label']}，{action}。"
        blocks.append({"type": "banner", "text": text})

    if sig.get("structureNote"):
        blocks.append({"type": "banner", "text": sig["structureNote"]})

    blocks.append({
        "type": "note",
        "text": (
            "档位由房型名归并而来（见 room_reputation._TIER_RULES），床型只作为子维度、"
            "不单独定价。归并只改变统计口径，不动任何点评原文。"
            "这里给的是方向性参考：各档位的实际挂牌价需要从 PMS 补充，"
            "系统目前只知道起价一个数，所以算不出「某一档该卖多少钱」，"
            "只能判断「这一档撑不撑得住现在的价」。"
        ),
    })
    return blocks


# ---------------------------------------------------------------------------
# 口碑分析
# ---------------------------------------------------------------------------
def build_reputation(s, price=None):
    snap, overall = s["snapshot"], s["overall"]
    worst_dim = s["dimensions"][0] if s["dimensions"] else None
    low_month = min(s["monthly"], key=lambda m: m["avg"]) if s["monthly"] else None
    top_theme = s["themes"][0] if s["themes"] else None

    kpis = [
        {"type": "kpi", "items": [
            {"label": "综合评分",
             "value": _fmt(overall["avg"], 2),
             "sub": f"{_fmt(overall['count'])} 条点评 · 携程"},
            {"label": "好评率",
             "value": _pct(overall["goodRate"]),
             "sub": f"评分≥{s['thresholds']['goodRating']:.0f} · {_fmt(overall['good'])} 条",
             "tone": "ok"},
            {"label": "差评",
             "value": f"{_fmt(overall['bad'])} 条",
             "sub": f"占 {_pct(overall['badRate'])} · 未回复 {overall['unreplied']} 条",
             "tone": "warm" if overall["bad"] else "ok"},
            {"label": "快照覆盖",
             "value": _pct(snap["coverage"]),
             "sub": f"{_fmt(snap['kept'])} / 站内 {_fmt(snap['totalCount'])} 条",
             "tone": "warm"},
        ]},
    ]

    star_bars = {"type": "bars", "title": "评分档位分布", "suffix": "%", "max": 100,
                 "items": [
                     {"label": d["label"], "value": d["ratio"],
                      "detail": f"{_fmt(d['count'])} 条",
                      "tone": "warm" if d["star"] <= 2 and d["count"] else ""}
                     for d in s["starDist"]
                 ]}

    dim_rows = []
    for i, d in enumerate(s["dimensions"]):
        if i == 0:
            verdict = "最弱项"
        elif d["lowRatio"] >= 2:
            verdict = "差评集中"
        elif (d["avg"] or 0) >= 4.95:
            verdict = "接近满分"
        else:
            verdict = "稳定"
        dim_rows.append({
            "dim": d["label"],
            "avg": _fmt(d["avg"], 2),
            "low": _pct(d["lowRatio"]),
            "count": _fmt(d["count"]),
            "verdict": verdict,
            "verdictTone": "warm" if verdict in ("最弱项", "差评集中") else "",
        })

    dim_table = {"type": "table", "title": "四维拆解（评分明细，按短板排序）",
                 "meta": "均分最低的一维排最前",
                 "columns": [
                     {"key": "dim", "label": "维度", "width": "80px", "nowrap": True},
                     {"key": "avg", "label": "均分", "align": "right", "width": "80px"},
                     {"key": "low", "label": "≤3 分占比", "align": "right", "width": "100px", "nowrap": True},
                     {"key": "count", "label": "样本", "align": "right", "width": "80px"},
                     {"key": "verdict", "label": "判读", "align": "right", "width": "90px", "nowrap": True},
                 ],
                 "rows": dim_rows}

    mention_bars = {"type": "bars", "title": "好评里被提到的点", "suffix": "%", "max": 100,
                    "items": [
                        {"label": m["label"], "value": m["ratio"],
                         "detail": f"{_fmt(m['count'])} 条 / {_fmt(overall['good'])} 条好评"}
                        for m in s["goodMentions"][:6]
                    ]}

    room_table = {"type": "table", "title": "原始房型明细（归并前，按点评量）",
                  "meta": "订单里的房型名保留原样，方便和 PMS 对照",
                  "columns": [
                      {"key": "room", "label": "房型", "width": "46%"},
                      {"key": "count", "label": "点评数", "align": "right", "width": "90px"},
                      {"key": "avg", "label": "均分", "align": "right", "width": "80px"},
                      {"key": "bad", "label": "差评", "align": "right", "width": "80px"},
                  ],
                  "rows": [
                      {"room": r["room"], "count": _fmt(r["count"]),
                       "avg": _fmt(r["avg"], 2), "bad": _fmt(r["bad"]),
                       "badTone": "warm" if r["bad"] else ""}
                      for r in s["rooms"][:6]
                  ]}

    month_table = {"type": "table",
                   "title": f"月度趋势（近 {len(s['monthly'])} 个月）",
                   "columns": [
                       {"key": "month", "label": "月份", "width": "100px", "nowrap": True},
                       {"key": "count", "label": "点评数", "align": "right", "width": "90px"},
                       {"key": "avg", "label": "均分", "align": "right", "width": "80px"},
                       {"key": "bad", "label": "差评", "align": "right", "width": "80px"},
                   ],
                   "rows": [
                       {"month": m["month"], "count": _fmt(m["count"]),
                        "avg": _fmt(m["avg"], 2), "bad": _fmt(m["bad"]),
                        "badTone": "warm" if m["bad"] else ""}
                       for m in reversed(s["monthly"])
                   ]}

    parts = []
    if worst_dim:
        parts.append(
            f"最弱一维是「{worst_dim['label']}」"
            f"（均分 {_fmt(worst_dim['avg'], 2)}，≤3 分占 {_pct(worst_dim['lowRatio'])}）"
        )
    if top_theme:
        parts.append(
            f"{_fmt(overall['bad'])} 条差评里有 {top_theme['count']} 条提到"
            f"「{top_theme['label']}」（{_pct(top_theme['ratio'])}）"
        )
    if low_month:
        parts.append(
            f"最差月份 {low_month['month']}（均分 {_fmt(low_month['avg'], 2)}、"
            f"差评 {low_month['bad']} 条）"
        )
    banner = {"type": "banner",
              "text": "；".join(parts) + "。口碑短板在硬件而非服务，"
                      "这与「设施老旧」类差评长期反复出现一致——不是一次性的服务事故。"}

    note = {"type": "note",
            "text": (f"口径：好评 ≥{s['thresholds']['goodRating']:.0f} 分，"
                     f"差评 ≤{s['thresholds']['badRating']:.0f} 分；星级按评分四舍五入。"
                     f"数据是 {snap['at']} 抓取的历史切片，仅覆盖携程公开点评页"
                     f"（{snap['before']} 之前发布的内容，"
                     f"{snap['dateMin']} 至 {snap['dateMax']}），"
                     f"采到 {_fmt(snap['collected'])} 条、保留 {_fmt(snap['kept'])} 条，"
                     f"占站内 {_fmt(snap['totalCount'])} 条的 {_pct(snap['coverage'])}。"
                     "美团、飞猪与商家后台数据未包含在内。")}

    status = (f"数据来自 {snap['at']} 的携程点评快照（{snap['dateMin']} 至 {snap['dateMax']}，"
              f"{_fmt(snap['kept'])} 条）。不是实时接口，刷新页面不会更新。")
    blocks = [kpis[0], star_bars, dim_table, mention_bars]
    blocks += _room_pricing_blocks(s, price)
    blocks += [room_table, month_table, banner, note]
    return _module("reputation", "口碑分析", status, blocks, snap)


# ---------------------------------------------------------------------------
# 差评预警
# ---------------------------------------------------------------------------
def _short_themes(text: str, keep: int = 3) -> str:
    """表格里最多列 3 个主题，其余用 +N 表示，避免行高被撑开。"""
    parts = [p for p in (text or "").split("、") if p]
    if len(parts) > keep:
        return "、".join(parts[:keep]) + f" +{len(parts) - keep}"
    return "、".join(parts) or "—"


def build_reviews(s):
    snap, overall = s["snapshot"], s["overall"]
    themes = s["themes"]
    max_theme = max((t["count"] for t in themes), default=1) or 1
    top = themes[0] if themes else None
    worst = s["worst"]

    kpis = {"type": "kpi", "items": [
        {"label": "差评总数",
         "value": f"{_fmt(overall['bad'])} 条",
         "sub": f"评分≤{s['thresholds']['badRating']:.0f} · 占 {_pct(overall['badRate'])}",
         "tone": "warm"},
        {"label": "未回复",
         "value": f"{_fmt(overall['unreplied'])} 条",
         "sub": "全部差评均有商家回复" if not overall["unreplied"] else "需要人工跟进",
         "tone": "ok" if not overall["unreplied"] else "warm"},
        {"label": "最近 90 天",
         "value": f"{_fmt(overall['recentBad'])} 条",
         "sub": f"截至 {snap['dateMax']}"},
        {"label": "最集中问题",
         "value": f"{_fmt(top['count'])} 条" if top else "—",
         "sub": f"{top['label']} · 占差评 {_pct(top['ratio'])}" if top else "无差评",
         "tone": "warm"},
    ]}

    theme_bars = {"type": "bars", "title": "差评主题分布", "suffix": " 条", "max": max_theme,
                  "items": [
                      {"label": t["label"], "value": t["count"],
                       "detail": f"占差评 {_pct(t['ratio'])}"
                                 + (f" · 例：{t['examples'][0]['text']}" if t["examples"] else ""),
                       "tone": "warm" if i == 0 else ""}
                      for i, t in enumerate(themes)
                  ]}

    detail_table = {"type": "table",
                    "title": f"差评明细（{len(s['badRows'])} 条，最近的在前）",
                    "meta": "点开原帖需到携程商家后台；这里只做归类",
                    "columns": [
                        {"key": "date", "label": "日期", "width": "100px", "nowrap": True},
                        {"key": "rating", "label": "评分", "align": "right", "width": "62px", "nowrap": True},
                        {"key": "room", "label": "房型", "width": "190px"},
                        {"key": "summary", "label": "内容摘要"},
                        {"key": "themes", "label": "主题", "width": "150px"},
                        {"key": "replied", "label": "回复", "align": "right", "width": "76px", "nowrap": True},
                    ],
                    "rows": [
                        {"date": r["date"], "rating": f"{r['rating']:.1f}",
                         "room": r["room"], "summary": r["summary"],
                         "themes": _short_themes(r["themes"]), "replied": r["replied"],
                         "repliedTone": r["repliedTone"]}
                        for r in s["badRows"]
                    ]}

    quote = None
    if worst:
        quote = {"type": "quote",
                 "title": f"评分最低的一条（{worst['date']} · {worst['rating']:.1f} 分 · {worst['room']}）",
                 "text": worst["content"],
                 "footnote": ("商家回复：" + worst["reply"]) if worst["reply"]
                             else "这条还没有商家回复。"}

    insights = []
    if top:
        insights.append(
            f"「{top['label']}」在 {snap['dateMin'][:4]}–{snap['dateMax'][:4]} 反复出现"
            f"（{top['count']}/{overall['bad']} 条差评提到）"
        )
    second = themes[1] if len(themes) > 1 else None
    if second:
        insights.append(f"其次是「{second['label']}」（{second['count']} 条）")
    if not overall["unreplied"]:
        insights.append("回复率 100%，服务响应不是短板，问题在硬件与早餐")
    banner = {"type": "banner", "text": "；".join(insights) + "。"}

    note = {"type": "note",
            "text": ("本页只读：不抓取、不回复、不修改任何平台内容。"
                     "主题归类是关键词朴素匹配（见 reviews_analytics.NEGATIVE_THEMES），"
                     "一条点评可命中多个主题，所以各主题条数之和大于差评总数。"
                     "差评与房型、定价的联动见「口碑分析」页的「房型 → 定价影响」。")}

    status = (f"数据来自 {snap['at']} 的携程点评快照（{_fmt(snap['kept'])} 条，"
              f"最新一条 {snap['dateMax']}，其中 {overall['bad']} 条差评）。"
              "不含美团/飞猪，也没有回复发布能力。")
    blocks = [kpis, theme_bars, detail_table]
    if quote:
        blocks.append(quote)
    blocks += [banner, note]
    return _module("reviews", "差评预警", status, blocks, snap)


_BUILDERS = {"reviews": build_reviews, "reputation": build_reputation}


def build(key, **ctx):
    """按模块 key 生成页面；快照不可用时返回 None，由调用方回退到示例数据。

    ctx 目前只用到 price（当前挂牌价）。定价页和口碑页都按同一个
    room_reputation.signal() 判断，口径不会分叉。
    """
    fn = _BUILDERS.get(key)
    if fn is None:
        return None
    import reviews_analytics

    summary = reviews_analytics.summary_or_none()
    if summary is None:
        return None
    if key == "reputation":
        return fn(summary, price=ctx.get("price"))
    return fn(summary)
