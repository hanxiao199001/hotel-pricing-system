"""把 eBooking 经营数据快照翻译成页面 block。

前端只认 block schema（kpi / list / table / bars / quote / banner / note），
所以这里只做「快照里的数 → 有信息量的文案」，不伪造任何数字。
数据全部来自 EBOOK_CONFIG["path"] 的原始接口响应。
"""
import html
import re
from datetime import date

from . import static_data

from app import config as cfg


def _fmt(n, digits=0):
    if n is None:
        return "—"
    try:
        return f"{float(n):,.{digits}f}"
    except (TypeError, ValueError):
        return str(n)


def _pct(n, digits=1):
    if n is None:
        return "—"
    try:
        return f"{float(n):.{digits}f}%"
    except (TypeError, ValueError):
        return str(n)


def _ynum(n):
    """数字原样返回（保留百分号换算用原始值）。"""
    if n is None:
        return None
    try:
        return float(n)
    except (TypeError, ValueError):
        return None


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


def _load_snapshot():
    path = cfg.EBOOK_CONFIG.get("path", "")
    try:
        import json
        import os

        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def _core(sections):
    """从原始 sections 汇总经营指标（缺失项为 None）。"""
    market = sections.get("market") or {}
    capacity = sections.get("capacity") or {}
    # market 有 data 包裹；capacity 实测是顶层字段——两种都兼容
    m_data = market.get("data") if isinstance(market.get("data"), dict) else market
    c_data = capacity.get("data") if isinstance(capacity.get("data"), dict) else capacity
    return {
        "adr": _ynum(m_data.get("averagePrice")),
        "rental_rate": _ynum(m_data.get("rentalRate")),
        "occupancy_rate": _ynum(c_data.get("occupancyRate")),
        "occupied_rooms": _ynum(c_data.get("occupiedRooms")),
        "order_quantity": _ynum(c_data.get("orderQuantity")),
        "book_amount": _ynum(m_data.get("bookAmount")),
        "book_quantity": _ynum(m_data.get("bookQuantity")),
        "stay_in": _ynum(m_data.get("stayInQuantity")),
        "close_rate": _ynum(m_data.get("closeRate")),
        "competitors": _ynum(m_data.get("competitorNumber")),
    }


def _today_room_rows(sections):
    """今日各房型挂牌价/库存/佣金。返回 rows 列表或空。"""
    inv = sections.get("inventory") or {}
    data = inv.get("data") or {}
    today = date.today().isoformat()  # 2026-11-04
    prices = (data.get("roomPriceResult") or {}).get("roomPriceInfo") or []
    status = data.get("roomStatusResult") or []
    room_name = _room_name_map(sections)

    # 今日价格行（一个房型可能多条日期，只取今天）
    price_by_type: dict = {}
    for p in prices:
        if p.get("effectDate") != today:
            continue
        price_by_type[str(p.get("roomTypeID"))] = p
    # 今日房态（可售/总量）
    status_by_type: dict = {}
    for r in status:
        if r.get("effectDate") != today:
            continue
        status_by_type[str(r.get("roomTypeID"))] = r

    rows = []
    for rid, p in price_by_type.items():
        st = status_by_type.get(rid) or {}
        commission = p.get("commissionRate")
        rows.append({
            "room": room_name.get(rid) or f"房型 {rid}",
            "price": _fmt(p.get("price"), 0),
            "available": st.get("canUsedQuantity"),
            "total": st.get("totalQuantity"),
            "commission": None if commission is None else f"{float(commission)*100:.0f}%",
        })
    return rows


def _room_name_map(sections):
    """从 getRcProductList 响应提取房型名 → roomTypeID 映射。

    响应里房型名是 HTML 实体，且带了促销/预售后缀（<格林大促>、<预售产品>
    (C000...) 等），只保留基础房型名，避免表格被装饰文本刷屏。
    """
    rooms = sections.get("rooms") or {}
    data = rooms.get("data")
    if not isinstance(data, list):
        return {}
    names = {}

    def _clean(name):
        name = html.unescape(name or "")
        name = re.sub(r"<[^>]*>", "", name)
        name = re.sub(r"\(ID:\s*\d+\)", "", name)
        name = re.sub(r"\(C[A-Za-z0-9_]+\)", "", name)
        name = re.sub(r"\s+", "", name).strip()
        return name

    for it in data:
        if not isinstance(it, dict):
            continue
        base_name = _clean(it.get("roomName"))
        for inf in (it.get("roomInfos") or []):
            if not isinstance(inf, dict):
                continue
            rid = str(inf.get("roomTypeID") or "")
            nm = _clean(inf.get("roomName")) or base_name
            if rid and nm:
                names[rid] = nm
    return names


