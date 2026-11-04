"""每日自动采集的判定与记录测试。

约定：
- 全部走临时库（conftest.empty_db），不碰项目里的 pricing.db；
- 判定函数是纯函数，时钟从用例注入，不依赖真实时间；
- 执行链用假任务验证标记落库，不真跑 pipeline、不开浏览器。
"""
from datetime import date, datetime, time as dtime, timedelta

import pytest

import config as cfg
from webapp import api, daily

AT = dtime(8, 0)
NOW_BEFORE = datetime(2026, 11, 3, 7, 15)
NOW_AFTER = datetime(2026, 11, 3, 9, 0)
TODAY = "2026-11-03"


@pytest.fixture(autouse=True)
def _fixed_schedule(monkeypatch):
    """隔离本机 .env 的每日采集时点/次数配置，用固定口径跑判定。

    conftest 隔离了 DATA_SOURCE / 窗口 / LLM，这里补齐 DAILY_COLLECT_*：
    否则本机 .env 一旦改了 DAILY_COLLECT_AT，判定用例会随机器配置漂移。"""
    monkeypatch.setitem(cfg.DAILY_COLLECT_CONFIG, "at", "08:00")
    monkeypatch.setitem(cfg.DAILY_COLLECT_CONFIG, "at_time", AT)
    monkeypatch.setitem(cfg.DAILY_COLLECT_CONFIG, "max_attempts", 1)
    monkeypatch.setitem(cfg.DAILY_COLLECT_CONFIG, "cooldown_minutes", 60)


# ---------------------------------------------------------------------------
# 判定纯函数
# ---------------------------------------------------------------------------
def _plan(done=False, attempts=0, max_attempts=1, running=False, now=NOW_AFTER,
          at_time=AT, cooldown=timedelta(0), last_attempt=None):
    return daily.plan_action(done, attempts, max_attempts, running, now,
                             at_time, cooldown, last_attempt)


def test_plan_action_runs_when_due():
    assert _plan() == "run"


def test_plan_action_postpones_before_at():
    assert _plan(now=NOW_BEFORE) == "postpone_before_at"


def test_plan_action_done_wins_over_everything():
    # 已采过是最高优先级：时间再早、次数用尽也不该推翻
    assert _plan(done=True, attempts=1, running=True, now=NOW_BEFORE) == "skip_done"


def test_plan_action_running_beats_time_of_day():
    assert _plan(running=True, now=NOW_BEFORE) == "postpone_running"


def test_plan_action_respects_attempt_cap():
    assert _plan(attempts=1, max_attempts=1) == "skip_cap"


def test_plan_action_cooldown_then_retry():
    last = datetime(2026, 11, 3, 8, 5)
    kwargs = dict(attempts=1, max_attempts=3, last_attempt=last,
                  cooldown=timedelta(minutes=60))
    # 失败后 25 分钟：还在冷却里
    assert _plan(now=datetime(2026, 11, 3, 8, 30), **kwargs) == "skip_cooldown"
    # 失败后 85 分钟：冷却已过，允许再试
    assert _plan(now=datetime(2026, 11, 3, 9, 30), **kwargs) == "run"


# ---------------------------------------------------------------------------
# 时点解析
# ---------------------------------------------------------------------------
def test_parse_hhmm_valid_and_invalid():
    assert cfg._parse_hhmm("08:00") == dtime(8, 0)
    assert cfg._parse_hhmm("23:59") == dtime(23, 59)
    with pytest.raises(cfg.ConfigError):
        cfg._parse_hhmm("8点")
    with pytest.raises(cfg.ConfigError):
        cfg._parse_hhmm("25:00")
    with pytest.raises(cfg.ConfigError):
        cfg._parse_hhmm("")


# ---------------------------------------------------------------------------
# daily_collects 表：判重与计数口径
# ---------------------------------------------------------------------------
def test_manual_success_does_not_consume_auto_attempts(empty_db):
    db = empty_db
    db.mark_daily_collect("prices", TODAY, "done", job_id="7", detail="49 家竞品")
    rec = db.get_daily_collects(TODAY)["prices"]
    assert rec["status"] == "done"
    assert rec["attempts"] == 0
    assert rec["jobId"] == "7"


