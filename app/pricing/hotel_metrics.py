"""自家酒店指标：当前价 / 入住率统一从 eBook 采集快照取。

不再依赖 .env 的 MY_HOTEL_CURRENT_PRICE / MY_HOTEL_OCCUPANCY_RATE：
- 入住率 ← eBook fetchCapacityOverViewV4 的 occupancyRate（实时）
- 当前价 ← eBook 今日房型挂牌价中「非钟点房」的最低整晚价

快照不可用时回退数据库最近一次写入（resolve(db=...)），再不可用返回 None，
由调用方决定报错还是显示占位——绝不编造。
"""
import html
import json
import os
from datetime import date

from app import config as cfg

_ROOM_MARKERS = ("钟点", "小时")


def _load_snapshot():
    path = cfg.EBOOK_CONFIG.get("path") or ""
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001 - 快照坏了当不可用，不拖垮调用方
        return None


def _room_names(sections):
    """roomTypeID -> 房型名（HTML 实体解码）。"""
    rooms = sections.get("rooms") or {}
    data = rooms.get("data")
    if not isinstance(data, list):
        return {}
    names = {}
    for it in data:
        if not isinstance(it, dict):
            continue
        base = html.unescape(it.get("roomName") or "")
        for inf in it.get("roomInfos") or []:
            if not isinstance(inf, dict):
                continue
            rid = str(inf.get("roomTypeID") or "")
            nm = html.unescape(inf.get("roomName") or "") or base
            if rid and nm:
                names[rid] = nm
    return names


def _is_hourly(name):
    return any(m in (name or "") for m in _ROOM_MARKERS)


def _from_sections(sections):
    today = date.today().isoformat()

    inv = sections.get("inventory") or {}
    inv_data = inv.get("data") or {}
    prices = (inv_data.get("roomPriceResult") or {}).get("roomPriceInfo") or []
    names = _room_names(sections)
    candidates = []
    for p in prices:
        if not isinstance(p, dict) or p.get("effectDate") != today:
            continue
        value = p.get("price")
        if value is None:
            continue
        if _is_hourly(names.get(str(p.get("roomTypeID")), "")):
            continue  # 钟点房不算整晚起价
        candidates.append(float(value))
    price = min(candidates) if candidates else None

    cap = sections.get("capacity") or {}
    cap_data = cap.get("data") if isinstance(cap.get("data"), dict) else cap
    occ_raw = cap_data.get("occupancyRate")
    try:
        occ = float(occ_raw) if occ_raw is not None else None
    except (TypeError, ValueError):
        occ = None

    return {"current_price": price, "occupancy_rate": occ}


def snapshot_metrics():
    """只从 eBook 快照取；不可用返回 None。"""
    raw = _load_snapshot()
    if not raw or raw.get("status") != "ok":
        return None
    m = _from_sections(raw.get("sections") or {})
    if m["current_price"] is None and m["occupancy_rate"] is None:
        return None
    m["collected_at"] = raw.get("collected_at") or ""
    return m


def resolve(db=None):
    """当前价 / 入住率：eBook 快照优先，db 最近一次写入回退。

    Args:
        db: 可选，PricingDatabase 实例；传入时快照缺值才回退它。

    Returns:
        {"current_price": float|None, "occupancy_rate": float|None,
         "source": "ebook"|"db"|None}
    """
    snap = snapshot_metrics()
    price = snap.get("current_price") if snap else None
    occ = snap.get("occupancy_rate") if snap else None
    source = "ebook" if snap else None

    if (price is None or occ is None) and db is not None:
        try:
            my = db.get_latest_my_price() or {}
        except Exception:  # noqa: BLE001
            my = {}
        if price is None and my.get("price") is not None:
            try:
                price = float(my["price"])
            except (TypeError, ValueError):
                price = None
        if occ is None and my.get("occupancy_rate") is not None:
            try:
                occ = float(my["occupancy_rate"])
            except (TypeError, ValueError):
                occ = None
        if source is None and (price is not None or occ is not None):
            source = "db"

    return {"current_price": price, "occupancy_rate": occ, "source": source}