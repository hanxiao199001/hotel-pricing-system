"""点评归因的模型层：把差评聚成「整改动作」级别的问题。

与 llm.py 的三条边界一致，另加一条针对分析的：

  1. **数字不归模型**。计数、均分、比例全部由 reviews_analytics 算好再喂进去，
     模型不输出任何统计数字——否则它会写出「约 30% 的差评」这种没出处的数。
  2. **论断必须有出处**。每条 evidence 的引文都要能在真实点评原文里找到连续片段，
     找不到就丢这条依据；依据全被丢光的问题整条丢弃。这是防幻觉的唯一硬约束。
  3. **可降级**。没配 key、超时、输出不合规，一律返回 None，
     调用方回退到 reviews_analytics.attribution() 的规则文案。

为什么必须落盘缓存：模型同一份输入两次能给出两种归因，页面每刷一次结论变一次，
这个工具就没人敢信。所以结果按 (快照路径, mtime, size, 模型名, prompt 版本)
做 key，快照不变就不重算——这是可复现性的来源，也顺带把成本压到每次采集一次。

用法（采集完想立刻生成、不等页面首次访问时）：
    python -m reviews_llm
"""
import hashlib
import json
import os
import re
import threading
from datetime import datetime

import config as cfg
import llm
import reviews_analytics as ra

# 改了 prompt 或输出结构就改这个版本号，旧缓存自动失效
PROMPT_VERSION = "v1"

MAX_ISSUES = 4
MAX_EVIDENCE = 2

# 同进程内的双检锁：多个页面请求同时 miss 时只调一次模型
_lock = threading.Lock()

# 进程内失败标记：key -> 原因。同一份快照失败过一次就不再重试，
# 否则页面每刷一次都要白等一个超时，比没有结论更难用。换快照/换模型会换 key，
# 自然重试；force=True 也会清掉标记重来。
_failed = {}


SYSTEM_PROMPT = (
    "你在为一家酒店做点评归因分析。你会收到两部分内容：已经算好的统计摘要，"
    "以及全部差评原文。你的任务是把差评聚成「一次整改动作能解决」的问题。\n\n"
    "硬性要求：\n"
    "1. 不要重新计算任何数字。统计已经算好了，你不要输出总数、均值、比例、百分比。\n"
    "2. 按整改动作聚类，不要照抄关键词分类。同一批整改能解决的问题要合并"
    "（例如「设施老旧」和「霉味」若都指向客房翻新，应合成一个问题）。\n"
    "3. 每个问题必须给 evidence。quote 必须是所给原文里连续的一段话，"
    "不得改写、不得拼接、不得跨点评组合。\n"
    "4. 不要编造原文里没有的事实（具体的房间号、人名、整改措施、时间）。\n"
    "5. 只输出 JSON 对象本身，不要解释、不要前言、不要代码块标记。\n\n"
    "输出格式：\n"
    "{\n"
    '  "headline": "不超过 40 字的总括",\n'
    '  "issues": [\n'
    "    {\n"
    '      "label": "不超过 12 字的问题名",\n'
    '      "severity": "high 或 medium 或 low",\n'
    '      "reasoning": "不超过 60 字，说明为什么这样归类",\n'
    '      "evidence": [{"date": "YYYY-MM-DD", "quote": "原文连续片段，不超过 30 字"}]\n'
    "    }\n"
    "  ],\n"
    '  "actions": ["不超过 30 字的整改建议"]\n'
    "}\n\n"
    "issues 给 1-4 个，每个问题只给 1-2 条 evidence。整段输出务必简短——"
    "宁可少写一个问题的理由，也不要让 JSON 被截断。"
    "如果差评太少不足以支撑聚类，issues 返回空数组，headline 写样本不足的原因。"
)


