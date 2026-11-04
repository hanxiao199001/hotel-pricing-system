"""点评采集链路的测试：命令拼装、结果校验、快照落盘、任务调度。

不真的启动浏览器 —— 子进程和 playwright 的行为由 validate/collect 的边界保证，
这里只验证「我们这一侧的契约」。
"""
import json
import os
import time

import pytest

import config
import reviews_analytics
from scrapers import reviews_runner
from webapp import api

CFG = dict(config.REVIEWS_CONFIG)
CFG.update({
    "script": r"D:\fake\ctrip_hotel_reviews.py",
    "python": r"D:\fake\python.exe",
    "profile": r"D:\fake\profiles\default",
    "channel": "edge",
    "cdp_port": 9222,
    "login_wait": 60,
    "page_size": 50,
    "max_pages": 80,
    "timeout": 1800,
    "before_days": 1,
    "before": None,
})


@pytest.fixture(autouse=True)
def _isolate_hotel_name(monkeypatch):
    """样例 payload 固定用「海南君华海逸」，避免受本机 .env 的 MY_HOTEL_NAME 影响。"""
    monkeypatch.setitem(config.MY_HOTEL, "name", "海南君华海逸")


def _payload(status="ok", hotel_name="海南君华海逸", reviews=None, before="2026-11-03"):
    base = {
        "status": status,
        "message": "",
        "platform": "ctrip",
        "hotel": "君华海逸酒店",
        "city": "海口",
        "hotel_id": "436560",
        "hotel_name": hotel_name,
        "before": before,
        "date_field": "created_at",
        "total_count": 10,
        "collected": len(reviews or []),
        "kept": len(reviews or []),
        "reviews": reviews if reviews is not None else [
            {"id": 1, "rating": 5, "created_at": "2026-09-01 10:00:00", "content": "很好",
             "room": "豪华客房-大床", "reply": "感谢"},
            {"id": 2, "rating": 2, "created_at": "2026-09-02 10:00:00", "content": "设施老旧",
             "room": "豪华客房-双床", "reply": ""},
        ],
        "errors": [],
    }
    return base


# ---------------------------------------------------------------------------
# 命令与日期
# ---------------------------------------------------------------------------
def test_build_command_contains_required_args():
    cmd = reviews_runner.build_command(CFG, r"D:\tmp\out.json", "君华海逸酒店", "海口", "2026-11-03")
    assert cmd[0] == CFG["python"]
    assert cmd[1] == CFG["script"]
    joined = " ".join(cmd)
    for flag in ("--hotel", "--out", "--profile", "--channel", "--cdp-port",
                 "--login-wait", "--page-size", "--max-pages", "--city", "--before"):
        assert flag in joined, f"缺少 {flag}"
    assert cmd[cmd.index("--out") + 1] == r"D:\tmp\out.json"
    assert cmd[cmd.index("--city") + 1] == "海口"
    assert cmd[cmd.index("--before") + 1] == "2026-11-03"


def test_build_command_omits_optional_city_and_before():
    cmd = reviews_runner.build_command(CFG, "o.json", "某酒店", "", "")
    assert "--city" not in cmd
    assert "--before" not in cmd


def test_resolve_before_defaults_to_tomorrow():
    got = reviews_runner.resolve_before(dict(CFG, before=None, before_days=1))
    from datetime import date, timedelta
    assert got == (date.today() + timedelta(days=1)).isoformat()


def test_resolve_before_explicit_and_disabled():
    assert reviews_runner.resolve_before(dict(CFG, before="2026-10-01")) == "2026-10-01"
    assert reviews_runner.resolve_before(dict(CFG, before="")) == ""


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------
def test_validate_accepts_ok_payload():
    out = reviews_runner.validate_payload(_payload(), expected_hotel="君华海逸酒店",
                                          before="2026-11-03")
    assert out["kept"] == 2
    assert out["dateMin"] == "2026-09-01"
    assert out["dateMax"] == "2026-09-02"


def test_validate_rejects_need_login():
    with pytest.raises(reviews_runner.ReviewsCollectError, match="登录"):
        reviews_runner.validate_payload(_payload(status="need_login", reviews=[]))


def test_validate_rejects_wrong_hotel():
    """采错酒店是静默污染，必须直接拒绝。"""
    with pytest.raises(reviews_runner.ReviewsCollectError, match="不一致"):
        reviews_runner.validate_payload(_payload(hotel_name="杭州西湖国宾馆"),
                                        expected_hotel="君华海逸酒店")


def test_validate_accepts_when_hotel_name_has_prefix():
    """「海南君华海逸」应被认作同一家。"""
    reviews_runner.validate_payload(_payload(hotel_name="海南君华海逸酒店"),
                                    expected_hotel="君华海逸酒店")


