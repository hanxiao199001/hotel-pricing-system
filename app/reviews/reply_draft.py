"""差评回复草稿。

设计前提：**系统不知道酒店实际做了什么整改**。
所以这个模块绝不自己编「已更换全部床品」这类事实，凡是涉及动作的句子
一律留占位符 {{整改动作}}，让人按实际情况替换。宁可留一个空格让人填，
也不写一句听起来漂亮但可能是假的承诺——那会变成二次差评的素材。

输出结构：
    text         可直接复制到携程商家后台的正文（含占位符）
    placeholders 需要人工替换的位置 + 建议措辞
    checklist    发布前的自查项
    warnings     归类可能不准时的提示（例如「服务被夸却被告知是短板」）
    outline      分段结构，便于前端按段高亮

模板覆盖 8 个差评主题，未归类走通用话术。
LLM 增强在 llm.py，本模块不依赖任何外部服务，离线可用。
"""
import hashlib
import re

from app.reviews import reputation as room_reputation

# 每个主题三段：承认问题 / 建议的整改动作（作为占位符示例）/ 后续保障
_THEME_COPY = {
    "设施老旧": {
        "ack": "您提到的设施陈旧问题，确实是我们现在最需要补的短板",
        "action": "已安排工程部逐间排查空调、卫浴与家具状态，并列出更换计划",
        "care": "在您下次入住前会先确认房间设备是否完好",
    },
    "卫生异味": {
        "ack": "房间有异味、清洁不到位，这是我们不该出现的问题",
        "action": "已让客房部按标准重新做房，并增加异味与布草的专项检查",
        "care": "入住前会再做一次空气与床品复核",
    },
    "服务态度": {
        "ack": "同事的响应没能让您满意，这是我们交接和培训没做到位",
        "action": "已就您反馈的情况做内部复盘，并加强前台交接与响应时效",
        "care": "下次入住可以直接找值班经理，由他全程跟进",
    },
    "早餐餐饮": {
        "ack": "早餐的选择和口味没能达到您的预期",
        "action": "已把您的意见转给餐饮部，调整菜品结构与出品时间",
        "care": "如果有忌口或特别需要，可以提前告知，我们单独准备",
    },
    "性价比": {
        "ack": "您觉得这次的价格和体验不匹配，这个反馈我们收到了",
        "action": "正在复核同档房型的价格与服务内容是否对得上",
        "care": "预订时可以直接问我们要当下最合适的方案",
    },
    "隔音噪音": {
        "ack": "隔音影响到了您的休息，很抱歉",
        "action": "已安排工程部排查该房型临近的通风口与门窗密封",
        "care": "下次可以为您安排远离电梯与主路的房间",
    },
    "泳池健身": {
        "ack": "康乐设施没能让您用得舒心",
        "action": "已核查开放时间与设备维护记录",
        "care": "入住时可以提前跟我们确认开放时段",
    },
    "发票开票": {
        "ack": "开票流程给您添了麻烦",
        "action": "已梳理开票环节，缩短处理时间",
        "care": "可以在退房前把开票信息给我们，提前准备好",
    },
}

_GENERIC = {
    "ack": "您的反馈我们逐条看过，没有让您住得舒心是我们的问题",
    "action": "已把您提到的情况转给相关部门核查",
    "care": "下次入住前可以告诉我们您的偏好",
}

# 「服务」被夸却仍被归进服务问题的典型句式。归类统计照旧，
# 但回复草稿不能顺着错误结论去道歉，否则客人会觉得你没读他的评价。
_SERVICE_PRAISE = re.compile(
    r"服务(很|挺|真心|确实|还是|也|都)?(不错|很好|好|棒|满意|热情|到位|贴心)"
)
_SERVICE_BLAME = re.compile(r"服务(很|太|真)?(差|糟|不行|冷漠|不到位)|态度(很|太)?差|前台(很|太)?(冷漠|差)")


def review_key(row: dict) -> str:
    """给一条点评一个稳定标识，用于草稿缓存与「已采用」状态。

    用日期 + 评分 + 正文哈希，不用列表下标——重新采集后顺序会变，
    下标一变，「已采用」的标记就错位到别的点评上了。
    """
    base = "|".join([
        str(row.get("date") or ""),
        str(row.get("rating") or ""),
        str(row.get("full") or row.get("summary") or ""),
    ])
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]


def _room_desc(row: dict) -> str:
    info = room_reputation.normalize_room(row.get("room"))
    if info["tier"] == "其他":
        return ""
    return info["tier"]


def _themes_of(row: dict):
    raw = row.get("themes") or ""
    return [t.strip() for t in raw.split("、") if t.strip() and t.strip() != "未归类"]


def _short_hotel(name: str) -> str:
    """OTA 上的全名常带门店后缀（「海南君华海逸酒店(海口万绿园万象城店)」），
    写进回复正文和署名会显得生硬，去掉括号部分只用主体名。"""
    text = (name or "").strip()
    if not text:
        return "酒店"
    short = re.sub(r"[（(].*?[）)]", "", text).strip()
    return short or text