def _data_of(resp):
    """接口响应的业务数据体：有 data 包裹取 data，否则取顶层。"""
    if not isinstance(resp, dict):
        return {}
    data = resp.get("data")
    if isinstance(data, dict) or isinstance(data, list):
        return data
    return resp


def _build_reports(sections, raw):
    core = _core(sections)
    if core.get("adr") is None and core.get("occupancy_rate") is None:
        return None

    collected_at = raw.get("collected_at") or raw.get("_collected_at") or ""
    blocks = []

    # ---- KPI：真实经营指标 ----
    adr = core["adr"]
    occupancy = core["occupancy_rate"]
    revpar = adr * occupancy / 100 if adr is not None and occupancy is not None else None
    kpi_items = [
        {
            "label": "平均卖价 ADR",
            "value": f"¥{_fmt(adr, 0)}" if adr is not None else "—",
            "sub": "eBook 经营报告·平均卖价",
        },
        {
            "label": "入住率",
            "value": _pct(occupancy) if occupancy is not None else "—",
            "sub": "eBook 容量概览·采集时",
        },
        {
            "label": "RevPAR",
            "value": f"¥{_fmt(revpar, 0)}" if revpar is not None else "—",
            "sub": "ADR × 入住率",
        },
        {
            "label": "预订销售额",
            "value": f"¥{_fmt(core['book_amount'], 0)}" if core["book_amount"] is not None else "—",
            "sub": f"预订 {_fmt(core['book_quantity'], 0) or '—'} 间夜"
            if core["book_quantity"] is not None else "—",
        },
    ]
    blocks = [{"type": "kpi", "items": kpi_items}]

    # ---- 房型挂牌价表（今日）----
    rows = _today_room_rows(sections)
    if rows:
        blocks.append({
            "type": "table",
            "title": "今日各房型挂牌价",
            "columns": [
                {"key": "room", "label": "房型"},
                {"key": "price", "label": "挂牌价", "align": "right"},
                {"key": "available", "label": "可售", "align": "right"},
                {"key": "total", "label": "总量", "align": "right"},
                {"key": "commission", "label": "佣金率", "align": "right"},
            ],
            "rows": rows,
        })

    # ---- 来源说明 ----
    note_parts = [f"采集自 eBooking（{collected_at or '时间未知'}）"]
    if core["competitors"]:
        note_parts.append(f"竞争圈 {core['competitors']} 家")
    if core["order_quantity"] is not None:
        note_parts.append(f"采集时订单 {_fmt(core['order_quantity'], 0)}")
    if core["stay_in"] is not None:
        note_parts.append(f"在住 {_fmt(core['stay_in'], 0)} 间")
    blocks.append({"type": "note", "text": "，".join(note_parts) + "。仅汇总读取，不改动后台。"})

    snapshot = {
        "collectedAt": collected_at,
        "hotelId": raw.get("hotel_id") or "",
        "source": "ctrip_ebook",
    }
    status_note = "eBook 经营数据" + (f"（采集于 {collected_at}）" if collected_at else "")
    return _module("reports", "经营报表", status_note, blocks, snapshot)


# ---------------------------------------------------------------------------
# 客源结构（eBooking 数据中心 → 用户行为 → 用户分析）
# ---------------------------------------------------------------------------

def _guest_source(sections):
    """客源地接口的业务体：localCityRate/otherCityRate/topCityRate/provinces/cities。"""
    data = _data_of(sections.get("guest_source"))
    return data if isinstance(data, dict) else {}


def _guest_channels(sections):
    """预订方式占比。真实结构：userColumnBos 逐项对应 titleList 里的标签。"""
    data = _data_of(sections.get("guest_order_type"))
    if not isinstance(data, dict):
        return []
    cols = data.get("userColumnBos") or []
    if not cols:
        return []
    labels = cols[0].get("titleList") or []
    out = []
    for i, label in enumerate(labels):
        col = cols[i] if i < len(cols) else {}
        vals = col.get("valueList") or []
        out.append({"channel": label, "value": _ynum(vals[0]) if vals else None})
    return out


def _guest_hours(sections):
    """24 小时预订分布，按占比降序取前 6 个时段。"""
    data = _data_of(sections.get("guest_order_dist"))
    if not isinstance(data, dict):
        return []
    ents = data.get("orderDistributionEntities") or []
    items = [
        {"label": e.get("hour") or "—", "value": _ynum(e.get("proportion")) or 0}
        for e in ents if isinstance(e, dict)
    ]
    items.sort(key=lambda x: x["value"], reverse=True)
    return items[:6]


def _guest_profiles(sections):
    """用户特征画像列表（取第一条作为主力客群）。"""
    data = _data_of(sections.get("guest_features"))
    if not isinstance(data, list):
        return []
    return [x for x in data if isinstance(x, dict) and x]