def test_auto_attempts_count_and_last_write_wins(empty_db):
    db = empty_db
    db.mark_daily_collect("reviews", TODAY, "failed",
                          job_id="1", detail="登录失效", count_attempt=True)
    db.mark_daily_collect("reviews", TODAY, "failed",
                          job_id="2", detail="又失败", count_attempt=True)
    rec = db.get_daily_collects(TODAY)["reviews"]
    assert rec["attempts"] == 2
    assert rec["detail"] == "又失败"
    # 手动补采成功：状态翻成 done，自动次数不动
    db.mark_daily_collect("reviews", TODAY, "done", job_id="3", detail="1975 条点评")
    rec = db.get_daily_collects(TODAY)["reviews"]
    assert rec["status"] == "done"
    assert rec["attempts"] == 2


def test_no_records_returns_empty(empty_db):
    assert empty_db.get_daily_collects(TODAY) == {}


# ---------------------------------------------------------------------------
# evaluate：读库判定
# ---------------------------------------------------------------------------
def test_evaluate_runs_both_when_due(empty_db):
    assert daily.evaluate(now=NOW_AFTER) == {"prices": "run", "reviews": "run"}


def test_evaluate_postpones_before_at(empty_db):
    assert daily.evaluate(now=NOW_BEFORE) == {
        "prices": "postpone_before_at", "reviews": "postpone_before_at",
    }


def test_evaluate_treats_today_competitor_rows_as_done(empty_db):
    # 外部跑过 main.py（schtasks 等）采到了今天竞品价 → 自动任务应当跳过价格
    empty_db.save_competitor_prices(TODAY, [
        {"name": "甲酒店", "id": "1", "price": 180, "distance": 0.3,
         "rating": 4.5, "reviews": 120},
    ])
    plans = daily.evaluate(now=NOW_AFTER)
    assert plans["prices"] == "skip_done"
    assert plans["reviews"] == "run"


def test_evaluate_respects_attempt_cap(empty_db):
    empty_db.mark_daily_collect("reviews", TODAY, "failed", count_attempt=True)
    plans = daily.evaluate(now=NOW_AFTER)
    assert plans["reviews"] == "skip_cap"
    assert plans["prices"] == "run"


def test_evaluate_yields_to_running_job(empty_db, monkeypatch):
    monkeypatch.setattr(api, "has_running_job", lambda: True)
    plans = daily.evaluate(now=NOW_AFTER)
    assert plans == {"prices": "postpone_running", "reviews": "postpone_running"}


# ---------------------------------------------------------------------------
# 分发：mock 跳过 / 当天已采不再触发
# ---------------------------------------------------------------------------
def test_dispatch_skips_mock_mode(empty_db, monkeypatch):
    # conftest 已把 DATA_SOURCE 固定为 mock：分发必须直接返回，不能起采集
    called = []
    monkeypatch.setattr(api, "start_collect", lambda: called.append("prices"))
    daily._wake_and_dispatch()
    assert called == []
    assert daily._INFLIGHT is False


def test_dispatch_does_nothing_when_done_today(empty_db, monkeypatch):
    monkeypatch.setattr(cfg, "DATA_SOURCE", "real")
    today = date.today().isoformat()
    empty_db.mark_daily_collect("prices", today, "done")
    empty_db.mark_daily_collect("reviews", today, "done")
    called = []
    monkeypatch.setattr(api, "start_collect", lambda: called.append("prices"))
    monkeypatch.setattr(api, "start_reviews_collect",
                        lambda: called.append("reviews"))
    daily._wake_and_dispatch()
    assert called == []
    assert daily._INFLIGHT is False


# ---------------------------------------------------------------------------
# 执行链：用假任务验证标记与让位
# ---------------------------------------------------------------------------
def test_run_one_marks_done_and_counts_attempt(empty_db, monkeypatch):
    fake = {"id": "5", "state": "done", "count": 49}
    monkeypatch.setattr(api, "wait_job", lambda jid, timeout: "done")
    daily._run_one("prices", TODAY, lambda: fake, wait_seconds=10)
    rec = empty_db.get_daily_collects(TODAY)["prices"]
    assert rec["status"] == "done"
    assert rec["attempts"] == 1
    assert rec["jobId"] == "5"
    assert rec["detail"] == "49 家竞品"