def _dedupe_copy(themes):
    """多主题时只保留最需要回应的两个，避免回复变成清单。

    超过两个主题的差评通常是「整体不满意」，逐条罗列反而像模板。
    """
    picked = []
    for t in themes:
        if t in _THEME_COPY and t not in picked:
            picked.append(t)
    return picked[:2]


def build_draft(row: dict, hotel_name: str = "", platform: str = "携程") -> dict:
    """按一条差评生成回复草稿。

    Args:
        row: 差评明细（reviews_analytics.summarize()['badRows'] 的元素）
        hotel_name: 酒店名，用于开头致谢；留空则用通用称呼
        platform: 平台名，仅用于说明文案

    Returns:
        dict，见模块 docstring。不可生成时返回 {"available": False, "reason": ...}
    """
    content = (row.get("full") or "").strip()
    if not content:
        return {"available": False, "reason": "这条点评没有正文，没有可回应的具体内容。"}

    themes = _themes_of(row)
    warnings = []
    dropped = []

    # 「服务」被夸却被归成服务问题：归类统计不动，但回复不能顺着道歉
    if "服务态度" in themes:
        if _SERVICE_PRAISE.search(content) and not _SERVICE_BLAME.search(content):
            themes = [t for t in themes if t != "服务态度"]
            dropped.append("服务态度")
            warnings.append(
                "这条被归类为「服务态度」，但原文里在夸服务"
                "（例如「服务真心不错」），可能是关键词误判。"
                "已按正面处理，不生成道歉句；统计口径未改动。"
            )

    picked = _dedupe_copy(themes)
    copies = [_THEME_COPY[t] for t in picked] or [_GENERIC]
    if not picked:
        warnings.append("没有命中具体主题，草稿用的是通用话术，建议人工补充细节。")

    rating = float(row.get("rating") or 0)
    is_harsh = rating <= 2
    room = _room_desc(row)
    hotel = _short_hotel(hotel_name)

    # 道歉强度按分数分档：1–2 分要正面认错，2–3 分是「没达到预期」，
    # 一上来就「非常抱歉」用在 2.9 分上会显得过度。
    if rating <= 2:
        apology = "非常抱歉这次没让您住好。"
        tone = "郑重"
    elif rating <= 3:
        apology = "抱歉这次没达到您的预期。"
        tone = "平和"
    else:
        apology = ""
        tone = "平和"

    parts = []
    greeting = "尊敬的客人，您好："
    parts.append(greeting)

    if apology:
        parts.append(apology)

    opening = f"感谢您选择{hotel}"
    if room:
        opening += f"，也谢谢您把{room}的入住体验写下来"
    else:
        opening += "，也谢谢您把这次入住体验写下来"
    thanks = opening + "。"
    parts.append(thanks)

    ack = "；".join(c["ack"] for c in copies)
    # 整改动作一律留占位符；后半句是「后续保障」，独立成句才通顺
    # （care 的句式各不相同，接在「并会」后面会出现「并会下次入住可以…」这种病句）
    response = f"{ack}。我们{{{{整改动作}}}}。{copies[0]['care']}。"
    parts.append(response)

    closing = "如果方便，欢迎您下次再来，我们会提前为您安排好房间"
    if is_harsh:
        closing += "；也欢迎您直接联系我们，让我们有机会把这次没做好的地方补上"
    closing += "。"
    parts.append(closing)

    sign = f"{hotel} 客服团队"
    parts.append(sign)

    text = "\n\n".join(parts)

    placeholders = [{
        "key": "整改动作",
        "label": "整改动作（必须按实际情况填）",
        "example": "；".join(c["action"] for c in copies),
        "note": "系统不知道酒店实际做了什么，这句留空是因为不能替你编。",
    }]

    checklist = [
        "「整改动作」是否属实？不确定就写「已记录并转交相关部门」，别写具体承诺。",
        "是否已经或需要私下联系客人？",
        "回复里有没有暴露客人隐私（房号、姓名、同行人）？",
        f"发布到{platform}后台后，回填到系统并标记「已采用」。",
    ]

    return {
        "available": True,
        "key": review_key(row),
        "text": text,
        "placeholders": placeholders,
        "checklist": checklist,
        "warnings": warnings,
        "outline": (
            [{"role": "称呼", "text": greeting}]
            + ([{"role": "致歉", "text": apology}] if apology else [])
            + [
                {"role": "致谢", "text": thanks},
                {"role": "回应", "text": response},
                {"role": "结语", "text": closing},
                {"role": "署名", "text": sign},
            ]
        ),
        "themes": picked,
        "skippedThemes": [t for t in themes if t not in picked],
        "droppedThemes": dropped,
        "room": row.get("room"),
        "roomTier": room or None,
        "rating": rating,
        "date": row.get("date"),
        "tone": tone,
        "wordCount": len(re.sub(r"\s", "", text)),
        "source": "template",
        "platform": platform,
        "alreadyReplied": bool((row.get("reply") or "").strip()),
    }


def build_all(summary: dict, hotel_name: str = "", limit: int = 0) -> list:
    """批量生成，按时间倒序（badRows 本身已是倒序）。"""
    rows = (summary or {}).get("badRows") or []
    out = []
    for row in rows:
        draft = build_draft(row, hotel_name=hotel_name)
        if draft.get("available"):
            out.append(draft)
        if limit and len(out) >= limit:
            break
    return out
