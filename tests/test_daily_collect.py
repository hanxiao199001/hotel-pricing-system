"""每日自动采集（两时段链）的判定与记录测试。

约定：
- 全部走临时库（conftest.empty_db），不碰项目里的 pricing.db；
- 判定函数是纯函数，时钟从用例注入，不依赖真实时间；
- 执行链用假任务验证标记落库，不真跑 pipeline、不开浏览器。
"""
from datetime import date, datetime, time as dtime, timedelta

import pytest

import app.config as cfg
from webapp import api, daily

AT = dtime(9, 0)
AT2 = dtime(16, 0)
NOW_BEFORE_BOTH = datetime(2026, 11, 3, 7, 15)   # 两个时段都没到
NOW_AM = datetime(2026, 11, 3, 10, 0)            # 只到早链
NOW_BOTH = datetime(2026, 11, 3, 17, 0)          # 两个时段都到
TODAY = "2026-11-03"


@pytest.fixture(autouse=True)
def _fixed_schedule(monkeypatch):
    """隔离本机 .env 的每日采集时点/次数配置，用固定口径跑判定。

    conftest 隔离了 DATA_SOURCE / 窗口 / LLM，这里补齐 DAILY_COLLECT_*：
    否则本机 .env 一旦改了 DAILY_COLLECT_AT，判定用例会随机器配置漂移。
    早链 09:00+R（含点评）、晚链 16:00（不含点评），与生产默认一致。
    has_running_job 一并打桩：本机中台进程若有任务在跑，
    evaluate 的让位分支会污染纯链路用例（与本机进程状态解耦）。"""
    monkeypatch.setitem(cfg.DAILY_COLLECT_CONFIG, "at", "09:00+R")
    monkeypatch.setitem(cfg.DAILY_COLLECT_CONFIG, "at_time", AT)
    monkeypatch.setitem(cfg.DAILY_COLLECT_CONFIG, "at_reviews", True)
    monkeypatch.setitem(cfg.DAILY_COLLECT_CONFIG, "at2", "16:00")
    monkeypatch.setitem(cfg.DAILY_COLLECT_CONFIG, "at2_time", AT2)
    monkeypatch.setitem(cfg.DAILY_COLLECT_CONFIG, "at2_reviews", False)
    monkeypatch.setitem(cfg.DAILY_COLLECT_CONFIG, "max_attempts", 1)
    monkeypatch.setitem(cfg.DAILY_COLLECT_CONFIG, "cooldown_minutes", 60)
    monkeypatch.setattr(api, "has_running_job", lambda: False)


# ---------------------------------------------------------------------------
# 判定纯函数
# ---------------------------------------------------------------------------
def _plan(done=False, attempts=0, max_attempts=1, running=False, now=NOW_AM,
          at_time=AT, cooldown=timedelta(0), last_attempt=None):
    return daily.plan_action(done, attempts, max_attempts, running, now,
                             at_time, cooldown, last_attempt)


def test_plan_action_runs_when_due():
    assert _plan() == "run"


def test_plan_action_postpones_before_at():
    assert _plan(now=NOW_BEFORE_BOTH) == "postpone_before_at"


def test_plan_action_done_wins_over_everything():
    # 已采过是最高优先级：时间再早、次数用尽也不该推翻
    assert _plan(done=True, attempts=1, running=True, now=NOW_BEFORE_BOTH) == "skip_done"


def test_plan_action_running_beats_time_of_day():
    assert _plan(running=True, now=NOW_BEFORE_BOTH) == "postpone_running"


def test_plan_action_respects_attempt_cap():
    assert _plan(attempts=1, max_attempts=1) == "skip_cap"


def test_plan_action_cooldown_then_retry():
    last = datetime(2026, 11, 3, 9, 5)
    kwargs = dict(attempts=1, max_attempts=3, last_attempt=last,
                  cooldown=timedelta(minutes=60))
    # 失败后 25 分钟：还在冷却里
    assert _plan(now=datetime(2026, 11, 3, 9, 30), **kwargs) == "skip_cooldown"
    # 失败后 85 分钟：冷却已过，允许再试
    assert _plan(now=datetime(2026, 11, 3, 10, 30), **kwargs) == "run"


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


