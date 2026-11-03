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
