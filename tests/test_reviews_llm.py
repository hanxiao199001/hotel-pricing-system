"""点评归因模型层的测试。

全部用 mock 的模型返回，不碰网络。重点验证三件事：
  1. 出处校验——引文对不上真实原文的依据必须被丢弃，依据全废的问题整条丢弃；
  2. 缓存——同一份快照第二次调用不打模型，快照变了才重算；
  3. 降级——不可用或输出不合规时返回 None，页面只剩规则文案。
"""
import json

import pytest

import config as cfg
import llm
import reviews_llm as rl
from webapp import review_pages as rp


BAD_ROWS = [
    {"date": "2026-02-11", "rating": 1.0, "room": "豪华客房-大床",
     "full": "房间很旧，空调有异味，前台态度也不好", "themes": "设施老旧、卫生异味、服务态度"},
    {"date": "2026-03-05", "rating": 2.0, "room": "行政商务房-大床",
     "full": "隔音太差，半夜被走廊吵醒", "themes": "隔音噪音"},
]


def _summary(path="missing.json"):
    return {
        "snapshot": {"path": path, "at": "2026-11-03 09:07", "hotel": "测试酒店",
                     "dateMin": "2023-01-01", "dateMax": "2026-10-31", "kept": 1975},
        "overall": {"count": 1975, "avg": 4.88, "bad": 2, "badRate": 0.1,
                    "good": 1900, "neutral": 73, "unreplied": 0},
        "thresholds": {"goodRating": 4.0, "badRating": 3.0},
        "dimensions": [{"label": "设施", "avg": 4.85, "count": 1900},
                       {"label": "服务", "avg": 4.91, "count": 1900}],
        "themes": [{"label": "设施老旧", "count": 19, "ratio": 63.3},
                   {"label": "服务态度", "count": 11, "ratio": 36.7}],
        "travelTypes": [{"label": "家庭亲子", "count": 800, "ratio": 40.5}],
        "badRows": [dict(r) for r in BAD_ROWS],
    }


GOOD = {
    "headline": "问题集中在客房硬件",
    "issues": [
        {"label": "客房翻新", "severity": "high",
         "reasoning": "老旧与异味指向同一批客房的翻新需求",
         "evidence": [{"date": "2026-02-11", "quote": "房间很旧，空调有异味"}]},
        {"label": "走廊隔音", "severity": "medium",
         "reasoning": "两处都提到走廊噪音",
         "evidence": [{"date": "2026-03-05", "quote": "隔音太差，半夜被走廊吵醒"}]},
    ],
    "actions": ["分批翻新空调与软装", "走廊加装隔音条"],
}


@pytest.fixture
def llm_env(tmp_path, monkeypatch):
    """让 available() 通过，并把缓存写到临时目录，不污染项目 data/cache。"""
    monkeypatch.setitem(cfg.LLM_CONFIG, "base_url", "https://example.invalid/v1")
    monkeypatch.setitem(cfg.LLM_CONFIG, "api_key", "test-key")
    monkeypatch.setitem(cfg.LLM_CONFIG, "model", "test-model")
    monkeypatch.setitem(cfg.REVIEWS_LLM_CONFIG, "enabled", True)
    monkeypatch.setitem(cfg.REVIEWS_LLM_CONFIG, "cache", str(tmp_path / "cache.json"))
    rl.reset()          # 进程内失败标记是模块级的，用例之间必须清
    yield tmp_path
    rl.reset()


@pytest.fixture
def fake_chat(monkeypatch):
    """替换模型调用，记录次数，返回预设 JSON。"""
    calls = []

    def _fake(system, user, **kwargs):
        calls.append({"system": system, "user": user, "kwargs": kwargs})
        return json.dumps(GOOD, ensure_ascii=False)

    monkeypatch.setattr(llm, "chat", _fake)
    return calls


# ---------------------------------------------------------------------------
# 出处校验
# ---------------------------------------------------------------------------
def test_match_row_exact_substring():
    assert rl._match_row("空调有异味", BAD_ROWS) is BAD_ROWS[0]


def test_match_row_ignores_whitespace():
    assert rl._match_row("房间很旧， 空调有异味", BAD_ROWS) is BAD_ROWS[0]


def test_match_row_falls_back_to_prefix():
    """模型爱加省略号，开头对得上就算同一句。"""
    assert rl._match_row("隔音太差，半夜被走廊…", BAD_ROWS) is BAD_ROWS[1]


def test_match_row_rejects_fabrication():
    assert rl._match_row("床垫塌陷弹簧外露", BAD_ROWS) is None


def test_match_row_rejects_too_short():
    assert rl._match_row("差", BAD_ROWS) is None


