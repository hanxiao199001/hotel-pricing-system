"""差评归因（硬件 vs 服务）的判定与文案测试。

背景：这两处结论以前是写死在字符串里的。实测 1975 条快照发现，全量口径
硬件 31 次 vs 服务 11 次（2.82 倍），切到近 6 个月却变成 2:2 打平、第一主题
是「服务态度」——写死的句子会和页面上的主题排序自相矛盾。

现在改成按「提及数 + 样本量」分档判定。这里覆盖全部分支、两侧为零的边界，
以及一条防止硬编码结论回流的源码级回归测试。
"""
import ast
import os

import pytest

import app.reviews.analytics as ra
from webapp import review_pages as rp


def _summary(hard=0, soft=0, bad=None):
    """构造最小 summary。只放「设施老旧」（硬件族）与「服务态度」（服务族）。"""
    themes = []
    if hard:
        themes.append({"label": "设施老旧", "count": hard, "ratio": 0.0})
    if soft:
        themes.append({"label": "服务态度", "count": soft, "ratio": 0.0})
    return {
        "themes": themes,
        "overall": {"bad": bad if bad is not None else max(hard, soft)},
    }


# ---------------------------------------------------------------------------
# 判定
# ---------------------------------------------------------------------------
def test_hardware_dominant():
    a = ra.attribution(_summary(31, 11))
    assert a["verdict"] == "hardware"
    assert a["ratio"] == 2.82
    assert a["weakSide"] == "hardware"
    assert (a["hardware"], a["service"]) == (31, 11)


def test_service_dominant():
    a = ra.attribution(_summary(8, 24))
    assert a["verdict"] == "service"
    assert a["ratio"] == 3.0
    assert a["weakSide"] == "service"


def test_mixed_when_close():
    """1.36 倍不构成显著偏向——这正是近 6 个月窗口下的真实形态。"""
    a = ra.attribution(_summary(15, 11))
    assert a["verdict"] == "mixed"
    assert a["weakSide"] is None


def test_insufficient_when_low_sample():
    """近 6 个月窗口的真实情况：2:2。绝不能说成「持平，问题不大」以外的结论。"""
    a = ra.attribution(_summary(2, 2))
    assert a["verdict"] == "insufficient"
    assert a["ratio"] is None


def test_none_when_no_bad_review():
    a = ra.attribution({"themes": [], "overall": {"bad": 0}})
    assert a["verdict"] == "none"
    assert a["hardware"] == 0 and a["service"] == 0


def test_zero_on_one_side_is_not_zero_times():
    """服务侧为 0 时倍数无定义，不能算成「0 倍」也不能判成 mixed。"""
    a = ra.attribution(_summary(9, 0))
    assert a["verdict"] == "hardware"
    assert a["ratio"] is None


def test_zero_hardware_side():
    a = ra.attribution(_summary(0, 7))
    assert a["verdict"] == "service"
    assert a["ratio"] is None


def test_zero_side_but_still_below_threshold():
    """对面为 0 但本侧不足门槛，仍判样本不足，不因为「有差评」就下结论。"""
    a = ra.attribution(_summary(0, 3))
    assert a["verdict"] == "insufficient"


@pytest.mark.parametrize("hard,soft,expected", [
    (20, 10, "hardware"),   # 正好 2.0 倍，达到门槛
    (19, 10, "mixed"),      # 1.9 倍，差一点不到
    (10, 20, "service"),
    (10, 19, "mixed"),
    (5, 5, "mixed"),        # 两侧都刚过样本门槛
])
def test_ratio_boundary(hard, soft, expected):
    assert ra.attribution(_summary(hard, soft))["verdict"] == expected


# ---------------------------------------------------------------------------
# 文案：每种判定必须是不同句式
# ---------------------------------------------------------------------------
def _texts():
    return {
        "hardware": rp._attribution_text(ra.attribution(_summary(31, 11))),
        "service": rp._attribution_text(ra.attribution(_summary(8, 24))),
        "mixed": rp._attribution_text(ra.attribution(_summary(15, 11))),
        "insufficient": rp._attribution_text(ra.attribution(_summary(2, 2))),
        "none": rp._attribution_text(ra.attribution({"themes": [], "overall": {"bad": 0}})),
    }


def test_each_verdict_has_distinct_text():
    texts = _texts()
    assert len(set(texts.values())) == len(texts)


def test_texts_carry_the_numbers():
    texts = _texts()
    assert "硬件与设施" in texts["hardware"] and "2.82 倍" in texts["hardware"]
    assert "服务与流程" in texts["service"]
    assert "不足以归因" in texts["mixed"] and "15" in texts["mixed"]
    assert "样本不足" in texts["insufficient"] and "少于 5 次" in texts["insufficient"]
    assert "不做" in texts["none"]


def test_text_is_data_driven_not_fixed():
    """同一判定、不同数字，文案必须跟着变。

    写死的句子做不到这一点——原来的两个 bug 正是这个形态：
    不管数据怎么变都追加同一句话。这条比扫描源码更能抓住行为回归。
    """
    pairs = [
        (ra.attribution(_summary(31, 11)), ra.attribution(_summary(40, 12))),
        (ra.attribution(_summary(8, 24)), ra.attribution(_summary(9, 30))),
        (ra.attribution(_summary(15, 11)), ra.attribution(_summary(16, 11))),
        (ra.attribution(_summary(3, 2)), ra.attribution(_summary(4, 3))),
        (ra.attribution(_summary(9, 0)), ra.attribution(_summary(12, 0))),
    ]
    for a, b in pairs:
        t1, t2 = rp._attribution_text(a), rp._attribution_text(b)
        assert a["verdict"] == b["verdict"]
        assert t1 != t2, f"同判定不同数据却输出相同文案，疑似硬编码：{t1}"


@pytest.mark.parametrize("hard,soft", [(31, 11), (8, 24), (15, 11), (3, 2), (9, 0)])
def test_text_contains_actual_counts(hard, soft):
    """文案里必须出现本次的真实计数，而不是笼统措辞。"""
    a = ra.attribution(_summary(hard, soft))
    t = rp._attribution_text(a)
    assert str(a["hardware"]) in t
    assert str(a["service"]) in t


def test_no_hardcoded_conclusion_in_code():
    """源码级回归：写死的那两句不许回到函数体里（注释/docstring 里提及不算）。

    只扫 _attribution_text 的函数体，避开注释中作为反例引用的原文。
    """
    tree = ast.parse(open(rp.__file__, encoding="utf-8").read())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_attribution_text")
    body = fn.body
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]  # 跳过 docstring
    literals = [n.value for stmt in body for n in ast.walk(stmt)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert literals, "函数体里没找到字符串常量，测试本身失效了"
    for s in literals:
        assert "短板在硬件而非服务" not in s
        assert "硬件与早餐" not in s


# ---------------------------------------------------------------------------
# 真实快照（存在才跑）
# ---------------------------------------------------------------------------
SNAP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "data", "reviews", "ctrip_reviews.json")


@pytest.mark.skipif(not os.path.isfile(SNAP), reason="缺少点评快照")
def test_real_snapshot_full_scope():
    """全量口径下硬件类确实显著多于服务类，判定应当成立。"""
    a = ra.attribution(ra.summarize())
    assert a["verdict"] == "hardware"
    assert a["hardware"] > a["service"] >= 0