def test_validate_rejects_empty_reviews():
    with pytest.raises(reviews_runner.ReviewsCollectError, match="为空"):
        reviews_runner.validate_payload(_payload(reviews=[]))


def test_validate_drops_rows_without_date_and_dedupes():
    reviews = _payload()["reviews"] + [
        {"id": 1, "rating": 5, "created_at": "2026-09-05 10:00:00", "content": "重复 id"},
        {"id": 3, "rating": 4, "content": "没有日期"},
    ]
    out = reviews_runner.validate_payload(_payload(reviews=reviews))
    assert out["kept"] == 2
    assert out["dropped"] == 1
    assert out["duplicates"] == 1


def test_validate_rejects_all_zero_ratings():
    reviews = [{"id": i, "rating": 0, "created_at": "2026-09-01", "content": "x"} for i in range(5)]
    with pytest.raises(reviews_runner.ReviewsCollectError, match="评分的值为 0"):
        reviews_runner.validate_payload(_payload(reviews=reviews))


def test_validate_rejects_review_newer_than_before():
    reviews = [{"id": 1, "rating": 5, "created_at": "2026-12-01 09:00:00", "content": "未来"}]
    with pytest.raises(reviews_runner.ReviewsCollectError, match="超出"):
        reviews_runner.validate_payload(_payload(reviews=reviews), before="2026-11-03")


# ---------------------------------------------------------------------------
# 快照路径：先项目内，再回退历史文件
# ---------------------------------------------------------------------------
def test_snapshot_path_prefers_primary(monkeypatch, tmp_path):
    primary = tmp_path / "primary.json"
    legacy = tmp_path / "legacy.json"
    primary.write_text("{}", encoding="utf-8")
    legacy.write_text("{}", encoding="utf-8")
    monkeypatch.setitem(config.REVIEWS_CONFIG, "path", str(primary))
    monkeypatch.setitem(config.REVIEWS_CONFIG, "legacy_path", str(legacy))
    assert reviews_analytics.snapshot_path() == str(primary)


def test_snapshot_path_falls_back_to_legacy(monkeypatch, tmp_path):
    legacy = tmp_path / "legacy.json"
    legacy.write_text("{}", encoding="utf-8")
    monkeypatch.setitem(config.REVIEWS_CONFIG, "path", str(tmp_path / "missing.json"))
    monkeypatch.setitem(config.REVIEWS_CONFIG, "legacy_path", str(legacy))
    assert reviews_analytics.snapshot_path() == str(legacy)


# ---------------------------------------------------------------------------
# 采集任务：把 subprocess 换掉，验证任务状态流转
# ---------------------------------------------------------------------------
@pytest.fixture
def stub_collect(monkeypatch):
    calls = {}

    def fake_collect(hotel="", city="", log=None, cfg_dict=None):
        calls["hotel"] = hotel
        if log:
            log("假装在翻页…")
        return {"status": "ok", "path": "x", "kept": 12, "rawCount": 12,
                "duplicates": 0, "dropped": 0, "dateMin": "2026-08-01",
                "dateMax": "2026-09-30", "hotel": hotel, "before": "2026-11-03"}

    monkeypatch.setattr(reviews_runner, "collect", fake_collect)
    return calls