def test_validate_drops_fabricated_evidence():
    """依据全是编的 → 整个结论不可用。"""
    payload = {"headline": "编的结论", "issues": [
        {"label": "床垫问题", "severity": "high", "reasoning": "客人反映床垫塌陷",
         "evidence": [{"date": "2026-02-11", "quote": "床垫塌陷弹簧外露"}]},
    ], "actions": []}
    assert rl._validate(payload, BAD_ROWS) is None


def test_validate_drops_issue_but_keeps_others():
    payload = {"headline": "x", "issues": [
        {"label": "编的", "severity": "high", "reasoning": "r",
         "evidence": [{"date": "2026-02-11", "quote": "床垫塌陷"}]},
        GOOD["issues"][0],
    ], "actions": []}
    out = rl._validate(payload, BAD_ROWS)
    assert [i["label"] for i in out["issues"]] == ["客房翻新"]
    assert out["dropped"]["issues"] == 1


def test_validate_corrects_wrong_date():
    """模型引对了原文但写错日期，用原文里的真实日期，不丢这条依据。"""
    payload = {"headline": "x", "issues": [
        {"label": "隔音", "severity": "medium", "reasoning": "r",
         "evidence": [{"date": "2026-99-99", "quote": "隔音太差，半夜被走廊吵醒"}]},
    ], "actions": []}
    out = rl._validate(payload, BAD_ROWS)
    assert out["issues"][0]["evidence"][0]["date"] == "2026-03-05"


def test_validate_normalizes_bad_severity():
    payload = {"headline": "x", "issues": [
        {"label": "隔音", "severity": "致命", "reasoning": "r",
         "evidence": [{"date": "2026-03-05", "quote": "隔音太差"}]},
    ], "actions": []}
    assert rl._validate(payload, BAD_ROWS)["issues"][0]["severity"] == "medium"


def test_validate_caps_issues_and_keeps_actions():
    payload = {"headline": "x",
               "issues": [GOOD["issues"][0]] * 10,
               "actions": ["a", "b", "c", "d", "e", "f"]}
    out = rl._validate(payload, BAD_ROWS)
    assert len(out["issues"]) == rl.MAX_ISSUES
    assert len(out["actions"]) == 4


def test_validate_returns_none_on_empty():
    assert rl._validate({"headline": "", "issues": [], "actions": []}, BAD_ROWS) is None


# ---------------------------------------------------------------------------
# 输出解析
# ---------------------------------------------------------------------------
def test_parse_fenced_json():
    raw = '```json\n{"headline": "x", "issues": [], "actions": []}\n```'
    assert rl._parse(raw)["headline"] == "x"


def test_parse_with_leading_prose():
    raw = '好的，分析如下：\n{"headline": "x", "issues": [], "actions": []}\n以上。'
    assert rl._parse(raw)["headline"] == "x"


def test_parse_rejects_non_json():
    with pytest.raises(ValueError):
        rl._parse("抱歉，我无法完成这个分析")


def test_parse_salvages_truncated_output():
    """输出被 max_tokens 截断时，已闭合的问题对象必须能抢救出来。

    这是真实踩到的坑：1600 token 下第四个问题写到一半就断了，
    整批丢弃等于白花一次调用。
    """
    raw = ('{"headline": "问题集中在硬件", "issues": ['
           '{"label": "客房翻新", "severity": "high", "reasoning": "r",'
           ' "evidence": [{"date": "2026-02-11", "quote": "房间很旧"}]},'
           '{"label": "隔音", "severity": "medium", "reasoning": "写到一半就断了')
    out = rl._parse(raw)
    assert out["headline"] == "问题集中在硬件"
    assert [i["label"] for i in out["issues"]] == ["客房翻新"]


def test_salvage_rejects_unclosed_single_issue():
    """连一个完整对象都没有时不做猜测性补全。"""
    raw = '{"headline": "x", "issues": [{"label": "客房翻新", "severity": "hi'
    with pytest.raises(ValueError):
        rl._parse(raw)


# ---------------------------------------------------------------------------
# 缓存与降级
# ---------------------------------------------------------------------------
def test_analyze_writes_and_reuses_cache(llm_env, fake_chat):
    s = _summary()
    first = rl.analyze(s)
    second = rl.analyze(s)
    assert len(fake_chat) == 1, "第二次应命中缓存，不该再调模型"
    assert first["issues"][0]["label"] == second["issues"][0]["label"]
    assert (llm_env / "cache.json").exists()


def test_cache_carries_generated_at(llm_env, fake_chat):
    rl.analyze(_summary())
    assert rl.analyze(_summary()).get("generatedAt")


def test_force_bypasses_cache(llm_env, fake_chat):
    rl.analyze(_summary())
    rl.analyze(_summary(), force=True)
    assert len(fake_chat) == 2


def test_cache_key_tracks_snapshot(llm_env):
    p = llm_env / "snap.json"
    p.write_text("a", encoding="utf-8")
    k1 = rl._cache_key(_summary(str(p)))
    p.write_text("a-longer-body", encoding="utf-8")
    k2 = rl._cache_key(_summary(str(p)))
    assert k1 != k2, "快照变了必须重算"


