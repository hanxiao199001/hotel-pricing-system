"""房型归一化与房型→定价信号测试。

用构造的 summary 测纯函数，不依赖外部点评快照文件；
末尾另有一条在真实快照存在时校验结论方向的用例。
"""
import os

import pytest

import config as cfg
import room_reputation as rr


@pytest.fixture
def summary():
    """6 个原始房型覆盖 3 个档位，均分与差评率刻意做出梯度。"""
    return {
        "overall": {"count": 1000, "avg": 4.80, "bad": 20, "badRate": 2.0},
        "snapshot": {"dateMax": "2026-11-01", "kept": 1000},
        "rooms": [
            {"room": "豪华客房-大床丨滨海城景", "count": 600, "avg": 4.90, "bad": 6},
            {"room": "豪华客房-双床丨滨海城景", "count": 200, "avg": 4.80, "bad": 4},
            {"room": "行政商务房-大床|阔览城市景致", "count": 80, "avg": 4.60, "bad": 8},
            {"room": "行政商务房-双床|阔览城市景致", "count": 20, "avg": 4.60, "bad": 2},
            {"room": "豪华套房|一房一厅｜赏城市美景", "count": 10, "avg": 4.90, "bad": 0},
            {"room": "【家庭城景连通房】超大客厅+1张特大床+2张单床", "count": 90, "avg": 4.70, "bad": 0},
        ],
        "badRows": [
            {"room": "豪华客房-大床丨滨海城景", "themes": "设施老旧"},
            {"room": "行政商务房-大床|阔览城市景致", "themes": "设施老旧、卫生异味"},
        ],
    }


# ---------------------------------------------------------------------------
# 归一化
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name,tier,bed", [
    ("豪华客房-大床丨滨海城景", "豪华客房", "大床"),
    ("豪华客房-双床丨滨海城景", "豪华客房", "双床"),
    ("雅致豪华房-大床丨滨海城景", "雅致豪华房", "大床"),
    ("行政商务房-大床|阔览城市景致", "行政商务房", "大床"),
    ("豪华套房|一房一厅｜赏城市美景", "套房", "未标注"),
    ("君华套房｜一房一厅阔览城景", "套房", "未标注"),
    ("【家庭城景连通房】超大客厅+1张特大床+2张单床", "家庭连通房", "家庭"),
    ("云杉优眠精选豪华大床房", "云杉优眠精选", "大床"),
])
def test_normalize_room_rules(name, tier, bed):
    out = rr.normalize_room(name)
    assert out["tier"] == tier
    assert out["bed"] == bed
    assert out["raw"] == name


def test_suite_wins_over_executive():
    """「行政套房」应归到套房，不能被「行政」抢先匹配。"""
    assert rr.normalize_room("行政套房｜一房一厅阔览城景")["tier"] == "套房"


def test_elegant_is_not_luxury():
    """「雅致豪华房」含「豪华」二字，但不能被归进豪华客房。"""
    assert rr.normalize_room("雅致豪华房-双床丨滨海城景")["tier"] == "雅致豪华房"


def test_unknown_room_falls_back_without_data_loss():
    out = rr.normalize_room("太空舱主题房")
    assert out["tier"] == "其他"
    assert out["raw"] == "太空舱主题房"


# ---------------------------------------------------------------------------
# 档位聚合
# ---------------------------------------------------------------------------
def test_profiles_weighted_average(summary):
    """均分必须按点评数加权，不能对房型均值再取平均。"""
    profiles = {p["tier"]: p for p in rr.build_profiles(summary)}
    # (4.90*600 + 4.80*200) / 800 = 4.875 → 4.88
    assert profiles["豪华客房"]["avg"] == 4.88
    assert profiles["豪华客房"]["count"] == 800
    assert profiles["豪华客房"]["bad"] == 10
    assert profiles["豪华客房"]["badRate"] == 1.2


def test_profiles_sorted_by_volume(summary):
    tiers = [p["tier"] for p in rr.build_profiles(summary)]
    assert tiers[0] == "豪华客房"
    assert tiers[-1] == "套房"  # 10 条，点评量最少


