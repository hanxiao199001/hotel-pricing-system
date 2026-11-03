"""采集结果 → 系统内部结构。

关键单位约定：采集脚本输出的 distance 是「米」，而系统内部（历史 mock 数据与
数据库字段 distance_km）用的是「千米」，这里统一换算，避免把 350 米当成 350 公里。
"""


def _to_float(value, default=None):
    try:
        text = str(value).strip().replace(",", "")
        return float(text) if text else default
    except (TypeError, ValueError):
        return default


def _to_int(value, default=None):
    number = _to_float(value)
    return int(number) if number is not None else default


def _extract_id(hotel: dict):
    """优先从详情页 URL 里取酒店 ID，便于回溯核对。"""
    url = (hotel.get("url") or "").strip()
    if "hotelId=" in url:
        tail = url.split("hotelId=", 1)[1]
        digits = ""
        for ch in tail:
            if ch.isdigit():
                digits += ch
            else:
                break
        if digits:
            return digits
    shid = (hotel.get("shid") or "").strip()
    if shid:
        return shid
    return None


def to_competitor(hotel: dict, date: str) -> dict:
    """把采集脚本的一条酒店记录转成系统 competitor 结构。"""
    distance_m = _to_float(hotel.get("distance"), None)
    return {
        "id": _extract_id(hotel),
        "name": (hotel.get("name") or "").strip(),
        "price": _to_float(hotel.get("price"), 0.0),
        # 米 -> 千米，与数据库 distance_km 字段和历史 mock 数据保持一致
        "distance": round(distance_m / 1000, 2) if distance_m is not None else None,
        "distance_text": hotel.get("distance_text") or "",
        "rating": _to_float(hotel.get("score"), None),
        "reviews": _to_int(hotel.get("comments"), None),
        "date": date,
        "platform": hotel.get("platform") or "ctrip",
        "url": hotel.get("url") or "",
        "room": hotel.get("room") or "",
        "rooms": hotel.get("rooms") or [],
    }


def to_competitors(hotels, date: str) -> list:
    return [to_competitor(h, date) for h in hotels if (h.get("name") or "").strip()]
