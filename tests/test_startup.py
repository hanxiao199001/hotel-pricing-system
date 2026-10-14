"""启动配置检查：必填环境变量缺失或为空时必须失败，且不能进入爬虫。"""
import pytest

import config
from config import ConfigError, validate_startup_config

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
    assert "CTRIP_USERNAME" in message
    assert "CTRIP_PASSWORD" in message
    assert "CTRIP_HOTEL_ID" in message
    assert "NOTIFY_RECIPIENT" not in message


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

    monkeypatch.setattr(
        config,
        "CTRIP_CONFIG",
        {"username": "", "password": "", "hotel_id": ""},
    )
    monkeypatch.setattr(config, "WECHAT_CONFIG", dict(DISABLED_NOTIFY))

    def _crawler_must_not_start(*_args, **_kwargs):
        raise AssertionError("爬虫不应在配置检查失败后启动")

    monkeypatch.setattr(main, "create_crawler", _crawler_must_not_start)
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

    monkeypatch.setattr(
        config,
        "CTRIP_CONFIG",
        {"username": "", "password": "x", "hotel_id": "1"},
    )
    monkeypatch.setattr(config, "WECHAT_CONFIG", dict(DISABLED_NOTIFY))
    monkeypatch.setattr(
        main,
        "create_crawler",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("不应创建爬虫")),
    )

    with pytest.raises(ConfigError, match="CTRIP_USERNAME"):
        main.run_pricing_check()