def test_profiles_carry_top_themes(summary):
    profiles = {p["tier"]: p for p in rr.build_profiles(summary)}
    assert profiles["豪华客房"]["topThemes"][0] == {"label": "设施老旧", "count": 1}


# ---------------------------------------------------------------------------
# 判断
# ---------------------------------------------------------------------------
def test_signal_levels(summary):
    sig = rr.signal(summary)
    assert sig["available"] is True
    by_tier = {p["tier"]: p["signal"] for p in sig["profiles"]}

    # 1.2% vs 整体 2.0% → 强
    assert by_tier["豪华客房"]["level"] == "strong"
    # 套房只有 10 条 < 30，不下结论
    assert by_tier["套房"]["level"] == "insufficient"
    assert by_tier["套房"]["adjustRange"] is None


def test_weak_level_suggests_range(summary):
    """行政商务房 12.5% 是整体 2.0% 的 6 倍，应判溢价承压并给出让价区间。"""
    sig = rr.signal(summary)
    by_tier = {p["tier"]: p["signal"] for p in sig["profiles"]}
    weak = by_tier["行政商务房"]
    assert weak["level"] == "weak"
    low, high = weak["adjustRange"]
    # 区间是「-5% 到 -3%」：两端都是让价，且 low < high
    assert low < high < 0
    assert (low, high) == (-5, -3)
    assert "让" in weak["hint"]


def test_base_tier_auto_picks_largest(summary):
    sig = rr.signal(summary)
    assert sig["baseTier"] == "豪华客房"
    assert sig["baseTierSource"] == "auto"


def test_base_tier_can_be_configured(summary, monkeypatch):
    monkeypatch.setitem(cfg.ROOM_PRICING, "base_tier", "行政商务房")
    sig = rr.signal(summary)
    assert sig["baseTier"] == "行政商务房"
    assert sig["baseTierSource"] == "config"


def test_structure_note_mentions_worse_tier(summary):
    note = rr.signal(summary)["structureNote"]
    assert "行政商务房" in note
    assert "豪华客房" in note


def test_min_sample_threshold_respected(summary, monkeypatch):
    monkeypatch.setitem(cfg.ROOM_PRICING, "min_sample", 5)
    sig = rr.signal(summary)
    by_tier = {p["tier"]: p["signal"] for p in sig["profiles"]}
    assert by_tier["套房"]["level"] != "insufficient"


def test_signal_without_snapshot(monkeypatch):
    import reviews_analytics

    monkeypatch.setattr(reviews_analytics, "summary_or_none", lambda: None)
    sig = rr.signal(None)
    assert sig["available"] is False
    assert "快照" in sig["reason"]


def test_empty_rooms_returns_unavailable():
    sig = rr.signal({"overall": {}, "rooms": [], "badRows": []})
    assert sig["available"] is False


# ---------------------------------------------------------------------------
# 真实快照（存在才跑）
# ---------------------------------------------------------------------------
def _has_snapshot():
    import reviews_analytics

    return os.path.isfile(reviews_analytics.snapshot_path())


@pytest.mark.skipif(not _has_snapshot(), reason="没有点评快照")
def test_real_snapshot_finds_dominant_tier():
    import reviews_analytics

    sig = rr.signal(reviews_analytics.summarize())
    assert sig["available"] is True
    top = sig["profiles"][0]
    # 真实数据里豪华客房占绝对多数，应被识别为起价档位
    assert top["tier"] == "豪华客房"
    assert sig["baseTier"] == "豪华客房"
    assert top["count"] > 1000


# ---------------------------------------------------------------------------
# 恒等式：档位聚合并后不丢数据
# ---------------------------------------------------------------------------
def test_profiles_conserve_totals(summary):
    """Σ 分档 count/bad 必须等于整体，否则归并逻辑丢了数据。"""
    profiles = rr.build_profiles(summary)
    assert sum(p["count"] for p in profiles) == summary["overall"]["count"]
    assert sum(p["bad"] for p in profiles) == summary["overall"]["bad"]