def test_cache_key_tracks_model(llm_env, monkeypatch):
    k1 = rl._cache_key(_summary())
    monkeypatch.setitem(cfg.LLM_CONFIG, "model", "another-model")
    assert rl._cache_key(_summary()) != k1


def test_unavailable_returns_none(llm_env, monkeypatch, fake_chat):
    monkeypatch.setitem(cfg.REVIEWS_LLM_CONFIG, "enabled", False)
    assert rl.analyze(_summary()) is None
    assert not fake_chat


def test_llm_error_returns_none_without_cache(llm_env, monkeypatch):
    def _boom(*a, **kw):
        raise llm.LLMError("超时")

    monkeypatch.setattr(llm, "chat", _boom)
    assert rl.analyze(_summary()) is None
    assert not (llm_env / "cache.json").exists()


def test_invalid_output_returns_none_without_cache(llm_env, monkeypatch):
    monkeypatch.setattr(llm, "chat", lambda *a, **kw: "我无法分析这些点评")
    assert rl.analyze(_summary()) is None
    assert not (llm_env / "cache.json").exists()


def test_no_bad_rows_returns_none(llm_env, fake_chat):
    s = _summary()
    s["badRows"] = []
    assert rl.analyze(s) is None
    assert not fake_chat


def test_failure_is_not_retried_in_same_process(llm_env, monkeypatch):
    """失败一次就记住，别让页面每次刷新都白等一个超时。"""
    calls = []

    def _boom(*a, **kw):
        calls.append(1)
        raise llm.LLMError("请求超时（25 秒）")

    monkeypatch.setattr(llm, "chat", _boom)
    s = _summary()
    assert rl.analyze(s) is None
    assert rl.analyze(s) is None
    assert len(calls) == 1


def test_force_retries_after_failure(llm_env, monkeypatch, fake_chat):
    def _boom(*a, **kw):
        raise llm.LLMError("请求超时")

    monkeypatch.setattr(llm, "chat", _boom)
    rl.analyze(_summary())
    monkeypatch.setattr(llm, "chat", lambda *a, **kw: json.dumps(GOOD, ensure_ascii=False))
    assert rl.analyze(_summary(), force=True)["issues"]


def test_summarize_for_page_surfaces_failure_reason(llm_env, monkeypatch):
    monkeypatch.setattr(llm, "chat", lambda *a, **kw: "抱歉，无法完成")
    out = rl.summarize_for_page(_summary())
    assert out["available"] is False
    assert "JSON" in out["reason"]


def test_summarize_for_page_reports_reason(llm_env, monkeypatch):
    monkeypatch.setitem(cfg.REVIEWS_LLM_CONFIG, "enabled", False)
    out = rl.summarize_for_page(_summary())
    assert out["available"] is False and out["reason"]


# ---------------------------------------------------------------------------
# 输入构造：数字来自统计，不是模型算的
# ---------------------------------------------------------------------------
def test_prompt_carries_precomputed_stats_and_raw_text(llm_env):
    prompt = rl._user_prompt(_summary(), rl._samples(_summary()))
    assert "差评主题提及次数" in prompt and "设施老旧 19" in prompt
    assert "四维均分" in prompt and "设施 4.85" in prompt
    assert "房间很旧，空调有异味，前台态度也不好" in prompt
    assert "不要重算" in prompt


def test_samples_sort_by_lowest_rating(llm_env):
    out = rl._samples(_summary())
    assert [r["rating"] for r in out] == [1.0, 2.0]


# ---------------------------------------------------------------------------
# 页面集成
# ---------------------------------------------------------------------------
def test_page_blocks_empty_when_disabled(llm_env, monkeypatch):
    """不可用时整块消失，不留空壳。"""
    monkeypatch.setitem(cfg.REVIEWS_LLM_CONFIG, "enabled", False)
    assert rp._llm_blocks(_summary()) == []


def test_page_blocks_render_issues(llm_env, fake_chat):
    blocks = rp._llm_blocks(_summary())
    kinds = [b["type"] for b in blocks]
    assert "table" in kinds and "banner" in kinds

    table = next(b for b in blocks if b["type"] == "table")
    assert [r["label"] for r in table["rows"]] == ["客房翻新", "走廊隔音"]
    assert table["rows"][0]["severity"] == "高"
    assert table["rows"][0]["severityTone"] == "warm"
    assert table["rows"][0]["evidence"] == "1 条"


def test_page_blocks_show_actions_and_provenance(llm_env, fake_chat):
    notes = [b["text"] for b in rp._llm_blocks(_summary()) if b["type"] == "note"]
    assert any("整改建议" in t for t in notes)
    assert any("规则计算" in t for t in notes), "必须声明数字不来自模型"
