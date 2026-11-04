"""点评快照统计与两页 block 生成的测试。

依赖真实的 ctrip_hotel_reviews.json。文件不在时整组跳过（别人 clone 仓库不会红），
不在时另有一组用「构造的小快照」验证计算逻辑本身。
"""
import os

import pytest

import reviews_analytics
from webapp import api, review_pages

REAL_SNAPSHOT = os.path.isfile(reviews_analytics.snapshot_path())
needs_snapshot = pytest.mark.skipif(not REAL_SNAPSHOT, reason="本机没有点评快照文件")


def _fake_snapshot(n_good=6, n_bad=3):
    reviews = []
    for i in range(n_good):
        reviews.append({
            "id": i,
            "rating": 5,
            "rating_detail": {"location": 5, "facility": 5, "service": 5, "room": 5},
            "content": "服务很好，房间干净，位置便利",
            "created_at": f"2026-0{1 + i % 3}-10 12:00:00",
            "room": "豪华大床房",
            "travel_type": "商务出差",
            "reply": "感谢您的入住",
        })
    bad_texts = [
        ("设施老旧，房间有霉味", 3.0),
        ("卫生间味道很大", 2.0),
        ("早餐难吃，设施太旧", 1.0),
    ]
    for i, (text, rating) in enumerate(bad_texts):
        reviews.append({
            "id": 100 + i,
            "rating": rating,
            "rating_detail": {"location": 5, "facility": 2, "service": 4, "room": 2},
            "content": text,
            "created_at": "2026-09-0%d 09:00:00" % (i + 1),
            "room": "豪华双床房",
            "travel_type": "家庭亲子",
            "reply": "" if i == 2 else "已安排工程部处理",
        })
    return {
        "status": "ok",
        "platform": "ctrip",
        "hotel_name": "测试酒店",
        "city": "海口",
        "before": "2026-10-01",
        "total_count": 100,
        "collected": len(reviews),
        "kept": len(reviews),
        "reviews": reviews,
        "_snapshot_at": "2026-10-31 01:00",
    }


@pytest.fixture
def fake_summary():
    return reviews_analytics.summarize(_fake_snapshot())


# ---------------------------------------------------------------------------
# 计算：用构造数据（与真实文件无关，永远能跑）
# ---------------------------------------------------------------------------
def test_thresholds_partition_all_reviews(fake_summary):
    o = fake_summary["overall"]
    assert o["good"] + o["neutral"] + o["bad"] == o["count"] == 9
    assert o["bad"] == 3  # 3.0 / 2.0 / 1.0 全部算差评


def test_star_distribution_sums_to_total(fake_summary):
    assert sum(d["count"] for d in fake_summary["starDist"]) == fake_summary["overall"]["count"]


def test_theme_count_never_exceeds_bad_count(fake_summary):
    bad = fake_summary["overall"]["bad"]
    for theme in fake_summary["themes"]:
        assert 0 < theme["count"] <= bad
    # 主题按命中条数降序
    counts = [t["count"] for t in fake_summary["themes"]]
    assert counts == sorted(counts, reverse=True)


def test_unreplied_counts_only_empty_reply(fake_summary):
    assert fake_summary["overall"]["unreplied"] == 1


def test_bad_rows_are_sorted_desc_and_flagged(fake_summary):
    rows = fake_summary["badRows"]
    assert [r["date"] for r in rows] == sorted((r["date"] for r in rows), reverse=True)
    assert all(r["rating"] <= 3 for r in rows)
    assert sum(1 for r in rows if r["replied"] == "未回复") == 1


def test_dimensions_sorted_by_weakness(fake_summary):
    avgs = [d["avg"] for d in fake_summary["dimensions"]]
    assert avgs == sorted(avgs)
    assert fake_summary["dimensions"][0]["label"] == "设施"


def test_coverage_is_ratio_of_kept_to_total(fake_summary):
    snap = fake_summary["snapshot"]
    assert snap["coverage"] == round(snap["kept"] / snap["totalCount"] * 100, 1)


def test_summary_is_deterministic():
    a = reviews_analytics.summarize(_fake_snapshot())
    b = reviews_analytics.summarize(_fake_snapshot())
    assert a["overall"] == b["overall"]
    assert a["themes"] == b["themes"]


