"""LLM 层测试：用本地 mock 的 OpenAI 兼容接口，不依赖任何真实服务或 API key。

重点验证的不是「能不能调到模型」，而是「模型乱写时会不会被放出去」——
差评回复是公开内容，编造整改措施比不回复更糟。
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import app.config as cfg
import infra.llm as llm

GOOD_REPLY = (
    "尊敬的客人，您好：\n\n"
    "抱歉这次入住没能达到您的预期。\n\n"
    "您提到的设施陈旧与房间异味，是我们客房维护上的疏漏。"
    "我们【整改动作】，并在您下次入住前确认房间状态。\n\n"
    "如果方便，欢迎您再次光临。\n\n"
    "海南君华海逸酒店 客服团队"
)

_no_placeholder = "感谢您的评价，我们会继续努力改进服务，期待您再次光临。" * 4
_fabricated = (
    "尊敬的客人，您好：\n\n"
    "非常抱歉。针对您反馈的问题，我们已全部更换房间床品并完成整改，"
    "工程部已经排查完毕。我们【整改动作】。\n\n"
    "欢迎再次光临。\n\n海南君华海逸酒店 客服团队"
)


class _Handler(BaseHTTPRequestHandler):
    payload = GOOD_REPLY
    status = 200
    last_body = {}

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        _Handler.last_body = json.loads(self.rfile.read(length) or b"{}")
        if _Handler.status != 200:
            self.send_response(_Handler.status)
            self.end_headers()
            self.wfile.write(b'{"error":"mock failure"}')
            return
        body = json.dumps({
            "choices": [{"message": {"role": "assistant", "content": _Handler.payload}}]
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def mock_api(monkeypatch):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    monkeypatch.setitem(cfg.LLM_CONFIG, "base_url", f"http://127.0.0.1:{port}")
    monkeypatch.setitem(cfg.LLM_CONFIG, "api_key", "test-key")
    monkeypatch.setitem(cfg.LLM_CONFIG, "model", "mock-model")

    _Handler.payload = GOOD_REPLY
    _Handler.status = 200
    yield _Handler
    srv.shutdown()


@pytest.fixture
def no_llm(monkeypatch):
    monkeypatch.setitem(cfg.LLM_CONFIG, "base_url", "")
    monkeypatch.setitem(cfg.LLM_CONFIG, "api_key", "")
    monkeypatch.setitem(cfg.LLM_CONFIG, "model", "")


def test_available_reports_missing_field(no_llm):
    ok, reason = llm.available()
    assert ok is False
    assert "LLM_BASE_URL" in reason


def test_endpoint_accepts_both_styles():
    assert llm._endpoint("https://api.x.com") == "https://api.x.com/v1/chat/completions"
    assert llm._endpoint("https://api.x.com/v1") == "https://api.x.com/v1/chat/completions"
    assert llm._endpoint("https://api.x.com/v1/") == "https://api.x.com/v1/chat/completions"
    assert (llm._endpoint("https://api.x.com/v1/chat/completions")
            == "https://api.x.com/v1/chat/completions")


def test_chat_sends_openai_payload(mock_api):
    text = llm.chat("system prompt", "user prompt")
    assert "海南君华海逸" in text
    sent = mock_api.last_body
    assert sent["model"] == "mock-model"
    assert sent["messages"][0]["role"] == "system"
    assert sent["messages"][1]["content"] == "user prompt"


def test_validate_accepts_compliant_reply():
    assert llm.validate_reply(GOOD_REPLY).startswith("尊敬的客人")


def test_validate_rejects_missing_placeholder():
    with pytest.raises(llm.LLMError, match="占位符"):
        llm.validate_reply(_no_placeholder)


def test_validate_rejects_fabricated_action():
    with pytest.raises(llm.LLMError, match="编造|确定性"):
        llm.validate_reply(_fabricated)


def test_validate_strips_code_fence():
    wrapped = "```\n" + GOOD_REPLY + "\n```"
    assert llm.validate_reply(wrapped).startswith("尊敬的客人")


def test_http_error_is_wrapped(mock_api):
    mock_api.status = 500
    with pytest.raises(llm.LLMError, match="500"):
        llm.chat("s", "u")


def test_draft_reply_returns_source(mock_api):
    row = {"rating": 2.0, "room": "豪华客房-大床丨滨海城景",
           "date": "2026-01-01", "full": "房间霉味很重，设施也旧"}
    out = llm.draft_reply(row, hotel_name="海南君华海逸酒店", themes=["卫生异味"])
    assert out["source"] == "llm"
    assert out["model"] == "mock-model"


# ---------------------------------------------------------------------------
# API 层的降级：模型不可用/不可信时，功能照常可用
# ---------------------------------------------------------------------------
def test_generate_reply_falls_back_when_not_configured(no_llm, empty_db):
    from webapp import api

    d = api.get_replies()
    key = d["items"][0]["key"]
    out = api.generate_reply(key, prefer_llm=True)
    assert out["source"] == "template"
    assert "未配置" in out["fallbackReason"]


def test_generate_reply_falls_back_on_bad_output(mock_api, empty_db):
    from webapp import api

    mock_api.payload = _no_placeholder
    d = api.get_replies()
    key = d["items"][0]["key"]
    out = api.generate_reply(key, prefer_llm=True)
    assert out["source"] == "template"
    assert "占位符" in out["fallbackReason"]


def test_generate_reply_uses_llm_when_compliant(mock_api, empty_db):
    from webapp import api

    d = api.get_replies()
    key = d["items"][0]["key"]
    out = api.generate_reply(key, prefer_llm=True)
    assert out["source"] == "llm"
    assert out["fallbackReason"] is None
    # 落库后再次读取应当读到模型稿
    again = api.get_replies()
    target = next(i for i in again["items"] if i["key"] == key)
    assert target["source"] == "llm"