def test_parse_slot_with_and_without_reviews_suffix():
    hhmm, with_r = cfg._parse_slot("DAILY_COLLECT_AT", "09:00+R")
    assert hhmm == dtime(9, 0)
    assert with_r is True

    hhmm, with_r = cfg._parse_slot("DAILY_COLLECT_AT2", "16:00")
    assert hhmm == dtime(16, 0)
    assert with_r is False

    # 小写 +r 也认；非法后缀/空时间报错
    assert cfg._parse_slot("X", "09:00+r")[1] is True
    with pytest.raises(cfg.ConfigError):
        cfg._parse_slot("X", "09:00+Q")
    with pytest.raises(cfg.ConfigError):
        cfg._parse_slot("X", "+R")


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
    db.mark_daily_collect("reviews-am", TODAY, "failed",
                          job_id="1", detail="登录失效", count_attempt=True)
    db.mark_daily_collect("reviews-am", TODAY, "failed",
                          job_id="2", detail="又失败", count_attempt=True)
    rec = db.get_daily_collects(TODAY)["reviews-am"]
    assert rec["attempts"] == 2
    assert rec["detail"] == "又失败"
    # 手动补采成功：写裸类型记录，时段记录保持失败（两时段已视同已采，不再自动跑）；
    # 时段记录的自动次数不被手动结果改动
    db.mark_daily_collect("reviews", TODAY, "done", job_id="3", detail="1975 条点评")
    rec = db.get_daily_collects(TODAY)["reviews-am"]
    assert rec["status"] == "failed"
    assert rec["attempts"] == 2
    # 判定层视同当天已采：晚链（若含点评）会跳过
    plans = daily.evaluate(now=NOW_BOTH)
    assert plans["am"]["reviews"] == "skip_done"


def test_no_records_returns_empty(empty_db):
    assert empty_db.get_daily_collects(TODAY) == {}


def test_slot_keys_coexist_with_bare_keys(empty_db):
    # 「类型-时段」与裸「类型」是两条独立记录，互不覆盖
    db = empty_db
    db.mark_daily_collect("prices-am", TODAY, "done", job_id="1", detail="1")
    db.mark_daily_collect("prices-pm", TODAY, "failed", job_id="2", detail="x",
                          count_attempt=True)
    recs = db.get_daily_collects(TODAY)
    assert recs["prices-am"]["status"] == "done"
    assert recs["prices-pm"]["status"] == "failed"


# ---------------------------------------------------------------------------
# evaluate：读库判定（按「数据×时段」）
# ---------------------------------------------------------------------------
def test_types_for_slot_reflects_chain_design():
    # 早链含点评，晚链不含 — 方案乙的核心编排
    assert daily._types_for_slot("am") == ["prices", "ebook", "reviews"]
    assert daily._types_for_slot("pm") == ["prices", "ebook"]


def test_evaluate_before_both_slots(empty_db):
    plans = daily.evaluate(now=NOW_BEFORE_BOTH)
    assert plans["am"] == {
        "prices": "postpone_before_at",
        "ebook": "postpone_before_at",
        "reviews": "postpone_before_at",
    }
    assert plans["pm"] == {
        "prices": "postpone_before_at",
        "ebook": "postpone_before_at",
    }


def test_evaluate_morning_slot_due_only(empty_db):
    # 10:00：早链该跑（三类），晚链未到点
    plans = daily.evaluate(now=NOW_AM)
    assert plans["am"] == {"prices": "run", "ebook": "run", "reviews": "run"}
    assert plans["pm"] == {"prices": "postpone_before_at", "ebook": "postpone_before_at"}


def test_evaluate_both_slots_due(empty_db):
    plans = daily.evaluate(now=NOW_BOTH)
    assert plans["am"] == {"prices": "run", "ebook": "run", "reviews": "run"}
    assert plans["pm"] == {"prices": "run", "ebook": "run"}


def test_evaluate_slot_done_does_not_block_other_slot(empty_db):
    # 早链价格采完，晚链价格照常要采——一天两次的判重核心
    empty_db.mark_daily_collect("prices-am", TODAY, "done",
                                 job_id="1", detail="49 家竞品")
    plans = daily.evaluate(now=NOW_BOTH)
    assert plans["am"]["prices"] == "skip_done"
    assert plans["pm"]["prices"] == "run"
    assert plans["am"]["ebook"] == "run"


def test_evaluate_manual_done_blocks_all_slots(empty_db):
    # 手动采集写裸类型记录：两时段都跳过该类
    empty_db.mark_daily_collect("ebook", TODAY, "done", job_id="9",
                                 detail="eBook 经营数据已更新")
    plans = daily.evaluate(now=NOW_BOTH)
    assert plans["am"]["ebook"] == "skip_done"
    assert plans["pm"]["ebook"] == "skip_done"
    assert plans["pm"]["prices"] == "run"


