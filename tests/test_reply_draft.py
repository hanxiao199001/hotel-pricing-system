"""差评回复草稿测试。

重点在两条：
  1. 绝不能编造整改措施（必须有 {{整改动作}} 占位）；
  2. 归类误判时不能顺着错误结论道歉（「服务真心不错」不能生成服务致歉）。
"""
import pytest

import app.reviews.reply_draft as rd


def _row(**kw):
    base = {
        "date": "2026-09-11",
        "rating": 2.0,
        "room": "豪华客房-大床丨滨海城景",
        "summary": "房间霉味很重",
        "themes": "卫生异味",
        "full": "房间霉味很重，床单也是潮的",
        "reply": "",
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# review_key
# ---------------------------------------------------------------------------
def test_review_key_is_stable():
    a = rd.review_key(_row())
    assert a == rd.review_key(_row())
    assert len(a) == 16


def test_review_key_changes_with_content():
    assert rd.review_key(_row()) != rd.review_key(_row(full="完全不同的内容"))
    assert rd.review_key(_row()) != rd.review_key(_row(date="2026-09-12"))
    assert rd.review_key(_row()) != rd.review_key(_row(rating=1.0))


# ---------------------------------------------------------------------------
# 结构与合规
# ---------------------------------------------------------------------------
def test_draft_has_required_shape():
    d = rd.build_draft(_row(), hotel_name="海南君华海逸酒店(海口万绿园万象城店)")
    assert d["available"] is True
    for field in ("text", "placeholders", "checklist", "outline", "themes",
                  "tone", "wordCount", "source"):
        assert field in d
    assert d["source"] == "template"


def test_draft_always_leaves_action_placeholder():
    """没有任何模板可以自己编整改措施——否则就是假承诺。"""
    for themes in ("设施老旧", "卫生异味", "服务态度", "早餐餐饮", "性价比",
                   "隔音噪音", "泳池健身", "发票开票", "未归类", ""):
        d = rd.build_draft(_row(themes=themes), hotel_name="测试酒店")
        assert "{{整改动作}}" in d["text"], themes
        assert d["placeholders"][0]["key"] == "整改动作"
        assert d["placeholders"][0]["example"]


def test_draft_has_no_fabricated_claims():
    """模板正文里不能出现确定性动作措辞（那是占位符要解决的问题）。"""
    d = rd.build_draft(_row(), hotel_name="测试酒店")
    for word in ("已更换", "已全部", "已完成整改", "已召回"):
        assert word not in d["text"]


def test_hotel_name_is_shortened():
    d = rd.build_draft(_row(), hotel_name="海南君华海逸酒店(海口万绿园万象城店)")
    assert "海南君华海逸酒店" in d["text"]
    assert "万象城" not in d["text"]  # 门店后缀不该出现在正文和署名里


def test_room_tier_in_opening():
    d = rd.build_draft(_row(), hotel_name="测试酒店")
    assert "豪华客房" in d["text"]
    assert d["roomTier"] == "豪华客房"


# ---------------------------------------------------------------------------
# 语气分档
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("rating,expect_word,tone", [
    (1.0, "非常抱歉", "郑重"),
    (2.0, "非常抱歉", "郑重"),
    (2.5, "抱歉这次没达到", "平和"),
    (3.0, "抱歉这次没达到", "平和"),
])
def test_apology_strength_by_rating(rating, expect_word, tone):
    d = rd.build_draft(_row(rating=rating), hotel_name="测试酒店")
    assert expect_word in d["text"]
    assert d["tone"] == tone


def test_harsh_review_offers_direct_contact():
    harsh = rd.build_draft(_row(rating=1.5), hotel_name="测试酒店")
    mild = rd.build_draft(_row(rating=3.0), hotel_name="测试酒店")
    assert "直接联系我们" in harsh["text"]
    assert "直接联系我们" not in mild["text"]


# ---------------------------------------------------------------------------
# 归类误判的处理
# ---------------------------------------------------------------------------
def test_praised_service_is_not_apologized():
    """「服务真心不错」被归进服务问题，草稿不能顺着道歉。"""
    row = _row(
        rating=2.5,
        themes="设施老旧、卫生异味、服务态度",
        full="太懒了\n服务真心不错\n设施 房间不行\n霉味儿重",
    )
    d = rd.build_draft(row, hotel_name="测试酒店")
    assert "服务态度" in d["droppedThemes"]
    assert "服务态度" not in d["themes"]
    assert "同事的响应" not in d["text"]
    assert any("误判" in w for w in d["warnings"])


def test_real_service_complaint_still_gets_apology():
    row = _row(rating=1.0, themes="服务态度", full="前台态度极差，投诉没人管")
    d = rd.build_draft(row, hotel_name="测试酒店")
    assert "服务态度" in d["themes"]
    assert "同事的响应" in d["text"]
    assert not d["warnings"]


# ---------------------------------------------------------------------------
# 主题数量与兜底
# ---------------------------------------------------------------------------
def test_at_most_two_themes_expanded():
    row = _row(themes="设施老旧、卫生异味、服务态度、性价比",
               full="设施老旧，卫生差，服务态度不好，价格还贵")
    d = rd.build_draft(row, hotel_name="测试酒店")
    assert len(d["themes"]) <= 2


def test_unclassified_falls_back_with_warning():
    d = rd.build_draft(_row(themes="未归类", full="各方面都比较一般"), hotel_name="测试酒店")
    assert d["themes"] == []
    assert "整改动作" in d["text"]
    assert any("通用话术" in w or "没命中" in w for w in d["warnings"])


def test_empty_content_is_not_drafted():
    d = rd.build_draft(_row(full="", summary=""), hotel_name="测试酒店")
    assert d["available"] is False
    assert d["reason"]


# ---------------------------------------------------------------------------
# 批量
# ---------------------------------------------------------------------------
def test_build_all_skips_empty_and_respects_limit():
    rows = [
        {"date": "2026-09-01", "rating": 1.0, "room": "豪华客房-大床丨滨海城景",
         "themes": "设施老旧", "full": "设施太旧了"},
        {"date": "2026-09-02", "rating": 2.0, "room": "", "themes": "", "full": ""},
        {"date": "2026-09-03", "rating": 3.0, "room": "", "themes": "卫生异味",
         "full": "打扫不干净"},
    ]
    out = rd.build_all({"badRows": rows}, hotel_name="测试酒店", limit=1)
    assert len(out) == 1
    assert out[0]["rating"] == 1.0

    out_all = rd.build_all({"badRows": rows}, hotel_name="测试酒店")
    assert len(out_all) == 2  # 空正文那条被跳过


# ---------------------------------------------------------------------------
# 真实快照（存在才跑）
# ---------------------------------------------------------------------------
def test_real_snapshot_drafts_are_usable():
    import os

    import app.reviews.analytics as reviews_analytics

    if not os.path.isfile(reviews_analytics.snapshot_path()):
        pytest.skip("没有点评快照")

    summary = reviews_analytics.summarize()
    drafts = rd.build_all(summary, hotel_name=summary["snapshot"].get("hotel") or "")
    assert len(drafts) == len(summary["badRows"])
    for d in drafts:
        assert "{{整改动作}}" in d["text"]
        assert 100 <= d["wordCount"] <= 400
        assert d["text"].startswith("尊敬的客人")