# ---------------------------------------------------------------------------
# 缓存
# ---------------------------------------------------------------------------
def _cache_key(summary: dict) -> str:
    """快照指纹。快照变了、模型换了、prompt 改了，都会得到不同的 key。"""
    snap = summary.get("snapshot") or {}
    path = snap.get("path") or ""
    try:
        st = os.stat(path)
        stamp = f"{st.st_mtime_ns}:{st.st_size}"
    except OSError:
        stamp = "missing"
    model = (cfg.LLM_CONFIG.get("model") or "").strip()
    raw = f"{os.path.abspath(path)}|{stamp}|{model}|{PROMPT_VERSION}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _read_cache(key: str):
    try:
        with open(cfg.REVIEWS_LLM_CONFIG["cache"], encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("key") != key:
        return None
    result = data.get("result")
    if not isinstance(result, dict):
        return None
    result["generatedAt"] = data.get("generated_at")
    return result


def _write_cache(key: str, result: dict) -> None:
    """原子写：先写临时文件再 replace，避免读到半截 JSON。

    写失败不影响本次返回——缓存只是加速，不是正确性的一部分。
    """
    path = cfg.REVIEWS_LLM_CONFIG["cache"]
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(
                {"key": key, "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                 "result": result},
                f, ensure_ascii=False, indent=2,
            )
        os.replace(tmp, path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 输入
# ---------------------------------------------------------------------------
def _samples(summary: dict) -> list:
    """要喂给模型的差评。

    评分最低的优先——归因看的是问题，不是客气的抱怨。
    全量快照里差评只有 30 条左右，通常一次喂得完。
    """
    rows = [r for r in (summary.get("badRows") or []) if (r.get("full") or "").strip()]
    rows.sort(key=lambda r: (float(r.get("rating") or 0), r.get("date") or ""))
    return rows[: int(cfg.REVIEWS_LLM_CONFIG.get("max_bad_samples") or 40)]


def _user_prompt(summary: dict, samples: list) -> str:
    snap = summary.get("snapshot") or {}
    overall = summary.get("overall") or {}
    th = summary.get("thresholds") or {}
    dims = summary.get("dimensions") or []
    themes = summary.get("themes") or []

    lines = [
        f"酒店：{snap.get('hotel') or '（未提供）'}",
        f"快照：{snap.get('at') or '未知'} 抓取，点评日期 {snap.get('dateMin')} 至 {snap.get('dateMax')}",
        f"口径：好评 ≥{th.get('goodRating')} 分，差评 ≤{th.get('badRating')} 分",
        "",
        "【已算好的统计，不要重算，仅供你判断时参考】",
        f"点评总数 {overall.get('count')}，综合评分 {overall.get('avg')}，"
        f"差评 {overall.get('bad')} 条（占 {overall.get('badRate')}%）",
    ]
    if dims:
        lines.append("四维均分：" + " / ".join(f"{d['label']} {d['avg']}" for d in dims))
    if themes:
        lines.append("差评主题提及次数：" + "、".join(f"{t['label']} {t['count']}" for t in themes))
    if summary.get("travelTypes"):
        lines.append("出游类型分布：" + "、".join(
            f"{t['label']} {t['ratio']}%" for t in summary["travelTypes"][:4]))

    lines += ["", f"【差评原文，共 {len(samples)} 条】"]
    for row in samples:
        text = re.sub(r"\s+", " ", row.get("full") or "").strip()
        lines.append(
            f"[{row.get('date')} | {row.get('rating')} 分 | {row.get('room') or '未标注'}] {text}"
        )

    lines += [
        "",
        "请输出 JSON。注意：evidence 的 quote 只能从上面这些原文里连续摘取，"
        "date 必须与所引用的那条点评日期一致。",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 输出校验
# ---------------------------------------------------------------------------
def _salvage_issues(text: str) -> dict:
    """从被截断的输出里抢救已经完整的问题对象。

    模型超长被 max_tokens 切断时 JSON 不完整（实测 1600 token 只够写约
    1300 字，第四个问题写到一半就断了）。整批丢弃太浪费——前面几个 issue
    往往完整且最有价值。这里按括号层级把 issues 数组里每个已经闭合的
    {...} 单独解析出来，重建一个合法 payload。

    抢救不出来的半截对象直接跳过，不做猜测性补全。
    """
    anchor = text.find('"issues"')
    if anchor < 0:
        return {}
    arr = text.find("[", anchor)
    if arr < 0:
        return {}

    items, depth, start = [], 0, None
    for i in range(arr + 1, len(text)):
        ch = text[i]
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    obj = json.loads(text[start:i + 1])
                    if isinstance(obj, dict):
                        items.append(obj)
                except ValueError:
                    pass                      # 半截对象，跳过
                start = None
        elif ch == "]" and depth == 0:
            break
    if not items:
        return {}

    m = re.search(r'"headline"\s*:\s*"([^"]{0,120})"', text[:anchor])
    return {"headline": m.group(1) if m else "", "issues": items, "actions": []}


def _parse(raw: str) -> dict:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("输出里没有 JSON 对象")
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        salvaged = _salvage_issues(text)      # 被截断时先抢救
        if salvaged.get("issues"):
            return salvaged
        raise
    if not isinstance(data, dict):
        raise ValueError("JSON 顶层不是对象")
    return data


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _match_row(quote: str, rows: list):
    """在真实点评里找出处。找不到返回 None。

    严格：quote 是原文连续子串。匹配前先去掉首尾省略号——模型引用时常写
    「隔音太差，半夜被走廊…」，去掉尾部的「…」就是原文原句。
    宽松一档：退一步用前 12 个字符匹配，应对句中改写标点的情况。
    再对不上就判失败：宁可少一条依据，也不让无出处的论断上屏。

    最小长度取 4 而不是更长：中文四字（「隔音太差」）已经有足够辨识度，
    阈值过高会把正确引用也判掉。
    """
    q = _squash(quote).strip("…⋯.。，,、；;：:　 ")
    if len(q) < 4:
        return None
    for row in rows:
        if q in _squash(row.get("full") or ""):
            return row
    head = q[:12]
    for row in rows:
        if head and head in _squash(row.get("full") or ""):
            return row
    return None


def _validate(payload: dict, samples: list):
    """把模型输出清洗成可上屏的结构。不合规的内容逐条丢弃。

    返回 None 表示一条可用的结论都没有，调用方应当降级到规则文案。
    """
    headline = str(payload.get("headline") or "").strip()[:80]

    issues, dropped_evidence, dropped_issues = [], 0, 0
    for item in (payload.get("issues") or [])[: MAX_ISSUES * 2]:
        if not isinstance(item, dict):
            dropped_issues += 1
            continue
        label = str(item.get("label") or "").strip()[:24]
        if not label:
            dropped_issues += 1
            continue
        severity = str(item.get("severity") or "").strip().lower()
        if severity not in ("high", "medium", "low"):
            severity = "medium"
        reasoning = str(item.get("reasoning") or "").strip()[:180]

        evidence = []
        for ev in (item.get("evidence") or [])[:MAX_EVIDENCE]:
            if not isinstance(ev, dict):
                dropped_evidence += 1
                continue
            quote = str(ev.get("quote") or "").strip()[:160]
            date = str(ev.get("date") or "").strip()[:10]
            # 先按模型给的日期找；找不到再全量找（模型可能引对原文但写错日期）
            cited = [r for r in samples if (r.get("date") or "") == date]
            row = _match_row(quote, cited) or _match_row(quote, samples)
            if row is None:
                dropped_evidence += 1
                continue
            evidence.append({
                "date": row.get("date") or "",
                "quote": quote,
                "room": row.get("room") or "—",
                "rating": round(float(row.get("rating") or 0), 1),
            })
        if not evidence:
            dropped_issues += 1   # 没有一条能对上原文，这个问题不能上屏
            continue
        issues.append({"label": label, "severity": severity,
                       "reasoning": reasoning, "evidence": evidence})
        if len(issues) >= MAX_ISSUES:
            break

    actions = [
        str(a).strip()[:60]
        for a in (payload.get("actions") or [])
        if str(a).strip()
    ][:4]

    if not issues:
        # 光有 headline 没有一条能对上原文的依据，等于一段无法核实的判断。
        # 这种情况宁可不出结论，让页面用规则文案（规则层此时会正确说出「样本不足」）。
        return None

    return {
        "headline": headline,
        "issues": issues,
        "actions": actions,
        "dropped": {"issues": dropped_issues, "evidence": dropped_evidence},
        "sampleSize": len(samples),
        "model": cfg.LLM_CONFIG.get("model") or "",
        "promptVersion": PROMPT_VERSION,
    }


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def available() -> tuple:
    """是否具备生成条件。返回 (bool, 原因)。"""
    if not cfg.REVIEWS_LLM_CONFIG.get("enabled"):
        return False, "REVIEWS_LLM_ENABLED 已关闭"
    return llm.available()


def analyze(summary: dict, force: bool = False):
    """返回结构化归因；不可用或一条都不合规时返回 None。

    缓存命中直接返回，不碰网络。force=True 用于 prompt 改动后的手动重算。
    """
    ok, _ = available()
    if not ok:
        return None

    key = _cache_key(summary)
    if not force:
        cached = _read_cache(key)
        if cached:
            return cached
        if key in _failed:
            return None

    with _lock:
        if not force:                      # 双检：等锁期间可能已经被别的请求算好了
            cached = _read_cache(key)
            if cached:
                return cached
            if key in _failed:
                return None

        samples = _samples(summary)
        if not samples:
            return None
        conf = cfg.REVIEWS_LLM_CONFIG
        try:
            raw = llm.chat(
                SYSTEM_PROMPT, _user_prompt(summary, samples),
                max_tokens=conf["max_tokens"],
                temperature=conf["temperature"],
                timeout=conf["timeout"],
            )
            payload = _parse(raw)
        except llm.LLMError as exc:
            _failed[key] = f"模型调用失败：{exc}"
            return None
        except ValueError as exc:
            _failed[key] = f"输出不是合法 JSON：{exc}"
            return None

        result = _validate(payload, samples)
        if result is None:
            _failed[key] = "输出未通过依据校验（引文对不上点评原文）"
            return None
        _failed.pop(key, None)
        _write_cache(key, result)
        return result


def summarize_for_page(summary: dict) -> dict:
    """给 Web 层用：附带可用性原因，方便页面在无结论时说明为什么。

    页面不该弹错，也不该假装有结论——reason 只用于 note 里的一行小字。
    """
    ok, reason = available()
    if not ok:
        return {"available": False, "reason": reason}
    result = analyze(summary)
    if not result:
        return {"available": False,
                "reason": _failed.get(_cache_key(summary)) or "模型未返回可用结论"}
    return {"available": True, **result}


def reset() -> None:
    """清掉进程内失败标记。测试和 CLI 强制重算前用。"""
    _failed.clear()


def main() -> int:
    summary = ra.summary_or_none()
    if not summary:
        print("没有可用的点评快照，无法分析")
        return 1
    ok, reason = available()
    print(f"模型可用：{ok}{('（' + reason + '）') if reason else ''}")
    reset()
    result = analyze(summary, force=True)
    if not result:
        print("未生成结论，页面会回退到规则文案")
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\n已写入缓存：{cfg.REVIEWS_LLM_CONFIG['cache']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