def guest_available(sections):
    """快照里是否包含可用的客源结构数据（供模块清单判断是否标记为快照）。"""
    src = _guest_source(sections)
    return bool(
        src.get("cities") or src.get("provinces")
        or src.get("localCityRate") is not None
        or _guest_channels(sections) or _guest_profiles(sections)
    )


def _build_guests(sections, raw):
    """把 eBooking 用户行为数据翻译成「客源结构洞察」页。"""
    if not guest_available(sections):
        return None

    src = _guest_source(sections)
    channels = _guest_channels(sections)
    profiles = _guest_profiles(sections)
    collected_at = raw.get("collected_at") or raw.get("_collected_at") or ""
    blocks = []

    local = _ynum(src.get("localCityRate"))
    other = _ynum(src.get("otherCityRate"))
    top = _ynum(src.get("topCityRate"))
    mobile = next(
        (c["value"] for c in channels if "移动" in (c.get("channel") or "")), None
    )
    blocks.append({
        "type": "kpi",
        "items": [
            {"label": "异地客源", "value": _pct(other), "sub": "本地 " + _pct(local)},
            {"label": "本地客源", "value": _pct(local), "sub": "本地/异地 · 过去180天口径"},
            {"label": "TOP5 城市集中度", "value": _pct(top), "sub": "头部城市贡献占比"},
            {"label": "移动端预订占比", "value": _pct(mobile), "sub": "预订方式结构"},
        ],
    })

    cities = src.get("cities") or []
    if cities:
        top_cities = sorted(
            [c for c in cities if isinstance(c, dict)],
            key=lambda c: _ynum(c.get("value")) or 0, reverse=True,
        )[:6]
        blocks.append({
            "type": "bars",
            "title": "客源地城市 TOP",
            "suffix": "%",
            "items": [
                {"label": c.get("name") or "—", "value": round(_ynum(c.get("value")) or 0, 2)}
                for c in top_cities
            ],
        })

    provinces = src.get("provinces") or []
    if provinces:
        top_prov = sorted(
            [p for p in provinces if isinstance(p, dict)],
            key=lambda p: _ynum(p.get("value")) or 0, reverse=True,
        )[:8]
        blocks.append({
            "type": "bars",
            "title": "客源地省份 TOP",
            "suffix": "%",
            "items": [
                {"label": p.get("name") or "—", "value": round(_ynum(p.get("value")) or 0, 2)}
                for p in top_prov
            ],
        })

    if channels:
        blocks.append({
            "type": "table",
            "title": "预订方式占比",
            "columns": [
                {"key": "channel", "label": "预订方式"},
                {"key": "share", "label": "占比", "align": "right"},
            ],
            "rows": [
                {"channel": c["channel"],
                 "share": _pct(c["value"], 2) if c["value"] is not None else "—"}
                for c in channels
            ],
        })

    if profiles:
        f = profiles[0]
        sex = f.get("sex")
        rows = [
            ("年龄", f.get("age")),
            ("性别", {"男": "男性", "女": "女性"}.get(sex, sex)),
            ("来源地", f.get("source")),
            ("出行类型", f.get("type")),
            ("出行时间", f.get("traveltime")),
            ("消费能力", f.get("consumer")),
            ("提前预订", f.get("bookingdays")),
            ("连住天数", f.get("staydays")),
        ]
        blocks.append({
            "type": "table",
            "title": "主力客群画像",
            "columns": [
                {"key": "k", "label": "维度"},
                {"key": "v", "label": "特征"},
            ],
            "rows": [{"k": k, "v": v or "—"} for k, v in rows],
        })

    hours = _guest_hours(sections)
    if hours:
        blocks.append({
            "type": "bars",
            "title": "下单高峰时段 TOP6",
            "suffix": "%",
            "items": hours,
        })

    blocks.append({
        "type": "note",
        "text": (
            f"采集自 eBooking 用户行为（{collected_at or '时间未知'}）；"
            "统计口径为过去 180 天订单数据，仅汇总读取，不改动后台。"
        ),
    })

    snapshot = {
        "collectedAt": collected_at,
        "hotelId": raw.get("hotel_id") or "",
        "source": "ctrip_ebook_guest",
    }
    status_note = "eBook 客源结构数据" + (
        f"（采集于 {collected_at}）" if collected_at else ""
    )
    return _module("guests", "客源结构洞察", status_note, blocks, snapshot)


def build(key):
    """经营报表 / 客源结构用 eBooking 真实数据；快照缺失/不可用返回 None。"""
    if key not in ("reports", "guests"):
        return None
    raw = _load_snapshot()
    if not raw or raw.get("status") != "ok":
        return None
    sections = raw.get("sections") or {}
    if key == "reports":
        return _build_reports(sections, raw)
    return _build_guests(sections, raw)