def _wait_job(job_id, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        job = api.get_job(job_id)
        if job["state"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError("任务没有在预期时间内结束")


def test_reviews_job_runs_and_reports(stub_collect, empty_db):
    # empty_db：_run_reviews_collect 结束时会写每日记录（daily_collects），
    # 不隔离会写进真实 pricing.db（实测泄漏过一次）
    job = api.start_reviews_collect()
    assert job["kind"] == "reviews"
    done = _wait_job(job["id"])
    assert done["state"] == "done"
    assert done["kept"] == 12
    assert any("翻页" in line for line in done["log"])


def test_reviews_job_failure_is_reported(monkeypatch, empty_db):
    def boom(**_kwargs):
        raise reviews_runner.ReviewsCollectError("携程要求登录或验证")

    monkeypatch.setattr(reviews_runner, "collect", boom)
    job = api.start_reviews_collect()
    done = _wait_job(job["id"])
    assert done["state"] == "failed"
    assert "登录" in done["error"]


def test_only_one_job_at_a_time(monkeypatch, empty_db):
    """同一时刻只允许一个采集任务，价格采集与点评采集共用一个锁。"""
    import threading

    gate = threading.Event()

    def slow_collect(**_kwargs):
        gate.wait(5)
        return {"status": "ok", "path": "x", "kept": 1, "rawCount": 1,
                "duplicates": 0, "dropped": 0, "dateMin": "2026-09-01",
                "dateMax": "2026-09-01", "hotel": "", "before": ""}

    monkeypatch.setattr(reviews_runner, "collect", slow_collect)
    first = api.start_reviews_collect()
    try:
        with pytest.raises(api.BusyError):
            api.start_reviews_collect()
        with pytest.raises(api.BusyError):
            api.start_collect()
    finally:
        gate.set()
    _wait_job(first["id"])


def test_snapshot_info_reports_paths(monkeypatch, tmp_path):
    monkeypatch.setitem(config.REVIEWS_CONFIG, "script", str(tmp_path / "nope.py"))
    info = reviews_runner.snapshot_info()
    assert info["scriptExists"] is False
    assert "snapshot" in info
    assert info["before"]


# ---------------------------------------------------------------------------
# 秒退重试：上一次的浏览器没释放 profile 时，脚本会在接管 CDP 后立刻退出
# ---------------------------------------------------------------------------
def _runnable_cfg(tmp_path, **extra):
    script = tmp_path / "scraper.py"
    script.write_text("# fake", encoding="utf-8")
    py = tmp_path / "python.exe"
    py.write_text("# fake", encoding="utf-8")
    prof = tmp_path / "profile"
    prof.mkdir(exist_ok=True)
    cfgd = dict(CFG)
    cfgd.update({
        "script": str(script),
        "python": str(py),
        "profile": str(prof),
        "path": str(tmp_path / "out" / "reviews.json"),
        "before": "2026-11-03",
        "attempts": 2,
    })
    cfgd.update(extra)
    return cfgd


def test_fast_failure_retries_once(monkeypatch, tmp_path):
    cfgd = _runnable_cfg(tmp_path)
    calls = {"n": 0}

    def fake_run(cmd, cfg_dict, env, log):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"rc": 1, "errLines": ["TargetClosedError"], "tail": ["接管成功"], "timedOut": False}
        out = cmd[cmd.index("--out") + 1]
        with open(out, "w", encoding="utf-8") as f:
            json.dump(_payload(), f, ensure_ascii=False)
        return {"rc": 0, "errLines": [], "tail": [], "timedOut": False}

    monkeypatch.setattr(reviews_runner, "_run_script", fake_run)
    monkeypatch.setattr(reviews_runner.time, "sleep", lambda _s: None)

    res = reviews_runner.collect(cfg_dict=cfgd)
    assert calls["n"] == 2
    assert res["kept"] == 2
    assert os.path.isfile(cfgd["path"])  # 原子替换后目标文件存在


def test_timeout_is_not_retried(monkeypatch, tmp_path):
    cfgd = _runnable_cfg(tmp_path)
    calls = {"n": 0}

    def fake_run(cmd, cfg_dict, env, log):
        calls["n"] += 1
        return {"rc": 1, "errLines": [], "tail": [], "timedOut": True}

    monkeypatch.setattr(reviews_runner, "_run_script", fake_run)
    with pytest.raises(reviews_runner.ReviewsCollectError, match="超时"):
        reviews_runner.collect(cfg_dict=cfgd)
    assert calls["n"] == 1


def test_non_fast_failure_is_not_retried(monkeypatch, tmp_path):
    """慢失败（跑了一会儿才挂）不是 profile 占用问题，不该盲目重试。"""
    cfgd = _runnable_cfg(tmp_path, fast_fail_seconds=0)
    calls = {"n": 0}

    def fake_run(cmd, cfg_dict, env, log):
        calls["n"] += 1
        return {"rc": 3, "errLines": ["需要登录"], "tail": [], "timedOut": False}

    monkeypatch.setattr(reviews_runner, "_run_script", fake_run)
    with pytest.raises(reviews_runner.ReviewsCollectError, match="退出码 3"):
        reviews_runner.collect(cfg_dict=cfgd)
    assert calls["n"] == 1


def test_price_job_still_runs_after_job_refactor(monkeypatch, empty_db):
    """价格采集与点评采集共用建任务逻辑，重构后价格链路必须照常。

    empty_db：价格任务结束会写每日记录，不隔离会写进真实 pricing.db
    （本用例曾经把假「done」泄漏进生产库，让当天自动采集误判已采）。
    """
    import pipeline

    monkeypatch.setattr(
        pipeline,
        "run_pricing_check",
        lambda: {"suggestion": {"suggested_price": 312}, "stats": {"count": 5}},
    )
    job = api.start_collect()
    assert job["kind"] == "prices"
    done = _wait_job(job["id"])
    assert done["state"] == "done"
    assert done["suggested"] == 312
    assert done["count"] == 5