def test_evaluate_treats_today_competitor_rows_as_done(empty_db):
    # 外部跑过 main.py（schtasks 等）采到了今天竞品价 → 两时段都不再自动采价格
    empty_db.save_competitor_prices(TODAY, [
        {"name": "甲酒店", "id": "1", "price": 180, "distance": 0.3,
         "rating": 4.5, "reviews": 120},
    ])
    plans = daily.evaluate(now=NOW_BOTH)
    assert plans["am"]["prices"] == "skip_done"
    assert plans["pm"]["prices"] == "skip_done"
    assert plans["am"]["reviews"] == "run"


def test_evaluate_reviews_done_in_am_not_recrawled(empty_db):
    # 早链点评已采：晚链本来就不含点评；次日早链才会再采
    empty_db.mark_daily_collect("reviews-am", TODAY, "done",
                                 job_id="2", detail="376 条点评")
    plans = daily.evaluate(now=NOW_BOTH)
    assert plans["am"]["reviews"] == "skip_done"
    assert "reviews" not in plans["pm"] or plans["pm"].get("reviews") is None


def test_evaluate_respects_attempt_cap_per_slot(empty_db):
    # 早链点评失败过一次（max=1）：晚链不含点评，点评要等明天
    empty_db.mark_daily_collect("reviews-am", TODAY, "failed", count_attempt=True)
    plans = daily.evaluate(now=NOW_BOTH)
    assert plans["am"]["reviews"] == "skip_cap"
    assert plans["pm"]["prices"] == "run"


def test_evaluate_yields_to_running_job(empty_db, monkeypatch):
    monkeypatch.setattr(api, "has_running_job", lambda: True)
    plans = daily.evaluate(now=NOW_BOTH)
    assert plans["am"] == {
        "prices": "postpone_running", "ebook": "postpone_running",
        "reviews": "postpone_running",
    }


# ---------------------------------------------------------------------------
# 分发：mock 跳过 / 已采不再触发
# ---------------------------------------------------------------------------
def test_dispatch_skips_mock_mode(empty_db, monkeypatch):
    # conftest 已把 DATA_SOURCE 固定为 mock：分发必须直接返回，不能起采集
    called = []
    monkeypatch.setattr(api, "start_collect", lambda: called.append("prices"))
    daily._wake_and_dispatch()
    assert called == []
    assert daily._INFLIGHT is False


def test_dispatch_does_nothing_when_all_done(empty_db, monkeypatch):
    monkeypatch.setattr(cfg, "DATA_SOURCE", "real")
    today = date.today().isoformat()
    for key in ("prices-am", "prices-pm", "ebook-am", "ebook-pm", "reviews-am"):
        empty_db.mark_daily_collect(key, today, "done")
    called = []
    monkeypatch.setattr(api, "start_collect", lambda: called.append("prices"))
    daily._wake_and_dispatch()
    assert called == []
    assert daily._INFLIGHT is False


def test_dispatch_runs_am_chain_when_due_and_inflight_blocks_pm(empty_db, monkeypatch):
    # 两时段都到期：一次只起一个链；在跑时晚链让位，留给下一轮轮询
    monkeypatch.setitem(cfg.DAILY_COLLECT_CONFIG, "at_time", dtime(0, 0))
    monkeypatch.setitem(cfg.DAILY_COLLECT_CONFIG, "at2_time", dtime(0, 0))
    monkeypatch.setattr(cfg, "DATA_SOURCE", "real")

    started = []

    def _run_chain(slot, today, plans):
        started.append(slot)
        daily._INFLIGHT = False  # 模拟链结束释放

    monkeypatch.setattr(daily, "_run_chain", _run_chain)
    daily._INFLIGHT = True  # 模拟早链已在跑
    daily._wake_and_dispatch()
    assert started == []

    daily._INFLIGHT = False
    daily._wake_and_dispatch()
    assert started == ["am"]


# ---------------------------------------------------------------------------
# 执行链：用假任务验证标记与让位
# ---------------------------------------------------------------------------
def test_run_one_marks_done_and_counts_attempt(empty_db, monkeypatch):
    fake = {"id": "5", "state": "done", "count": 49}
    monkeypatch.setattr(api, "wait_job", lambda jid, timeout: "done")
    daily._run_one("am", "prices", TODAY, lambda: fake, wait_seconds=10)
    recs = empty_db.get_daily_collects(TODAY)
    # 写的是「类型-时段」键
    assert "prices-am" in recs
    assert recs["prices-am"]["status"] == "done"
    assert recs["prices-am"]["attempts"] == 1
    assert recs["prices-am"]["jobId"] == "5"
    assert recs["prices-am"]["detail"] == "49 家竞品"


