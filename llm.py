"""可选的大模型调用（OpenAI 兼容格式）。

只做一件事：把一条差评交给模型，要一段回复正文回来。

边界（和 config.LLM_CONFIG 的注释一致）：
  - 只读：入参是一条点评的文本/评分/房型，不传库、不传价格、不传客人信息；
  - 不参与算价；
  - 失败一律抛 LLMError，由调用方降级到 reply_draft 的模板，绝不阻塞页面。

用标准库 urllib 而不是引入 openai SDK——这个项目至今只有 python-dotenv
一个运行依赖，为了一个功能装一个 SDK 不划算，而且 multipart/streaming
这些能力这里也用不上。
"""
import json
import re
import urllib.error
import urllib.request

import config as cfg


class LLMError(RuntimeError):
    """调用失败：未配置、网络错误、输出不合规。"""


SYSTEM_PROMPT = (
    "你是酒店客服，负责在携程商家后台回复差评。"
    "写作要求："
    "1) 中文，150–260 字，分 3–4 段，不要小标题、不要列表；"
    "2) 必须具体回应点评里提到的问题，不能只写套话；"
    "3) 最重要：你不知道酒店实际做了什么整改，所以任何整改动作都必须写成占位符"
    "【整改动作】，不要自己编「已更换」「已全部排查」这类事实；"
    "4) 不透露客人隐私（房号、姓名、同行人）；"
    "5) 不要承诺赔偿、退款、免费升级；"
    "6) 结尾署名用酒店简称，邀请复住但不要过度热情；"
    "7) 只输出回复正文本身，不要任何解释、前言或引号。"
)

# 模型偶尔会无视约束自己编动作。出现这些确定性措辞却没有占位符时判为不合规。
_FABRICATED = re.compile(r"已(全部|经)?(更换|更换完毕|排查完毕|完成整改|召回)|均已(更换|整改)")
_PLACEHOLDER = "【整改动作】"


def available():
    """是否具备调用条件。返回 (bool, 原因)。"""
    conf = cfg.LLM_CONFIG
    if not conf.get("base_url"):
        return False, "未配置 LLM_BASE_URL"
    if not conf.get("api_key"):
        return False, "未配置 LLM_API_KEY"
    if not conf.get("model"):
        return False, "未配置 LLM_MODEL"
    return True, ""


def _endpoint(base_url: str) -> str:
    url = base_url.rstrip("/")
    # 允许用户填 https://api.x.com 或 https://api.x.com/v1
    if url.endswith("/chat/completions"):
        return url
    if not url.endswith("/v1"):
        url += "/v1"
    return url + "/chat/completions"


def chat(system: str, user: str, max_tokens: int = None,
         temperature: float = None, timeout: float = None) -> str:
    """发一次 chat 请求，返回纯文本。

    三个 override 参数供不同用途覆盖全局配置：回复草稿要 700 token / 0.4 温度，
    点评归因要 1600 token / 0.2 温度且超时更短。默认全部走 cfg.LLM_CONFIG。
    """
    ok, reason = available()
    if not ok:
        raise LLMError(reason)

    conf = cfg.LLM_CONFIG
    payload = {
        "model": conf["model"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": conf["temperature"] if temperature is None else temperature,
        "max_tokens": conf["max_tokens"] if max_tokens is None else max_tokens,
    }
    req = urllib.request.Request(
        _endpoint(conf["base_url"]),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {conf['api_key']}",
        },
        method="POST",
    )

    wait = conf["timeout"] if timeout is None else timeout
    try:
        with urllib.request.urlopen(req, timeout=wait) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        raise LLMError(f"接口返回 {exc.code}：{detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise LLMError(f"无法连接 {conf['base_url']}：{exc.reason}") from exc
    except TimeoutError as exc:
        raise LLMError(f"请求超时（{wait} 秒）") from exc

    try:
        data = json.loads(raw)
        content = data["choices"][0]["message"]["content"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"响应结构不符合 OpenAI 格式：{raw[:160]}") from exc

    return (content or "").strip()


def _clean(text: str) -> str:
    text = text.strip()
    # 模型常把正文包在引号或 ``` 里
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    text = text.strip().strip('"').strip("“”").strip()
    return text


def validate_reply(text: str) -> str:
    """输出合规检查。不合规就抛错，让调用方回退模板。

    宁可回退到模板，也不把一段可能是编造的回复放出去——
    差评回复是公开内容，写错比不写更糟。
    """
    text = _clean(text or "")
    if len(text) < 60:
        raise LLMError(f"生成内容过短（{len(text)} 字），可能被截断")
    if len(text) > 900:
        raise LLMError(f"生成内容过长（{len(text)} 字），超出回复框常见上限")
    if _PLACEHOLDER not in text:
        raise LLMError("生成内容没有预留【整改动作】占位符，存在编造整改措施的风险")
    if _FABRICATED.search(text):
        raise LLMError("生成内容包含确定性整改措辞（如「已全部更换」），不可直接使用")
    if any(w in text for w in ("无法处理", "不予", "概不负责")):
        raise LLMError("生成内容含有推卸责任的表述")
    return text


def build_user_prompt(row: dict, hotel_name: str, themes, room_tier: str) -> str:
    lines = [
        f"酒店：{hotel_name or '（未提供）'}",
        f"评分：{row.get('rating')} 分（满分 5）",
        f"房型：{row.get('room') or '未标注'}"
        + (f"（归并档位：{room_tier}）" if room_tier else ""),
        f"日期：{row.get('date') or '未知'}",
        f"系统归类主题：{'、'.join(themes) if themes else '未归类（内容较笼统）'}",
        "",
        "客人原文：",
        (row.get("full") or "").strip()[:600],
        "",
        "请写一段回复。注意：整改动作一律写【整改动作】占位，不要编造酒店做过什么。",
    ]
    return "\n".join(lines)


def draft_reply(row: dict, hotel_name: str = "", themes=None, room_tier: str = "") -> dict:
    """让模型写一条回复，返回 {text, source, model}。不合规抛 LLMError。"""
    user = build_user_prompt(row, hotel_name, themes or [], room_tier)
    text = validate_reply(chat(SYSTEM_PROMPT, user))
    return {"text": text, "source": "llm", "model": cfg.LLM_CONFIG["model"]}