@pytest.mark.skipif(not _has_snapshot(), reason="没有点评快照")
def test_real_snapshot_conserve_totals():
    """在真实快照上复核同一条恒等式。"""
    import reviews_analytics

    s = reviews_analytics.summarize()
    profiles = rr.build_profiles(s)
    assert sum(p["count"] for p in profiles) == s["overall"]["count"]
    assert sum(p["bad"] for p in profiles) == s["overall"]["bad"]


# ---------------------------------------------------------------------------
# structureNote 收紧：mixed 不进更弱名单
# ---------------------------------------------------------------------------
def test_structure_note_ignores_mixed_tier():
    """与基础档持平（mixed）的档位不进更弱名单，没有 weak 档位时整句不出。"""
    s = {
        "overall": {"count": 1000, "avg": 4.8, "bad": 20, "badRate": 2.0},
        "snapshot": {"dateMax": "2026-11-01", "kept": 1000},
        "rooms": [
            {"room": "豪华客房-大床", "count": 900, "avg": 4.9, "bad": 9},
            {"room": "雅致豪华房-大床", "count": 100, "avg": 4.8, "bad": 3},  # 1.5 倍 → mixed
        ],
        "badRows": [],
    }
    assert rr.signal(s)["structureNote"] == ""


# ---------------------------------------------------------------------------
# 统计窗口（ROOM_WINDOW_MONTHS）
# ---------------------------------------------------------------------------
def test_window_default_zero_keeps_full(monkeypatch):
    """窗口为 0（默认）时完全不切，行为与改动前一致。"""
    import reviews_analytics as ra

    monkeypatch.setitem(cfg.ROOM_PRICING, "window_months", 0)
    reviews = [
        {"room": "豪华客房-大床", "rating": 5.0, "created_at": "2026-10-01"},
        {"room": "豪华客房-大床", "rating": 1.0, "created_at": "2023-01-05"},
    ]
    monkeypatch.setattr(ra, "load", lambda: {"reviews": reviews})
    monkeypatch.setattr(ra, "summary_or_none", lambda: ra.summarize({"reviews": reviews}))
    sig = rr.signal()
    assert sig["windowMonths"] is None
    assert sig["overall"]["count"] == 2


def test_window_cuts_old_reviews(monkeypatch):
    """启用窗口后，窗口外的旧差评不再进入统计。"""
    import reviews_analytics as ra

    reviews = [
        {"room": "豪华客房-大床", "rating": 5.0, "created_at": "2026-10-01"},
        {"room": "豪华客房-大床", "rating": 5.0, "created_at": "2025-12-15"},
        {"room": "豪华客房-大床", "rating": 1.0, "created_at": "2023-01-05"},  # 应被切掉
    ]
    monkeypatch.setattr(ra, "load", lambda: {"reviews": reviews})
    monkeypatch.setattr(ra, "summary_or_none", lambda: ra.summarize({"reviews": reviews}))
    monkeypatch.setitem(cfg.ROOM_PRICING, "window_months", 12)
    sig = rr.signal()
    assert sig["windowMonths"] == 12
    assert sig["overall"]["count"] == 2   # 2023 年那条不在窗口内
    assert sig["overall"]["badRate"] == 0.0  # 窗口内没有差评


# ---------------------------------------------------------------------------
# 边界：整体零差评时有差评的档位判 weak，不再误判「持平」
# ---------------------------------------------------------------------------
def test_zero_overall_bad_rate_marks_weak():
    s = {
        "overall": {"count": 130, "avg": 4.9, "bad": 0, "badRate": 0.0},
        "snapshot": {"dateMax": "2026-11-01", "kept": 130},
        "rooms": [
            {"room": "豪华客房-大床", "count": 90, "avg": 5.0, "bad": 0},
            {"room": "行政商务房-大床", "count": 40, "avg": 4.5, "bad": 2},
        ],
        "badRows": [],
    }
    by = {p["tier"]: p["signal"] for p in rr.signal(s)["profiles"]}
    assert by["行政商务房"]["level"] == "weak"
    assert by["行政商务房"]["adjustRange"] == [-5, -3]