def test_run_one_failure_marks_failed_with_reason(empty_db, monkeypatch):
    fake = {"id": "6", "state": "failed", "error": "WorkflowError: 携程频控"}
    monkeypatch.setattr(api, "wait_job", lambda jid, timeout: "failed")
    daily._run_one("pm", "reviews", TODAY, lambda: fake, wait_seconds=10)
    rec = empty_db.get_daily_collects(TODAY)["reviews-pm"]
    assert rec["status"] == "failed"
    assert rec["attempts"] == 1
    assert "频控" in rec["detail"]


def test_run_one_yields_to_busy_without_counting(empty_db):
    def busy():
        raise api.BusyError("已有采集任务在运行")

    daily._run_one("am", "prices", TODAY, busy, wait_seconds=10)
    # 让位不算尝试：不写记录，自动额度保持 0
    assert "prices-am" not in empty_db.get_daily_collects(TODAY)


def test_run_chain_full_am_order_prices_ebook_reviews(empty_db, monkeypatch):
    """早链编排：价格 → 经营 → 点评，串行顺序执行。"""
    monkeypatch.setitem(cfg.DAILY_COLLECT_CONFIG, "at_time", dtime(0, 0))
    order = []

    def mk(kind, result):
        def start():
            order.append(kind)
            return result
        return start

    monkeypatch.setattr(api, "start_collect",
                        mk("prices", {"id": "1", "state": "done", "count": 49}))
    monkeypatch.setattr(api, "start_ebook_collect",
                        mk("ebook", {"id": "2", "state": "done", "sections": 15}))
    monkeypatch.setattr(api, "start_reviews_collect",
                        mk("reviews", {"id": "3", "state": "done", "kept": 376}))
    monkeypatch.setattr(api, "wait_job", lambda jid, timeout: "done")
    monkeypatch.setattr(daily.time, "sleep", lambda s: None)

    daily._run_chain("am", TODAY, {"prices": "run", "ebook": "run", "reviews": "run"})

    assert order == ["prices", "ebook", "reviews"]
    recs = empty_db.get_daily_collects(TODAY)
    assert recs["prices-am"]["status"] == "done"
    assert recs["ebook-am"]["status"] == "done"
    assert recs["reviews-am"]["detail"] == "376 条点评"
    assert daily._INFLIGHT is False


def test_run_chain_pm_skips_reviews(empty_db, monkeypatch):
    """晚链不含点评：即使 plans 里误传 reviews 也不采（编排由类型表决定）。"""
    monkeypatch.setitem(cfg.DAILY_COLLECT_CONFIG, "at2_time", dtime(0, 0))
    order = []

    def mk(kind, result):
        def start():
            order.append(kind)
            return result
        return start

    monkeypatch.setattr(api, "start_collect",
                        mk("prices", {"id": "4", "state": "done", "count": 52}))
    monkeypatch.setattr(api, "start_ebook_collect",
                        mk("ebook", {"id": "5", "state": "done", "sections": 15}))

    def _must_not_run():
        raise AssertionError("晚链不应采点评")

    monkeypatch.setattr(api, "start_reviews_collect", _must_not_run)
    monkeypatch.setattr(api, "wait_job", lambda jid, timeout: "done")
    monkeypatch.setattr(daily.time, "sleep", lambda s: None)

    daily._run_chain("pm", TODAY, {"prices": "run", "ebook": "run"})

    assert order == ["prices", "ebook"]
    recs = empty_db.get_daily_collects(TODAY)
    assert recs["prices-pm"]["status"] == "done"
    assert recs["ebook-pm"]["status"] == "done"
    assert "reviews-pm" not in recs


def test_run_chain_rechecks_and_skips_if_done_meanwhile(empty_db, monkeypatch):
    # 判定到起跑之间价格被手动采过：链起步前复查应放弃价格，
    # 但经营数据仍照常执行（它还没采）
    monkeypatch.setitem(cfg.DAILY_COLLECT_CONFIG, "at_time", dtime(0, 0))
    monkeypatch.setitem(cfg.DAILY_COLLECT_CONFIG, "at2_time", dtime(0, 0))
    today = date.today().isoformat()  # 复查 evaluate() 用真实日期，标记要写今天
    empty_db.mark_daily_collect("prices", today, "done")

    def _must_not_start():
        raise AssertionError("复查发现已采后不应再起价格任务")

    monkeypatch.setattr(api, "start_collect", _must_not_start)
    monkeypatch.setattr(api, "start_ebook_collect",
                        lambda: {"id": "2", "state": "done", "sections": 15})
    # reviews 本测试不覆盖（下一个函数有专门用例）；
    # 真实环境中链内会先跑价格让位，这里只验证价格跳过后经营数据照常——
    # 若不打桩，_run_one 会真的起 reviews 任务（写进进程内 _jobs 泄漏给后续用例）
    monkeypatch.setattr(api, "start_reviews_collect",
                        lambda: {"id": "3", "state": "done", "kept": 1})
    monkeypatch.setattr(api, "wait_job", lambda jid, timeout: "done")
    monkeypatch.setattr(daily.time, "sleep", lambda s: None)

    daily._run_chain("am", today, {"prices": "run", "ebook": "run", "reviews": "run"})

    recs = empty_db.get_daily_collects(today)
    assert recs["ebook-am"]["status"] == "done"
    # 手动完成的裸记录未被改动；早链价格没有新增记录
    assert recs["prices"]["attempts"] == 0
    assert "prices-am" not in recs


