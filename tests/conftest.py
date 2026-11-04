import os
import sys

import pytest

# 将项目根目录加入 sys.path，便于直接 import 各模块
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config


@pytest.fixture(autouse=True)
def _isolate_from_local_env(monkeypatch):
    """让测试不受本机 .env 影响。

    本机 .env 可能是 DATA_SOURCE=real（真实采集）。若测试跟着它走，
    大量按 mock 语义写的用例（校验 CTRIP_* 而非 MY_HOTEL_*）会成片失败。
    测试应当只验证代码逻辑，不依赖开发机上的实际配置。
    """
    monkeypatch.setattr(config, "DATA_SOURCE", "mock")
    # 房型口碑统计窗口同理：本机若启用了 ROOM_WINDOW_MONTHS，
    # 会让所有传 summary 的口碑用例被切窗污染。测试统一用全量口径，
    # 窗口行为由 test_room_reputation 的专门用例自行覆盖。
    monkeypatch.setitem(config.ROOM_PRICING, "window_months", 0)
    # 本机 .env 通常配了真实 LLM key。口碑页渲染会调 reviews_llm.analyze，
    # 不隔离的话整个测试套件会真的打网络（实测慢了 30 秒，还会随网络波动红）。
    # 需要模型的用例自己 monkeypatch 开回来（见 test_llm / test_reviews_llm）。
    for key in ("base_url", "api_key", "model"):
        monkeypatch.setitem(config.LLM_CONFIG, key, "")
    monkeypatch.setitem(config.REVIEWS_LLM_CONFIG, "enabled", False)


@pytest.fixture
def empty_db(tmp_path, monkeypatch):
    """接管 api 的单例数据库，不预置业务数据。

    给草稿、口碑这类用例用——它们依赖的是点评快照而不是竞品价格表。
    """
    from database import PricingDatabase
    from webapp import api

    db = PricingDatabase(str(tmp_path / "empty.db"))
    monkeypatch.setattr(api, "_db_instance", db)
    yield db
    api._db_instance = None
