"""启动配置检查：必填环境变量缺失或为空时必须失败，且不能进入爬虫。"""
import pytest

import app.config as config
from app.config import ConfigError, validate_startup_config

VALID_CTRIP = {
    "username": "user",
    "password": "secret",
    "hotel_id": "12345",
}
DISABLED_NOTIFY = {"enabled": False, "recipient": ""}
ENABLED_NOTIFY = {"enabled": True, "recipient": "+8613800138000"}


def test_missing_ctrip_credentials_raise():
    with pytest.raises(ConfigError) as exc:
        validate_startup_config(
            {"username": "", "password": "   ", "hotel_id": None},
            DISABLED_NOTIFY,
        )
    message = str(exc.value)
    # 只检查「缺失清单」那一段，诊断信息里会列出 .env 的键名，不代表该项被要求
    missing_line = message.split("诊断信息:")[0]
    assert "CTRIP_USERNAME" in missing_line
    assert "CTRIP_PASSWORD" in missing_line
    assert "CTRIP_HOTEL_ID" in missing_line
    assert "NOTIFY_RECIPIENT" not in missing_line


def test_partial_ctrip_credentials_still_fail():
    incomplete = dict(VALID_CTRIP)
    incomplete["password"] = ""
    with pytest.raises(ConfigError, match="CTRIP_PASSWORD"):
        validate_startup_config(incomplete, DISABLED_NOTIFY)


def test_notify_recipient_required_when_enabled():
    with pytest.raises(ConfigError, match="NOTIFY_RECIPIENT") as exc:
        validate_startup_config(VALID_CTRIP, {"enabled": True, "recipient": "  "})
    assert "NOTIFY_ENABLED=false" in str(exc.value)


def test_notify_recipient_optional_when_disabled():
    validate_startup_config(VALID_CTRIP, DISABLED_NOTIFY)
    validate_startup_config(VALID_CTRIP, {"enabled": False, "recipient": None})


def test_complete_config_passes():
    validate_startup_config(VALID_CTRIP, ENABLED_NOTIFY)


def test_main_exits_before_scraper_when_config_missing(monkeypatch, capsys):
    import main
    import app.pricing.pipeline as pipeline

    monkeypatch.setattr(
        config,
        "CTRIP_CONFIG",
        {"username": "", "password": "", "hotel_id": ""},
    )
    monkeypatch.setattr(config, "WECHAT_CONFIG", dict(DISABLED_NOTIFY))

    def _crawler_must_not_start(*_args, **_kwargs):
        raise AssertionError("爬虫不应在配置检查失败后启动")

    # 真正的创建点在 pipeline 里（CLI 与中台共用同一份工作流）
    monkeypatch.setattr(pipeline, "create_crawler", _crawler_must_not_start)
    monkeypatch.setattr(
        main,
        "run_pricing_check",
        lambda: (_ for _ in ()).throw(AssertionError("不应执行定价检查")),
    )

    with pytest.raises(SystemExit) as exc:
        main.main()

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "启动失败" in err
    assert "CTRIP_USERNAME" in err


def test_main_runs_check_when_config_present(monkeypatch):
    import main

    monkeypatch.setattr(config, "CTRIP_CONFIG", dict(VALID_CTRIP))
    monkeypatch.setattr(config, "WECHAT_CONFIG", dict(ENABLED_NOTIFY))
    ran = {}
    monkeypatch.setattr(main, "run_pricing_check", lambda: ran.setdefault("ok", True))

    main.main()
    assert ran["ok"] is True


def test_run_pricing_check_rejects_empty_credentials(monkeypatch):
    import main
    import app.pricing.pipeline as pipeline

    monkeypatch.setattr(
        config,
        "CTRIP_CONFIG",
        {"username": "", "password": "x", "hotel_id": "1"},
    )
    monkeypatch.setattr(config, "WECHAT_CONFIG", dict(DISABLED_NOTIFY))
    monkeypatch.setattr(
        pipeline,
        "create_crawler",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("不应创建爬虫")),
    )

    with pytest.raises(ConfigError, match="CTRIP_USERNAME"):
        main.run_pricing_check()


def test_builtin_env_parser_handles_comments_quotes_and_export(tmp_path):
    """没装 python-dotenv 时的兜底解析：注释、空行、引号、export 前缀都要正确处理。"""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# 注释行\n"
        "\n"
        "CTRIP_USERNAME=user001\n"
        'CTRIP_PASSWORD="p@ss=word"\n'
        "export CTRIP_HOTEL_ID='123'\n"
        "EMPTY_VALUE=\n",
        encoding="utf-8",
    )

    values = config._parse_env_file(str(env_file))

    assert values["CTRIP_USERNAME"] == "user001"
    assert values["CTRIP_PASSWORD"] == "p@ss=word"
    assert values["CTRIP_HOTEL_ID"] == "123"
    assert values["EMPTY_VALUE"] == ""


def test_config_error_reports_diagnostics():
    """启动失败时必须给出解释器、工作目录和 .env 路径，便于区分「没读到」和「没填」。"""
    with pytest.raises(ConfigError) as exc:
        validate_startup_config(
            {"username": "", "password": "", "hotel_id": ""},
            dict(DISABLED_NOTIFY),
        )
    message = str(exc.value)
    assert "诊断信息" in message
    assert "当前解释器" in message
    assert ".env" in message