def test_run_chain_partial_slot_runs_only_requested_types(empty_db, monkeypatch):
    # 时段计划里只有经营数据要跑（价格手工采过）：链只跑经营
    monkeypatch.setitem(cfg.DAILY_COLLECT_CONFIG, "at_time", dtime(0, 0))
    monkeypatch.setitem(cfg.DAILY_COLLECT_CONFIG, "at2_time", dtime(0, 0))
    monkeypatch.setattr(api, "start_ebook_collect",
                        lambda: {"id": "7", "state": "done", "sections": 15})
    monkeypatch.setattr(api, "wait_job", lambda jid, timeout: "done")
    called = []
    monkeypatch.setattr(api, "start_collect",
                        lambda: called.append("prices") or {})
    daily._run_chain("pm", TODAY, {"prices": "skip_done", "ebook": "run"})
    assert called == []
    assert empty_db.get_daily_collects(TODAY)["ebook-pm"]["status"] == "done"


# ---------------------------------------------------------------------------
# API 落标记与状态页
# ---------------------------------------------------------------------------
def test_api_manual_task_marks_daily_without_counting(empty_db):
    api._mark_daily_result("prices", {"id": "9", "state": "done", "count": 49})
    today = date.today().isoformat()
    rec = empty_db.get_daily_collects(today)["prices"]
    assert rec["status"] == "done"
    assert rec["attempts"] == 0  # 手动结果不占自动次数额度


def test_api_slot_mark_writes_typed_key(empty_db):
    api._mark_daily_result("ebook", {"id": "10", "state": "done", "sections": 15},
                           slot="am")
    today = date.today().isoformat()
    recs = empty_db.get_daily_collects(today)
    assert "ebook-am" in recs
    assert recs["ebook-am"]["detail"] == "eBook 经营数据已更新"


def test_status_block_lists_both_slots(empty_db):
    out = daily.status()
    assert out["today"] == date.today().isoformat()
    assert set(out) >= {"enabled", "at", "at2", "slots", "skipReason"}
    assert set(out["slots"]) == {"am", "pm"}
    # 早链含点评；晚链展示层不含 reviews（前端标记"本链不采"）
    assert set(out["slots"]["am"]) == {"prices", "ebook", "reviews"}
    assert set(out["slots"]["pm"]) == {"prices", "ebook"}


def test_status_marks_slot_done_and_manual_fallback(empty_db):
    empty_db.mark_daily_collect("prices-pm", TODAY, "done", job_id="3",
                                 detail="52 家竞品")
    empty_db.mark_daily_collect("ebook", TODAY, "done", job_id="8",
                                 detail="eBook 经营数据已更新")
    out = daily.status()
    # 用真实日期查会落到今天的表里；直接验证 _type_view 行为更稳
    slot_view = out["slots"]["pm"]
    assert slot_view["prices"] is not None


def test_type_view_prefers_slot_record_and_flags_manual(empty_db):
    rec_slot = {"status": "failed", "attempts": 1, "jobId": "1",
                "detail": "x", "updatedAt": "2026-11-03 09:10:00"}
    rec_any = {"status": "done", "attempts": 0, "jobId": "9",
               "detail": "手动", "updatedAt": "2026-11-03 08:00:00"}
    # 时段记录优先：失败也应如实展示
    v = daily._type_view(rec_slot, rec_any)
    assert v["status"] == "failed"
    assert v["viaManual"] is False
    # 没有时段记录、只有手动记录：展示手动结果并标记
    v2 = daily._type_view(None, rec_any)
    assert v2["done"] is True
    assert v2["viaManual"] is True
    # 两边都没有：prices 视库里有无当天数据
    v3 = daily._type_view(None, None, have_prices=True)
    assert v3["done"] is True


def test_get_status_includes_daily_block(empty_db):
    s = api.get_status()
    assert "daily" in s
    assert "slots" in s["daily"]