# ---------------------------------------------------------------------------
# 快照缺失时的降级
# ---------------------------------------------------------------------------
def test_missing_snapshot_falls_back_to_static(monkeypatch, tmp_path):
    monkeypatch.setitem(reviews_analytics.cfg.REVIEWS_CONFIG, "path", str(tmp_path / "nope.json"))
    monkeypatch.setitem(
        reviews_analytics.cfg.REVIEWS_CONFIG, "legacy_path", str(tmp_path / "nope_legacy.json")
    )
    assert reviews_analytics.summary_or_none() is None
    mod = api.get_module("reviews")
    assert mod["source"] == "static"
    assert mod["blocks"]


def test_broken_snapshot_falls_back_to_static(monkeypatch, tmp_path):
    p = tmp_path / "broken.json"
    p.write_text("{not json", encoding="utf-8")
    monkeypatch.setitem(reviews_analytics.cfg.REVIEWS_CONFIG, "path", str(p))
    assert reviews_analytics.summary_or_none() is None
    assert api.get_module("reputation")["source"] == "static"


# ---------------------------------------------------------------------------
# 真实快照
# ---------------------------------------------------------------------------
@needs_snapshot
def test_real_snapshot_summary_is_consistent():
    s = reviews_analytics.summarize()
    o = s["overall"]
    assert o["count"] == len(s["badRows"]) + (o["count"] - o["bad"])
    assert 0 <= o["badRate"] <= 100
    assert s["snapshot"]["dateMax"] <= s["snapshot"]["before"]
    assert len(s["monthly"]) <= reviews_analytics.cfg.REVIEWS_CONFIG["monthly_window"]


@needs_snapshot
def test_real_bad_reviews_have_reply_field():
    s = reviews_analytics.summarize()
    for row in s["badRows"]:
        assert row["replied"] in ("已回复", "未回复")
        assert row["replied"] == ("已回复" if row["reply"] else "未回复")


@needs_snapshot
def test_pages_use_snapshot_source_with_renderable_blocks():
    allowed = {"kpi", "list", "table", "bars", "quote", "banner", "note"}
    for key in ("reviews", "reputation"):
        mod = api.get_module(key)
        assert mod["source"] == "snapshot"
        assert mod["status"] == "snapshot"
        assert mod["statusNote"]
        types = {b["type"] for b in mod["blocks"]}
        assert types <= allowed
        assert "kpi" in types


@needs_snapshot
def test_reviews_page_bars_need_max_for_count_scaling():
    """差评条数是计数不是百分比，必须给 bars 一个 max，否则柱状图全被压到 2%。"""
    mod = api.get_module("reviews")
    bars = [b for b in mod["blocks"] if b["type"] == "bars"][0]
    assert bars["suffix"].strip() == "条"
    values = [it["value"] for it in bars["items"]]
    assert bars["max"] >= max(values)


@needs_snapshot
def test_reputation_does_not_claim_seaview():
    """回归：早期示例数据写「海景是最大加分项」，实测只有 3 条提到海景，房型是城景。"""
    mod = api.get_module("reputation")
    labels = [
        it["label"]
        for b in mod["blocks"] if b["type"] == "bars"
        for it in b["items"]
    ]
    assert not any("海景" in label for label in labels)


@needs_snapshot
def test_review_pages_builder_direct():
    s = reviews_analytics.summarize()
    for key in ("reviews", "reputation"):
        mod = review_pages.build(key)
        assert mod and mod["snapshot"]["hotel"]
        assert len(mod["blocks"]) >= 3
    assert review_pages.build("reports") is None


@needs_snapshot
def test_status_module_list_marks_snapshot(monkeypatch):
    """系统状态页的模块清单不能把口碑两页再标成「示例」。"""
    # reports 可能被本机真实 eBook 快照接管（变成 snapshot）；
    # 这里模拟没有 eBook 采集，验证它保持示例。
    monkeypatch.setattr(api.ebook_pages, "_load_snapshot", lambda: None)
    mods = {m["key"]: m for m in api._modules_with_status()}
    for key in ("reviews", "reputation"):
        assert mods[key]["status"] == "snapshot"
        assert "真实点评快照" in mods[key]["statusNote"]
    # 其余模块保持示例
    assert mods["reports"]["status"] == "static"


def test_status_module_list_stays_static_without_snapshot(monkeypatch, tmp_path):
    monkeypatch.setitem(reviews_analytics.cfg.REVIEWS_CONFIG, "path", str(tmp_path / "x.json"))
    monkeypatch.setitem(
        reviews_analytics.cfg.REVIEWS_CONFIG, "legacy_path", str(tmp_path / "y.json")
    )
    mods = {m["key"]: m for m in api._modules_with_status()}
    assert mods["reviews"]["status"] == "static"
