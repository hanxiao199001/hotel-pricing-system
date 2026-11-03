"""采集结果校验。

存在的意义：采集脚本在"采错了城市"时依然返回 status=ok（实测过：请求海口，
实际采回杭州/北京的酒店），如果直接落库就会污染定价。这里做四道检查：
  1. 状态必须是 ok
  2. 价格必须大于 0（实测出现过 price=0）
  3. 按酒店名去重（实测飞猪有重复行）
  4. 城市一致性：结果里大量出现其他城市名 → 整批拒绝
"""


class DataValidationError(RuntimeError):
    """采集结果不可用于定价。"""


# 常见城市名，用于识别"采窜城市"。只作为反向证据使用：
# 结果里出现这些词且几乎不含期望城市时才判定异常。
OTHER_CITY_MARKERS = (
    "北京", "上海", "广州", "深圳", "杭州", "成都", "重庆", "西安", "南京",
    "武汉", "长沙", "三亚", "天津", "苏州", "厦门", "青岛", "昆明", "大理",
    "丽江", "哈尔滨", "沈阳", "郑州", "济南", "合肥", "福州", "南宁", "贵阳",
    "太原", "石家庄", "乌鲁木齐", "兰州", "银川", "西宁", "拉萨", "呼和浩特",
    "大连", "宁波", "无锡", "佛山", "东莞", "珠海", "中山", "惠州", "桂林",
)


def _hotel_text(hotel: dict) -> str:
    return " ".join(
        str(hotel.get(key) or "")
        for key in ("name", "address", "distance_text")
    )


def _to_price(hotel: dict) -> float:
    try:
        return float(hotel.get("price") or 0)
    except (TypeError, ValueError):
        return 0.0


def validate_result(result: dict, expected_city: str, expected_place: str = "",
                    profile_hint: str = "") -> dict:
    """校验并清洗采集结果。

    返回 {"hotels": [...], "stats": {...}}；不通过则抛 DataValidationError。
    """
    if not isinstance(result, dict):
        raise DataValidationError("采集结果不是合法 JSON 对象。")

    status = (result.get("status") or "").strip()
    if status != "ok":
        message = (result.get("message") or "").strip()
        reason = {
            "need_login": "需要人工登录（登录/验证等待超时）",
            "blocked": "被风控拦截",
            "no_data": "未采集到距离范围内的酒店",
            "error": "采集过程报错",
        }.get(status, f"未知状态 {status!r}")
        raise DataValidationError(f"采集未成功：{reason}。{message}".strip())

    hotels = result.get("hotels") or []
    if not isinstance(hotels, list) or not hotels:
        raise DataValidationError("采集结果为空。")

    cleaned = []
    seen = set()
    dropped_no_price = 0
    dropped_duplicate = 0

    for hotel in hotels:
        if not isinstance(hotel, dict):
            continue
        name = (hotel.get("name") or "").strip()
        if not name:
            continue
        price = _to_price(hotel)
        if price <= 0:
            dropped_no_price += 1
            continue
        if name in seen:
            dropped_duplicate += 1
            continue
        seen.add(name)
        cleaned.append(hotel)

    if not cleaned:
        # 价格全缺失有明确且高频的根因，直接说出来，否则调用方只能看到"0 家"
        if dropped_no_price:
            hint = (
                "\n    最常见原因：采集 profile 未登录携程。未登录时列表页只显示"
                "「登录以查看会员价」，价格元素是空 div，脚本能采到酒店名和房型"
                "却采不到价格。"
            )
            if profile_hint:
                hint += f"\n    当前 profile: {profile_hint}"
            hint += (
                "\n    修复：用有头浏览器打开该 profile 手动登录携程一次（长期有效），"
                "或把 SCRAPER_PROFILE 指向已有登录态的 profile 目录。"
            )
            raise DataValidationError(
                f"采集结果全部无效（价格缺失 {dropped_no_price} 条）。{hint}"
            )
        raise DataValidationError(
            f"采集结果全部无效（价格缺失 {dropped_no_price} 条、重复 {dropped_duplicate} 条）。"
        )

    expected_city = (expected_city or "").strip()
    if expected_city:
        city_hits = sum(1 for h in cleaned if expected_city in _hotel_text(h))
        other_hits = [
            h for h in cleaned
            if expected_city not in _hotel_text(h)
            and any(marker in _hotel_text(h) for marker in OTHER_CITY_MARKERS)
        ]
        threshold = max(2, len(cleaned) * 0.5)
        if city_hits == 0 and len(other_hits) >= threshold:
            sample = "、".join((h.get("name") or "")[:12] for h in other_hits[:3])
            raise DataValidationError(
                f"采集结果疑似来自其他城市：期望「{expected_city}」，"
                f"但 {len(other_hits)}/{len(cleaned)} 家酒店不含该城市且命中其他城市"
                f"（如 {sample}）。已整批拒绝，避免污染定价数据。"
                f" 提示: 采集 profile 可能有历史搜索残留，请更换 SCRAPER_PROFILE。"
            )

    return {
        "hotels": cleaned,
        "stats": {
            "total_raw": len(hotels),
            "total_valid": len(cleaned),
            "dropped_no_price": dropped_no_price,
            "dropped_duplicate": dropped_duplicate,
            "expected_city": expected_city,
            "expected_place": expected_place,
            "city_hits": sum(1 for h in cleaned if expected_city in _hotel_text(h)) if expected_city else 0,
        },
    }