def test_run_one_failure_marks_failed_with_reason(empty_db, monkeypatch):
    fake = {"id": "6", "state": "failed", "error": "WorkflowError: 携程频控"}
    monkeypatch.setattr(api, "wait_job", lambda jid, timeout: "failed")
    daily._run_one("reviews", TODAY, lambda: fake, wait_seconds=10)
    rec = empty_db.get_daily_collects(TODAY)["reviews"]
    assert rec["status"] == "failed"
    assert rec["attempts"] == 1
    assert "频控" in rec["detail"]


def test_run_one_yields_to_busy_without_counting(empty_db):
    def busy():
        raise api.BusyError("已有采集任务在运行")

    daily._run_one("prices", TODAY, busy, wait_seconds=10)
    # 让位不算尝试：不写记录，自动额度保持 0
    assert "prices" not in empty_db.get_daily_collects(TODAY)


def test_run_chain_runs_both_and_clears_inflight(empty_db, monkeypatch):
    # 判定时点固定为 00:00，避免用例在真实时钟的 8 点前跑到 postpone 分支
    monkeypatch.setitem(cfg.DAILY_COLLECT_CONFIG, "at_time", dtime(0, 0))
    monkeypatch.setattr(api, "start_collect",
                        lambda: {"id": "1", "state": "done", "count": 49})
    monkeypatch.setattr(api, "start_reviews_collect",
                        lambda: {"id": "2", "state": "done", "kept": 1975})
    monkeypatch.setattr(api, "wait_job", lambda jid, timeout: "done")
    monkeypatch.setattr(daily.time, "sleep", lambda s: None)

    daily._run_chain(TODAY, {"prices": "run", "reviews": "run"})

    recs = empty_db.get_daily_collects(TODAY)
    assert recs["prices"]["status"] == "done"
    assert recs["prices"]["attempts"] == 1
    assert recs["reviews"]["status"] == "done"
    assert recs["reviews"]["detail"] == "1975 条点评"
    assert recs["reviews"]["attempts"] == 1
    assert daily._INFLIGHT is False


def test_run_chain_rechecks_and_skips_if_done_meanwhile(empty_db, monkeypatch):
    # 判定到起跑之间价格被手动采过：链起步前复查应放弃价格，
    # 但点评仍照常执行（它今天还没采）
    monkeypatch.setitem(cfg.DAILY_COLLECT_CONFIG, "at_time", dtime(0, 0))
    empty_db.mark_daily_collect("prices", TODAY, "done")

    def _must_not_start():
        raise AssertionError("复查发现已采后不应再起价格任务")

    monkeypatch.setattr(api, "start_collect", _must_not_start)
    monkeypatch.setattr(api, "start_reviews_collect",
                        lambda: {"id": "2", "state": "done", "kept": 5})
    monkeypatch.setattr(api, "wait_job", lambda jid, timeout: "done")
    monkeypatch.setattr(daily.time, "sleep", lambda s: None)

    daily._run_chain(TODAY, {"prices": "run", "reviews": "run"})

    recs = empty_db.get_daily_collects(TODAY)
    assert recs["reviews"]["status"] == "done"
    assert recs["prices"]["attempts"] == 0  # 手动完成的标记未被改动、未计数


# ---------------------------------------------------------------------------
# API 落标记与状态页
# ---------------------------------------------------------------------------
def test_api_task_finish_marks_daily_without_counting(empty_db):
    api._mark_daily_result("prices", {"id": "9", "state": "done", "count": 49})
    rec = empty_db.get_daily_collects(date.today().isoformat())["prices"]
    assert rec["status"] == "done"
    assert rec["attempts"] == 0  # 手动结果不占自动次数额度


def test_status_block_lists_both_types(empty_db):
    out = daily.status()
    assert out["today"] == date.today().isoformat()
    assert set(out) >= {"enabled", "at", "prices", "reviews", "skipReason"}
    assert out["prices"]["attempts"] == 0


def test_get_status_includes_daily_block(empty_db):
    s = api.get_status()
    assert "daily" in s
    assert "prices" in s["daily"] and "reviews" in s["daily"]
